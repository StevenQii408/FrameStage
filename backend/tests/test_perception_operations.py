"""Persistence/API safety tests; cloud is replaced by a deterministic fake."""
import copy
import json
from unittest.mock import Mock

import pytest
from app.perception_models import PerceptionRecord, StaticObservationV2, MotionObservationV2, empty_stage, merge_stages, project_v1
from app.services.perception_operations import PerceptionOperationRequest, PerceptionOperationService
from test_event_agent_api import client, run_queued


@pytest.fixture
def saved(client):
    jobs = client.app.state.job_service
    jobs.db.update_job("source-job", model=jobs.provider.health().model)
    frame = jobs.db.fetchone("SELECT * FROM frames WHERE job_id='source-job'")
    static = empty_stage(StaticObservationV2, "不可观察")
    field = dict(value="女人", observable=True, confidence=.9,
                 evidence=[dict(frame_id=frame["id"], timestamp=frame["timestamp"], description="画面可见女人")], uncertainties=[])
    static.facts.primary_subject = static.facts.primary_subject.model_validate(field)
    motion = empty_stage(MotionObservationV2, "动态请求失败")
    record = PerceptionRecord(observation=merge_stages(static, motion), normalized_stages={"static": static.model_dump()},
        stages={"static": {"state": "succeeded", "attempts": 1, "schema_status": "passed"},
                "motion": {"state": "failed", "attempts": 1, "schema_status": "not_run", "error": {"kind": "transport", "category": "read_timeout"}}})
    observed = project_v1(record.observation)
    jobs.db.execute("UPDATE shots SET perception_json=?,observation_json=? WHERE id='source-job-shot-001'", (record.model_dump_json(), observed.model_dump_json()))
    return record


def payload(mode="recover", key="request-fixture-001", revision=0):
    return dict(mode=mode, idempotency_key=key, expected_revision=revision,
                **({"stages": ["motion"]} if mode == "recover" else {"field_group": "action", "reason": "需要对照新证据检查举杯动作"}))


BASE = "/api/jobs/source-job/shots/source-job-shot-001/perception"


def install_recovery(monkeypatch, saved, callback=None, change=False):
    def run():
        if callback:
            callback()
        record = saved.model_copy(deep=True)
        record.raw_responses.append({"phase": "shot.motion", "response": "fixture response", "attempt": 1})
        record.stages["motion"] = record.stages["motion"].model_copy(update={"state": "succeeded", "error": None, "attempts": 2, "schema_status": "passed"})
        if change:
            data = record.observation.dynamics.subject_action.model_dump()
            data.update(value="女人举起杯子", observable=True, confidence=.85,
                        evidence=[dict(frame_id="source-job-frame-1", timestamp=1., description="女人将杯子抬高")], uncertainties=[])
            record.observation.dynamics.subject_action = record.observation.dynamics.subject_action.model_validate(data)
        observed = project_v1(record.observation)
        observed.perception_record = record
        return observed
    constructor = Mock(return_value=Mock(run=Mock(side_effect=run)))
    monkeypatch.setattr("app.services.perception.PerceptionOrchestrator", constructor)
    return constructor


def test_get_never_infers_and_old_task_does_not_invent_failures(client, saved):
    value = client.get(BASE).json()
    assert value["recoverable_stages"] == ["motion"]
    assert value["stages"]["static"]["state"] == "succeeded"
    assert client.get(BASE + "/history").json() == {"revisions": []}
    client.app.state.job_service.executor.submit.assert_not_called()
    legacy = client.get("/api/jobs/other-job/shots/other-job-shot-001/perception").json()
    assert legacy["stages"] == {} and legacy["recoverable_stages"] == []


def test_idempotency_and_successful_stage_not_resubmitted(client, saved, monkeypatch):
    call = install_recovery(monkeypatch, saved)
    first = client.post(BASE + "/operations", json=payload())
    assert first.status_code == 202, first.text
    assert client.post(BASE + "/operations", json=payload()).json()["id"] == first.json()["id"]
    assert client.app.state.job_service.executor.submit.call_count == 1
    assert client.post(BASE + "/operations", json={**payload(), "stages": ["static"]}).status_code == 409
    run_queued(client)
    operation = client.get(BASE + "/operations/" + first.json()["id"]).json()
    assert operation["status"] == "unchanged", operation
    assert call.call_args.kwargs["selected_stages"] == ["motion"]
    status = client.get(BASE).json()
    assert status["revision"] == 1 and status["recoverable_stages"] == []
    assert client.post(BASE + "/operations", json=payload(key="second-operation", revision=1)).status_code == 409
    history = client.get(BASE + "/history").json()["revisions"]
    assert history[0]["changes"]["changed"] is False
    assert history[0]["before"]["shot"]["perception_revision"] == 0


def test_cancel_queued_does_not_charge_or_change_revision(client, saved, monkeypatch):
    call = install_recovery(monkeypatch, saved)
    op = client.post(BASE + "/operations", json=payload()).json()
    assert client.post(BASE + "/operations/" + op["id"] + "/cancel").json()["status"] == "cancelled"
    run_queued(client)
    call.assert_not_called()
    assert client.get(BASE).json()["revision"] == 0


def test_long_budget_pending_spans_can_continue_without_replaying_attempted_windows(client, saved, monkeypatch):
    install_recovery(monkeypatch, saved)
    first = client.post(BASE + "/operations", json=payload()).json()
    run_queued(client)
    jobs = client.app.state.job_service
    source = jobs.db.fetchone("SELECT perception_json FROM shots WHERE id='source-job-shot-001'")
    record = PerceptionRecord.model_validate_json(source["perception_json"])
    record.analysis_span_observations = [
        {"start_time": 0.0, "end_time": 12.0, "status": "pending_review", "perception_record": saved.model_dump(mode="json"), "observation": saved.observation.model_dump(mode="json")},
        {"start_time": 12.0, "end_time": 24.0, "status": "pending_review", "perception_record": None, "observation": None}]
    jobs.db.execute("UPDATE shots SET perception_json=? WHERE id='source-job-shot-001'", (record.model_dump_json(),))
    assert "motion" in client.get(BASE).json()["recoverable_stages"]
    calls = []
    def continue_spans(*args, **kwargs):
        calls.append(kwargs)
        observed = project_v1(record.observation)
        observed.perception_record = record.model_copy(deep=True)
        return observed
    monkeypatch.setattr("app.services.source_perception.analyze_source", continue_spans)
    second = client.post(BASE + "/operations", json=payload(key="continue-unexecuted-spans", revision=1))
    assert second.status_code == 202, second.text
    run_queued(client)
    assert len(calls) == 1 and calls[0]["only_unexecuted"] is True
    assert calls[0]["selected_stages"] == ["motion"]
    assert calls[0]["seed_record"]["analysis_span_observations"][0] == record.analysis_span_observations[0]
    assert client.get(BASE + "/operations/" + first["id"]).json()["status"] == "unchanged"


def test_partial_core_schema_failure_is_explicitly_recoverable(client, saved):
    saved.stages["motion"].state = "partial"
    saved.stages["motion"].schema_status = "partial"
    saved.stages["motion"].error = None
    client.app.state.job_service.db.execute("UPDATE shots SET perception_json=? WHERE id='source-job-shot-001'", (saved.model_dump_json(),))
    assert client.get(BASE).json()["recoverable_stages"] == ["motion"]
    assert client.post(BASE + "/operations", json=payload(key="partial-core-recovery")).status_code == 202


def test_running_cancel_preserves_original_and_records_sent_request(client, saved, monkeypatch):
    op_id = []
    install_recovery(monkeypatch, saved, lambda: client.app.state.job_service.perception_operations.cancel("source-job", "source-job-shot-001", op_id[0]), change=True)
    op = client.post(BASE + "/operations", json=payload()).json()
    op_id.append(op["id"])
    run_queued(client)
    result = client.get(BASE + "/operations/" + op["id"]).json()
    assert result["status"] == "cancelled"
    assert result["result"]["model_call_started"] is True
    assert client.get(BASE).json()["revision"] == 0
    assert client.post(BASE + "/operations", json=payload(key="no-double-charge")).status_code == 409


def test_concurrent_edit_rejects_stale_publication(client, saved, monkeypatch):
    jobs = client.app.state.job_service
    def edit():
        jobs.db.execute("UPDATE shots SET cut_reason='用户编辑' WHERE id='source-job-shot-001'")
    install_recovery(monkeypatch, saved, edit, change=True)
    op = client.post(BASE + "/operations", json=payload()).json()
    run_queued(client)
    result = client.get(BASE + "/operations/" + op["id"]).json()
    assert result["status"] == "conflict"
    assert client.get(BASE).json()["revision"] == 0
    assert json.loads(jobs.db.fetchone("SELECT perception_json FROM shots WHERE id='source-job-shot-001'")[0]) == saved.model_dump(mode="json")


def test_changed_recovery_regenerates_prompt_without_changing_source_boundaries(client, saved, monkeypatch):
    install_recovery(monkeypatch, saved, change=True)
    jobs = client.app.state.job_service
    before = dict(jobs.db.fetchone("SELECT * FROM shots WHERE id='source-job-shot-001'"))
    op = client.post(BASE + "/operations", json=payload()).json()
    run_queued(client)
    result = client.get(BASE + "/operations/" + op["id"]).json()
    assert result["status"] == "applied", result
    after = dict(jobs.db.fetchone("SELECT * FROM shots WHERE id='source-job-shot-001'"))
    assert all(before[key] == after[key] for key in ("id", "number", "start_time", "end_time", "duration", "source_raw_shot_ids_json", "scene_group_id"))
    assert jobs.db.fetchone("SELECT count(*) FROM prompts WHERE job_id='source-job'")[0] == 2


def test_restart_does_not_replay_paid_work(client, saved):
    jobs = client.app.state.job_service
    op = client.post(BASE + "/operations", json=payload()).json()
    PerceptionOperationService(jobs)
    assert client.get(BASE + "/operations/" + op["id"]).json()["status"] == "interrupted"
    assert client.post(BASE + "/operations", json=payload(key="do-not-replay")).status_code == 409
    assert jobs.executor.submit.call_count == 1


def test_scope_and_request_validation(client, saved):
    assert client.post(BASE + "/operations", json={**payload(), "expected_revision": 9}).status_code == 409
    assert client.post(BASE + "/operations", json={**payload(), "stages": ["motion", "motion"]}).status_code == 422
    assert client.post(BASE + "/operations", json={**payload("review"), "reason": ""}).status_code == 422
    assert client.post(BASE + "/operations", json={**payload("review"), "field_group": "artistic_effects"}).status_code == 422
    assert client.post(BASE + "/operations", json=payload()).status_code == 202
    assert client.post("/api/jobs/source-job/event-agent/runs").status_code == 409


def test_usage_does_not_consume_previous_other_job_carry(client, saved, monkeypatch):
    jobs = client.app.state.job_service
    carry = {"phase": "other-job", "total_tokens": 99}
    fresh = {"phase": "shot.motion", "total_tokens": 150, "estimated_list_price_cny": .001}
    jobs.provider.usage = [carry]
    install_recovery(monkeypatch, saved, lambda: jobs.provider.usage.append(fresh))
    op = client.post(BASE + "/operations", json=payload()).json()
    run_queued(client)
    result = client.get(BASE + "/operations/" + op["id"]).json()
    assert result["usage"] == [fresh]
    assert jobs.provider.usage == [carry]
    assert json.loads(jobs.db.fetchone("SELECT model_usage_json FROM jobs WHERE id='source-job'")[0]) == [fresh]


def test_existing_manual_prompt_is_preserved_and_flagged(client, saved, monkeypatch):
    jobs = client.app.state.job_service
    jobs.db.execute("INSERT INTO prompts(id,job_id,shot_id,prompt_type,platform,version,positive_prompt,negative_prompt,status,template_id,template_version,contract_json) VALUES('p','source-job','source-job-shot-001','video','seedance',7,'用户手动改的文案','用户负面约束','ready','seedance',1,?)", (json.dumps({"delivery": {"seedance_video_prompt": "原先自动文案"}}),))
    install_recovery(monkeypatch, saved, change=True)
    op = client.post(BASE + "/operations", json=payload()).json()
    run_queued(client)
    assert client.get(BASE + "/operations/" + op["id"]).json()["status"] == "applied"
    current = jobs.db.fetchone("SELECT * FROM prompts WHERE id='p'")
    assert current["positive_prompt"] == "用户手动改的文案" and current["negative_prompt"] == "用户负面约束"
    assert current["version"] == 7 and current["status"] == "warning"


def test_regenerated_prompt_preserves_exact_global_and_scene_style(client, saved, monkeypatch):
    jobs = client.app.state.job_service
    jobs.db.execute("UPDATE shots SET scene_group_id='scene-1' WHERE id='source-job-shot-001'")
    source = jobs.perception_operations
    with jobs.db.connect() as conn:
        snapshot = source._source(conn, "source-job", "source-job-shot-001")
        source._render_prompts(conn, snapshot, project_v1(saved.observation))
    style = "整体画面风格：皮克斯式三维角色动画。"
    scene = "本场景视觉变化：冷色暗部与暖色高光。"
    rows = jobs.db.fetchall("SELECT * FROM prompts WHERE job_id='source-job'")
    for row in rows:
        contract = json.loads(row["contract_json"])
        contract.update(observed_style=style, style_profile_hash="fixed-style", style_profile_version="style-profile.v3:4")
        contract["delivery"]["style_layers"] = {"global": style, "scene": scene}
        contract["delivery"]["style_provenance"] = {"anchor": "kept", "version": 4}
        jobs.db.execute("UPDATE prompts SET contract_json=? WHERE id=?", (json.dumps(contract, ensure_ascii=False), row["id"]))
    install_recovery(monkeypatch, saved, change=True)
    op = client.post(BASE + "/operations", json=payload()).json()
    run_queued(client)
    assert client.get(BASE + "/operations/" + op["id"]).json()["status"] == "applied"
    for row in jobs.db.fetchall("SELECT * FROM prompts WHERE job_id='source-job'"):
        assert style in row["positive_prompt"] and scene in row["positive_prompt"]
        contract = json.loads(row["contract_json"])
        assert contract["style_profile_hash"] == "fixed-style"
        assert contract["delivery"]["style_provenance"]["anchor"] == "kept"
