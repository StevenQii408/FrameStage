"""Explicit short-video versus four-frame probe for difficult motion examples."""
import argparse
import json
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

from app.config import load_settings
from app.providers.bailian import BailianVisionProvider
from app.providers.base import FrameInput, ShotContext
from app.schemas import JobConfig
from app.services.video_processor import DetectedShot, extract_frames, probe_video
from evals.manifest import sha256_file


def run_probe(video: Path, start: float, duration: float, model: str) -> dict:
    if not 0 <= start or not 0 < duration <= 4.8:
        raise ValueError("起点必须非负，短片段时长必须在 0–4.8 秒内")
    if not video.is_file() or not shutil.which("ffmpeg"):
        raise ValueError("视频不存在或 ffmpeg 不可用")
    metadata = probe_video(video)
    if start + duration > metadata["duration"] + 0.01:
        raise ValueError("短片段超出源视频范围")
    settings = replace(load_settings(), provider="bailian", cloud_model=model)
    provider = BailianVisionProvider(settings)
    if not provider.health().ready:
        raise RuntimeError(provider.health().detail)
    try:
        with tempfile.TemporaryDirectory(prefix="storyboard-video-probe-") as directory:
            root = Path(directory)
            clip = root / "visual-only.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(start),
                "-i", str(video), "-t", str(duration), "-an", "-vf", "scale=640:-2",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-y", str(clip),
            ], check=True)
            if clip.stat().st_size > 20 * 1024 * 1024:
                raise ValueError("短片段超过 20MB，取消上传")
            actual = probe_video(clip)["duration"]
            frames = extract_frames(clip, [DetectedShot(1, 0, actual)], root / "frames", JobConfig())
            baseline = provider.analyze_shot(
                [FrameInput(frame.id, frame.timestamp, frame.path) for frame in frames],
                ShotContext("probe-shot-001", 1, 0, actual, ""),
            )
            direct_video = provider.analyze_short_clip(clip)
            return {
                "source_sha256": sha256_file(video), "model": model, "start_time": start,
                "requested_duration_seconds": duration, "actual_clip_duration_seconds": actual,
                "audio_uploaded": False, "full_source_uploaded": False,
                "four_frame_prediction": baseline.model_dump(), "short_video_prediction": direct_video,
                "model_usage": provider.drain_usage(),
            }
    finally:
        provider.unload()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="仅对疑难样本显式上传无音轨短片段，与四帧结果对照")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--start", required=True, type=float)
    parser.add_argument("--duration", required=True, type=float)
    parser.add_argument("--model", default="qwen3.8-flash")
    parser.add_argument("--output", type=Path, default=Path("eval-results/video-probe.json"))
    args = parser.parse_args(argv)
    report = run_probe(args.video, args.start, args.duration, args.model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
