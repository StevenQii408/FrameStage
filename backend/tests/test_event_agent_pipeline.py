"""Main-worker event integration with isolated files/SQLite and mocked inference."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.event_sequences import rerender_sequence_payload, sequence_payload_view
from app.perception_models import PerceptionRecord, VisualObservationV2, merge_stages, project_v1
from app.schemas import ProviderHealth
from app.services.video_processor import DetectedShot, ExtractedFrame, VideoProcessResult
from test_event_agent import event, final, ref
from test_event_agent_api import client


def prepare(jobs, count=4, policy="event-agent-v2"):
    row = jobs.status("source-job")
    jobs.db.update_job("source-job", status="processing", stage="analyzing", progress=85,
                       config_json=json.dumps({"event_grouping_policy": policy}))
    saved_frame = dict(jobs.db.fetchone("SELECT * FROM frames WHERE job_id='source-job'"))
    actions = ["男孩把水杯递给女子", "女子接过水杯", "男孩微笑注视女子", "女子喝下杯中液体"]
    for index in range(1, count + 1):
        sid = f"source-job-shot-{index:03d}"
        text = actions[(index - 1) % len(actions)]
        observation = {"subject_action": text, "confidence": .9,
            "visual_facts": {"scene": "家庭厨房", "people": ["男孩", "女子"], "objects": ["水杯"]},
            "action_details": {"primary_subject": "男孩" if index % 2 else "女子", "steps": [text],
                "initial_state": "两人站在厨房，杯子位于两人之间", "final_state": "杯子仍在两人之间",
                "interaction_target": "水杯"}}
        start = float((index - 1) * 2)
        jobs.db.execute("INSERT OR REPLACE INTO shots(id,job_id,number,start_time,end_time,duration,time,status,observation_json,scene_group_id) VALUES(?,?,?,?,?,2,'source','ready',?,'scene-001')",
            (sid, "source-job", index, start, start + 2.0, json.dumps(observation, ensure_ascii=False)))
        if index == 1:
            jobs.db.execute("UPDATE frames SET timestamp=1 WHERE job_id='source-job'")
        else:
            jobs.db.execute("INSERT INTO frames(id,job_id,shot_id,timestamp,path,thumbnail_path,width,height) VALUES(?,?,?,?,?,?,32,32)",
                (f"source-frame-{index}", "source-job", sid, start + 1.0, saved_frame["path"], saved_frame["thumbnail_path"]))
    jobs.running_job = "source-job"
    return row


def install_response(jobs, *, before=None):
    coordinator = jobs.event_agent_service
    source = coordinator._snapshot("source-job")
    shots, frames = coordinator._inputs(source)
    proposal = event(shots, frames, duration=8.0)
    if len(shots) == 4:
        proposal["stages"] = [
            {"id": "handoff", "source_shot_ids": [shots[0].id, shots[1].id], "fact_ids": ["fact-1", "fact-2"], "duration_seconds": 3.0, "transition": "initial"},
            {"id": "feedback", "source_shot_ids": [shots[2].id], "fact_ids": ["fact-3"], "duration_seconds": 1.0, "transition": "continuation"},
            {"id": "drink", "source_shot_ids": [shots[3].id], "fact_ids": ["fact-4"], "duration_seconds": 3.0, "transition": "continuation"},
        ]
        proposal["causal_facts"][2]["kind"] = "reaction"
    calls = []
    def analyze(prompt, frames, **options):
        calls.append(options)
        jobs.provider.usage.append({"phase": options["phase"], "status": "success", "total_tokens": 20,
                                    "estimated_list_price_cny": .001})
        if before:
            before()
        return final(proposal)
    jobs.provider.analyze_event_window = analyze
    return calls


def test_new_videos_opt_into_main_agent_but_old_and_script_jobs_do_not(client, tmp_path):
    jobs = client.app.state.job_service
    video = tmp_path / "new.mp4"
    video.write_bytes(b"fixture")
    video_id = jobs.create_job("new.mp4", "", video)
    script_id = jobs.create_script_job("女孩举杯")
    assert json.loads(jobs.status(video_id)["config_json"])["event_grouping_policy"] == "event-agent-v2"
    assert json.loads(jobs.status(script_id)["config_json"])["event_grouping_policy"] == "script-director-v3"
    assert jobs.result("source-job").job.config.event_grouping_policy == "legacy-pairs-v1"


def test_main_pipeline_projects_one_whole_event_and_preserves_semantic_stages(client):
    jobs = client.app.state.job_service
    prepare(jobs)
    calls = install_response(jobs)
    submits = jobs.executor.submit.call_count
    jobs._build_event_sequences("source-job")
    payload = json.loads(jobs.status("source-job")["event_sequence_json"])
    assert len(calls) == 1
    assert jobs.executor.submit.call_count == submits  # no self-queue deadlock
    assert len(payload["narrative_events"]) == len(payload["generation_sequences"]) == 1
    seq = payload["generation_sequences"][0]
    assert len(seq["source_shot_ids"]) == 4
    assert len(seq["timeline"]) == 3
    assert len(seq["timeline"][0]["source_shot_ids"]) == 2
    assert seq["generation_duration_seconds"] == 8.0
    assert "男孩微笑注视女子" in seq["seedance_prompt"]["positive_prompt"]
    assert [item["transition"] for item in seq["timeline"]] == ["initial", "continuation", "continuation"]
    assert payload["sequence_metadata"]["event_grouping_policy"] == "event-agent-v2"
    run = dict(jobs.db.fetchone("SELECT * FROM event_agent_runs WHERE job_id='source-job'"))
    assert run["status"] == "ready" and json.loads(run["usage_json"])[0]["run_id"] == run["id"]
    assert jobs.provider.usage[0]["phase"] == "event_agent.initial"  # main usage owner gets it exactly once
    assert jobs.status("source-job")["progress"] >= 88
    returned = client.get("/api/jobs/source-job/result").json()
    assert returned["generation_sequences"][0]["seedance_prompt"]["positive_prompt"] == seq["seedance_prompt"]["positive_prompt"]
    assert returned["generation_sequences"][0]["timeline"] == seq["timeline"]


def test_main_agent_handles_more_than_four_sources_without_pair_repartition(client):
    jobs = client.app.state.job_service
    prepare(jobs, count=8)
    for index in range(1, 9):
        sid = f"source-job-shot-{index:03d}"
        jobs.db.execute("UPDATE shots SET start_time=?,end_time=?,duration=1 WHERE id=?", (float(index - 1), float(index), sid))
        jobs.db.execute("UPDATE frames SET timestamp=? WHERE shot_id=?", (float(index - 1) + .5, sid))
    calls = install_response(jobs)
    jobs._build_event_sequences("source-job")
    payload = json.loads(jobs.status("source-job")["event_sequence_json"])
    assert len(calls) == 1
    assert len(payload["narrative_events"]) == len(payload["generation_sequences"]) == 1
    assert len(payload["generation_sequences"][0]["source_shot_ids"]) == 8
    covered = [sid for seq in payload["generation_sequences"] for sid in seq["source_shot_ids"]]
    assert covered == [f"source-job-shot-{index:03d}" for index in range(1, 9)]
    assert len(set(covered)) == 8


def test_cancel_during_main_agent_keeps_job_cancelled_and_does_not_commit_sequences(client):
    jobs = client.app.state.job_service
    prepare(jobs)
    install_response(jobs, before=lambda: jobs.cancel("source-job"))
    jobs._build_event_sequences("source-job")
    assert jobs.status("source-job")["status"] == "cancelled"
    assert not jobs.status("source-job")["event_sequence_json"]
    assert jobs.db.fetchone("SELECT status FROM event_agent_runs")["status"] == "cancelled"


def test_legacy_worker_never_starts_new_paid_agent_implicitly(client):
    jobs = client.app.state.job_service
    prepare(jobs, policy="legacy-pairs-v1")
    jobs._build_event_sequences("source-job")
    jobs.provider.analyze_event_window.assert_not_called()
    assert not jobs.db.fetchall("SELECT * FROM event_agent_runs")


def test_main_progress_change_is_not_source_edit_but_observation_edit_invalidates_run(client):
    jobs = client.app.state.job_service
    prepare(jobs)
    def edit():
        jobs.db.execute("UPDATE shots SET observation_json=? WHERE id='source-job-shot-001'", ('{"subject_action":"用户修改的动作"}',))
    install_response(jobs, before=edit)
    jobs._build_event_sequences("source-job")
    run = jobs.db.fetchone("SELECT status,error FROM event_agent_runs")
    assert run["status"] == "failed" and "来源" in run["error"]
    payload = json.loads(jobs.status("source-job")["event_sequence_json"])
    assert payload["sequence_metadata"]["agent_status"] == "failed"
    assert len(payload["generation_sequences"]) == 4
    assert all(seq["status"] == "warning" for seq in payload["generation_sequences"])


def test_persisted_agent_read_and_local_rebuild_never_use_pair_renderer(client):
    jobs = client.app.state.job_service
    prepare(jobs)
    calls = install_response(jobs)
    jobs._build_event_sequences("source-job")
    jobs.db.update_job("source-job", status="completed", stage="completed", progress=100)
    original = json.loads(jobs.status("source-job")["event_sequence_json"])
    assert sequence_payload_view(original, [], [], []) == original
    rebuilt = client.post("/api/jobs/source-job/sequences/rebuild").json()
    assert len(calls) == 1
    assert rebuilt["generation_sequences"][0]["timeline"] == original["generation_sequences"][0]["timeline"]
    assert rebuilt["generation_sequences"][0]["seedance_prompt"]["positive_prompt"] == original["generation_sequences"][0]["seedance_prompt"]["positive_prompt"]


def test_global_style_application_does_not_rewrite_agent_actions_cuts_or_sources(client, monkeypatch):
    jobs = client.app.state.job_service
    prepare(jobs)
    install_response(jobs)
    jobs._build_event_sequences("source-job")
    payload = json.loads(jobs.status("source-job")["event_sequence_json"])
    source = jobs.result("source-job")
    contract = SimpleNamespace(style_profile_hash="new-style-hash", style_profile_version="v2",
        observed_style="剪纸拼贴动画，纸层投影与低饱和色彩", delivery={})
    monkeypatch.setattr("app.services.event_sequences._source_prompts", lambda *args: {
        shot.id: SimpleNamespace(contract=contract) for shot in source.shots})
    styled = rerender_sequence_payload(payload, source.shots)
    for old, new in zip(payload["generation_sequences"], styled["generation_sequences"]):
        assert new["id"] == old["id"] and new["source_shot_ids"] == old["source_shot_ids"]
        assert new["timeline"] == old["timeline"]
        for key in ("first_frame_prompt", "seedance_prompt"):
            without_style = lambda text: [line for line in text.splitlines() if not line.startswith("整体画面风格：")]
            assert without_style(new[key]["positive_prompt"]) == without_style(old[key]["positive_prompt"])
            assert "剪纸拼贴动画" in new[key]["positive_prompt"]
            assert new[key]["contract"]["delivery"]["render_version"] == "event-agent-render.v5"


def test_health_reports_main_agent_policy_without_inference(client):
    config = client.get("/api/health").json()["configuration"]
    assert config["event_grouping_policy"] == "event-agent-v2"
    assert config["event_render_version"] == "event-agent-render.v5"
    client.app.state.job_service.provider.analyze_event_window.assert_not_called()


def test_confirming_style_clears_only_resolved_renderer_warnings(client, monkeypatch):
    jobs = client.app.state.job_service
    prepare(jobs)
    install_response(jobs)
    jobs._build_event_sequences("source-job")
    payload = json.loads(jobs.status("source-job")["event_sequence_json"])
    source = jobs.result("source-job")
    seq = payload["generation_sequences"][0]
    assert any("observed_style" in warning for warning in seq["warnings"])
    contract = SimpleNamespace(style_profile_hash="confirmed", style_profile_version="v2",
        observed_style="剪纸拼贴，纸纤维边缘清晰", delivery={})
    monkeypatch.setattr("app.services.event_sequences._source_prompts", lambda *args: {
        shot.id: SimpleNamespace(contract=contract) for shot in source.shots})
    refreshed = rerender_sequence_payload(payload, source.shots)["generation_sequences"][0]
    assert refreshed["status"] == "ready" and not refreshed["warnings"]
    assert all(refreshed[key]["contract"]["delivery"]["prompt_lint"][refreshed[key]["prompt_type"]]["passed"]
               for key in ("first_frame_prompt", "seedance_prompt"))
    seq["warnings"].append("来源动作需要人工复核")
    refreshed = rerender_sequence_payload(payload, source.shots)["generation_sequences"][0]
    assert refreshed["status"] == "warning" and refreshed["warnings"] == ["来源动作需要人工复核"]
    seq["warnings"] = []
    seq["seedance_prompt"]["contract"]["end_state"] = "未知"
    refreshed = rerender_sequence_payload(payload, source.shots)["generation_sequences"][0]
    assert refreshed["status"] == "warning"
    assert refreshed["seedance_prompt"]["status"] == "warning"
    assert any("end_state" in warning for warning in refreshed["warnings"])


def test_multiline_agent_style_replaces_complete_block_across_repeated_applications(client, monkeypatch):
    jobs = client.app.state.job_service
    prepare(jobs)
    install_response(jobs)
    jobs._build_event_sequences("source-job")
    payload = json.loads(jobs.status("source-job")["event_sequence_json"])
    source = jobs.result("source-job")
    contract = SimpleNamespace(style_profile_hash="new", style_profile_version="v2",
        observed_style="剪纸风格\n清晰纸纤维与层叠阴影", delivery={})
    monkeypatch.setattr("app.services.event_sequences._source_prompts", lambda *args: {
        shot.id: SimpleNamespace(contract=contract) for shot in source.shots})
    for seq in payload["generation_sequences"]:
        for key, baseline in (("first_frame_prompt", "first_frame_prompt"), ("seedance_prompt", "seedance_video_prompt")):
            item = seq[key]
            marker = "画面约束：" if key == "first_frame_prompt" else "结束状态："
            item["positive_prompt"] = item["positive_prompt"].replace(marker, "整体画面风格：旧油画\n浓密笔触、暖色厚涂\n" + marker, 1)
            item["contract"]["delivery"][baseline] = item["positive_prompt"]
    first = rerender_sequence_payload(payload, source.shots)
    contract.observed_style = "冷色纪实摄影\n自然肤色与克制颗粒"
    second = rerender_sequence_payload(first, source.shots)
    for before, after in zip(payload["generation_sequences"], second["generation_sequences"]):
        assert after["timeline"] == before["timeline"]
        for key in ("first_frame_prompt", "seedance_prompt"):
            text = after[key]["positive_prompt"]
            assert "旧油画" not in text and "浓密笔触" not in text
            assert "剪纸" not in text and "纸纤维" not in text
            assert "冷色纪实摄影\n自然肤色与克制颗粒" in text
            marker = "画面约束：" if key == "first_frame_prompt" else "结束状态："
            assert text.split(marker, 1)[1] == before[key]["positive_prompt"].split(marker, 1)[1]
            assert text.split("整体画面风格：", 1)[0] == before[key]["positive_prompt"].split("整体画面风格：", 1)[0]
    broken = json.loads(json.dumps(second))
    broken["generation_sequences"][0]["seedance_prompt"]["positive_prompt"] += "\n声音与约束：另一段"
    broken["generation_sequences"][0]["seedance_prompt"]["contract"]["delivery"]["seedance_video_prompt"] = broken["generation_sequences"][0]["seedance_prompt"]["positive_prompt"]
    with pytest.raises(ValueError, match="画风段不可唯一定位"):
        rerender_sequence_payload(broken, source.shots)


def run_video_fixture(jobs, monkeypatch, *, count=4, queued=False, policy="event-agent-v2"):
    prepare(jobs, count=count, policy=policy)
    shots = [DetectedShot(index, (index - 1) * 2.0, index * 2.0) for index in range(1, count + 1)]
    saved = jobs.db.fetchone("SELECT * FROM frames WHERE job_id='source-job' LIMIT 1")
    frames = [ExtractedFrame(f"test-frame-{index}", f"shot-{index:03d}", (index - 1) * 2.0 + 1,
        saved["path"], saved["thumbnail_path"], 32, 32) for index in range(1, count + 1)] if queued else []
    processed = VideoProcessResult({"duration": count * 2.0}, shots, frames, [])
    monkeypatch.setattr("app.services.job_service.process_video", lambda *args: processed)
    monkeypatch.setattr(jobs, "_draft_style", Mock(return_value={}))
    monkeypatch.setattr(jobs.provider, "health", lambda: ProviderHealth(provider="bailian", model="mock", ready=True, detail="fixture"))
    jobs.provider.analyze_montage = Mock(return_value={"scene_starts": [1], "relations": []})
    if queued:
        jobs.db.execute("UPDATE shots SET status='queued',observation_json=NULL,perception_json=NULL WHERE job_id='source-job'")
    return processed


@pytest.mark.parametrize("policy,expected_montage", [("event-agent-v2", 0), ("legacy-pairs-v1", 1)])
def test_new_pipeline_skips_duplicate_montage_but_legacy_keeps_it(client, monkeypatch, policy, expected_montage):
    jobs = client.app.state.job_service
    run_video_fixture(jobs, monkeypatch, policy=policy)
    calls = install_response(jobs) if policy == "event-agent-v2" else []
    jobs._run("source-job")
    assert jobs.status("source-job")["status"] == "completed"
    assert jobs.provider.analyze_montage.call_count == expected_montage
    assert len(calls) == (1 if policy == "event-agent-v2" else 0)
    if policy != "event-agent-v2":
        jobs.provider.analyze_event_window.assert_not_called()


def unknown_perception(category="connect_timeout"):
    v2 = merge_stages(None, None)
    value = project_v1(v2, partial_failure=True)
    value.perception_record = PerceptionRecord(observation=v2,
        raw_responses=[{"stage": "static", "error": category}], trace=[{"state": "failed", "category": category}],
        budgets={"network_failure": True, "transport_error_category": category})
    return value


def test_two_connection_unknowns_stop_future_paid_work_and_keep_frames(client, monkeypatch):
    jobs = client.app.state.job_service
    run_video_fixture(jobs, monkeypatch, queued=True)
    jobs.provider.analyze_shot = Mock(side_effect=lambda *args: unknown_perception())
    jobs._run("source-job")
    row = jobs.status("source-job")
    assert row["status"] == "failed" and "显式恢复" in row["error"]
    assert jobs.provider.analyze_shot.call_count == 2
    jobs.provider.analyze_montage.assert_not_called()
    jobs.provider.analyze_event_window.assert_not_called()
    assert len(jobs.result("source-job").shots) == 4
    assert len(jobs.result("source-job").frames) >= 4
    remaining = jobs.db.fetchall("SELECT status FROM shots WHERE job_id='source-job' AND number>2")
    assert all(row["status"] == "failed" for row in remaining)
    payload = json.loads(jobs.status("source-job")["event_sequence_json"])
    assert len(payload["generation_sequences"]) == 4
    assert payload["sequence_metadata"]["degraded"] is True


def test_all_unknown_is_not_success_and_does_not_start_agent(client, monkeypatch):
    jobs = client.app.state.job_service
    run_video_fixture(jobs, monkeypatch, count=2, queued=True)
    jobs.provider.analyze_shot = Mock(side_effect=lambda *args: unknown_perception("read_timeout"))
    jobs._run("source-job")
    assert jobs.status("source-job")["status"] == "failed"
    assert "可追溯" in jobs.status("source-job")["error"]
    assert jobs.provider.analyze_shot.call_count == 2  # no recovery without a working source
    jobs.provider.analyze_montage.assert_not_called()
    jobs.provider.analyze_event_window.assert_not_called()


def test_network_only_unknown_recovery_once_uses_same_source_pipeline(client, monkeypatch):
    jobs = client.app.state.job_service
    run_video_fixture(jobs, monkeypatch, count=3, queued=True)
    source_calls = []
    def analyze(frames, context):
        source_calls.append(context.shot_id)
        if context.number == 1:
            return unknown_perception()
        return project_v1(merge_stages(None, None)).model_copy(update={"subject_action": "女子握住水杯",
            "action_details": jobs.result("other-job").shots[0].action_details})
    jobs.provider.analyze_shot = analyze
    # Inspect the bounded recovery only; do not make the grouping fixture invent facts.
    monkeypatch.setattr(jobs, "_build_event_sequences", Mock())
    jobs._run("source-job")
    assert source_calls == ["source-job-shot-001", "source-job-shot-002", "source-job-shot-003", "source-job-shot-001"]
    record = json.loads(jobs.db.fetchone("SELECT perception_json FROM shots WHERE id='source-job-shot-001'")["perception_json"])
    assert record["budgets"]["network_recovery_attempts"] == 1
    assert len(record["raw_responses"]) == 2 and record["trace"][-2]["state"] == "network_recovery"
    assert jobs.status("source-job")["status"] == "completed"


def install_scene_response(jobs, continuity="unknown"):
    source = jobs.event_agent_service._snapshot("source-job")
    shots, frames = jobs.event_agent_service._inputs(source)
    left = event(shots[:2], frames, duration=4.0)
    right = event(shots[2:], frames, duration=4.0)
    right["goal"] = "在相同厨房完成另一个独立目标"
    right["scene_continuity"] = continuity
    if continuity != "unknown":
        right["scene_evidence"] = [ref(next(frame for frame in frames if frame.shot_id == shot.id), "相邻两侧可见厨房与人物位置")
            for shot in (shots[1], shots[2])]
    jobs.provider.analyze_event_window = Mock(return_value=final(left, right))
    return source


def test_single_event_reprojects_visual_scene_hints_without_changing_frozen_sources(client):
    from dataclasses import asdict
    jobs = client.app.state.job_service
    prepare(jobs)
    for index in range(1, 5):
        jobs.db.execute("UPDATE shots SET scene_group_id=?,scene_reason='视觉边界保守划分，需复核场次' WHERE job_id='source-job' AND number=?",
            (f"scene-{index:03d}", index))
    planned = [asdict(DetectedShot(index, (index - 1) * 2.0, index * 2.0, source_raw_numbers=[index],
        scene_group_id=f"scene-{index:03d}", scene_reason="视觉边界保守划分，需复核场次")) for index in range(1, 5)]
    jobs.db.update_job("source-job", generation_plan_json=json.dumps({"shots": planned, "raw_shots": []}))
    calls = install_response(jobs)
    jobs._build_event_sequences("source-job")
    assert len(calls) == 1
    result = jobs.result("source-job")
    assert len(result.scene_groups) == len(result.narrative_events) == 1
    assert result.scene_groups[0].grouping_mode == "semantic"
    assert result.scene_groups[0].shot_ids == [shot.id for shot in result.shots]
    assert all(shot.scene_group_id == "scene-001" for shot in result.shots)
    saved = json.loads(jobs.status("source-job")["generation_plan_json"])
    assert saved["scene_projection"]["run_id"]
    for before, after in zip(planned, saved["shots"]):
        assert {key: value for key, value in before.items() if key not in {"scene_group_id", "scene_reason"}} == {
            key: value for key, value in after.items() if key not in {"scene_group_id", "scene_reason"}}
        DetectedShot(**after)  # frozen plans remain compatible with decode()


@pytest.mark.parametrize("continuity,scene_count", [("unknown", 2), ("same_scene", 1), ("scene_change", 2), ("time_elision", 2)])
def test_scene_projection_distinguishes_new_goal_from_real_scene_or_time_change(client, continuity, scene_count):
    jobs = client.app.state.job_service
    prepare(jobs)
    jobs.db.execute("UPDATE shots SET scene_group_id='scene-002',scene_reason='视觉边界保守划分，需复核场次' WHERE job_id='source-job' AND number>2")
    jobs.db.execute("UPDATE shots SET scene_reason='视觉连续，场次待语义复核' WHERE job_id='source-job' AND number<=2")
    install_scene_response(jobs, continuity)
    jobs._build_event_sequences("source-job")
    result = jobs.result("source-job")
    assert len(result.narrative_events) == 2  # no forced goal/event merge
    assert len(result.scene_groups) == scene_count
    assert [sid for group in result.scene_groups for sid in group.shot_ids] == [shot.id for shot in result.shots]
    assert len(result.generation_sequences) == 2
    if continuity != "unknown":
        payload = json.loads(jobs.status("source-job")["event_sequence_json"])
        decision = payload["sequence_metadata"]["scene_projection"]["decisions"][2]
        assert decision["scene_continuity"] == continuity and len(decision["evidence"]) == 2


@pytest.mark.parametrize("change", ["scene_change", "time_elision"])
def test_trusted_saved_boundary_cannot_be_swallowed_by_scene_projection(client, change):
    jobs = client.app.state.job_service
    prepare(jobs)
    relation = {"left_shot_id": "source-job-shot-002", "right_shot_id": "source-job-shot-003",
        "observed_edit_fact": "已校验时空边界", "relation_type": "时间省略" if change == "time_elision" else "场景转换",
        "scene_change": change == "scene_change", "confidence": .95, "evidence": ["相邻两侧真实帧"]}
    jobs.db.update_job("source-job", montage_json=json.dumps({"relations": [relation]}))
    install_scene_response(jobs, "same_scene")
    jobs._build_event_sequences("source-job")
    result = jobs.result("source-job")
    assert len(result.scene_groups) == 2
    assert result.shots[1].scene_group_id != result.shots[2].scene_group_id
    assert [sid for group in result.scene_groups for sid in group.shot_ids] == [shot.id for shot in result.shots]


def known_perception(frames):
    artifact = frames[0]
    evidence = {"frame_id": artifact.id, "timestamp": float(artifact.timestamp), "description": "女子握住杯子"}
    payload = merge_stages(None, None).model_dump(mode="json")
    payload["facts"]["primary_subject"] = {"value": "女子", "observable": True, "confidence": .9,
        "evidence": [evidence], "uncertainties": []}
    payload["dynamics"]["subject_action"] = {"value": "女子握住杯子", "observable": True, "confidence": .9,
        "evidence": [evidence], "uncertainties": []}
    v2 = VisualObservationV2.model_validate(payload)
    value = project_v1(v2)
    value.perception_record = PerceptionRecord(observation=v2,
        raw_responses=[{"stage": "static", "content": "saved fixture"}], budgets={"network_failure": False})
    return value


def test_explicit_network_resume_reuses_sources_skips_success_and_is_atomic(client, monkeypatch):
    jobs = client.app.state.job_service
    run_video_fixture(jobs, monkeypatch, count=4, queued=True)
    jobs.db.update_job("source-job", provider="bailian", model="mock")
    calls = []
    def failed_connection(frames, context):
        calls.append(context.shot_id)
        return known_perception(frames) if context.number == 1 else unknown_perception()
    jobs.provider.analyze_shot = failed_connection
    jobs._run("source-job")
    assert calls == [f"source-job-shot-{index:03d}" for index in (1, 2, 3)]
    before = dict(jobs.db.fetchone("SELECT * FROM shots WHERE id='source-job-shot-001'"))
    frame_ids = [row["id"] for row in jobs.db.fetchall("SELECT id FROM frames WHERE job_id='source-job' ORDER BY id")]
    assert json.loads(jobs.status("source-job")["config_json"])["recoverable_network_failure"] is True
    submits = jobs.executor.submit.call_count
    assert client.post("/api/jobs/source-job/resume").status_code == 200
    assert client.post("/api/jobs/source-job/resume").status_code == 409
    assert jobs.executor.submit.call_count == submits + 1
    def recovered(frames, context):
        calls.append(context.shot_id)
        return known_perception(frames)
    jobs.provider.analyze_shot = recovered
    monkeypatch.setattr("app.services.job_service.process_video", Mock(side_effect=AssertionError("no recutting on resume")))
    monkeypatch.setattr(jobs, "_build_event_sequences", Mock())
    jobs._run("source-job")
    assert jobs.status("source-job")["status"] == "completed"
    assert calls == [f"source-job-shot-{index:03d}" for index in (1, 2, 3, 2, 3, 4)]
    assert dict(jobs.db.fetchone("SELECT * FROM shots WHERE id='source-job-shot-001'")) == before
    assert [row["id"] for row in jobs.db.fetchall("SELECT id FROM frames WHERE job_id='source-job' ORDER BY id")] == frame_ids
    assert [(shot.number, shot.start_time, shot.end_time) for shot in jobs.result("source-job").shots] == [
        (index, (index - 1) * 2.0, index * 2.0) for index in range(1, 5)]
    for index in (2, 3):
        record = json.loads(jobs.db.fetchone("SELECT perception_json FROM shots WHERE job_id='source-job' AND number=?", (index,))["perception_json"])
        assert record["budgets"]["network_recovery_attempts"] == 1
        assert record["raw_responses"][0]["error"] == "connect_timeout"
        assert record["raw_responses"][-1]["content"] == "saved fixture"


@pytest.mark.parametrize("status,provider,model,flag,error", [
    ("cancelled", "bailian", "mock", True, "连续两个镜头云端连接失败"),
    ("failed", "mock", "mock", True, "连续两个镜头云端连接失败"),
    ("failed", "bailian", "changed", True, "连续两个镜头云端连接失败"),
    ("failed", "bailian", "mock", False, "连续两个镜头云端连接失败"),
    ("failed", "bailian", "mock", True, "普通分析失败"),
])
def test_network_resume_rejects_cancelled_changed_provider_and_non_recoverable(client, monkeypatch, status, provider, model, flag, error):
    jobs = client.app.state.job_service
    jobs.db.update_job("source-job", status=status, provider=provider, model=model, error=error,
        config_json=json.dumps({"recoverable_network_failure": flag}))
    monkeypatch.setattr(jobs.provider, "health", lambda: ProviderHealth(provider="bailian", model="mock", ready=True, detail="fixture"))
    count = jobs.executor.submit.call_count
    assert client.post("/api/jobs/source-job/resume").status_code == 409
    assert jobs.executor.submit.call_count == count


def test_old_saved_connect_timeout_is_a_bounded_recovery_candidate(client):
    jobs = client.app.state.job_service
    value = unknown_perception()
    record = value.perception_record.model_dump(mode="json")
    record["budgets"] = {}
    record["raw_responses"] = [{"stage": "static", "error": "百炼网络请求失败:ConnectTimeout:"}]
    saved = {"observation_json": value.model_dump_json(), "perception_json": json.dumps(record)}
    assert jobs._network_recovery_candidate(saved) is True
    record["budgets"]["network_recovery_attempts"] = 1
    saved["perception_json"] = json.dumps(record)
    assert jobs._network_recovery_candidate(saved) is False


def test_direct_typed_transport_exceptions_also_open_network_circuit(client, monkeypatch):
    from app.providers.cloud_errors import CloudTransportError
    jobs = client.app.state.job_service
    run_video_fixture(jobs, monkeypatch, queued=True)
    jobs.provider.analyze_shot = Mock(side_effect=CloudTransportError("connect_timeout", "ConnectTimeout"))
    jobs._run("source-job")
    assert jobs.provider.analyze_shot.call_count == 2
    jobs.provider.analyze_event_window.assert_not_called()
    assert jobs.status("source-job")["error"].startswith("连续两个镜头云端连接失败")
    record = json.loads(jobs.db.fetchone("SELECT perception_json FROM shots WHERE id='source-job-shot-001'")["perception_json"])
    assert record["budgets"]["transport_error_category"] == "connect_timeout"
    assert record["raw_responses"][0]["category"] == "connect_timeout"


def test_main_pipeline_single_long_raw_source_uses_spans_without_silent_compression(client):
    jobs = client.app.state.job_service
    prepare(jobs, count=1)
    jobs.db.execute("UPDATE shots SET start_time=0,end_time=30,duration=30 WHERE job_id='source-job'")
    saved = dict(jobs.db.fetchone("SELECT * FROM frames WHERE job_id='source-job' LIMIT 1"))
    for index, timestamp in enumerate((1.0, 13.0, 25.0)):
        jobs.db.execute("INSERT OR REPLACE INTO frames(id,job_id,shot_id,timestamp,path,thumbnail_path,width,height) VALUES(?,?,?,?,?,?,32,32)",
            (f"long-source-frame-{index}", "source-job", saved["shot_id"], timestamp, saved["path"], saved["thumbnail_path"]))
    jobs._build_event_sequences("source-job")
    payload = json.loads(jobs.status("source-job")["event_sequence_json"])
    assert payload["sequence_metadata"]["event_grouping_policy"] == "event-agent-v2"
    assert payload["sequence_metadata"].get("agent_run_id")
    assert len(jobs.result("source-job").shots) == 1
    assert len(payload["narrative_events"]) == 1
    clips = payload["generation_sequences"]
    assert [clip["generation_duration_seconds"] for clip in clips] == [12.0, 12.0, 6.0]
    assert [(clip["source_start_time"], clip["source_end_time"]) for clip in clips] == [(0, 12), (12, 24), (24, 30)]
    assert all(clip["status"] == "warning" for clip in clips)
    assert not any("压到上限" in warning for clip in clips for warning in clip["warnings"])


def test_disabled_agent_long_source_preserves_pending_coverage_without_trimming(client):
    jobs = client.app.state.job_service
    prepare(jobs, count=1)
    jobs.db.execute("UPDATE shots SET start_time=0,end_time=30,duration=30 WHERE job_id='source-job'")
    jobs._build_event_sequences("source-job", allow_agent=False, warning="恢复前不额外调用模型")
    payload = json.loads(jobs.status("source-job")["event_sequence_json"])
    assert payload["generation_sequences"] == []
    assert payload["sequence_metadata"]["unplanned_source_shot_ids"] == ["source-job-shot-001"]
    assert "未压缩或截断" in " ".join(payload["sequence_metadata"]["warnings"])
    assert jobs.result("source-job").shots[0].duration == 30


def test_main_pipeline_reads_independent_span_observations_from_saved_perception(client):
    from app.perception_models import StaticObservationV2, MotionObservationV2, empty_stage, merge_stages
    from app.schemas import FrameArtifact, Shot
    jobs = client.app.state.job_service
    prepare(jobs, count=1)
    jobs.db.execute("UPDATE shots SET start_time=0,end_time=30,duration=30 WHERE job_id='source-job'")
    saved = dict(jobs.db.fetchone("SELECT * FROM frames WHERE job_id='source-job' LIMIT 1"))
    records = []
    for index, (start, end) in enumerate(((0.0, 12.0), (12.0, 24.0), (24.0, 30.0))):
        refs, ids = [], []
        for number, fraction in enumerate((0.1, 0.35, 0.65, 0.9)):
            fid, timestamp = f'long-verified-{index}-{number}', start + (end-start)*fraction
            jobs.db.execute("INSERT INTO frames(id,job_id,shot_id,timestamp,path,thumbnail_path,width,height,selected,artifact_kind) VALUES(?,?,?,?,?,?,32,32,0,'analysis')",
                (fid, 'source-job', saved['shot_id'], timestamp, saved['path'], saved['thumbnail_path']))
            ids.append(fid)
            refs.append({'frame_id': fid, 'timestamp': timestamp, 'description': '本区间动作证据'})
        static, motion = empty_stage(StaticObservationV2, 'unknown'), empty_stage(MotionObservationV2, 'unknown')
        for group, name, value in ((static.facts,'primary_subject','女子'),(static.facts,'scene','厨房'),
            (motion.dynamics,'subject_action',f'女子完成第{index+1}个独立操作'),
            (motion.dynamics,'initial_state',f'女子准备进行第{index+1}个操作'),
            (motion.dynamics,'final_state',f'女子结束第{index+1}个操作')):
            field = getattr(group, name)
            setattr(group, name, type(field).model_validate({'value': value,'observable': True,'confidence': .9,'evidence': refs,'uncertainties': []}))
        records.append({'source_shot_id': saved['shot_id'], 'start_time': start,'end_time': end,
            'status': 'ready','observation': merge_stages(static,motion).model_dump(), 'frame_ids': ids})
    jobs.db.execute("UPDATE shots SET perception_json=? WHERE job_id='source-job'",
        (json.dumps({'observation': records[0]['observation'], 'analysis_span_observations': records}),))
    calls = []
    def analyze(prompt, sent_frames, **options):
        payload = json.loads(prompt.split('\n输入：\n',1)[1])
        calls.append(payload)
        owned = set(payload['owned_source_shot_ids'])
        sources = [Shot(id=row['id'], number=row['number'], start_time=row['start_time'],end_time=row['end_time'],
            duration=row['end_time']-row['start_time'],time='span',status='ready',action=row['action'],
            action_details=row['action_details'],visual_facts=row['visual_facts'])
            for row in payload['source_observations'] if row['id'] in owned]
        artifacts = [FrameArtifact(id=row['id'],shot_id=row['shot_id'],timestamp=row['timestamp'],path='fixture',thumbnail_path='fixture')
                     for row in payload['frames'] if row['shot_id'] in owned]
        return final(event(sources, artifacts, duration=sum(shot.duration for shot in sources)))
    jobs.provider.analyze_event_window = analyze
    jobs._build_event_sequences('source-job')
    payload = json.loads(jobs.status('source-job')['event_sequence_json'])
    assert len(calls) == 3
    assert payload['sequence_metadata'].get('agent_run_id'), str(payload['sequence_metadata'].get('warnings'))
    assert len(jobs.result('source-job').shots) == len(payload['narrative_events']) == 1
    clips = payload['generation_sequences']
    assert [clip['generation_duration_seconds'] for clip in clips] == [12.0,12.0,6.0]
    for index, clip in enumerate(clips):
        assert f'女子完成第{index+1}个独立操作' in clip['seedance_prompt']['positive_prompt']
        assert clip['first_frame_prompt']['contract']['delivery']['source_spans'][0]['start_time'] == index*12
    assert all(len([frame for frame in payload['frames'] if frame['shot_id'] in payload['owned_source_shot_ids']]) >= 4 for payload in calls)
