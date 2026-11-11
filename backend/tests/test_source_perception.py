"""Long raw shots get bounded evidence analyses, not fake source cards."""
import copy
import json
import shutil
import subprocess
from dataclasses import replace

import httpx
import pytest

from app.perception_models import MotionObservationV2, PerceptionRecord, empty_stage
from app.providers.base import FrameInput
from app.providers.bailian import BailianVisionProvider
from app.services.source_perception import analyze_source
from test_perception import compact_stage, provider_with, setup, stages
from test_perception_protocol_v2 import complete_motion


def long_fixture(setup, monkeypatch):
    from app.services import source_perception
    context = replace(setup[1], end_time=30.0)
    windows = []
    for index, (start, end) in enumerate(((0.0, 12.0), (12.0, 24.0), (24.0, 30.0))):
        windows.append([FrameInput(f"span-{index}-{n}", start+(end-start)*fraction,
                                  setup[2][n].path, 96, 160, "analysis")
                        for n, fraction in enumerate((.1, .35, .65, .9))])
    replies = []
    for index, frames in enumerate(windows):
        static, _ = stages(frames)
        motion = complete_motion(frames)
        motion["dynamics"]["subject_action"]["value"] = f"女子执行第{index+1}段可见操作"
        replies.extend([compact_stage(static, frames[:1]), compact_stage(motion, frames)])
    extracted = []
    def extract(provider, context, span, state):
        index = int(span["start_time"] // 12)
        extracted.append(index)
        return windows[index]
    monkeypatch.setattr(source_perception, "_span_frames", extract)
    return context, windows, replies, extracted


def test_thirty_second_raw_source_has_three_independent_perception_records(setup, monkeypatch):
    context, windows, replies, extracted = long_fixture(setup, monkeypatch)
    requests, checkpoints = [], []
    context.checkpoint_perception = checkpoints.append
    provider = provider_with(setup, replies, requests)
    provider.helper._sharpest_image = lambda paths: paths[0]
    try:
        state = {}
        result = analyze_source(provider, setup[2], context, shared_budget=state)
        record = result.perception_record
        assert len(requests) == 6 and extracted == [0, 1, 2]
        assert [(entry["start_time"], entry["end_time"]) for entry in record.analysis_span_observations] == [(0, 12), (12, 24), (24, 30)]
        assert all(entry["status"] == "ready" for entry in record.analysis_span_observations)
        assert [entry["observation"]["dynamics"]["subject_action"]["value"] for entry in record.analysis_span_observations] == [f"女子执行第{n}段可见操作" for n in (1, 2, 3)]
        assert state["used_model_calls"] == 6 and state["used_windows"] == 3
        assert context.shot_id == "shot-1" and context.end_time == 30
        assert record.observation.dynamics.initial_state.evidence[0].timestamp == windows[0][0].timestamp
        assert record.observation.dynamics.final_state.evidence[0].timestamp == windows[-1][-1].timestamp
        assert len(checkpoints) == 3 and all(len(checkpoint.analysis_span_observations) == 3 for checkpoint in checkpoints)
        assert checkpoints[0].budgets["review"]["dynamic"]["status"] == "pending_review"
        assert result.analysis_review["dynamic"]["status"] == "ready"
        assert len(provider.drain_usage()) == 6
    finally:
        provider.unload()


def test_long_source_project_budget_keeps_unanalysed_intervals_pending(setup, monkeypatch):
    context, _, replies, extracted = long_fixture(setup, monkeypatch)
    requests = []
    provider = provider_with(setup, replies[:2], requests)
    provider.helper._sharpest_image = lambda paths: paths[0]
    try:
        result = analyze_source(provider, setup[2], context, max_windows=1, max_model_calls=2)
        assert len(requests) == 2 and extracted == [0]
        entries = result.perception_record.analysis_span_observations
        assert entries[0]["observation"] and entries[1]["observation"] is None and entries[2]["observation"] is None
        assert entries[-1]["end_time"] == 30
        assert result.visual_facts.scene == "厨房"
        assert result.analysis_review["static"]["status"] == "ready"
        assert result.analysis_review["dynamic"]["status"] == "pending_review"
        assert not result.analysis_review["video_ready"]
        assert result.action_details.final_state == "未知"
    finally:
        provider.unload()


def test_ready_spans_resume_without_repeated_model_or_frame_requests(setup, monkeypatch):
    context, _, replies, extracted = long_fixture(setup, monkeypatch)
    provider = provider_with(setup, replies, [])
    provider.helper._sharpest_image = lambda paths: paths[0]
    try:
        original = analyze_source(provider, setup[2], context).perception_record
    finally:
        provider.unload()
    seed = original.model_dump(mode="json")
    unchanged = copy.deepcopy(seed)
    requests = []
    provider = provider_with(setup, [], requests)
    try:
        result = analyze_source(provider, setup[2], context, seed_record=seed)
        assert not requests and extracted == [0, 1, 2]
        assert result.perception_record.budgets["used_model_calls"] == 0
        assert result.perception_record.analysis_span_observations == original.analysis_span_observations
        assert seed == unchanged
        assert PerceptionRecord.model_validate(result.perception_record.model_dump()).analysis_span_observations
    finally:
        provider.unload()


def test_explicit_long_recovery_only_retries_pending_stage_and_preserves_static_refs(setup, monkeypatch):
    from app.services import source_perception
    context, windows, replies, _ = long_fixture(setup, monkeypatch)
    provider = provider_with(setup, replies, [])
    provider.helper._sharpest_image = lambda paths: paths[0]
    try:
        original = analyze_source(provider, setup[2], context).perception_record
    finally:
        provider.unload()
    seed = original.model_dump(mode="json")
    pending = seed["analysis_span_observations"][1]
    old_static = copy.deepcopy(pending["observation"]["facts"])
    pending["status"] = "pending_review"
    pending["perception_record"]["stages"]["motion"] = {"state": "failed", "error": {"kind": "schema"}}
    pending["perception_record"]["observation"]["dynamics"] = empty_stage(MotionObservationV2, "动态阶段失败").dynamics.model_dump()
    pending["perception_record"]["normalized_stages"].pop("motion", None)
    pending["observation"] = copy.deepcopy(pending["perception_record"]["observation"])
    new_images = [replace(frame, id="recovery-"+frame.id) for frame in windows[1]]
    seen = []
    def extract(provider, context, span, state):
        seen.append(span["start_time"])
        return new_images
    monkeypatch.setattr(source_perception, "_span_frames", extract)
    requests = []
    provider = provider_with(setup, [compact_stage(complete_motion(new_images), new_images)], requests)
    provider.helper._sharpest_image = lambda paths: paths[0]
    try:
        result = analyze_source(provider, setup[2], context, seed_record=seed, selected_stages=["motion"])
        assert len(requests) == 1 and seen == [12.0]
        entries = result.perception_record.analysis_span_observations
        assert entries[0] == original.analysis_span_observations[0] and entries[2] == original.analysis_span_observations[2]
        assert entries[1]["observation"]["facts"] == old_static
        assert entries[1]["status"] == "ready"
        assert entries[1]["observation"]["dynamics"]["initial_state"]["evidence"][0]["frame_id"].startswith("recovery-")
        assert result.analysis_review["dynamic"]["status"] == "ready"
        assert result.perception_record.budgets["used_model_calls"] == 1
    finally:
        provider.unload()
def test_short_source_keeps_existing_two_call_path(setup, monkeypatch):
    from app.services import source_perception
    def forbidden(*args):
        raise AssertionError("short source must not extract analysis windows")
    monkeypatch.setattr(source_perception, "_span_frames", forbidden)
    static, motion = stages(setup[2])
    requests = []
    provider = provider_with(setup, [static, motion], requests)
    try:
        result = analyze_source(provider, setup[2], setup[1])
        assert len(requests) == 2 and not result.perception_record.analysis_span_observations
    finally:
        provider.unload()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_real_ffmpeg_span_frames_register_original_source_and_exact_pts(setup, tmp_path):
    video = tmp_path / "thirty-seconds.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=64x96:r=2", "-t", "30", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
    registered, requests = [], []
    context = replace(setup[1], end_time=30.0, video_path=str(video), register_frame=registered.append)
    def field(value, refs):
        return {"value": value, "frames": refs, "note": "附图可见女子的姿态及位置", "confidence": .9}
    def handler(request):
        requests.append(json.loads(request.content))
        fields = ({"facts.primary_subject": field("女子", ["F1"]), "facts.scene": field("厨房", ["F1"])}
                  if len(requests) % 2 else {
                      "dynamics.subject_action": field("女子保持站立", ["F1", "F4"]),
                      "dynamics.initial_state": field("女子站立", ["F1"]),
                      "dynamics.action_steps": field(["女子保持站立"], ["F1", "F4"]),
                      "dynamics.final_state": field("女子站立", ["F4"])})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"fields": fields}, ensure_ascii=False)}}]})
    provider = BailianVisionProvider(setup[0], httpx.MockTransport(handler))
    provider.helper._sharpest_image = lambda paths: paths[0]
    try:
        result = analyze_source(provider, setup[2], context)
        assert len(requests) == 6 and len(registered) == 12
        assert all(frame.id.startswith(context.shot_id + "-analysis-") and frame.artifact_kind == "analysis" for frame in registered)
        assert context.shot_id == "shot-1" and context.start_time == 0 and context.end_time == 30
        by_id = {frame.id: frame for frame in registered}
        for entry in result.perception_record.analysis_span_observations:
            frames = [by_id[frame_id] for frame_id in entry["frame_ids"]]
            assert len(frames) == 4 and len({frame.timestamp for frame in frames}) == 4
            assert all(entry["start_time"] <= frame.timestamp < entry["end_time"] for frame in frames)
            assert all(frame.timestamp * 2 == int(frame.timestamp * 2) for frame in frames)
            assert all(frame.width == 64 and frame.height == 96 for frame in frames)
    finally:
        provider.unload()
