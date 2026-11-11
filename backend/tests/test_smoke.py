import shutil
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app
from app.providers.base import ScriptPlanError
from app.schemas import ProviderHealth
from app.services.video_processor import DetectedShot, _sample_times


def test_sample_times_keep_the_last_frame_away_from_video_end():
    times = _sample_times(DetectedShot(1, 0.0, 2.0), 3)
    assert times == [0.2, 1.0, 1.8]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for the smoke test")
def test_mock_job_completes_end_to_end(tmp_path, monkeypatch):
    video = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=12",
            "-t", "2", "-pix_fmt", "yuv420p", "-y", str(video),
        ],
        check=True,
    )
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))

    with TestClient(app) as client:
        original_analysis = client.app.state.job_service.provider.analyze_shot

        def analysis_with_review_warning(*args, **kwargs):
            observation = original_analysis(*args, **kwargs)
            observation.analysis_warnings = ["测试：需要人工复核"]
            return observation

        client.app.state.job_service.provider.analyze_shot = analysis_with_review_warning
        with video.open("rb") as handle:
            response = client.post(
                "/api/jobs",
                files={"video": ("sample.mp4", handle, "video/mp4")},
                data={"script_text": "一个人在黄昏中行走。"},
            )
        assert response.status_code == 202
        job_id = response.json()["job_id"]

        final = None
        for _ in range(160):
            final = client.get(f"/api/jobs/{job_id}").json()
            if final["status"] in {"completed", "failed", "paused_memory_pressure"}:
                break
            time.sleep(0.05)

        assert final["status"] == "completed", final
        result = client.get(f"/api/jobs/{job_id}/result").json()
        assert final["provider"] == "mock"
        assert final["model"] == "mock-vision"
        assert result["job"]["provider"] == "mock"
        assert result["job"]["model"] == "mock-vision"
        assert len(result["shots"]) >= 1
        assert len(result["frames"]) == len(result["shots"]) * 4
        assert len(result["prompts"]) == len(result["shots"]) * 2
        assert result["job"]["segmentation_profile"] == "source-boundaries-v4"
        assert result["job"]["raw_shot_count"] >= result["job"]["narrative_shot_count"] >= 1
        assert result["raw_shots"][0]["narrative_shot_id"] == result["shots"][0]["id"]
        assert result["raw_shots"][0]["generation_shot_ids"] == [result["shots"][0]["id"]]
        assert result["shots"][0]["duration"] < 5.0
        assert result["shots"][0]["generation_duration_seconds"] < 5.0
        assert result["shots"][0]["status"] == "warning"
        assert result["shots"][0]["analysis_warnings"] == ["测试：需要人工复核"]
        assert all("测试：需要人工复核" in prompt["warnings"] for prompt in result["prompts"])
        assert all(result["shots"][0]["action"] in prompt["positive_prompt"] for prompt in result["prompts"] if prompt["prompt_type"] == "video")
        assert result["frames"][0]["path"].startswith(f"/api/jobs/{job_id}/frames/")
        assert client.get(f"/api/jobs/{job_id}/export").status_code == 200


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for the smoke test")
def test_long_mock_video_persists_every_generation_shot_id(tmp_path, monkeypatch):
    video = tmp_path / "long.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=160x90:rate=5",
         "-t", "10", "-pix_fmt", "yuv420p", "-y", str(video)],
        check=True,
    )
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        with video.open("rb") as handle:
            response = client.post("/api/jobs", files={"video": ("long.mp4", handle, "video/mp4")})
        job_id = response.json()["job_id"]
        for _ in range(240):
            state = client.get(f"/api/jobs/{job_id}").json()
            if state["status"] in {"completed", "failed", "paused_memory_pressure"}:
                break
            time.sleep(0.05)
        assert state["status"] == "completed", state
        result = client.get(f"/api/jobs/{job_id}/result").json()
        assert len(result["shots"]) == 1
        assert result["shots"][0]["duration"] == 10.0
        assert result["raw_shots"][0]["generation_shot_ids"] == [shot["id"] for shot in result["shots"]]
        assert all(shot["scene_group_id"] == "scene-001" for shot in result["shots"])
        assert result["scene_groups"][0]["grouping_mode"] == "visual-fallback"
        assert all(prompt["contract"]["generation_duration_seconds"] == 10.0 for prompt in result["prompts"])
        assert result["job"]["config"]["event_grouping_policy"] == "event-agent-v2"
        assert result["sequence_metadata"]["event_grouping_policy"] == "event-agent-v2"
        assert result["sequence_metadata"]["agent_status"] == "not-needed"
        runs = client.get(f"/api/jobs/{job_id}/event-agent/runs").json()["runs"]
        assert runs == []  # One real source needs no artificial merge request.
        assert [sid for seq in result["generation_sequences"] for sid in seq["source_shot_ids"]] == [shot["id"] for shot in result["shots"]]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for the smoke test")
def test_job_fails_when_every_visual_analysis_fails_but_keeps_frames(tmp_path, monkeypatch):
    video = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=12",
            "-t", "2", "-pix_fmt", "yuv420p", "-y", str(video),
        ],
        check=True,
    )
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))

    with TestClient(app) as client:
        def fail_analysis(*args, **kwargs):
            raise RuntimeError("provider exploded")

        client.app.state.job_service.provider.analyze_shot = fail_analysis
        with video.open("rb") as handle:
            response = client.post(
                "/api/jobs",
                files={"video": ("sample.mp4", handle, "video/mp4")},
                data={"script_text": "测试失败结果保留。"},
            )
        job_id = response.json()["job_id"]
        for _ in range(160):
            state = client.get(f"/api/jobs/{job_id}").json()
            if state["status"] in {"completed", "failed", "paused_memory_pressure"}:
                break
            time.sleep(0.05)

        assert state["status"] == "failed"
        assert state["error"] == "所有分镜视觉分析均失败"
        result = client.get(f"/api/jobs/{job_id}/result").json()
        assert result["shots"][0]["status"] == "failed"
        assert len(result["frames"]) == 4
        assert result["prompts"] == []


def test_script_job_creates_descriptions_without_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_SCRIPT_STRATEGY", "legacy-v2")
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    script = "雨后的码头空无一人，远处灯光摇曳。\n\n一个人撑伞走入画面，停在水洼边。"

    with TestClient(app) as client:
        response = client.post("/api/jobs", data={"input_type": "script", "script_text": script})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        for _ in range(80):
            state = client.get(f"/api/jobs/{job_id}").json()
            if state["status"] in {"completed", "failed"}:
                break
            time.sleep(0.03)
        assert state["status"] == "completed", state
        result = client.get(f"/api/jobs/{job_id}/result").json()
        assert result["job"]["input_type"] == "script"
        assert len(result["shots"]) == 2
        assert result["frames"] == []
        assert {prompt["prompt_type"] for prompt in result["prompts"]} == {"first_frame", "video"}
        assert {prompt["template_id"] for prompt in result["prompts"]} == {"first-frame-v7", "seedance-video-v8"}
        assert all(prompt["contract"]["delivery"]["schema_version"] == "prompt-delivery.zh-v3" for prompt in result["prompts"])
        assert len(result["prompts"]) == 4
        assert result["job"]["segmentation_profile"] == "script-v2-rules"
        assert all(4 <= shot["duration"] <= 5 for shot in result["shots"])
        assert all(prompt["status"] == "warning" for prompt in result["prompts"])
        assert all(any("画风" in warning or "observed_style" in warning for warning in prompt["warnings"]) for prompt in result["prompts"])
        assert all(prompt["template_id"] for prompt in result["prompts"])
        assert all(prompt["contract"] for prompt in result["prompts"])
        events = client.get(f"/api/jobs/{job_id}/events")
        assert events.status_code == 200
        assert "shot.description_ready" in events.text
        assert "prompt.ready" in events.text
        assert "frame.created" not in events.text


def test_single_paragraph_golden_story_becomes_five_to_seven_shots(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_SCRIPT_STRATEGY", "legacy-v2")
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    story = (
        "一个女人和一个肥胖中年男人在路上走路，突然身边经过一个十分健壮的穿着健身背心的肌肉男，"
        "女人的目光被走过的肌肉男吸引了，看向肌肉男流出了口水。女人身边的男人十分生气，扇了女人一个巴掌，"
        "这时女人拿出两粒棕色胶囊塞到男人嘴里，男人吃了后瞬间变成更加健壮的肌肉男形象，"
        "然后直接抱起女人走旁边的房子里关上门。"
    )

    with TestClient(app) as client:
        response = client.post("/api/jobs", data={"input_type": "script", "script_text": story})
        job_id = response.json()["job_id"]
        for _ in range(100):
            state = client.get(f"/api/jobs/{job_id}").json()
            if state["status"] in {"completed", "failed"}:
                break
            time.sleep(0.03)
        result = client.get(f"/api/jobs/{job_id}/result").json()
        assert state["status"] == "completed", state
        assert 5 <= len(result["shots"]) <= 7
        assert result["job"]["raw_shot_count"] == 8
        assert result["metadata"]["script_planning"]["mode"] == "script-v2-rules"
        assert len(result["prompts"]) == len(result["shots"]) * 2
        assert all(len(shot["action_details"]["steps"]) <= 4 for shot in result["shots"])
        assert all(4 <= shot["duration"] <= 5 for shot in result["shots"])


def test_script_planner_failure_persists_raw_responses_and_completes_with_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_SCRIPT_STRATEGY", "legacy-v2")
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))

    with TestClient(app) as client:
        service = client.app.state.job_service
        service.provider.health = lambda: ProviderHealth(provider="bailian", model="test-qwen", ready=True)
        service.memory.can_start_inference = lambda: True
        service.memory.inference_slot = lambda: nullcontext()

        def fail_plan(*args, **kwargs):
            raise ScriptPlanError("invalid model JSON", ["raw first", "raw retry"])

        service.provider.plan_script = fail_plan
        response = client.post("/api/jobs", data={"input_type": "script", "script_text": "女孩起身。随后女孩推门离开。"})
        job_id = response.json()["job_id"]
        for _ in range(100):
            state = client.get(f"/api/jobs/{job_id}").json()
            if state["status"] in {"completed", "failed"}:
                break
            time.sleep(0.03)
        result = client.get(f"/api/jobs/{job_id}/result").json()
        private_row = client.app.state.db.fetchone("SELECT script_raw_response_json FROM jobs WHERE id = ?", (job_id,))
        assert state["status"] == "completed", state
        assert result["metadata"]["script_planning"]["mode"] == "script-v2-rules"
        assert "raw first" in private_row["script_raw_response_json"] and "raw retry" in private_row["script_raw_response_json"]
        assert any("局部兜底" in warning for warning in result["metadata"]["script_planning"]["warnings"])


def test_job_input_validation_and_storyboard_export(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))

    with TestClient(app) as client:
        empty_script = client.post("/api/jobs", data={"input_type": "script", "script_text": "  "})
        assert empty_script.status_code == 400

        invalid_video = client.post(
            "/api/jobs",
            files={"video": ("notes.txt", b"not a video", "text/plain")},
            data={"input_type": "video"},
        )
        assert invalid_video.status_code == 400

        created = client.post("/api/jobs", data={"input_type": "script", "script_text": "一个人走进空房间。"})
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        for _ in range(80):
            state = client.get(f"/api/jobs/{job_id}").json()
            if state["status"] in {"completed", "failed"}:
                break
            time.sleep(0.03)
        assert state["status"] == "completed", state
        exported = client.get(f"/api/jobs/{job_id}/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("application/json")
        assert exported.json()["schema_version"] == "storyboard.v1"

        cancel_after_complete = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancel_after_complete.status_code == 409


def test_multiple_jobs_keep_shots_and_prompts_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))

    with TestClient(app) as client:
        job_ids = []
        for script in ("第一个任务的镜头。", "第二个任务的镜头。"):
            response = client.post("/api/jobs", data={"input_type": "script", "script_text": script})
            assert response.status_code == 202
            job_ids.append(response.json()["job_id"])
        for job_id in job_ids:
            for _ in range(80):
                state = client.get(f"/api/jobs/{job_id}").json()
                if state["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.03)
            assert state["status"] == "completed", state
            result = client.get(f"/api/jobs/{job_id}/result").json()
            assert len(result["shots"]) == 1
            assert len(result["prompts"]) == 2
            assert all(prompt["shot_id"] == result["shots"][0]["id"] for prompt in result["prompts"])


def test_script_result_recovers_orphan_prompt_shot_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_SCRIPT_STRATEGY", "legacy-v2")
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))

    with TestClient(app) as client:
        response = client.post("/api/jobs", data={"input_type": "script", "script_text": "一个人推门进入房间。"})
        job_id = response.json()["job_id"]
        for _ in range(80):
            state = client.get(f"/api/jobs/{job_id}").json()
            if state["status"] in {"completed", "failed"}:
                break
            time.sleep(0.03)
        assert state["status"] == "completed", state
        client.app.state.db.execute("DELETE FROM shots WHERE job_id = ?", (job_id,))
        recovered = client.get(f"/api/jobs/{job_id}/result").json()
        assert len(recovered["shots"]) == 1
        assert recovered["shots"][0]["id"] == recovered["prompts"][0]["shot_id"]
