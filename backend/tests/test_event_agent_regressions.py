"""End-to-end regressions for independent IDs and the explicit Mock pathway."""
import json
from contextlib import contextmanager

from test_event_agent import Provider, event, final, frame, run, source
from test_event_agent_api import client, create, install_agent, run_queued

from app.event_agent.models import EventAgentConfig
from app.event_agent.service import EventAgentService
from app.providers.mock import MockVisionProvider


def test_internal_cut_camera_rendering_never_overwrites_clip_or_prompt_ids():
    shots = [source(i, (i - 1) * 2) for i in range(1, 7)]
    frames = [frame(shot) for shot in shots]
    stages = [{"id": f"stage-{i}", "source_shot_ids": [shot.id], "fact_ids": [f"fact-{i}"],
               "duration_seconds": 2.0, "transition": "initial" if i == 1 else "cut"}
              for i, shot in enumerate(shots, 1)]
    proposal = event(shots, frames, stages=stages, mode="internal_cuts")
    result = run(shots, frames, Provider([final(proposal)]),
                 config=EventAgentConfig(generation_target_seconds=6.0, generation_max_seconds=6.0))
    assert len(result.generation_clips) == 2
    ids = [clip.id for clip in result.generation_clips]
    assert ids[0].endswith("clip-01") and ids[1].endswith("clip-02")
    assert len(ids) == len(set(ids))
    prompt_ids = [prompt.id for clip in result.generation_clips for prompt in (clip.first_frame_prompt, clip.seedance_prompt)]
    assert len(prompt_ids) == len(set(prompt_ids))


def test_mock_backend_runs_actual_agent_without_api_or_source_mutation(client, monkeypatch):
    jobs = client.app.state.job_service
    original = client.get("/api/jobs/source-job/result").json()
    monkeypatch.setattr(jobs.provider, "analyze_event_window", MockVisionProvider.analyze_event_window.__get__(jobs.provider))
    run_id = create(client)
    run_queued(client)
    status = client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}").json()
    assert status["status"] == "ready", status
    payload = client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}/result").json()
    assert payload["generation_clips"] and payload["usage_summary"]["model_calls"] == 1
    assert payload["generation_clips"][0]["title"].startswith("Mock")
    assert client.get("/api/jobs/source-job/result").json() == original


def test_persisted_protected_reveal_is_passed_to_core(client, monkeypatch):
    jobs = client.app.state.job_service
    first = "source-job-shot-001"
    second = "source-job-shot-002"
    jobs.db.execute("INSERT INTO shots(id,job_id,number,start_time,end_time,duration,time,status,observation_json) "
                    "VALUES(?,?,2,4,5,1,'00:04','ready','{}')", (second, "source-job"))
    montage = {"relations": [{"left_shot_id": first, "right_shot_id": second,
        "observed_edit_fact": "杯中特写揭示虫子", "relation_type": "视觉呼应", "confidence": .9,
        "boundary_role": "reveal", "evidence": ["source-job-frame-1"]}]}
    jobs.db.update_job("source-job", montage_json=json.dumps(montage, ensure_ascii=False))
    captured = []
    install_agent(monkeypatch, lambda options: captured.append(options["protected_source_shot_ids"]))
    create(client)
    run_queued(client)
    assert captured == [{second}]


def test_malformed_or_foreign_saved_montage_does_not_protect_wrong_source():
    source_rows = {"job": {"montage_json": '{"relations":null}'}, "shots": [{"id": "s1"}, {"id": "s2"}]}
    assert EventAgentService._protected_sources(source_rows) == set()
    source_rows["job"]["montage_json"] = json.dumps({"relations": [{"left_shot_id": "foreign",
        "right_shot_id": "s2", "observed_edit_fact": "揭示", "relation_type": "视觉呼应", "confidence": .9,
        "boundary_role": "reveal"}]})
    assert EventAgentService._protected_sources(source_rows) == set()


def test_cancel_racing_checkpoint_does_not_restore_analyzing_stage(client, monkeypatch):
    jobs = client.app.state.job_service
    original_connect = jobs.db.connect
    raced = []

    class Cursor:
        def __init__(self, cursor, run_id):
            self.cursor, self.run_id = cursor, run_id

        def fetchone(self):
            row = self.cursor.fetchone()
            if not raced:
                raced.append(True)
                with original_connect() as conn:
                    conn.execute("UPDATE event_agent_runs SET cancel_requested=1,stage='cancelling' WHERE id=?", (self.run_id,))
            return row

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, parameters=()):
            cursor = self.conn.execute(sql, parameters)
            if sql == "SELECT * FROM event_agent_runs WHERE id=?":
                return Cursor(cursor, parameters[0])
            return cursor

    @contextmanager
    def racing_connect():
        with original_connect() as conn:
            yield Connection(conn)

    def action(options):
        monkeypatch.setattr(jobs.db, "connect", racing_connect)
        options["emit"]("event_agent.progress", {"stage": "analyzing", "progress": 20})
        with original_connect() as conn:
            state = conn.execute("SELECT stage,cancel_requested FROM event_agent_runs WHERE id=?", (options["run_id"],)).fetchone()
        assert state["stage"] == "cancelling" and state["cancel_requested"] == 1

    install_agent(monkeypatch, action)
    run_id = create(client)
    run_queued(client)
    assert raced and client.get(f"/api/jobs/source-job/event-agent/runs/{run_id}").json()["status"] == "cancelled"
