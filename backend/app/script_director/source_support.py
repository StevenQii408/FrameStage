"""Compile narrative facts from authored actions; camera decisions stay creative.

Exact quotation is a deliberately limited, deterministic contract, not a claim
that substring matching proves semantic entailment. Legacy plans remain readable.
"""
from __future__ import annotations

import re

VERSION = "script-source-support.v1"
NORMALIZATION_NOTE = "剧情动作与结果由原文引用编译，摄影与事件分组保留导演方案"


def _text(value):
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def authored_actions(spans):
    return [(span.id, span.text if span.role == "action" else span.parenthetical)
            for span in spans if span.role == "action" or
            (span.role in {"dialogue", "voiceover"} and span.parenthetical)]


def _checked_quotes(quotes, actions):
    by_id = dict(actions)
    positions = {sid: index for index, (sid, _) in enumerate(actions)}
    previous = (-1, -1)
    result = []
    for reference in quotes:
        original = by_id.get(reference.span_id)
        if original is None or reference.quote not in original:
            raise ValueError("剧情引用必须逐字来自本镜已归属的动作或括号表演，不能引用对白意图、声音或其他镜头")
        start = original.find(reference.quote, previous[1] if previous[0] == positions[reference.span_id] else 0)
        position = (positions[reference.span_id], start)
        if start < 0 or position < previous:
            raise ValueError("剧情引用必须按原文顺序，不得重排或重复动作")
        previous = (position[0], start + len(reference.quote))
        result.append(reference.quote)
    if _text("".join(result)) != _text("".join(text for _, text in actions)):
        raise ValueError("剧情引用必须完整保留本镜原文动作，不能遗漏、重复或用新增剧情替换")
    return result


def compile_source_support(decision, document, previous_state=None):
    """No new paid call: source-only facts, free camera/coverage decisions.

    References can split a single authored action into timed phases. With no
    valid timing references, retain source order without inventing timestamps.
    """
    spans = {span.id: span for span in document.spans}
    previous = (previous_state or {}).get("previous_shot", {}).get("end_state", "")
    for event in decision.events:
        event_spans = [spans[sid] for sid in event.source_span_ids]
        actions = authored_actions(event_spans)
        # Titles/goals are displayed as source narrative, so never publish an
        # unverified model-written new goal or ending as if it were authored.
        source_summary = "；".join(text.rstrip("。；") for _, text in actions)
        if source_summary:
            event.title = source_summary[:60]
            event.goal = source_summary
        else:
            event.title = "原文对白与声音"
            event.goal = "按原文顺序呈现对白与声音，不补写剧情结果"
        for shot in event.shots:
            selected = [spans[sid] for sid in shot.source_span_ids]
            actions = authored_actions(selected)
            if actions:
                quotes = _checked_quotes(shot.source_quotes, actions) if shot.source_quotes else [text for _, text in actions]
                # Re-check persisted derived fields as well: a marker must not
                # authorize an edited plan to smuggle in a new action later.
                shot.action_steps = quotes if len(quotes) <= 4 else ["；".join(quotes)]
                if shot.action_timeline:
                    phase_refs = [ref for phase in shot.action_timeline for ref in phase.source_quotes]
                    if phase_refs:
                        _checked_quotes(phase_refs, actions)
                        if any(not phase.source_quotes for phase in shot.action_timeline):
                            raise ValueError("每个剧情阶段都必须有原文引用，不能混入无来源的动作阶段")
                        for phase in shot.action_timeline:
                            phase.description = "；".join(ref.quote for ref in phase.source_quotes)
                    else:
                        shot.action_timeline = []
                shot.end_state = quotes[-1]
            elif all(span.role == "sound" for span in selected):
                # validate_decision already retained the prior image for sound.
                shot.action_steps = ["保持当前画面与人物姿态，原文声音仅在画外发生"]
                shot.action_timeline = []
                shot.end_state = previous or "原文未指定可见动作，保持人物与环境的当前状态"
            else:
                shot.action_steps = ["主体保持当前姿态，按原对白或旁白自然表达"]
                shot.action_timeline = []
                shot.end_state = previous or "保持当前姿态，原文未说明新的动作结果"
            # A beginning pose is staging, not permission to add a plot result.
            # Reuse a verified preceding state, otherwise explicitly prepare for
            # the first authored action without asserting it has happened.
            # The preceding end may itself be an exact multi-action quote, not
            # a static pose. Never copy that sequence into a first-frame image.
            shot.first_frame_state = ("承接上一镜结束时的人物与场景，主体处于本镜动作开始前的准备姿态" if previous else
                                      "原文主体处于首个动作开始前的准备姿态，人物与环境沿用原文设定")
            shot.source_support_version = VERSION
            previous = shot.end_state
    return NORMALIZATION_NOTE
