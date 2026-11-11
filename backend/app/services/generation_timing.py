"""Generation-only half-second timebase. Source PTS/evidence are never changed."""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import math

VERSION = "generation-timing.half-second.v1"


def half_second_ticks(seconds):
    """Round up once; tolerate binary noise at an existing half-second boundary."""
    value = float(seconds)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("生成时长必须为正且有限")
    nearest = round(value * 2)
    if nearest > 0 and abs(value * 2 - nearest) <= 1e-9:
        return nearest
    return int((Decimal(str(seconds)) * 2).to_integral_value(rounding=ROUND_CEILING))


def ceil_half_seconds(seconds):
    return half_second_ticks(seconds) / 2


def allocate_phase_durations(durations, total_seconds=None):
    """Map semantic durations into one fixed tick budget, never ceil each phase.

    Returns positive half-second durations in source order. A caller must merge
    compatible micro-phases when there are more phases than available ticks.
    """
    weights = [float(value) for value in durations]
    if not weights or any(not math.isfinite(value) or value <= 0 for value in weights):
        raise ValueError("阶段时长必须为正且有限")
    total = half_second_ticks(sum(weights) if total_seconds is None else total_seconds)
    if total < len(weights):
        raise ValueError("半秒预算不足以容纳全部语义阶段")
    weight_sum, consumed, cumulative = sum(weights), 0, 0.0
    boundaries = [0]
    for index, weight in enumerate(weights[:-1]):
        cumulative += weight
        suggested = int((Decimal(str(cumulative / weight_sum)) * total).to_integral_value(rounding=ROUND_HALF_UP))
        consumed = max(consumed + 1, min(suggested, total - (len(weights) - index - 1)))
        boundaries.append(consumed)
    boundaries.append(total)
    return [(right - left) / 2 for left, right in zip(boundaries, boundaries[1:])]
