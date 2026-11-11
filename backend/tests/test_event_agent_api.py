"""Isolated contracts for explicit, durable runs; no real cloud requests."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.db import utc_now
from app.event_agent.service import EventAgentService
from app.main import app
from app.schemas import FrameArtifact


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        service = client.app.state.job_service
        monkeypatch.setattr(service.memory, "snapshot", lambda: {"available_gb": 32.0})
        # Submit is held so each test can control cancellation and model completion.
        monkeypatch.setattr(service.executor, "submit", Mock())
        service.provider.usage = []
        def drain_usage():
            records, service.provider.usage = service.provider.usage, []
            return records
        monkeypatch.setattr(service.provider, "drain_usage", drain_usage, raising=False)
        monkeypatch.setattr(service.provider, "analyze_event_window", Mock(side_effect=AssertionError("real model call forbidden")), raising=False)
        now = utc_now()
        directory = service.settings.data_dir / "source"
        directory.mkdir()
        video = directory / "input.mp4"
        video.write_bytes(b"isolated-video-fixture")
        frame = directory / "frame.jpg"
        Image.new("RGB", (32, 32), "gray").save(frame)
        for identifier in ("source-job", "other-job"):
            service.db.insert_job(dict(id=identifier, status="completed", stage="completed", progress=100,
                input_type="video", provider="mock", model="mock", video_path=str(video),
                config={}, created_at=now, updated_at=now))
            shot_id = identifier + "-shot-001"
            observation = {"subject_action": "女人举起杯子", "action_details": {
                "primary_subject": "女人", "initial_state": "女人手持杯子", "steps": ["女人举起杯子"],
                "final_state": "女人举起杯子", "interaction_target": "杯子"}}
            service.db.execute("INSERT INTO shots(id,job_id,number,start_time,end_time,duration,time,status,observation_json) VALUES(?,?,1,0,4,4,'00:00','ready',?)",
                               (shot_id, identifier, json.dumps(observation, ensure_ascii=False)))
            service.db.execute("INSERT INTO frames(id,job_id,shot_id,timestamp,path,thumbnail_path,width,height) VALUES(?,?,?,?,?,?,32,32)",
                               (identifier + "-frame-1", identifier, shot_id, 1.0, str(frame), str(frame)))
        yield client


def create(client, job="source-job"):
    response = client.post(f"/api/jobs/{job}/event-agent/runs", json={"config": {}})
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def install_agent(monkeypatch, action=None, status="ready"):
    def run(**options):
        if action:
            action(options)
        payload = {"schema_version": "event-agent.v1", "job_id": options["job_id"], "run_id": options["run_id"],
            "status": status, "config": options["config"].model_dump(mode="json"), "windows": [],
            "events": [], "generation_clips": [], "frames": [], "audit": [], "raw_responses": [],
            "warnings": [], "usage_summary": {"model_calls": 1, "tool_calls": 0, "extra_frames": 0, "windows": 1},
            "source_coverage": [], "version_hash": "fixture-result-hash"}
        return SimpleNamespace(model_dump=lambda **kwargs: copy.deepcopy(payload))
    agent = Mock(side_effect=run)
    monkeypatch.setattr("app.event_agent.run_event_agent", agent)
    return agent


def run_queued(client):
    args = client.app.state.job_service.executor.submit.call_args.args
    args[0](*args[1:])


def test_read_endpoints_never_start_inference(client, monkeypatch):
    agent = install_agent(monkeypatch)
    run_id = create(client)
    jobs = client.app.state.job_service
    submitted = jobs.executor.submit.call_count
    assert client.get("/api/jobs/source-job/event-agent/runs").json()["runs"][0]["run_id"] == run_id
    assert client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}").json()["status"] == "queued"
    assert client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}/result").status_code == 409
    assert client.get(f"/api/jobs/other-job/event-agent/runs/{run_id}").status_code == 404
    assert client.get("/api/jobs/missing/event-agent/runs").status_code == 404
    assert jobs.executor.submit.call_count == submitted
    agent.assert_not_called()
    jobs.provider.analyze_event_window.assert_not_called()


def test_create_is_explicit_and_validates_completed_video_provider_and_budget(client, monkeypatch):
    jobs = client.app.state.job_service
    jobs.db.update_job("source-job", input_type="script")
    assert client.post("/api/jobs/source-job/event-agent/runs").status_code == 409
    jobs.db.update_job("source-job", input_type="video", status="processing")
    assert client.post("/api/jobs/source-job/event-agent/runs").status_code == 409
    jobs.db.update_job("source-job", status="completed")
    assert client.post("/api/jobs/source-job/event-agent/runs", json={"config": {"max_model_calls_per_window": 3}}).status_code == 422
    monkeypatch.setattr(jobs.provider, "health", lambda: SimpleNamespace(provider="bailian", model="fixture", ready=False, detail="未配置"))
    assert client.post("/api/jobs/source-job/event-agent/runs").status_code == 503
    monkeypatch.setattr(jobs.provider, "health", lambda: SimpleNamespace(provider="incompatible", model="fixture", ready=True))
    assert client.post("/api/jobs/source-job/event-agent/runs").status_code == 409
    assert client.post("/api/jobs/missing/event-agent/runs").status_code == 404
    assert not jobs.db.fetchall("SELECT * FROM event_agent_runs")
    jobs.executor.submit.assert_not_called()


def test_duplicate_active_run_and_queued_cancel_are_independent(client, monkeypatch):
    agent = install_agent(monkeypatch)
    run_id = create(client)
    assert client.post("/api/jobs/source-job/event-agent/runs").status_code == 409
    response = client.post(f"/api/jobs/source-job/event-agent/runs/{run_id}/cancel")
    assert response.json()["status"] == "cancelled"
    run_queued(client)
    agent.assert_not_called()
    assert client.app.state.job_service.status("source-job")["status"] == "completed"
    assert create(client) != run_id


def test_committed_cache_keeps_source_results_and_usage_is_attributed(client, monkeypatch):
    jobs = client.app.state.job_service
    before = client.get("/api/jobs/source-job/result").json()
    old_usage = {"phase": "motion", "estimated_list_price_cny": .7}
    jobs.provider.usage.append(old_usage)
    raw = {"response": '{"action":"finalize"}', "phase": "initial"}
    def action(options):
        jobs.provider.usage.append({"phase": "event_agent.initial", "status": "success", "total_tokens": 12, "estimated_list_price_cny": .02})
        options["emit"]("event_agent.progress", {"window_count": 1, "window_index": 1})
        options["emit"]("event_agent.window", {"window_id": "window-1", "raw_responses": [raw],
                                               "usage": {"model_calls": 1, "tool_calls": 0, "extra_frames": 0}})
    agent = install_agent(monkeypatch, action)
    run_id = create(client)
    run_queued(client)
    summary = client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}").json()
    assert summary["status"] == "ready" and summary["progress"] == 100
    assert summary["window_completed"] == summary["window_total"] == 1
    assert summary["checkpoints"][0]["raw_responses"] == [raw]
    assert summary["usage"][0]["run_id"] == run_id
    assert summary["usage_summary"]["estimated_list_price_cny"] == .02
    assert jobs.provider.usage == [old_usage]
    first = client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}/result").json()
    assert client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}/result").json() == first
    assert agent.call_count == 1
    assert client.get("/api/jobs/source-job/result").json() == before
    events = jobs.db.events_since("source-job")
    assert all(item["type"].startswith("event_agent.") and item["payload"]["run_id"] == run_id for item in events)


def test_running_cancel_preserves_checkpoint_and_unknown_cost(client, monkeypatch):
    jobs = client.app.state.job_service
    def action(options):
        jobs.provider.usage.append({"phase": "event_agent.initial", "status": "failed", "estimated_list_price_cny": None})
        client.app.state.event_agent_service.cancel(options["job_id"], options["run_id"])
        assert options["cancelled"]()
        options["emit"]("event_agent.window", {"window_id": "window-1", "raw_responses": [{"error": "cancelled"}], "usage": {"model_calls": 1}})
    install_agent(monkeypatch, action)
    run_id = create(client)
    run_queued(client)
    summary = client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}").json()
    assert summary["status"] == "cancelled" and not summary["has_result"]
    assert summary["checkpoint_count"] == 1 and len(summary["usage"]) == 1
    assert summary["usage_summary"]["estimated_list_price_cny"] is None
    assert summary["usage_summary"]["unknown_cost_count"] == 1
    assert jobs.status("source-job")["status"] == "completed"


def test_real_core_uses_existing_worker_and_commits_traceable_cached_clips(client, monkeypatch):
    jobs = client.app.state.job_service
    source_id = "source-job-shot-001"
    evidence = {"frame_id": "source-job-frame-1", "timestamp": 1.0, "description": "女人举起杯子"}
    response = {"action": "finalize", "events": [{
        "title": "女人举杯", "goal": "举杯", "source_shot_ids": [source_id],
        "causal_facts": [{"id": "fact-1", "text": "女人举起杯子", "source_shot_ids": [source_id], "evidence": [evidence]}],
        "presentation": [{"source_shot_id": source_id, "disposition": "preserve", "reason": "保留完整举杯动作"}],
        "generation_mode": "continuous", "stages": [{"id": "stage-1", "source_shot_ids": [source_id],
            "fact_ids": ["fact-1"], "duration_seconds": 8.0, "transition": "initial"}], "confidence": .9,
    }]}
    threads, futures = [], []
    def analyze(prompt, frames, **options):
        threads.append(threading.current_thread().name)
        jobs.provider.usage.append({"phase": options["phase"], "status": "success", "estimated_list_price_cny": .01})
        assert frames[0].path.endswith("frame.jpg")
        return json.dumps(response, ensure_ascii=False)
    monkeypatch.setattr(jobs.provider, "analyze_event_window", analyze)
    def submit(*args):
        future = ThreadPoolExecutor.submit(jobs.executor, *args)
        futures.append(future)
        return future
    monkeypatch.setattr(jobs.executor, "submit", submit)
    run_id = create(client)
    futures[0].result(timeout=3)
    status = client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}").json()
    assert status["status"] == "ready", status
    assert status["checkpoint_count"] == status["window_total"] == 1
    result = client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}/result").json()
    assert result["source_coverage"][0]["source_shot_id"] == source_id
    assert result["generation_clips"][0]["generation_duration_seconds"] == 4.0
    assert result["raw_responses"][0]["response"] == json.dumps(response, ensure_ascii=False)
    assert result["usage_summary"]["estimated_list_price_cny"] == .01
    assert threads and all(name.startswith("storyboard-worker") for name in threads)


def test_failed_model_call_keeps_usage_and_does_not_publish_result(client, monkeypatch):
    jobs = client.app.state.job_service
    def action(options):
        jobs.provider.usage.append({"phase": "event_agent.initial", "status": "failed", "estimated_list_price_cny": None})
        raise RuntimeError("cloud transport failed")
    install_agent(monkeypatch, action)
    run_id = create(client)
    run_queued(client)
    result = client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}").json()
    assert result["status"] == "failed" and not result["has_result"]
    assert result["usage"][0]["run_id"] == run_id
    assert result["usage_summary"]["estimated_list_price_cny"] is None


@pytest.mark.parametrize("when", ["queued", "running", "frame"])
def test_source_changes_prevent_paid_start_or_final_commit(client, monkeypatch, when):
    jobs = client.app.state.job_service
    def mutate():
        if when == "frame":
            path = jobs.db.fetchone("SELECT path FROM frames WHERE job_id='source-job'")["path"]
            Image.new("RGB", (32, 32), "red").save(path)
        else:
            jobs.db.execute("UPDATE shots SET source_text='用户修改了来源' WHERE job_id='source-job'")
    agent = install_agent(monkeypatch, lambda options: mutate() if when != "queued" else None)
    run_id = create(client)
    if when == "queued":
        mutate()
    run_queued(client)
    summary = client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}").json()
    assert summary["status"] == "failed" and not summary["has_result"]
    assert "变化" in summary["error"]
    assert agent.call_count == (0 if when == "queued" else 1)


def test_restart_marks_only_experimental_runs_interrupted_and_never_resumes(client):
    run_id = create(client)
    jobs = client.app.state.job_service
    jobs.db.execute("UPDATE event_agent_runs SET checkpoints_json=?,usage_json=? WHERE id=?",
                    ('[{"window_id":"window-1"}]', '[{"phase":"event_agent.initial"}]', run_id))
    count = jobs.executor.submit.call_count
    restarted = EventAgentService(jobs)
    summary = restarted.get("source-job", run_id)
    assert summary["status"] == "interrupted" and summary["checkpoint_count"] == 1
    assert len(summary["usage"]) == 1
    assert jobs.status("source-job")["status"] == "completed"
    assert jobs.executor.submit.call_count == count


def test_run_artifacts_have_real_source_ownership_and_do_not_enter_original_results(client, tmp_path):
    service = client.app.state.event_agent_service
    run_id = create(client)
    source = service._snapshot("source-job")
    directory = service.settings.data_dir / "jobs" / "source-job" / "event_agent" / run_id
    directory.mkdir(parents=True)
    path = directory / "evidence.jpg"
    Image.new("RGB", (32, 32), "blue").save(path)
    frame = FrameArtifact(id="run-evidence-1", shot_id="source-job-shot-001", timestamp=2.0,
                          path=str(path), thumbnail_path=str(path), selected=False, artifact_kind="tool_sample")
    before = client.get("/api/jobs/source-job/result").json()
    service._register_frame("source-job", run_id, frame, source, directory)
    with service.db.connect() as conn:
        stored = json.loads(conn.execute("SELECT payload_json FROM event_agent_frames WHERE id=?", (frame.id,)).fetchone()[0])
        snapshot = json.loads(conn.execute("SELECT source_snapshot_json FROM event_agent_runs WHERE id=?", (run_id,)).fetchone()[0])
    assert stored["path"] == stored["thumbnail_path"] == path.relative_to(service.settings.data_dir).as_posix()
    assert not Path(snapshot["job"]["video_path"]).is_absolute()
    assert all(not Path(item["path"]).is_absolute() for item in snapshot["frames"])
    assert not service.db.fetchone("SELECT id FROM frames WHERE id=?", (frame.id,))
    assert service.db.fetchone("SELECT shot_id FROM event_agent_frames WHERE id=?", (frame.id,))["shot_id"] == frame.shot_id
    assert client.get("/api/jobs/source-job/result").json() == before
    assert client.get(f"/api/jobs/source-job/frames/{frame.id}").status_code == 200
    assert client.get(f"/api/jobs/other-job/frames/{frame.id}").status_code == 404
    with pytest.raises(ValueError, match="真实来源镜头"):
        service._register_frame("source-job", run_id, frame.model_copy(update={"id": "bad-1", "shot_id": "virtual-shot"}), source, directory)
    with pytest.raises(ValueError, match="真实来源镜头"):
        service._register_frame("source-job", run_id, frame.model_copy(update={"id": "bad-2", "timestamp": 5.0}), source, directory)
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (32, 32), "blue").save(outside)
    with pytest.raises(ValueError, match="实验目录"):
        service._register_frame("source-job", run_id, frame.model_copy(update={"id": "bad-3", "path": str(outside)}), source, directory)
