"""Non-billable planning estimates, replaced by exact frozen-source counts."""
from __future__ import annotations

import math
from types import SimpleNamespace

from app.event_agent.engine import build_event_windows
from app.event_agent.models import EventAgentConfig


# Match the project-wide bounded long-source perception operation. Ordinary
# sources each retain their own two base calls and at most one extra call.
LONG_SOURCE_MAX_WINDOWS = 12
LONG_SOURCE_MAX_CALLS = 36
LONG_SOURCE_TIMEOUT_SECONDS = 600


def video_preflight(duration_seconds, max_shots=120, max_duration_seconds=600, shots=None):
    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("视频时长必须为有限正数")
    # Duration says nothing about the number of real edit boundaries: a
    # ten-minute continuous take is still one source shot. Actual shot count
    # is checked after local detection, before any billable inference.
    minimum = 1
    config = EventAgentConfig()
    span_seconds = min(config.window_seconds, math.floor(config.generation_max_seconds * 2) / 2)
    exact = shots is not None
    count = len(shots) if exact else None
    minimum_perception_windows = max(1, math.ceil(duration / span_seconds))
    short_windows, long_windows, scheduled_long_windows, pending_windows = 0, 0, 0, 0
    if exact:
        sources = [SimpleNamespace(id=f"source-{i}", number=i, start_time=s.start_time, end_time=s.end_time)
                   for i, s in enumerate(shots)]
        event_windows = build_event_windows(sources, config)
        windows = len(event_windows)
        # Match source_perception's chronological, project-shared budget. The
        # pending source spans stay in the plan but do not imply billed calls.
        available_spans = set()
        remaining = min(LONG_SOURCE_MAX_WINDOWS, LONG_SOURCE_MAX_CALLS // 2)
        for source in sources:
            required = max(1, math.ceil((source.end_time - source.start_time) / span_seconds))
            if required == 1:
                short_windows += 1
                scheduled = 1
            else:
                long_windows += required
                scheduled = min(required, remaining)
                scheduled_long_windows += scheduled
                remaining -= scheduled
            for index in range(scheduled):
                available_spans.add((source.id, source.start_time + index * span_seconds,
                    min(source.end_time, source.start_time + (index + 1) * span_seconds)))
        pending_windows = long_windows - scheduled_long_windows
        perception_windows = short_windows + long_windows
        scheduled_perception_windows = short_windows + scheduled_long_windows
        eligible_agent_windows = sum(any((span["source_shot_id"], span["start_time"], span["end_time"]) in available_spans
                                        for span in window.source_spans) for window in event_windows)
        if count <= 1 and not long_windows:
            eligible_agent_windows = 0  # A single short source needs no merge.
    else:
        # A lower bound only: real cuts and the image cap can add windows.
        windows = max(1, math.ceil(duration / config.window_seconds))
        perception_windows = scheduled_perception_windows = eligible_agent_windows = None
    reasons = []
    if duration > max_duration_seconds:
        reasons.append(f"视频超过 {max_duration_seconds} 秒上限，请分批上传")
    if exact and count > max_shots:
        reasons.append(f"来源镜头数为 {count}，超过 {max_shots} 镜上限；请按场次分批，未截断视频")
    if windows > 64:
        reasons.append("事件规划超过64个受控窗口，请分批处理")
    budget_notes = []
    if exact and pending_windows:
        budget_notes.append(f"长来源镜头需要 {long_windows} 个分析窗口，首轮最多安排 {scheduled_long_windows} 个，"
                            f"其余 {pending_windows} 个保留待分析；不会截断来源或声称已完整分析")
    cross_windows = math.ceil(max(0, (count or minimum) - 1) / 7)
    bounded_short_windows = short_windows if exact else max_shots
    bounded_long_calls = min(LONG_SOURCE_MAX_CALLS, scheduled_long_windows * 3) if exact else LONG_SOURCE_MAX_CALLS
    bounded_agent_windows = eligible_agent_windows if exact else 64
    bounded_cross_windows = math.ceil(max(0, (count if exact else max_shots) - 1) / 7)
    perception_base_calls = scheduled_perception_windows * 2 if exact else None
    perception_max_calls = bounded_short_windows * 3 + bounded_long_calls
    coverage = "rejected" if reasons else "pending" if not exact else "partial" if pending_windows else "full"
    return {"version": "capacity-preflight.v2", "input_type": "video", "exact": exact,
            "accepted": not reasons, "reasons": reasons, "duration_seconds": duration,
            "source_shot_count": count, "source_count_basis": "frozen-plan" if exact else "not-yet-detected",
            "minimum_source_shots": minimum, "max_source_shots": max_shots,
            "effective_max_duration_seconds": max_duration_seconds,
            "event_window_count": windows, "event_window_count_basis": "frozen-plan" if exact else "lower-bound",
            "perception_window_count": perception_windows,
            "minimum_perception_windows": minimum_perception_windows,
            "long_source_window_count": long_windows if exact else None,
            "scheduled_perception_window_count": scheduled_perception_windows,
            "scheduled_long_source_window_count": scheduled_long_windows if exact else None,
            "pending_perception_window_count": pending_windows if exact else None,
            "agent_analyzable_window_count": eligible_agent_windows,
            "long_source_budget": {"window_seconds": span_seconds, "max_windows": LONG_SOURCE_MAX_WINDOWS,
                                   "max_model_calls": LONG_SOURCE_MAX_CALLS, "timeout_seconds": LONG_SOURCE_TIMEOUT_SECONDS},
            "agent_max_windows": min(64, max(config.max_windows, windows)),
            "budget_coverage": coverage, "budget_notes": budget_notes,
            "model_calls": {"normal_estimate": 2 + perception_base_calls + cross_windows + eligible_agent_windows if exact else None,
                            "minimum_estimate": 2 + (scheduled_perception_windows if exact else min(minimum_perception_windows, LONG_SOURCE_MAX_WINDOWS)) * 2,
                            "full_coverage_normal_estimate": 2 + perception_windows * 2 + cross_windows + (windows if count > 1 or long_windows else 0) if exact else None,
                            "perception_base_calls": perception_base_calls,
                            "perception_bounded_maximum": perception_max_calls,
                            "event_agent_normal_estimate": eligible_agent_windows,
                            "event_agent_bounded_maximum": bounded_agent_windows * config.max_model_calls_per_window,
                            "style_normal_allowance": 2,
                            "bounded_maximum": 4 + perception_max_calls + bounded_cross_windows * 2 + bounded_agent_windows * 2,
                            "estimate_only": not exact, "excludes_explicit_recovery": True},
            "estimated_list_price_cny": None,
            "cost_note": "调用量按实际分析窗口估算，含画风与连续性预算；超预算区间保留待分析，耗时也可能提前耗尽预算。人民币费用需实际图片Token与响应长度，未将未知费用记为0"}


def script_preflight(text, max_shots=120):
    from app.script_director.document import parse_script_document
    from app.script_director.engine import director_windows
    if not text.strip():
        raise ValueError("请提供剧本内容")
    if len(text) > 10000:
        raise ValueError(f"剧本为{len(text)}字，超过10000字上限；请按场次分批，未截断原文")
    document = parse_script_document(text)
    windows = director_windows(document)
    reasons = []
    if not windows:
        reasons.append("只有人物设定或备注，没有剧情、对白或声音")
    if len(document.content_spans) > max_shots * 4:
        reasons.append("剧情片段超过安全上限，请按场次分批")
    return {"version": "capacity-preflight.v1", "input_type": "script", "accepted": not reasons,
            "exact": False, "reasons": reasons, "characters": len(text),
            "content_span_count": len(document.content_spans), "window_count": len(windows),
            "max_source_shots": max_shots, "budget_coverage": "pending" if not reasons else "rejected",
            "model_calls": {"normal_estimate": len(windows), "bounded_maximum": len(windows) * 2},
            "estimated_list_price_cny": None,
            "cost_note": "导演镜头数由事件与表演决定；实际费用待Token返回后记录"}
