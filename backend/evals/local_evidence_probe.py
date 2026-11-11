"""Explicit local-media evidence smoke check, with no cloud model or human labels."""
import argparse
import json
import time
from dataclasses import replace
from pathlib import Path

from app.config import load_settings
from app.memory_guard import MemoryGuard
from app.providers.base import FrameInput, ShotContext
from app.services.video_processor import DetectedShot, extract_frames, probe_video
from app.services.visual_tools import LocalToolRegistry


def run(video, output, start=0.0, duration=4.8):
    metadata = probe_video(video)
    if not 0 <= start < start + duration <= metadata["duration"] or not 0 < duration <= 4.8:
        raise ValueError("证据检查片段须在视频内且不超过 4.8 秒")
    started = time.monotonic()
    memory = MemoryGuard(0, 0)
    before = memory.snapshot()
    settings = replace(load_settings(), frames_per_shot=4, max_frames_per_shot=4)
    shot = DetectedShot(1, start, start + duration)
    frames = extract_frames(video, [shot], output / "frames", settings)
    context = ShotContext("local-probe", 1, start, start + duration, "",
                          video_path=str(video), artifact_dir=str(output / "evidence"))
    inputs = [FrameInput(frame.id, frame.timestamp, frame.path, frame.width, frame.height) for frame in frames]
    tools = LocalToolRegistry(context, inputs, deadline=started + 60)
    highres = tools.call("get_high_res_frame", {"frame_id": inputs[0].id}, step_allowance=4)
    tools.call("crop_frame", {"frame_id": highres.frames[0].id, "bbox": [0.25, 0.25, 0.75, 0.75]}, step_allowance=4)
    tools.call("sample_motion_peak_frames", {"count": 2}, step_allowance=4)
    tools.call("compare_frames", {"first_frame_id": inputs[0].id, "last_frame_id": inputs[-1].id}, step_allowance=4)
    result = {"type": "local-evidence-smoke-only", "model_calls": 0, "human_quality_status": "not-measured",
              "video_metadata": metadata, "start_time": start, "end_time": start + duration,
              "base_frame_count": len(frames), "extra_frame_count": tools.extra_frames,
              "crop_count": tools.crops, "tools": tools.audit,
              "memory_before": before, "memory_after": memory.snapshot(),
              "elapsed_seconds": round(time.monotonic() - started, 3)}
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="真实素材的本地证据工具检查；不调用云端、不评分模型")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--duration", type=float, default=4.8)
    args = parser.parse_args(argv)
    result = run(args.video, args.output_dir, args.start, args.duration)
    print(json.dumps({key: result[key] for key in ("type", "model_calls", "base_frame_count",
                                                 "extra_frame_count", "crop_count", "elapsed_seconds")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
