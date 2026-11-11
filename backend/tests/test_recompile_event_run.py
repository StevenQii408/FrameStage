from copy import deepcopy
import sqlite3

import pytest

from app.recompile_event_run import (
    assert_same_evidence, assert_verified_recompilation, read_saved_run, recompile, verified_published_scene_ids,
)
from app.event_agent.service import EventAgentService
from test_event_agent import Provider, event, final, frame, ref, run, source


def test_offline_publication_is_explicit():
    with pytest.raises(ValueError, match="confirm"):
        recompile("job", "run")


def snapshot():
    return {"file_fingerprints": {"frame": "sha"}, "shots": [{"id": "shot", "number": 1,
        "start_time": 0, "end_time": 2, "duration": 2, "observation_json": "saved", "perception_json": "v2",
        "scene_group_id": "scene-001", "scene_reason": "old"}]}


def test_only_scene_reason_and_operational_job_state_can_change():
    saved = snapshot()
    current = deepcopy(saved)
    current["shots"][0]["scene_reason"] = "new audit reason"
    current["job"] = {"status": "completed"}
    assert_same_evidence(saved, current)


@pytest.mark.parametrize("key,value", [("observation_json", "changed"), ("perception_json", "changed"),
                                      ("start_time", .5), ("scene_group_id", "scene-002")])
def test_stale_evidence_cannot_be_recompiled(key, value):
    saved = snapshot()
    current = deepcopy(saved)
    current["shots"][0][key] = value
    with pytest.raises(ValueError, match="observations/boundaries"):
        assert_same_evidence(saved, current)


def test_changed_file_cannot_be_recompiled():
    saved = snapshot()
    current = deepcopy(saved)
    current["file_fingerprints"]["frame"] = "changed"
    with pytest.raises(ValueError, match="files changed"):
        assert_same_evidence(saved, current)


def test_exact_verified_published_scene_projection_can_be_replayed():
    saved, current = snapshot(), snapshot()
    current["shots"][0]["scene_group_id"] = "scene-002"
    assert_same_evidence(saved, current, published_scene_ids={"shot": "scene-002"})


def test_unrelated_scene_edit_and_observation_edits_are_not_waived_by_projection():
    saved, current = snapshot(), snapshot()
    current["shots"][0]["scene_group_id"] = "scene-003"
    with pytest.raises(ValueError, match="boundaries"):
        assert_same_evidence(saved, current, published_scene_ids={"shot": "scene-002"})
    current["shots"][0]["scene_group_id"] = "scene-002"
    current["shots"][0]["observation_json"] = "edited"
    with pytest.raises(ValueError, match="observations"):
        assert_same_evidence(saved, current, published_scene_ids={"shot": "scene-002"})


def test_different_published_run_cannot_authorize_scene_changes():
    current = snapshot()
    current["job"] = {"event_sequence_json": '{"sequence_metadata":{"agent_run_id":"another-run"}}'}
    assert verified_published_scene_ids({"id": "run"}, snapshot(), current) == {}


@pytest.mark.parametrize("field", ["source_raw_shot_ids_json", "source_edit_times_json", "cut_reason",
                                    "generation_duration_seconds", "source_text"])
def test_scene_projection_never_waives_source_cut_provenance_changes(field):
    saved, current = snapshot(), snapshot()
    current["shots"][0][field] = "changed"
    with pytest.raises(ValueError, match="boundaries"):
        assert_same_evidence(saved, current, published_scene_ids={"shot": "scene-002"})


@pytest.mark.parametrize("field", ["raw_shots", "frames"])
def test_scene_projection_never_waives_raw_boundaries_or_frame_metadata(field):
    saved, current = snapshot(), snapshot()
    saved[field], current[field] = [{"id": "saved"}], [{"id": "changed"}]
    with pytest.raises(ValueError, match="metadata"):
        assert_same_evidence(saved, current, published_scene_ids={"shot": "scene-002"})


def published_projection_fixture(monkeypatch):
    import json
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    second = event(shots[1:], frames[1:])
    second["scene_continuity"] = "scene_change"
    second["scene_evidence"] = [ref(frames[0], "边界左侧"), ref(frames[1], "边界右侧")]
    result = run(shots, frames, Provider([final(event(shots[:1], frames[:1]), second)]))
    assert result.status == "ready"
    saved = {"job": {}, "shots": [{"id": shot.id, "scene_group_id": shot.scene_group_id,
              "scene_reason": "", "start_time": shot.start_time, "end_time": shot.end_time} for shot in shots]}
    decisions = EventAgentService.scene_projection(result, saved, shots, frames)
    current = deepcopy(saved)
    for row, decision in zip(current["shots"], decisions):
        row["scene_group_id"], row["scene_reason"] = decision["scene_group_id"], decision["scene_reason"]
    metadata = {"agent_run_id": "run", "scene_projection": {"run_id": "run", "decisions": decisions}}
    current["job"]["event_sequence_json"] = json.dumps({"sequence_metadata": metadata})
    monkeypatch.setattr(EventAgentService, "_inputs", staticmethod(lambda saved: (shots, frames)))
    saved_run = {"id": "run", "job_id": "job", "result_json": result.model_dump_json()}
    return saved_run, saved, current, metadata


def test_published_projection_is_recomputed_from_real_two_sided_frame_evidence(monkeypatch):
    saved_run, saved, current, _ = published_projection_fixture(monkeypatch)
    assert verified_published_scene_ids(saved_run, saved, current) == {"shot-1": "scene-001", "shot-2": "scene-002"}


@pytest.mark.parametrize("tamper", ["published-evidence", "current-reason", "saved-result-evidence", "result-job"])
def test_published_projection_rejects_tampered_evidence_reasons_and_result_identity(monkeypatch, tamper):
    import json
    saved_run, saved, current, metadata = published_projection_fixture(monkeypatch)
    if tamper == "published-evidence":
        metadata["scene_projection"]["decisions"][1]["evidence"][0]["timestamp"] = 99
        current["job"]["event_sequence_json"] = json.dumps({"sequence_metadata": metadata})
    elif tamper == "current-reason":
        current["shots"][1]["scene_reason"] = "Manually edited reason"
    else:
        result = json.loads(saved_run["result_json"])
        if tamper == "saved-result-evidence":
            result["events"][1]["scene_evidence"][0]["timestamp"] = 99
        else:
            result["job_id"] = "another-job"
        saved_run["result_json"] = json.dumps(result)
    with pytest.raises(ValueError):
        verified_published_scene_ids(saved_run, saved, current)


def test_saved_validation_run_is_read_only_and_job_scoped(tmp_path):
    path = tmp_path / "validation.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE event_agent_runs (id TEXT, job_id TEXT, status TEXT)")
        conn.execute("INSERT INTO event_agent_runs VALUES ('run','job','ready')")
    before = path.read_bytes()
    assert read_saved_run(path, "job", "run")["status"] == "ready"
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="exact job"):
        read_saved_run(path, "other-job", "run")


def test_failed_validation_run_is_not_publishable(tmp_path):
    path = tmp_path / "validation.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE event_agent_runs (id TEXT, job_id TEXT, status TEXT)")
        conn.execute("INSERT INTO event_agent_runs VALUES ('run','job','failed')")
    with pytest.raises(ValueError, match="ready/degraded"):
        read_saved_run(path, "job", "run")


def test_missing_validation_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        read_saved_run(path, "job", "run")
    assert not path.exists()


def verified_result():
    return {"status": "degraded", "events": [{"status": "ready"}],
            "generation_clips": [{"status": "ready"}],
            "audit": [{"validation_errors": [], "warnings": ["Optional subject label removed"]}]}


def test_display_field_audit_warning_does_not_reject_verified_recompilation():
    assert_verified_recompilation(verified_result())


@pytest.mark.parametrize("failure", ["event", "clip", "evidence", "cancelled", "missing"])
def test_recompilation_still_rejects_unverified_results(failure):
    result = verified_result()
    if failure == "event":
        result["events"][0]["status"] = "fallback"
    elif failure == "clip":
        result["generation_clips"][0]["status"] = "fallback"
    elif failure == "evidence":
        result["audit"][0]["validation_errors"] = ["Evidence frame outside its source"]
    elif failure == "cancelled":
        result["status"] = "cancelled"
    else:
        result["events"] = []
    with pytest.raises(RuntimeError, match="review"):
        assert_verified_recompilation(result)
