import json

import pytest

from app.db import Database
from app.repair_cloud_job import repair, require_repairable


@pytest.fixture
def repair_db(tmp_path):
    db = Database(tmp_path / "job.sqlite3")
    db.init()
    db.insert_job({"id": "repair-me", "status": "completed", "stage": "completed", "progress": 100,
                   "input_type": "video", "provider": "bailian", "model": "qwen3.8-flash",
                   "script_text": "", "config": {}, "created_at": "now", "updated_at": "now"})
    db.execute("INSERT INTO shots(id,job_id,number,start_time,end_time,duration,time,status,observation_json,perception_json) VALUES('unknown','repair-me',1,0,2,2,'0-2','warning',?,?)",
               (json.dumps({"subject_action": "未知"}), json.dumps({"budgets": {"network_failure": True}})))
    return db


def test_paid_maintenance_is_explicit(tmp_path):
    with pytest.raises(ValueError, match="allow-paid"):
        repair("unknown", database=tmp_path / "not-created.sqlite3")
    assert not list(tmp_path.iterdir())


def test_maintenance_refuses_running_backend(monkeypatch):
    class Probe:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def settimeout(self, value):
            pass
        def connect_ex(self, address):
            return 0
    monkeypatch.setattr("app.repair_cloud_job.socket.socket", Probe)
    with pytest.raises(ValueError, match="Stop the backend"):
        repair("unknown", allow_paid=True)


def test_single_network_unknown_is_eligible(repair_db):
    job, shots, candidates = require_repairable(repair_db, "repair-me")
    assert job["status"] == "completed"
    assert candidates == ["unknown"] and len(shots) == 1


@pytest.mark.parametrize("status", ["queued", "processing", "cancelled", "failed"])
def test_only_selected_completed_job_is_repairable(repair_db, status):
    repair_db.update_job("repair-me", status=status)
    with pytest.raises(ValueError, match="completed"):
        require_repairable(repair_db, "repair-me")


def test_no_cross_model_paid_recovery(repair_db):
    repair_db.update_job("repair-me", model="other-model")
    with pytest.raises(ValueError, match="same"):
        require_repairable(repair_db, "repair-me")


def test_known_observation_does_not_get_replayed(repair_db):
    repair_db.execute("UPDATE shots SET observation_json=? WHERE id='unknown'",
                      (json.dumps({"subject_action": "女子拿起杯子"}),))
    with pytest.raises(ValueError, match="No unrecovered"):
        require_repairable(repair_db, "repair-me")


def test_active_task_blocks_maintenance(repair_db):
    repair_db.insert_job({"id": "other", "status": "processing", "stage": "analyzing", "progress": 40,
                          "script_text": "", "config": {}, "created_at": "now", "updated_at": "now"})
    with pytest.raises(ValueError, match="queued/running"):
        require_repairable(repair_db, "repair-me")


def test_non_network_unknown_does_not_get_paid_replay(repair_db):
    repair_db.execute("UPDATE shots SET perception_json='{}' WHERE id='unknown'")
    with pytest.raises(ValueError, match="No unrecovered"):
        require_repairable(repair_db, "repair-me")
