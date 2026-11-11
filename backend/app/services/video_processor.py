from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from app.config import Settings
from app.runtime import (check_execution, current_execution, execution_deadline,
                         ExecutionCancelled, ExecutionDeadlineExceeded)


SEGMENTATION_PROFILE = "source-boundaries-v4"


class TooManyRawShotsError(RuntimeError):
    pass


@dataclass
class DetectedShot:
    number: int
    start_time: float
    end_time: float
    source_raw_numbers: List[int] = field(default_factory=list)
    scene_group_id: str = "scene-001"
    split_reason: str = "原始切段"
    source_edit_times: List[float] = field(default_factory=list)
    scene_reason: str = ""

    @property
    def duration(self) -> float:
        return max(0.01, self.end_time - self.start_time)


@dataclass
class RawDetectedShot:
    number: int
    start_time: float
    end_time: float
    boundary_metrics: dict = field(default_factory=dict)
    decision: str = "keep"
    reason: str = ""
    narrative_number: int = 0
    generation_numbers: List[int] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.01, self.end_time - self.start_time)


@dataclass
class ExtractedFrame:
    id: str
    shot_id: str
    timestamp: float
    path: str
    thumbnail_path: str
    width: int = 0
    height: int = 0


@dataclass
class VideoProcessResult:
    metadata: dict
    shots: List[DetectedShot]
    frames: List[ExtractedFrame]
    warnings: List[str]
    raw_shots: List[RawDetectedShot] = field(default_factory=list)
    segmentation_profile: str = SEGMENTATION_PROFILE
    frame_times: List[float] = field(default_factory=list)


def _run(command: List[str], timeout=None) -> subprocess.CompletedProcess:
    """Bound local processes, drain pipes and reap the complete child group."""
    check_execution()
    deadline = execution_deadline(timeout if timeout is not None else 90)
    env = {key: value for key, value in os.environ.items()
           if key not in {"STORYBOARD_BAILIAN_API_KEY", "DASHSCOPE_API_KEY"}}
    owns_group = os.name != "nt" and os.environ.get("STORYBOARD_MEDIA_WORKER") != "1"
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=owns_group, env=env)
    try:
        while True:
            check_execution()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout if timeout is not None else 90)
            try:
                stdout, stderr = process.communicate(timeout=min(.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        check_execution()
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            if owns_group:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.communicate()


def probe_video(path: Path, timeout=None) -> dict:
    try:
        result = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("未找到 ffprobe，请先安装 FFmpeg")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"视频无法读取：{exc.stderr[-500:]}")
    payload = json.loads(result.stdout)
    video_stream = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"), None)
    if not video_stream:
        raise RuntimeError("文件中没有可用的视频流")
    duration = float(payload.get("format", {}).get("duration") or video_stream.get("duration") or 0)
    return {"duration": duration, "width": int(video_stream.get("width") or 0), "height": int(video_stream.get("height") or 0), "fps": video_stream.get("r_frame_rate", ""), "codec": video_stream.get("codec_name", ""), "has_audio": any(s.get("codec_type") == "audio" for s in payload.get("streams", []))}


def detect_scenes(path: Path, metadata: dict, max_shots: int) -> Tuple[List[RawDetectedShot], List[str]]:
    check_execution()
    warnings: List[str] = []
    try:
        from scenedetect import ContentDetector, SceneManager, ThresholdDetector, open_video

        video = open_video(str(path))
        manager = SceneManager()
        manager.add_detector(ContentDetector(threshold=27.0, min_scene_len=15))
        manager.add_detector(ThresholdDetector(threshold=12.0, min_scene_len=15, add_final_scene=True))
        manager.detect_scenes(video=video)
        raw = manager.get_scene_list()
        if len(raw) > max_shots * 4:
            raise TooManyRawShotsError(f"检测到 {len(raw)} 个原始切段，超过可追溯处理上限 {max_shots * 4}；不会截断视频")
        scenes = [RawDetectedShot(index + 1, start.get_seconds(), end.get_seconds()) for index, (start, end) in enumerate(raw)]
        if scenes:
            return scenes, warnings
        warnings.append("未检测到明显切点，按单镜头处理")
    except (TooManyRawShotsError, ExecutionCancelled, ExecutionDeadlineExceeded):
        raise
    except Exception as exc:
        warnings.append(f"PySceneDetect 不可用，已降级为单镜头：{exc}")
    return [RawDetectedShot(1, 0.0, max(metadata["duration"], 0.01), reason="未检测到切点")], warnings


def _read_frame(capture, timestamp: float, duration: float):
    import cv2

    capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, min(timestamp, max(duration - 0.002, 0.0))) * 1000)
    ok, frame = capture.read()
    if not ok or frame is None:
        return None
    height, width = frame.shape[:2]
    scale = min(1.0, 192.0 / max(width, height, 1))
    if scale < 1:
        frame = cv2.resize(frame, (max(8, int(width * scale)), max(8, int(height * scale))), interpolation=cv2.INTER_AREA)
    return frame


def _phash_bits(gray):
    import cv2
    import numpy as np

    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype("float32")
    low = cv2.dct(resized)[:8, :8]
    median = float(np.median(low[1:]))
    return low > median


def _frame_metrics(first, second) -> dict:
    import cv2
    import numpy as np

    if first is None or second is None:
        return {"distance": 1.0, "ssim": 0.0, "hsv_distance": 1.0, "phash_distance": 1.0, "edge_distance": 1.0, "brightness_delta": 1.0}
    size = (160, 96)
    a = cv2.resize(first, size, interpolation=cv2.INTER_AREA)
    b = cv2.resize(second, size, interpolation=cv2.INTER_AREA)
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    c1, c2 = 6.5025, 58.5225
    mu_a, mu_b = float(ga.mean()), float(gb.mean())
    var_a, var_b = float(ga.var()), float(gb.var())
    covariance = float(((ga.astype("float32") - mu_a) * (gb.astype("float32") - mu_b)).mean())
    ssim = ((2 * mu_a * mu_b + c1) * (2 * covariance + c2)) / max((mu_a * mu_a + mu_b * mu_b + c1) * (var_a + var_b + c2), 1e-6)
    hsv_a = cv2.cvtColor(a, cv2.COLOR_BGR2HSV)
    hsv_b = cv2.cvtColor(b, cv2.COLOR_BGR2HSV)
    hist_a = cv2.calcHist([hsv_a], [0, 1], None, [24, 16], [0, 180, 0, 256])
    hist_b = cv2.calcHist([hsv_b], [0, 1], None, [24, 16], [0, 180, 0, 256])
    cv2.normalize(hist_a, hist_a)
    cv2.normalize(hist_b, hist_b)
    histogram_distance = (1.0 - float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL))) / 2.0
    phash_distance = float(np.count_nonzero(_phash_bits(ga) != _phash_bits(gb))) / 64.0
    edges_a = cv2.Canny(ga, 80, 160)
    edges_b = cv2.Canny(gb, 80, 160)
    edge_distance = float(np.mean(np.abs(edges_a.astype("float32") - edges_b.astype("float32")))) / 255.0
    brightness_delta = abs(float(ga.mean()) - float(gb.mean())) / 255.0
    distance = max(0.0, min(1.0, 0.45 * (1.0 - ssim) + 0.25 * histogram_distance + 0.20 * phash_distance + 0.10 * edge_distance))
    return {
        "distance": round(distance, 4),
        "ssim": round(max(-1.0, min(1.0, ssim)), 4),
        "hsv_distance": round(histogram_distance, 4),
        "phash_distance": round(phash_distance, 4),
        "edge_distance": round(edge_distance, 4),
        "brightness_delta": round(brightness_delta, 4),
    }


def _frame_distance(first, second) -> float:
    return float(_frame_metrics(first, second)["distance"])


def analyze_boundaries(video_path: Path, raw_shots: List[RawDetectedShot], duration: float) -> None:
    if len(raw_shots) < 2:
        return
    try:
        import cv2

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            return
        try:
            for raw in raw_shots[:-1]:
                check_execution()
                boundary = raw.end_time
                offsets = (-0.30, -0.15, -0.05, 0.05, 0.15, 0.30)
                frames = [_read_frame(capture, boundary + offset, duration) for offset in offsets]
                cross_metrics = _frame_metrics(frames[2], frames[3])
                cross = float(cross_metrics["distance"])
                left_motion = _frame_distance(frames[0], frames[2])
                right_motion = _frame_distance(frames[3], frames[5])
                within = max((left_motion + right_motion) / 2.0, 0.04)
                jump_ratio = cross / within
                brightness = [float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()) for frame in frames if frame is not None]
                fade = bool(brightness and min(brightness) <= 14 and max(brightness) >= 38)
                protected = fade or (cross >= 0.50 and jump_ratio >= 1.45)
                raw.boundary_metrics = {
                    "cross_distance": round(cross, 4),
                    "ssim": cross_metrics["ssim"],
                    "hsv_distance": cross_metrics["hsv_distance"],
                    "phash_distance": cross_metrics["phash_distance"],
                    "edge_distance": cross_metrics["edge_distance"],
                    "brightness_delta": cross_metrics["brightness_delta"],
                    "visual_similarity": round(1.0 - cross, 4),
                    "left_motion": round(left_motion, 4),
                    "right_motion": round(right_motion, 4),
                    "jump_ratio": round(jump_ratio, 4),
                    "fade_or_black": fade,
                    "protected": protected,
                }
        finally:
            capture.release()
    except (ExecutionCancelled, ExecutionDeadlineExceeded):
        raise
    except Exception as exc:
        for raw in raw_shots[:-1]:
            raw.boundary_metrics = {"analysis_error": str(exc), "protected": True}


def consolidate_scenes(raw_shots: List[RawDetectedShot], settings: Settings) -> List[DetectedShot]:
    if not raw_shots:
        return []
    narrative: List[DetectedShot] = []
    current: List[RawDetectedShot] = [raw_shots[0]]
    scene_number = 1

    def finish_group() -> None:
        nonlocal current
        number = len(narrative) + 1
        narrative.append(DetectedShot(number, current[0].start_time, current[-1].end_time, [item.number for item in current], f"scene-{scene_number:03d}", "连续动作合并" if len(current) > 1 else "原始切段"))
        for item in current:
            item.narrative_number = number
        current = []

    for index, next_raw in enumerate(raw_shots[1:]):
        previous = raw_shots[index]
        metrics = previous.boundary_metrics
        protected = bool(metrics.get("protected", True))
        similarity = float(metrics.get("visual_similarity", 0.0))
        jump_ratio = float(metrics.get("jump_ratio", math.inf))
        short_side = min(previous.duration, next_raw.duration)
        within_limits = len(current) < settings.max_raw_shots_per_narrative
        merge = within_limits and not protected and (
            similarity >= 0.74
            or (current[-1].end_time - current[0].start_time < settings.narrative_min_seconds and similarity >= 0.48)
            or (short_side < 0.80 and similarity >= 0.56)
            or (jump_ratio <= 1.05 and similarity >= 0.45)
        )
        if merge:
            previous.decision = "merge_next"
            previous.reason = f"视觉连续：相似度 {similarity:.2f}，跳变比 {jump_ratio:.2f}"
            current.append(next_raw)
        else:
            previous.decision = "keep"
            if protected:
                previous.reason = "保护边界：硬切、渐变或显著视觉跳变"
            elif not within_limits:
                previous.reason = "达到原始镜头数量上限"
            else:
                previous.reason = f"视觉变化足够明显：相似度 {similarity:.2f}，跳变比 {jump_ratio:.2f}"
            finish_group()
            if protected:
                scene_number += 1
            current = [next_raw]
    if current:
        raw_shots[-1].reason = raw_shots[-1].reason or "视频结束"
        finish_group()
    return narrative


def _sample_times(shot: DetectedShot, count: int) -> List[float]:
    if count <= 1:
        return [shot.start_time + shot.duration / 2]
    if count == 4:
        return [shot.start_time + shot.duration * fraction for fraction in (0.10, 0.35, 0.65, 0.90)]
    margin = min(0.25, shot.duration / 10)
    start = shot.start_time + margin
    end = shot.end_time - margin
    return [start + (end - start) * index / (count - 1) for index in range(count)]


def extract_frames(video_path: Path, shots: List[DetectedShot], output_dir: Path, settings: Settings) -> List[ExtractedFrame]:
    frames: List[ExtractedFrame] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for shot in shots:
        check_execution()
        shot_dir = output_dir / f"shot-{shot.number:03d}"
        shot_dir.mkdir(parents=True, exist_ok=True)
        count = min(settings.frames_per_shot, settings.max_frames_per_shot)
        for index, timestamp in enumerate(_sample_times(shot, count)):
            frame_id = f"frame-{shot.number:03d}-{index + 1:02d}"
            output = shot_dir / f"{frame_id}.jpg"
            vf = f"scale=w='min({settings.frame_max_long_edge},iw)':h='min({settings.frame_max_long_edge},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuvj420p"
            try:
                _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.3f}", "-i", str(video_path), "-frames:v", "1", "-vf", vf, "-q:v", "4", "-y", str(output)])
            except FileNotFoundError:
                raise RuntimeError("未找到 ffmpeg，请先安装 FFmpeg")
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"关键帧提取失败：{exc.stderr[-500:]}")
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError(f"关键帧提取失败：时间点 {timestamp:.3f}s 未生成图像")
            width, height = 0, 0
            try:
                from PIL import Image

                with Image.open(output) as image:
                    width, height = image.size
            except Exception:
                pass
            frames.append(ExtractedFrame(frame_id, f"shot-{shot.number:03d}", timestamp, str(output), str(output), width, height))
    return frames


def extract_raw_contact_frames(video_path: Path, raw_shots: List[RawDetectedShot], output_dir: Path, settings: Settings) -> List[ExtractedFrame]:
    """Extract one traceable midpoint image per raw shot for semantic grouping."""
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: List[ExtractedFrame] = []
    for raw in raw_shots:
        timestamp = raw.start_time + raw.duration / 2
        frame_id = f"raw-{raw.number:03d}"
        output = output_dir / f"{frame_id}.jpg"
        vf = f"scale='min({settings.frame_max_long_edge},iw)':-2,format=yuvj420p"
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.3f}", "-i", str(video_path), "-frames:v", "1", "-vf", vf, "-q:v", "5", "-y", str(output)])
        frames.append(ExtractedFrame(frame_id, frame_id, timestamp, str(output), str(output)))
    return frames


def apply_semantic_groups(raw_shots: List[RawDetectedShot], groups: List[List[int]], settings: Settings, allow_confirmed_angle_cuts: bool = False) -> List[DetectedShot]:
    """Apply valid adjacent VLM groups without crossing protected or safety limits."""
    expected = [raw.number for raw in raw_shots]
    flattened = [number for group in groups for number in group]
    if flattened != expected:
        raise ValueError("语义分组未完整覆盖原始镜头")
    by_number = {raw.number: raw for raw in raw_shots}
    narrative: List[DetectedShot] = []
    for group in groups:
        items = [by_number[number] for number in group]
        adjacent = all(right.number == left.number + 1 for left, right in zip(items, items[1:]))
        protected = any(
            bool(item.boundary_metrics.get("fade_or_black"))
            or bool(item.boundary_metrics.get("scene_discontinuity"))
            or (bool(item.boundary_metrics.get("protected", True)) and not allow_confirmed_angle_cuts)
            for item in items[:-1]
        )
        if not adjacent or protected or len(items) > settings.max_raw_shots_per_narrative:
            items_groups = [[item] for item in items]
        else:
            items_groups = [items]
        for safe_items in items_groups:
            number = len(narrative) + 1
            narrative.append(DetectedShot(number, safe_items[0].start_time, safe_items[-1].end_time, [item.number for item in safe_items], f"scene-{number:03d}", "视觉模型确认动作连续" if len(safe_items) > 1 else "原始切段"))
            for item in safe_items:
                item.narrative_number = number
                item.decision = "merge_next" if item is not safe_items[-1] else "keep"
                item.reason = "视觉模型联系表确认人物、地点和动作连续" if len(safe_items) > 1 else (item.reason or "视觉模型联系表保留边界")
    return narrative


def probe_frame_times(video_path: Path, duration: float) -> List[float]:
    """Read presentation timestamps once; VFR videos must not be cut on nominal FPS."""
    try:
        result = _run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "frame=best_effort_timestamp_time",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video_path),
        ])
        times = sorted({float(line) for line in result.stdout.splitlines() if line.strip() and line.strip() != "N/A"})
        return [0.0, *[time for time in times if 0.0 < time < duration], duration]
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
        return []


def _cut_quality(capture, timestamp: float, duration: float) -> Tuple[float, str]:
    """Prefer stable action pauses, or a persistent high-contrast visual insert."""
    before_far = _read_frame(capture, timestamp - 0.28, duration)
    before = _read_frame(capture, timestamp - 0.055, duration)
    after = _read_frame(capture, timestamp + 0.055, duration)
    after_far = _read_frame(capture, timestamp + 0.28, duration)
    if any(frame is None for frame in (before_far, before, after, after_far)):
        return -1.0, "时长保护"
    cross = _frame_distance(before, after)
    local = (_frame_distance(before_far, before) + _frame_distance(after, after_far)) / 2
    if cross >= 0.55 and cross > max(local, 0.04) * 1.8 and local < 0.30:
        return 2.0 + cross, "显著视觉变化"
    return 1.0 - cross - local * 0.35, "动作停顿"


def _source_for_interval(raw_shots: List[RawDetectedShot], start: float, end: float, fallback: List[int]) -> List[int]:
    numbers = [raw.number for raw in raw_shots if raw.start_time < end - 1e-5 and raw.end_time > start + 1e-5]
    return numbers or list(fallback)


def split_long_shots(
    shots: List[DetectedShot],
    maximum_seconds: float | None = None,
    video_path: Path | None = None,
    frame_times: List[float] | None = None,
    raw_shots: List[RawDetectedShot] | None = None,
) -> List[DetectedShot]:
    """Legacy opt-in span splitter; source analysis never adds duration cuts.

    Kept for explicit historical tooling. Omitting a limit preserves the source
    boundaries and only restores the provenance metadata.
    """
    if maximum_seconds is not None and (not math.isfinite(maximum_seconds) or maximum_seconds <= 0):
        raise ValueError("显式区间上限必须为有限正数")
    result: List[DetectedShot] = []
    frame_times = frame_times or []
    raw_shots = raw_shots or []
    capture = None
    if video_path and maximum_seconds is not None:
        try:
            import cv2

            capture = cv2.VideoCapture(str(video_path))
            if not capture.isOpened():
                capture.release()
                capture = None
        except Exception:
            capture = None
    try:
        for shot in shots:
            if shot.end_time <= shot.start_time:
                raise ValueError(f"原始镜头 {shot.number} 时间范围无效")
            count = max(1, math.ceil((shot.end_time - shot.start_time) / maximum_seconds)) if maximum_seconds is not None else 1
            boundaries = [shot.start_time]
            reasons: List[str] = []
            for index in range(1, count):
                previous = boundaries[-1]
                remaining = count - index
                lower = max(previous + 0.08, shot.end_time - remaining * maximum_seconds)
                upper = min(previous + maximum_seconds, shot.end_time - remaining * 0.08)
                if lower > upper + 1e-6:
                    raise ValueError(f"镜头 {shot.number} 找不到符合显式区间上限的连续切分点")
                target = shot.start_time + shot.duration * index / count
                if frame_times:
                    left = bisect_left(frame_times, lower - 1e-6)
                    right = bisect_right(frame_times, upper + 1e-6)
                    candidates = frame_times[left:right]
                else:
                    candidates = [min(upper, max(lower, target))]
                nearby = [time for time in candidates if abs(time - target) <= 0.9] or candidates
                if len(nearby) > 10:
                    stride = math.ceil(len(nearby) / 9)
                    nearest = min(nearby, key=lambda time: abs(time - target))
                    nearby = sorted({*nearby[::stride], nearest})
                if not nearby:
                    raise ValueError(f"镜头 {shot.number} 缺少可用视频帧切点")
                if capture is None:
                    chosen, reason = min(nearby, key=lambda time: abs(time - target)), "时长保护"
                else:
                    scored = [(_cut_quality(capture, time, shot.end_time), -abs(time - target), time) for time in nearby]
                    quality, _, chosen = max(scored, key=lambda entry: (entry[0][0], entry[1]))
                    reason = quality[1]
                boundaries.append(chosen)
                reasons.append(reason)
            boundaries.append(shot.end_time)
            for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
                if end <= start or (maximum_seconds is not None and end - start > maximum_seconds + 1e-6):
                    raise ValueError(f"镜头 {shot.number} 切分后时长不合法：{start:.3f}–{end:.3f}")
                source = _source_for_interval(raw_shots, start, end, shot.source_raw_numbers)
                edits = [raw.end_time for raw in raw_shots if raw.number in source and start + 1e-5 < raw.end_time < end - 1e-5]
                result.append(DetectedShot(
                    len(result) + 1, start, end, source, shot.scene_group_id,
                    reasons[index - 1] if index else shot.split_reason,
                    edits, shot.scene_reason,
                ))
    finally:
        if capture is not None:
            capture.release()
    return result


def assign_scene_groups(shots: List[DetectedShot], raw_shots: List[RawDetectedShot], semantic_starts: List[int] | None = None) -> None:
    """Group by narrative continuity, independently of edit and generation boundaries."""
    by_end = {round(raw.end_time, 4): raw for raw in raw_shots[:-1]}
    starts = set(semantic_starts or [])
    scene_number = 1
    for index, shot in enumerate(shots):
        if not index:
            shot.scene_reason = "视觉模型判断剧情时空与事件连续" if semantic_starts is not None else "视觉保守分组，待语义复核"
        if index:
            boundary = by_end.get(round(shots[index - 1].end_time, 4))
            metrics = boundary.boundary_metrics if boundary else {}
            if semantic_starts is not None:
                scene_break = shot.number in starts
                reason = "视觉模型判断剧情时空或连续事件变化" if scene_break else "剧情时空与事件连续"
            else:
                scene_break = bool(metrics.get("fade_or_black")) or (
                    boundary is not None
                    and float(metrics.get("visual_similarity", 0.0)) < 0.22
                    and float(metrics.get("jump_ratio", math.inf)) > 2.2
                )
                reason = "视觉边界保守划分，需复核场次" if scene_break else "视觉连续，场次待语义复核"
            if scene_break:
                scene_number += 1
            shot.scene_reason = reason
        shot.scene_group_id = f"scene-{scene_number:03d}"



def validate_generation_plan(raw_shots: List[RawDetectedShot], shots: List[DetectedShot], maximum_seconds: float | None = None) -> None:
    """Validate source coverage independently of future generation duration."""
    if not raw_shots or not shots:
        raise ValueError("生成镜头或原始切段为空")
    if maximum_seconds is not None and (not math.isfinite(maximum_seconds) or maximum_seconds <= 0):
        raise ValueError("显式区间上限必须为有限正数")
    for index, raw in enumerate(raw_shots):
        if not all(math.isfinite(time) for time in (raw.start_time, raw.end_time)) or raw.end_time <= raw.start_time:
            raise ValueError(f"原始镜头 {raw.number} 时间无效")
        if index and abs(raw_shots[index - 1].end_time - raw.start_time) > 0.002:
            raise ValueError(f"原始镜头 {raw.number} 前有时间缺口或重叠")
    if abs(raw_shots[0].start_time - shots[0].start_time) > 0.002 or abs(raw_shots[-1].end_time - shots[-1].end_time) > 0.002:
        raise ValueError("生成镜头未覆盖视频完整时间范围")
    by_number = {raw.number: raw for raw in raw_shots}
    if len(by_number) != len(raw_shots):
        raise ValueError("原始切段编号重复")
    for index, shot in enumerate(shots):
        if not all(math.isfinite(time) for time in (shot.start_time, shot.end_time)) or shot.end_time <= shot.start_time:
            raise ValueError(f"镜头 {shot.number} 时间无效")
        if maximum_seconds is not None and shot.duration > maximum_seconds + 1e-6:
            raise ValueError(f"镜头 {shot.number} 超过显式区间上限")
        if index and abs(shots[index - 1].end_time - shot.start_time) > 0.002:
            raise ValueError(f"镜头 {shot.number} 前有时间缺口或重叠")
        expected = _source_for_interval(raw_shots, shot.start_time, shot.end_time, [])
        if shot.source_raw_numbers != expected:
            raise ValueError(f"镜头 {shot.number} 原始切段归属不完整")
    if any(not any(raw.number in shot.source_raw_numbers for shot in shots) for raw in raw_shots):
        raise ValueError("存在未归属的原始切段")


def process_video(video_path: Path, output_dir: Path, settings: Settings) -> VideoProcessResult:
    if current_execution() is not None:
        # OpenCV/PySceneDetect decoding also belongs to the cancellable process
        # group: a stuck native decoder cannot strand the serial worker.
        payload = {key: getattr(settings, key) for key in (
            "max_video_duration_seconds", "max_shots", "frames_per_shot", "max_frames_per_shot",
            "frame_max_long_edge", "jpeg_quality")}
        response = _run([sys.executable, "-m", "app.media_worker", str(video_path),
                         str(output_dir), json.dumps(payload)], timeout=300)
        data = json.loads(response.stdout)
        return VideoProcessResult(metadata=data["metadata"],
            shots=[DetectedShot(**item) for item in data["shots"]],
            frames=[ExtractedFrame(**item) for item in data["frames"]],
            raw_shots=[RawDetectedShot(**item) for item in data["raw_shots"]],
            warnings=data["warnings"], segmentation_profile=data["segmentation_profile"],
            frame_times=data["frame_times"])
    check_execution()
    metadata = probe_video(video_path)
    if metadata["duration"] > settings.max_video_duration_seconds:
        raise RuntimeError(f"视频时长超过限制（{settings.max_video_duration_seconds} 秒）")
    from app.services.capacity import video_preflight
    capacity = video_preflight(metadata["duration"], settings.max_shots, settings.max_video_duration_seconds)
    if not capacity["accepted"]:
        raise RuntimeError("；".join(capacity["reasons"]))
    raw_shots, warnings = detect_scenes(video_path, metadata, settings.max_shots)
    analyze_boundaries(video_path, raw_shots, metadata["duration"])
    # Raw edit boundaries are evidence, not a generation-length budget. Leave
    # narrative aggregation to the evidence-bearing event Agent after analysis.
    shots = [DetectedShot(raw.number, raw.start_time, raw.end_time, [raw.number]) for raw in raw_shots]
    frame_times = probe_frame_times(video_path, metadata["duration"])
    if not frame_times:
        warnings.append("无法读取视频帧时间戳，切点已按时间估计；请复核可变帧率素材")
    if len(shots) > settings.max_shots:
        raise RuntimeError(f"来源镜头共 {len(shots)} 个，超过 {settings.max_shots} 镜上限；请按场次分批，未截断视频")
    validate_generation_plan(raw_shots, shots)
    assign_scene_groups(shots, raw_shots)
    for raw in raw_shots:
        raw.generation_numbers = [shot.number for shot in shots if raw.number in shot.source_raw_numbers]
        raw.narrative_number = raw.generation_numbers[0] if raw.generation_numbers else 0
    frames = extract_frames(video_path, shots, output_dir / "frames", settings)
    return VideoProcessResult(metadata, shots, frames, warnings, raw_shots, SEGMENTATION_PROFILE, frame_times)
