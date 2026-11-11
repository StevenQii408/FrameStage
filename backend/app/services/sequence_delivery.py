"""Shared deterministic delivery settings for complete event video prompts.

These are generator settings, never a claim about the source video's resolution.
The renderer does not infer motion, alter source coverage or request a model.
"""
from __future__ import annotations

import math
import re


VERSION = "sequence-delivery.v2"


def _ratio(value):
    match = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[:：/]\s*(\d+(?:\.\d+)?)(?![\d.])", str(value or ""))
    if not match:
        return None
    left, right = float(match[1]), float(match[2])
    if not 0 < left <= 10000 or not 0 < right <= 10000:
        return None
    return left, right


def video_spec(duration, *, aspect_ratio="待设定", frames=(), resolution="1080p"):
    """Prefer saved aspect, then uncropped source evidence, then an explicit default."""
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("生成视频时长必须为正数")
    pair = _ratio(aspect_ratio)
    aspect_source = "source_contract"
    if pair is None:
        aspect_source = "reference_frame"
        for frame in frames:
            field = lambda key, default=None: frame.get(key, default) if isinstance(frame, dict) else getattr(frame, key, default)
            width, height = field("width", 0), field("height", 0)
            if field("parent_frame_id") or field("artifact_kind", "keyframe") != "keyframe":
                continue
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                divisor = math.gcd(width, height)
                pair = width // divisor, height // divisor
                break
    if pair is None:
        pair, aspect_source = (9, 16), "generation_default"
    left, right = pair
    orientation = "横屏" if left > right else "竖屏" if left < right else "方形画幅"
    return {"duration_seconds": duration, "resolution": str(resolution or "1080p"),
            "aspect_ratio": f"{left:g}:{right:g}", "orientation": orientation,
            "aspect_source": aspect_source, "resolution_source": "generation_setting"}


def video_spec_line(spec):
    default_note = "（未设置画幅，采用默认生成画幅）" if spec["aspect_source"] == "generation_default" else ""
    return (f"视频规格（生成设置）：时长 {spec['duration_seconds']:g} 秒；"
            f"分辨率 {spec['resolution']}；{spec['orientation']} {spec['aspect_ratio']}{default_note}。")


def style_line(value):
    """One named style block, without manufacturing a missing style."""
    value = str(value or "").strip()
    if not value or value in {"未知", "无", "画风待确认", "待确认"}:
        return "整体画面风格：待确认，保持来源已见视觉语言。"
    if value.startswith("整体画面风格"):
        return value
    return "整体画面风格：" + value
