"""Evidence-preserving action delivery, never equal-time subdivision.

Semantic summaries/phases are produced in the existing motion request. Local
rules only guard obvious omissions; they are not a semantic equivalence proof.
"""
from __future__ import annotations

import re
from app.services.generation_timing import allocate_phase_durations, ceil_half_seconds

GENERIC = {"未知", "人物移动", "主体移动", "表情变化", "人物表情变化", "画面变化", "动作变化", "人物动作"}
# Independent outcomes must not disappear behind a shorter summary. Aliases are
# deliberately conservative; unrecognized source clauses are not rewritten.
KEY_EVENTS = (
    ("打碎", "摔碎", "砸碎"), ("递出", "递给", "递水", "交给"),
    ("接住", "接过", "接水"), ("放下", "放在", "放到"),
    ("打开", "推开", "开门"), ("关上", "关门", "关闭"),
    ("倒入", "倒进", "倾倒"), ("舀起", "舀出", "舀取"),
    ("跌落", "掉落", "掉下"), ("转身",), ("离开", "走出"),
    ("皱眉", "眉头", "笑容消失"),
)


def known_action(value):
    text = str(value or "").strip().strip("。；; ")
    return bool(text and text not in GENERIC and not text.startswith(("未知", "无法判断", "待判断", "unknown")))


def _omitted_events(summary, steps):
    return [step for step in steps if any(
        any(alias in step for alias in event) and not any(alias in summary for alias in event)
        for event in KEY_EVENTS)]


def _direction_gap(summary, steps):
    # Explicit source directions are stronger than an abbreviated description.
    # This does not infer directions from scene position, gaze or camera motion.
    pattern = r"逆时针|顺时针|向左|向右|向上|向下|向前|向后|从左到右|从右到左"
    aliases = {"从左到右": "向右", "从右到左": "向左"}
    markers = lambda text: {aliases.get(token, token) for token in re.findall(pattern, text)}
    required = set().union(*(markers(step) for step in steps))
    actual = markers(summary)
    opposites = (("向左", "向右"), ("向上", "向下"), ("向前", "向后"), ("顺时针", "逆时针"))
    return not required.issubset(actual) or any(
        (left in required and right in actual and right not in required) or
        (right in required and left in actual and left not in required) for left, right in opposites)


def validate_phase_sequence(phases, step_count):
    """Structural source coverage and evidence order, not semantic correctness."""
    if not 2 <= len(phases) <= 3:
        raise ValueError("语义阶段应为 2–3 个；单动作无需阶段时间表")
    indices = [index for phase in phases for index in phase.source_step_indices]
    if indices != list(range(1, step_count + 1)):
        raise ValueError("阶段必须按序覆盖全部来源步骤，不得遗漏或重复")
    if len({phase.description.strip() for phase in phases}) != len(phases):
        raise ValueError("重复描述不构成不同语义阶段")
    if any(not known_action(phase.description) for phase in phases):
        raise ValueError("阶段描述不能是未知或泛化空话")
    if any(phase.confidence < 0.5 for phase in phases):
        raise ValueError("阶段证据置信度不足")
    if any(re.search(r"保持[^，。；]{0,40}(?:姿态|状态|注视|凝视)|始终|全程|一直", phase.description) for phase in phases):
        raise ValueError("保持姿态或未经证实的全程状态不能单独编成后续阶段")
    if any(min(left.evidence, key=lambda ref: ref.timestamp).timestamp >
           max(right.evidence, key=lambda ref: ref.timestamp).timestamp
           for left, right in zip(phases, phases[1:])):
        raise ValueError("阶段证据与动作顺序冲突")


def valid_generation_timing(phases, duration):
    """Accept only explicit, contiguous model suggestions covering the budget."""
    if not phases or any(phase.generation_start_seconds is None or phase.generation_end_seconds is None for phase in phases):
        return False
    if abs(phases[0].generation_start_seconds) > 0.001 or abs(phases[-1].generation_end_seconds - duration) > 0.001:
        return False
    return all(0 <= phase.generation_start_seconds < phase.generation_end_seconds <= duration + 0.001
               for phase in phases) and all(
        abs(left.generation_end_seconds - right.generation_start_seconds) <= 0.001
        for left, right in zip(phases, phases[1:]))


def build_action_presentation(observation, duration):
    details = observation.action_details
    steps = [step.strip().strip("。；; ") for step in details.steps if known_action(step)]
    summary = observation.subject_action.strip().strip("。；; ")
    warnings = []
    source = "model_summary"
    if not known_action(summary):
        summary, source = ("；".join(steps), "source_steps") if steps else ("未知", "unknown")
    elif _omitted_events(summary, steps) or _direction_gap(summary, steps):
        summary, source = "；".join(steps), "source_steps"
        warnings.append("动作摘要可能省略独立事件或来源方向，已保留来源动作；未自动拆镜")
    states = [state.strip().strip("。；; ") for state in details.accompanying_states if known_action(state)][:2]
    # A list's ordering alone never proves simultaneity or full-shot persistence.
    states = list(dict.fromkeys(states))
    phases = []
    timing_mode = "none"
    if details.action_phases:
        try:
            validate_phase_sequence(details.action_phases, len(details.steps))
            phase_text = "；".join(phase.description for phase in details.action_phases)
            if _omitted_events(phase_text, steps) or _direction_gap(phase_text, steps):
                raise ValueError("语义阶段可能遗漏独立动作事件或来源方向")
            source_end = details.action_phases[-1].generation_end_seconds
            timed = valid_generation_timing(details.action_phases, duration)
            if not timed and source_end and ceil_half_seconds(source_end) == duration:
                timed = valid_generation_timing(details.action_phases, source_end)
            if any(phase.generation_start_seconds is not None for phase in details.action_phases) and not timed:
                warnings.append("阶段秒数与时长预算不一致，保留动作顺序，不重新均分")
            timing_mode = "generation-suggestion" if timed else "sequence"
            source = "phases"
            phases = [dict(description=phase.description,
                           start_seconds=phase.generation_start_seconds if timed else None,
                           end_seconds=phase.generation_end_seconds if timed else None,
                           source_step_indices=phase.source_step_indices,
                           evidence=[ref.model_dump() for ref in phase.evidence])
                      for phase in details.action_phases]
            if timed:
                try:
                    allocated = allocate_phase_durations([
                        phase.generation_end_seconds - phase.generation_start_seconds
                        for phase in details.action_phases], duration)
                    cursor = 0.0
                    for phase, seconds in zip(phases, allocated):
                        phase['start_seconds'], phase['end_seconds'] = cursor, cursor + seconds
                        cursor += seconds
                except ValueError:
                    # Too many micro-phases for this budget: retain ordered
                    # actions without inventing zero-length generation stages.
                    timing_mode = 'sequence'
                    for phase in phases:
                        phase['start_seconds'] = phase['end_seconds'] = None
        except (ValueError, TypeError, AttributeError) as exc:
            warnings.append(f"阶段安排待复核，保留已观察动作：{exc}")
    return dict(schema_version="action-presentation.v1", main_action=summary,
                accompanying_states=states, phases=phases, timing_mode=timing_mode,
                source=source, warnings=warnings,
                evidence_granularity="item" if phases or details.step_evidence else "field" if details.evidence else "none")


def render_action_lines(presentation, *, compact=False):
    """Render a delivery view without changing exact source/evidence metadata.

    The legacy view remains available for stored contracts and callers. Compact
    delivery moves measurement provenance to the contract, but retains a sampled
    state's limited scope instead of claiming it persists for the whole shot.
    """
    if presentation["timing_mode"] == "generation-suggestion":
        if compact:
            from app.services.prompt_delivery import display_timing
            timing = display_timing(presentation["phases"][-1]["end_seconds"], [
                (phase["start_seconds"], phase["end_seconds"]) for phase in presentation["phases"]])
            lines = ["动作时间线："]
            lines += [f"{display['display_start_seconds']}–{display['display_end_seconds']} 秒：{phase['description']}。"
                      for phase, display in zip(presentation["phases"], timing["intervals"])]
        else:
            lines = ["动作时间安排（生成编排建议，不是原片动作时间实测）："]
            lines += [f"{phase['start_seconds']:g}–{phase['end_seconds']:g} 秒：{phase['description']}。"
                      for phase in presentation["phases"]]
    elif presentation["phases"]:
        lines = ["动作阶段：" if compact else "动作阶段（仅表示观察到的先后顺序）："]
        lines += [f"阶段 {index + 1}：{phase['description']}。" for index, phase in enumerate(presentation["phases"])]
    else:
        lines = [f"动作：{presentation['main_action']}。" if presentation["source"] != "unknown" else "动作：未知，不猜测动作。"]
    if presentation["accompanying_states"]:
        prefix = "伴随状态（采样可见）：" if compact else "伴随状态（采样帧可见，不推定全程）："
        lines.append(prefix + "；".join(presentation["accompanying_states"]) + "。")
    return lines
