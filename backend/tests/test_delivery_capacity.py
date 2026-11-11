from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import utc_now
from app.main import app
from app.services.capacity import script_preflight, video_preflight
from app.services.delivery_quality import delivery_quality
from app.services.delivery_quality import review_requests_view


def test_duration_and_source_limits_agree_before_paid_analysis():
    result = video_preflight(600)
    assert result["accepted"] and result["minimum_source_shots"] == 1
    assert result["source_shot_count"] is None
    assert result["source_count_basis"] == "not-yet-detected"
    assert result["effective_max_duration_seconds"] == 600
    assert result["model_calls"]["normal_estimate"] is None
    assert result["estimated_list_price_cny"] is None
    assert video_preflight(576)["accepted"]
    assert not video_preflight(601)["accepted"]
    shots = [SimpleNamespace(start_time=i, end_time=i+1) for i in range(121)]
    assert not video_preflight(121, shots=shots)["accepted"]
    for invalid in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            video_preflight(invalid)


def test_exact_plan_covers_long_video_instead_of_twelve_window_fallback():
    shots = [SimpleNamespace(start_time=i * 4, end_time=(i + 1) * 4) for i in range(100)]
    result = video_preflight(400, shots=shots)
    assert result["accepted"] and result["exact"]
    assert result["event_window_count"] == 34
    assert result["agent_max_windows"] == 34
    assert result["budget_coverage"] == "full"


def test_long_continuous_source_counts_perception_spans_and_base_calls():
    result = video_preflight(30, shots=[SimpleNamespace(start_time=0.0, end_time=30.0)])
    assert result["accepted"] and result["source_shot_count"] == 1
    assert result["perception_window_count"] == result["long_source_window_count"] == 3
    assert result["scheduled_perception_window_count"] == 3
    assert result["pending_perception_window_count"] == 0
    assert result["event_window_count"] == result["agent_analyzable_window_count"] == 3
    assert result["model_calls"]["perception_base_calls"] == 6
    assert result["model_calls"]["perception_bounded_maximum"] == 9
    assert result["model_calls"]["event_agent_normal_estimate"] == 3
    assert result["model_calls"]["normal_estimate"] == 11  # 6 perception + 3 Agent + 2 style allowance.
    assert result["model_calls"]["bounded_maximum"] == 19
    assert result["budget_coverage"] == "full"


def test_long_source_budget_retains_pending_ranges_instead_of_claiming_full_analysis():
    result = video_preflight(600, shots=[SimpleNamespace(start_time=0.0, end_time=600.0)])
    assert result["accepted"] and not result["reasons"]
    assert result["source_shot_count"] == 1 and result["max_source_shots"] == 120
    assert result["perception_window_count"] == result["long_source_window_count"] == 50
    assert result["scheduled_long_source_window_count"] == result["scheduled_perception_window_count"] == 12
    assert result["pending_perception_window_count"] == 38
    assert result["budget_coverage"] == "partial"
    assert result["budget_notes"] and "38" in result["budget_notes"][0]
    assert result["long_source_budget"] == dict(window_seconds=12.0, max_windows=12, max_model_calls=36, timeout_seconds=600)
    assert result["model_calls"]["perception_base_calls"] == 24
    assert result["model_calls"]["perception_bounded_maximum"] == 36
    assert result["agent_analyzable_window_count"] == 12
    assert result["model_calls"]["normal_estimate"] == 38
    assert result["model_calls"]["full_coverage_normal_estimate"] == 152


def test_script_estimate_preserves_roles_and_does_not_invent_shot_count():
    result = script_preflight("人物：\n程远：40岁刑警\n剧情：\n程远推开门。\n程远：你到了。")
    assert result["accepted"] and result["window_count"] >= 1
    assert not result["exact"] and result["estimated_list_price_cny"] is None
    with pytest.raises(ValueError, match="未截断"):
        script_preflight("甲" * 10001)


def test_completed_execution_can_deliver_partial_and_stale_results():
    job = {"status": "completed", "input_type": "video"}
    shots = [{"id": "s1", "status": "ready", "action": "递杯"},
             {"id": "s2", "status": "warning", "action": "未知"}]
    seqs = {"generation_sequences": [{"status": "ready", "source_shot_ids": ["s1", "s2"],
             "seedance_prompt": {"status": "warning"}}], "sequence_metadata": {"source_refresh_needed": True}}
    quality = delivery_quality(job, shots, [{"status": "ready"}], seqs)
    assert quality["execution_state"] == "completed" and quality["overall"] == "partial"
    assert quality["visual"]["usable"] == 1 and quality["visual"]["unknown"] == 1
    assert quality["aggregation"]["state"] == "stale" and quality["prompts"]["stale"] == 1
    assert any(i["code"] == "source_refresh_needed" for i in quality["issues"])


def test_ready_means_usable_outputs_not_just_a_green_job():
    profile = {"profile": {"application_status": "ready", "rendered_prompt": "剪纸拼贴"}}
    seqs = {"generation_sequences": [{"status": "ready", "source_shot_ids": ["s1"]}]}
    quality = delivery_quality({"status": "completed", "input_type": "video"},
        [{"id": "s1", "status": "ready", "action": "递杯"}], [{"status": "ready"}], seqs, profile)
    assert quality["overall"] == "ready"


def test_review_suggestion_is_not_permanently_pending_after_explicit_review():
    request = {"source_shot_id": "s1", "field_group": "action", "status": "pending"}
    operations = [{"id": "done", "shot_id": "s1", "mode": "review", "status": "unchanged",
                   "created_at": "2026-11-07T01:02:00Z", "request_json": '{"field_group":"action"}'}]
    assert review_requests_view({"fact_review_requests": [request]}, operations, "2026-11-07T01:00:00Z")[0]["status"] == "reviewed"
    assert request["status"] == "pending"
    assert review_requests_view({"fact_review_requests": [request]}, operations, "2026-11-07T01:03:00Z")[0]["status"] == "pending"


def test_sse_reconnect_and_preflight_are_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        service = client.app.state.job_service
        monkeypatch.setattr(service.provider, "analyze_shot", lambda *a, **kw: pytest.fail("unexpected inference"))
        now = utc_now()
        service.db.insert_job(dict(id="audit", status="interrupted", stage="interrupted", progress=30,
            config=service.config().model_dump(), created_at=now, updated_at=now))
        service._emit("audit", "shot.created", {"shot_id": "s1"})
        service._emit("audit", "job.interrupted", {})
        rows = service.db.events_since("audit", 0)
        response = client.get("/api/jobs/audit/events", headers={"Last-Event-ID": str(rows[0]["sequence"])})
        assert response.status_code == 200
        assert "event: shot.created" not in response.text and "event: job.interrupted" in response.text
        assert client.get("/api/jobs/audit/events", headers={"Last-Event-ID": "bad"}).status_code == 422
        assert client.post("/api/jobs/preflight", data={"input_type": "video", "duration_seconds": 600}).json()["accepted"] is True
        assert client.post("/api/jobs/preflight", data={"input_type": "script", "script_text": "男人推开门。"}).json()["accepted"]
        assert len(service.db.fetchall("SELECT id FROM jobs")) == 1
