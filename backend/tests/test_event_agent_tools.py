import json
from dataclasses import replace

import httpx
import numpy as np
import pytest
from PIL import Image

from app.config import load_settings
from app.event_agent.models import EventWindow, ToolRequest
from app.event_agent.tools import EventWindowToolRegistry
from app.providers.bailian import BailianVisionProvider
from app.providers.base import FrameInput
from app.schemas import FrameArtifact, Shot


def inputs(tmp_path):
    shots = [Shot(id=f"s{i}", number=i, start_time=float(2 * (i - 1)), end_time=float(2 * i),
                  duration=2.0, time="source", action="男孩递出杯子" if i == 1 else "女子接过杯子", status="ready")
             for i in (1, 2)]
    frames = []
    for i, timestamp in enumerate((0.5, 1.5, 2.5, 3.5)):
        path = tmp_path / f"f{i}.jpg"
        Image.new("RGB", (60, 40), (30 + i * 30, 20, 90)).save(path)
        frames.append(FrameArtifact(id=f"f{i}", shot_id="s1" if i < 2 else "s2", timestamp=timestamp,
                                    path=str(path), thumbnail_path=str(path), width=60, height=40))
    return shots, frames


def registry(tmp_path, **kwargs):
    shots, frames = inputs(tmp_path)
    window = EventWindow(id="w1", start_time=0.0, end_time=4.0, source_shot_ids=["s1", "s2"])
    return EventWindowToolRegistry(window, shots, frames, video_path=tmp_path / "source.mp4",
                                   artifact_dir=tmp_path / "artifacts", **kwargs)


def fake_decode(timestamps):
    return [(timestamp, np.full((80, 120, 3), 50, dtype=np.uint8), "decoder-pts") for timestamp in timestamps]


def test_interval_images_keep_real_source_ownership_and_registration(tmp_path, monkeypatch):
    registered = []
    tools = registry(tmp_path, register_frame=registered.append)
    monkeypatch.setattr(tools, "_decode", fake_decode)
    result = tools.call(ToolRequest(name="sample_interval_frames", arguments={"start_time": 1.0, "end_time": 3.0, "max_frames": 4}))
    assert result.status == "success"
    assert [f.shot_id for f in result.frames] == ["s1", "s1", "s2", "s2"]
    assert [f.timestamp for f in result.frames] == [1.25, 1.75, 2.25, 2.75]
    assert len(registered) == tools.extra_frames == 4
    assert all(not f.selected and f.artifact_kind.startswith("event-agent-") for f in registered)


@pytest.mark.parametrize("arguments", [
    {"start_time": -1.0, "end_time": 2.0},
    {"start_time": 1.0, "end_time": 5.0},
    {"start_time": 1.0, "end_time": 1.0},
    {"start_time": 1.0, "end_time": 2.0, "max_frames": 7},
    {"start_time": 1.0, "end_time": 2.0, "path": "/etc/passwd"},
    {"start_time": float("nan"), "end_time": 2.0},
])
def test_interval_rejects_unscoped_unbounded_or_file_arguments(tmp_path, arguments):
    tools = registry(tmp_path)
    result = tools.call(ToolRequest(name="sample_interval_frames", arguments=arguments))
    assert result.status == "error" and tools.extra_frames == 0


def test_compare_cross_shot_frames_returns_metrics_not_semantic_decision(tmp_path):
    tools = registry(tmp_path)
    result = tools.call(ToolRequest(name="compare_frames", arguments={"first_frame_id": "f1", "last_frame_id": "f2"}))
    assert result.status == "success"
    assert "feature_flow_x" in result.output and "mean_absolute_difference" in result.output
    assert "merge" not in result.output and not result.frames


@pytest.mark.parametrize("same_timestamp", [False, True])
def test_compare_rejects_duplicate_frames_and_same_time_crops(tmp_path, same_timestamp):
    tools = registry(tmp_path)
    if same_timestamp:
        tools.frames["f1"] = tools.frames["f1"].model_copy(update={"timestamp": tools.frames["f0"].timestamp})
    result = tools.call(ToolRequest(name="compare_frames", arguments={
        "first_frame_id": "f0", "last_frame_id": "f1" if same_timestamp else "f0"}))
    assert result.status == "error" and "不同时间" in result.error
    assert tools.extra_frames == 0 and not result.output


def test_unknown_frame_and_failed_tool_attempts_count_budget(tmp_path):
    tools = registry(tmp_path)
    request = ToolRequest(name="compare_frames", arguments={"first_frame_id": "other-job-frame", "last_frame_id": "f2"})
    assert tools.call(request).status == "error"
    assert tools.call(request).status == "error"
    result = tools.call(ToolRequest(name="get_neighbor_context"))
    assert result.status == "error" and "预算" in result.error


def test_extra_frame_cap_applies_to_crops_and_sampling_together(tmp_path, monkeypatch):
    tools = registry(tmp_path, max_extra_frames=2)
    monkeypatch.setattr(tools, "_decode", fake_decode)
    assert tools.call(ToolRequest(name="crop_frame", arguments={"frame_id": "f0", "bbox": [0.0, 0.0, 0.5, 0.5]})).status == "success"
    result = tools.call(ToolRequest(name="sample_interval_frames", arguments={"start_time": 1.0, "end_time": 3.0, "max_frames": 2}))
    assert result.status == "error" and tools.extra_frames == 1


def test_boundary_metrics_and_evidence_do_not_force_event_split(tmp_path, monkeypatch):
    tools = registry(tmp_path)
    monkeypatch.setattr(tools, "_decode", fake_decode)
    result = tools.call(ToolRequest(name="inspect_boundary", arguments={"timestamp": 2.0}))
    assert result.status == "success" and len(result.frames) == 2
    assert len(result.output["curve"]) == 5
    assert {"ssim", "hsv_distance", "phash_distance", "edge_distance", "brightness_delta"} <= result.output["curve"][0].keys()
    assert "split_event" not in result.output


def test_deadline_stops_even_context_lookup(tmp_path):
    tools = registry(tmp_path, deadline=0.0)
    result = tools.call(ToolRequest(name="get_neighbor_context"))
    assert result.status == "error" and "时间预算" in result.error


def test_overlap_context_does_not_lose_actual_frame_ownership(tmp_path):
    shots, frames = inputs(tmp_path)
    window = EventWindow(id="w2", start_time=2.0, end_time=4.0, source_shot_ids=["s2"],
                         context_shot_ids=["s1"], context_start_time=0.0)
    tools = EventWindowToolRegistry(window, shots, frames, video_path=None, artifact_dir=tmp_path)
    result = tools.call(ToolRequest(name="compare_frames", arguments={"first_frame_id": "f1", "last_frame_id": "f2"}))
    assert result.status == "success"


def test_decoder_reports_actual_pts_on_local_video(tmp_path):
    import cv2

    path = tmp_path / "source.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (120, 80))
    if not writer.isOpened():
        pytest.skip("Local MP4 encoder unavailable")
    for i in range(40):
        writer.write(np.full((80, 120, 3), i * 5, dtype=np.uint8))
    writer.release()
    tools = registry(tmp_path)
    result = tools.call(ToolRequest(name="sample_interval_frames", arguments={"start_time": 1.0, "end_time": 3.0, "max_frames": 2}))
    assert result.status == "success", result.error
    assert result.output["timestamp_basis"] == "decoder-pts"
    assert all(1.0 <= f.timestamp < 3.0 for f in result.frames)


def test_cloud_event_requests_use_flash_zero_temperature_no_hidden_retry(tmp_path):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(429, json={"error": {"message": "rate limit"}})
    settings = replace(load_settings(), provider="bailian", cloud_model="qwen3.8-flash", cloud_api_key="test-only",
                       cloud_base_url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
    provider = BailianVisionProvider(settings, httpx.MockTransport(handler))
    path = tmp_path / "f.jpg"
    Image.new("RGB", (20, 20)).save(path)
    from app.event_agent.models import FinalizeResponse
    try:
        with pytest.raises(RuntimeError, match="429"):
            provider.analyze_event_window("返回JSON", [FrameInput("f", 1.0, str(path))],
                                          phase="event_agent.observe", schema=FinalizeResponse, timeout_seconds=5.0, max_tokens=1024)
        assert len(calls) == len(provider.usage) == 1
        assert calls[0]["model"] == "qwen3.8-flash"
        assert calls[0]["temperature"] == 0 and calls[0]["enable_thinking"] is False
        assert provider.usage[0]["estimated_list_price_cny"] is None
    finally:
        provider.unload()
