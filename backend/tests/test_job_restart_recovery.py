"""Restart uses committed artifacts and paid decisions; no network in these tests."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app.db import utc_now
from app.main import app
from app.script_director.service import run_script_director
from app.services.perception import PerceptionOrchestrator
from test_perception import setup, stages, provider_with


def configure(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STORYBOARD_MIN_AVAILABLE_MEMORY_GB", "1")
    monkeypatch.setenv("STORYBOARD_SCRIPT_STRATEGY", "director-v3")


def insert(service, job_id="restart-job", *, status="processing", text="", kind="video"):
    now = utc_now()
    service.db.insert_job({"id": job_id, "status": status, "stage": "analyzing", "progress": 30,
        "input_type": kind, "script_text": text, "provider": "mock", "model": "mock-vision",
        "config": {**service.config().model_dump(), "style_strategy": "qwen-direct-v3",
                   "event_grouping_policy": "script-director-v3" if kind == "script" else "event-agent-v2"},
        "created_at": now, "updated_at": now})


def test_restart_marks_orphans_and_requires_explicit_resume(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        insert(client.app.state.job_service)
        insert(client.app.state.job_service, "queued-job", status="queued")
        insert(client.app.state.job_service, "done-job", status="completed")
    with TestClient(app) as client:
        service = client.app.state.job_service
        assert service.status("restart-job")["status"] == "interrupted"
        assert service.status("queued-job")["status"] == "interrupted"
        assert service.status("done-job")["status"] == "completed"
        assert service.running_job is None
        event = service.db.events_since("restart-job")[-1]
        assert event["type"] == "job.interrupted"
        assert event["payload"]["in_flight_cost_status"] == "unknown"
        queued = []
        monkeypatch.setattr(service.executor, "submit", lambda *args: queued.append(args))
        assert client.post("/api/jobs/restart-job/resume").status_code == 200
        assert len(queued) == 1
        assert not service.resume("restart-job")
        assert not service.resume("done-job")
        config = json.loads(service.status("restart-job")["config_json"])
        assert config["restart_resume_requested"] and config["restart_resume_count"] == 1


def test_create_and_resume_compete_for_same_queue_slot(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monkeypatch.setenv("STORYBOARD_MAX_PENDING_JOBS", "1")
    with TestClient(app) as client:
        service = client.app.state.job_service
        submissions = []
        monkeypatch.setattr(service.executor, "submit", lambda *args: submissions.append(args))
        insert(service, "recover-a", status="interrupted")
        insert(service, "recover-b", status="interrupted")
        before = service.status("recover-b")["config_json"]
        start = Barrier(2)

        def submit(path, data=None):
            start.wait(timeout=5)
            return client.post(path, data=data)

        with ThreadPoolExecutor(max_workers=2) as callers:
            resume = callers.submit(submit, "/api/jobs/recover-a/resume")
            create = callers.submit(submit, "/api/jobs", {"script_text": "人物伸手推开门。"})
            responses = [resume.result(), create.result()]
        assert [r.status_code for r in responses] in ([200, 429], [429, 202])
        active = service.db.fetchall("SELECT id FROM jobs WHERE status='queued'")
        assert len(active) == len(submissions) == 1
        # Attempts that hit the capacity guard must not change checkpoints or retries.
        rejected = client.post("/api/jobs/recover-b/resume")
        assert rejected.status_code == 429 and "队列已满" in rejected.json()["detail"]
        assert service.status("recover-b")["config_json"] == before
        assert service.status("recover-b")["status"] == "interrupted"
        assert client.post("/api/jobs/" + active[0]["id"] + "/resume").status_code == 409
        assert service.cancel(active[0]["id"])
        assert client.post("/api/jobs/recover-b/resume").status_code == 200
        assert len(submissions) == 2


@pytest.mark.parametrize("kind", ["script", "video"])
def test_oversized_text_rejected_before_enqueue_without_truncation(tmp_path, monkeypatch, kind):
    configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post("/api/jobs", data={"input_type": kind, "script_text": "甲" * 10000 + "结尾"},
                               files={"video": ("v.mp4", b"fixture", "video/mp4")} if kind == "video" else None)
        assert response.status_code == 422
        assert "10002" in response.json()["detail"]
        assert client.app.state.job_service.db.fetchall("SELECT id FROM jobs") == []


def test_exact_limit_text_is_preserved(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        captured = []
        monkeypatch.setattr(client.app.state.job_service, "create_script_job", lambda text: captured.append(text) or "test")
        text = "甲" * 9998 + "结尾"
        assert client.post("/api/jobs", data={"input_type": "script", "script_text": text}).status_code == 202
        assert captured == [text]


def test_pending_paid_script_result_reused_after_render_crash(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    import app.script_director.service as module
    original = module.build_prompts
    with TestClient(app) as client:
        service = client.app.state.job_service
        insert(service, text="内景：客厅 日\n女孩推开房门走进房间。", kind="script")
        monkeypatch.setattr(module, "build_prompts", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("render crash")))
        with pytest.raises(RuntimeError, match="render crash"):
            run_script_director(service, service.status("restart-job"), service.config())
        plan = json.loads(service.status("restart-job")["script_plan_json"])
        assert plan["pending_window_result"]["window"] == 1
        assert service.db.fetchall("SELECT id FROM shots WHERE job_id='restart-job'") == []
    monkeypatch.setattr(module, "build_prompts", original)
    with TestClient(app) as client:
        service = client.app.state.job_service
        row = service.status("restart-job")
        config = json.loads(row["config_json"])
        config["restart_resume_requested"] = True
        service.db.update_job("restart-job", config_json=json.dumps(config), status="queued")
        monkeypatch.setattr(service.provider, "plan_script_director", lambda *a, **kw: pytest.fail("paid decision replayed"))
        run_script_director(service, service.status("restart-job"), service.config())
        assert service.status("restart-job")["status"] == "completed"
        result = service.result("restart-job")
        assert len(result.prompts) == len(result.shots) * 2
        plan = json.loads(service.status("restart-job")["script_plan_json"])
        assert plan["checkpoint"]["window"] == 1
        assert "pending_window_result" not in plan
        assert len(plan["window_audit"]) == 1


def test_completed_script_window_not_replanned_after_restart(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    text = "内景：客厅 日\n女孩推开房门走进房间。\n外景：街道 夜\n汽车缓缓驶过街道。"
    with TestClient(app) as client:
        service = client.app.state.job_service
        insert(service, text=text, kind="script")
        original = service.provider.plan_script_director
        calls = []
        def crash_second(prompt, **kwargs):
            calls.append(prompt)
            if len(calls) == 2:
                raise SystemExit("worker terminated")
            return original(prompt, **kwargs)
        monkeypatch.setattr(service.provider, "plan_script_director", crash_second)
        with pytest.raises(SystemExit):
            run_script_director(service, service.status("restart-job"), service.config())
        first = [dict(r) for r in service.db.fetchall("SELECT * FROM prompts WHERE job_id='restart-job' ORDER BY id")]
        assert first
    with TestClient(app) as client:
        service = client.app.state.job_service
        config = json.loads(service.status("restart-job")["config_json"])
        config["restart_resume_requested"] = True
        service.db.update_job("restart-job", config_json=json.dumps(config), status="queued")
        original = service.provider.plan_script_director
        calls = []
        def remaining(prompt, **kwargs):
            calls.append(prompt)
            return original(prompt, **kwargs)
        monkeypatch.setattr(service.provider, "plan_script_director", remaining)
        run_script_director(service, service.status("restart-job"), service.config())
        assert len(calls) == 1
        assert '"window_number": 2' in calls[0]
        for previous in first:
            assert dict(service.db.fetchone("SELECT * FROM prompts WHERE id=?", (previous["id"],))) == previous
        plan = json.loads(service.status("restart-job")["script_plan_json"])
        assert [audit["window"] for audit in plan["window_audit"]] == [1, 2]


def test_static_paid_stage_checkpoint_survives_before_motion(setup):
    _, context, frames = setup
    static, motion = stages(frames)
    requests, snapshots = [], []
    context.checkpoint_perception = lambda record: snapshots.append(record.model_copy(deep=True))
    provider = provider_with(setup, [static, motion], requests)
    PerceptionOrchestrator(provider, context, frames).run()
    assert snapshots[0].stages["static"].state == "succeeded"
    assert snapshots[0].stages["motion"].state == "deferred"
    second_requests = []
    recovering = provider_with(setup, [motion], second_requests)
    result = PerceptionOrchestrator(recovering, context, frames, seed_record=snapshots[0], selected_stages=["motion"]).run()
    assert len(second_requests) == 1
    assert result.perception_record.stages["static"].attempts == 1
    assert result.perception_record.observation.facts.primary_subject.value == "男孩"


def test_restart_repairs_missing_prompt_without_repeating_visual_call(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from test_event_agent_api import client as event_client
    from test_event_agent_pipeline import run_video_fixture
    fixture = event_client.__wrapped__(tmp_path, monkeypatch)
    client = next(fixture)
    try:
        service = client.app.state.job_service
        run_video_fixture(service, monkeypatch)
        config = json.loads(service.status("source-job")["config_json"])
        config["restart_resume_requested"] = True
        service.db.update_job("source-job", config_json=json.dumps(config))
        service.provider.analyze_shot = Mock(side_effect=AssertionError("completed visual stage replayed"))
        monkeypatch.setattr(service, "_build_event_sequences", Mock())
        before = dict(service.db.fetchone("SELECT * FROM shots WHERE id='source-job-shot-001'"))
        service._run("source-job")
        assert service.status("source-job")["status"] == "completed", [event["payload"] for event in service.db.events_since("source-job") if event["type"] == "shot.failed"]
        assert not service.provider.analyze_shot.called
        assert service.db.fetchone("SELECT COUNT(*) AS n FROM prompts WHERE job_id='source-job'")["n"] == 8
        after = dict(service.db.fetchone("SELECT * FROM shots WHERE id='source-job-shot-001'"))
        assert {key: after[key] for key in ("id", "observation_json", "perception_json", "start_time", "end_time")} == {
            key: before[key] for key in ("id", "observation_json", "perception_json", "start_time", "end_time")}
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass
