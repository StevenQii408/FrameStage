import sys
import types
from dataclasses import replace

import pytest

from app.config import load_settings
from app.providers.base import FrameInput
from app.providers.bailian import BailianVisionProvider
from app.schemas import ActionDetails, ProviderHealth, VisionObservation
from app.services.job_service import JobService
from app.services.prompt_service import build_prompts
from app.services import video_processor as video


def test_frame_aligned_action_pause_beats_uniform_split(monkeypatch):
    class Capture:
        def isOpened(self):
            return True

        def release(self):
            pass

    import cv2

    monkeypatch.setattr(cv2, "VideoCapture", lambda path: Capture())
    monkeypatch.setattr(video, "_cut_quality", lambda capture, time, duration: (10.0 if time in {3.75, 6.75} else 0.0, "动作停顿"))
    segment = video.DetectedShot(1, 0.0, 10.0, [1])
    result = video.split_long_shots(
        [segment], maximum_seconds=4.8, video_path=video.Path("synthetic.mp4"),
        frame_times=[index / 4 for index in range(41)],
        raw_shots=[video.RawDetectedShot(1, 0.0, 10.0)],
    )
    assert [shot.end_time for shot in result] == [3.75, 6.75, 10.0]
    assert all(0 < shot.duration <= 4.8 for shot in result)
    assert all(left.end_time == right.start_time for left, right in zip(result, result[1:]))


def test_long_source_retains_real_boundaries_without_duration_cuts(monkeypatch, tmp_path):
    monkeypatch.setattr(video, "probe_video", lambda path: {"duration": 10.0})
    monkeypatch.setattr(video, "detect_scenes", lambda path, metadata, limit: ([video.RawDetectedShot(1, 0.0, 10.0)], []))
    monkeypatch.setattr(video, "analyze_boundaries", lambda *args: None)
    monkeypatch.setattr(video, "probe_frame_times", lambda path, duration: [index / 4 for index in range(41)])
    monkeypatch.setattr(video, "extract_frames", lambda *args: [])
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path))
    result = video.process_video(tmp_path / "unused.mp4", tmp_path / "output", load_settings())
    assert len(result.shots) == 1
    assert result.raw_shots[0].generation_numbers == [1]
    assert [(shot.start_time, shot.end_time) for shot in result.shots] == [(0.0, 10.0)]
    assert result.segmentation_profile == "source-boundaries-v4"


@pytest.mark.parametrize("duration", [5.483333, 30.0, 60.0])
def test_uncut_video_is_not_split_for_generation_budget(monkeypatch, tmp_path, duration):
    monkeypatch.setattr(video, "probe_video", lambda path: {"duration": duration})
    monkeypatch.setattr(video, "detect_scenes", lambda *args: ([video.RawDetectedShot(1, 0.0, duration)], []))
    monkeypatch.setattr(video, "analyze_boundaries", lambda *args: None)
    monkeypatch.setattr(video, "probe_frame_times", lambda *args: [0.0, duration])
    monkeypatch.setattr(video, "extract_frames", lambda *args: [])
    monkeypatch.setattr(video, "split_long_shots", lambda *args, **kwargs: pytest.fail("analysis must not add duration cuts"))
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path))
    result = video.process_video(tmp_path / "unused.mp4", tmp_path, load_settings())
    assert [(s.start_time, s.end_time, s.source_raw_numbers) for s in result.shots] == [(0.0, duration, [1])]


def test_real_source_cuts_survive_visual_similarity(monkeypatch, tmp_path):
    raw = [video.RawDetectedShot(1, 0.0, 3.0, {"protected": False, "visual_similarity": .99}),
           video.RawDetectedShot(2, 3.0, 7.0, {})]
    monkeypatch.setattr(video, "probe_video", lambda path: {"duration": 7.0})
    monkeypatch.setattr(video, "detect_scenes", lambda *args: (raw, []))
    monkeypatch.setattr(video, "analyze_boundaries", lambda *args: None)
    monkeypatch.setattr(video, "probe_frame_times", lambda *args: [0.0, 3.0, 7.0])
    monkeypatch.setattr(video, "extract_frames", lambda *args: [])
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path))
    result = video.process_video(tmp_path / "unused.mp4", tmp_path, load_settings())
    assert [(s.start_time, s.end_time) for s in result.shots] == [(0.0, 3.0), (3.0, 7.0)]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_source_validation_rejects_invalid_raw_intervals(bad):
    with pytest.raises(ValueError, match="原始镜头"):
        video.validate_generation_plan([video.RawDetectedShot(1, 0.0, bad)], [video.DetectedShot(1, 0.0, bad, [1])])


def test_confirmed_angle_cut_may_merge_but_fade_may_not(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path))
    raw = [
        video.RawDetectedShot(1, 0.0, 1.0, {"protected": True}),
        video.RawDetectedShot(2, 1.0, 2.0, {"protected": False}),
    ]
    merged = video.apply_semantic_groups(raw, [[1, 2]], load_settings(), allow_confirmed_angle_cuts=True)
    assert [shot.source_raw_numbers for shot in merged] == [[1, 2]]
    raw[0].boundary_metrics["fade_or_black"] = True
    separated = video.apply_semantic_groups(raw, [[1, 2]], load_settings(), allow_confirmed_angle_cuts=True)
    assert [shot.source_raw_numbers for shot in separated] == [[1], [2]]


def test_semantic_scene_group_has_no_six_shot_limit():
    shots = [video.DetectedShot(index, index - 1, index, [index]) for index in range(1, 13)]
    raw = [video.RawDetectedShot(index, index - 1, index) for index in range(1, 13)]
    video.assign_scene_groups(shots, raw, semantic_starts=[1, 9])
    assert [shot.scene_group_id for shot in shots[:8]] == ["scene-001"] * 8
    assert [shot.scene_group_id for shot in shots[8:]] == ["scene-002"] * 4


def test_scene_planner_validates_strict_json(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path))
    provider = BailianVisionProvider(load_settings())
    monkeypatch.setattr(provider.helper, "_contact_sheets", lambda frames, directory: ["contact.jpg"])
    frames = [FrameInput(f"shot-{index:03d}", float(index), "unused.jpg") for index in range(1, 13)]
    monkeypatch.setattr(provider, "_call", lambda *args: '{"scene_starts":[9]}')
    assert provider.group_scenes(frames) == [1, 9]
    monkeypatch.setattr(provider, "_call", lambda *args: '{"scene_starts":[1]}')
    with pytest.raises(ValueError, match="场次编号"):
        provider.group_scenes(frames)
    provider.unload()


def test_prompt_contract_records_original_cut_without_requiring_jump():
    observation = VisionObservation(
        subject_action="女孩打开盒子", shot_size="近景", lens="标准", camera_movement="固定",
        lighting_vibe="自然柔光", action_details=ActionDetails(primary_subject="女孩", initial_state="盒子未打开", steps=["打开盒子"], final_state="盒子已打开"),
    )
    prompts = build_prompts("shot-1", observation, source_edit_times=[1.2], generation_duration_seconds=3.5)
    assert all(prompt.contract.source_edit_times == [1.2] for prompt in prompts)
    assert all(prompt.contract.generation_duration_seconds == 3.5 for prompt in prompts)
    video = next(prompt for prompt in prompts if prompt.prompt_type == "video")
    assert "动作：女孩打开盒子" in video.positive_prompt
    assert "连续动作重新表达" not in video.positive_prompt
    assert "连续动作重构" in video.contract.editorial_note


def test_ordinary_source_edit_remains_in_generation_contract():
    raw = [video.RawDetectedShot(1, 0.0, 2.0), video.RawDetectedShot(2, 2.0, 4.0)]
    shots = video.split_long_shots([video.DetectedShot(1, 0.0, 4.0, [1, 2])], raw_shots=raw)
    video.validate_generation_plan(raw, shots)
    assert len(shots) == 1
    assert shots[0].source_edit_times == [2.0]
    assert shots[0].source_raw_numbers == [1, 2]


def test_generation_validation_rejects_gaps_and_missing_raw_sources():
    raw = [video.RawDetectedShot(1, 0.0, 1.0), video.RawDetectedShot(2, 1.0, 2.0)]
    with pytest.raises(ValueError, match="缺口"):
        video.validate_generation_plan(raw, [
            video.DetectedShot(1, 0.0, 0.9, [1]),
            video.DetectedShot(2, 1.0, 2.0, [2]),
        ])
    with pytest.raises(ValueError, match="归属"):
        video.validate_generation_plan(raw, [video.DetectedShot(1, 0.0, 2.0, [1])])


def test_cpu_memory_guard_remains_available_without_a_local_model(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path))
    service = object.__new__(JobService)
    service.settings = replace(load_settings(), min_available_memory_gb=16.0)
    service.memory = types.SimpleNamespace(snapshot=lambda: {"available_gb": 15.0})
    health = ProviderHealth(provider="mock", model="cpu-fixture", ready=True)
    assert service._can_start_provider_inference(health) is False
    service.memory = types.SimpleNamespace(snapshot=lambda: {"available_gb": 19.0})
    assert service._can_start_provider_inference(health) is True


def test_detect_scenes_never_silently_truncates(monkeypatch):
    class Clock:
        def __init__(self, seconds):
            self.seconds = seconds

        def get_seconds(self):
            return self.seconds

    class Manager:
        def add_detector(self, detector):
            pass

        def detect_scenes(self, video):
            pass

        def get_scene_list(self):
            return [(Clock(index), Clock(index + 1)) for index in range(121)]

    fake = types.SimpleNamespace(
        ContentDetector=lambda **kwargs: object(),
        ThresholdDetector=lambda **kwargs: object(),
        SceneManager=Manager,
        open_video=lambda path: object(),
    )
    monkeypatch.setitem(sys.modules, "scenedetect", fake)
    with pytest.raises(RuntimeError, match="不会截断"):
        video.detect_scenes(video.Path("unused.mp4"), {"duration": 121}, max_shots=30)
