"""Bounded, evidence-gated orchestration with conservative whole-source fallback."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import List

from app.event_agent.models import (
    EVENT_RESPONSE_ADAPTER, CausalFact, EventAgentConfig, EventAgentResult,
    EventProposal, EventResponseSchema, EventWindow, FinalizeResponse, GeneratedStage, GenerationPhotography,
    NarrativeEvent, PresentationDecision, RequestEvidenceResponse, SourceOwnership,
    ToolResult, WindowAudit, WindowUsage,
)
from app.event_agent.prompts import build_window_prompt
from app.event_agent.observations import saved_observations
from app.event_agent.rendering import digest, render_event_clips
from app.perception_models import EvidenceRef, iter_observed_fields
from app.providers.base import FrameInput
from app.schemas import FrameArtifact, Shot


VERSION = "event-storyboard-agent.v3.3"
_SUMMARY_SPEECH = re.compile(r"交谈|交談|聊天|对话|對話|对白|對白|说话|說話|讲话|講話|旁白|台词|臺詞|发声|發聲|说出|說出|喊道|问道|問道|\b(?:speak(?:s|ing)?|talk(?:s|ing)?|dialogue|conversation|voice-?over)\b", re.IGNORECASE)
# A focused guard for interpretations that still-frame-visible motions do not
# establish. Ordinary handoff, drinking, smiling and looking remain free to use
# natural wording; these risk labels need literal support in this stage's facts.
_SUMMARY_INFERENCE_TERMS = (
    "品尝", "吹气", "喘息", "呼吸急促",
    "喝光", "喝完", "喝尽", "一饮而尽",
    "疲惫", "疲倦", "虚弱", "不适", "疼痛", "痛苦", "中毒", "生病", "病倒", "窒息", "昏迷",
    "焦虑", "恐惧", "惊恐", "愤怒", "悲伤", "满意",
    "导致", "引发", "造成", "因为", "因此",
)
_SUMMARY_UNCERTAINTY = re.compile(r"或|可能|似乎|无法判断|不能确定|疑似|看似")


class WindowStopped(RuntimeError):
    pass


class ActionSummaryError(ValueError):
    """A recoverable delivery field error, never a source/fact/ownership error."""

    def __init__(self, message, stage, kind):
        super().__init__(message)
        self.stage = stage
        self.kind = kind


def build_event_windows(shots: List[Shot], config: EventAgentConfig) -> List[EventWindow]:
    """Assign each whole source exactly once; previous-window tail is read-only."""
    from app.event_agent.spans import span_plan
    ordered, span_metadata = span_plan(shots, config)
    if len({shot.id for shot in ordered}) != len(ordered):
        raise ValueError("Source Shot IDs must be unique")
    groups, current = [], []
    for shot in ordered:
        if shot.end_time <= shot.start_time:
            raise ValueError(f"Invalid source interval: {shot.id}")
        if current and (shot.end_time - current[0].start_time > config.window_seconds
                        or len(current) >= config.max_initial_frames_per_window):
            groups.append(current)
            current = []
        current.append(shot)
    if current:
        groups.append(current)
    windows, previous = [], []
    for index, group in enumerate(groups, 1):
        start = group[0].start_time
        context = [shot for shot in previous if shot.end_time > start - config.overlap_seconds]
        windows.append(EventWindow(
            id=f"event-window-{index:03d}", start_time=start, end_time=group[-1].end_time,
            source_shot_ids=[shot.id for shot in group],
            context_shot_ids=[shot.id for shot in context],
            context_start_time=max(context[0].start_time, start - config.overlap_seconds) if context else None,
            source_spans=[span_metadata[shot.id] for shot in group],
        ))
        previous = group
    return windows


def _normal(text):
    compact = re.sub(r"\s", "", text or "")
    # Formatting is not a paraphrase. Preserve punctuation between digits so
    # e.g. 1.5 litres, 15 litres and 1/5 litres cannot become the same fact.
    return "".join(char for index, char in enumerate(compact)
                   if not unicodedata.category(char).startswith("P")
                   or (index > 0 and index + 1 < len(compact)
                       and compact[index - 1].isdigit() and compact[index + 1].isdigit()))


def _source_texts(shot):
    return [row["text"] for row in saved_observations(shot)]


def _generation_labels(members, shot_map):
    owned = [shot_map[sid] for sid in members if sid in shot_map]
    known = lambda value: isinstance(value, str) and value.strip() not in ("", "未知", "无", "待设定", "待判断", "待确认")
    subjects = {value for shot in owned for value in (
        shot.action_details.primary_subject, *shot.visual_facts.people) if known(value)}
    objects = {value for shot in owned for value in shot.visual_facts.objects if known(value)}
    return subjects, objects


def source_has_saved_observations(shot):
    """Photography/style and a failed request are not successful observations.

    Prefer the evidence-bearing IR when present: compatibility projections can
    contain generic failure text that must not turn an entirely unknown source
    into a model-owned causal event. Legacy/script inputs retain literal atoms.
    """
    observation = shot.visual_observation_v2
    if observation is not None:
        return bool(_source_texts(shot)) and any(
            field.observable and field.evidence for group in (observation.facts, observation.dynamics)
            for field in iter_observed_fields(group))
    return bool(_source_texts(shot))


def _actual_scene_id(shot):
    value = (shot.scene_group_id or "").strip()
    reason = shot.scene_reason or ""
    if (value in ("", "未知", "无") or value.startswith(("legacy-", "presentation-"))
            or reason == "legacy-presentation" or "待" in reason or "复核" in reason
            or reason.startswith("视觉") or reason.startswith("candidate-boundary:")
            or reason in ("剧情时空与事件连续", "剧情时空或连续事件变化")):
        return None
    return value


def _protected_boundaries(shots, boundaries):
    ordered = sorted(shots, key=lambda shot: (shot.start_time, shot.number, shot.id))
    adjacent = {(left.id, right.id) for left, right in zip(ordered, ordered[1:])}
    return {tuple(pair) for pair in (boundaries or ())
            if isinstance(pair, (tuple, list)) and len(pair) == 2
            and all(isinstance(sid, str) for sid in pair) and tuple(pair) in adjacent}


def _validate_ref(ref, allowed_ids, frames, shot_map):
    frame = frames.get(ref.frame_id)
    if frame is None:
        raise ValueError(f"Evidence frame does not exist: {ref.frame_id}")
    if frame.shot_id not in allowed_ids:
        raise ValueError(f"Evidence frame {frame.id} is outside its claimed source")
    if abs(ref.timestamp - frame.timestamp) > 0.001:
        raise ValueError(f"Evidence timestamp does not match artifact: {frame.id}")
    shot = shot_map[frame.shot_id]
    if not shot.start_time - 0.1 <= frame.timestamp <= shot.end_time + 0.1:
        raise ValueError(f"Evidence artifact is outside the whole source interval: {frame.id}")
    if ref.crop_artifact_id:
        crop = frames.get(ref.crop_artifact_id)
        if crop is None or not crop.artifact_kind.endswith("crop") or crop.parent_frame_id != frame.id:
            raise ValueError("Evidence crop must refer to a real crop of its referenced frame")
    if ref.bbox is not None and frame.bbox is not None:
        if tuple(ref.bbox) != tuple(frame.bbox):
            raise ValueError("Evidence crop bbox does not match the saved artifact")


def validate_event_proposals(proposals, window, shots, frames, *, protected_source_shot_ids=None, protected_boundaries=None):
    """Check coverage, chronology, saved evidence, and critical presentation locally."""
    shot_map = {shot.id: shot for shot in shots}
    frame_map = {frame.id: frame for frame in frames}
    owned = window.source_shot_ids
    protected = set(protected_source_shot_ids or ()) & set(owned)
    boundaries = _protected_boundaries(shots, protected_boundaries)
    flattened = [sid for proposal in proposals for sid in proposal.source_shot_ids]
    if flattened != owned:
        raise ValueError("Events must cover ordered whole owned sources exactly once; context is read-only")
    for proposal in proposals:
        members = proposal.source_shot_ids
        known_subjects, known_objects = _generation_labels(members, shot_map)
        for values, allowed, label in ((proposal.generation_subjects, known_subjects, "subjects"),
                                       (proposal.generation_objects, known_objects, "objects")):
            if values is not None and (len(set(values)) != len(values) or not set(values).issubset(allowed)):
                raise ValueError(f"Generation {label} must uniquely select exact saved labels from owned event sources")
        source_order = {shot.id: index for index, shot in enumerate(
            sorted(shots, key=lambda shot: (shot.start_time, shot.number, shot.id)))}
        member_order = [source_order[sid] for sid in members]
        if member_order != list(range(member_order[0], member_order[-1] + 1)):
            raise ValueError("An event cannot bridge independently retained unknown or unavailable whole sources")
        scene_ids = {_actual_scene_id(shot_map[sid]) for sid in members} - {None}
        if len(scene_ids) > 1:
            raise ValueError("An event cannot cross saved actual scene-group boundaries")
        if any(left in members and right in members for left, right in boundaries):
            raise ValueError("An event cannot cross a trusted scene-change or time-elision boundary")
        if len(members) > 1 and any(shot_map[sid].status == "failed" for sid in members):
            raise ValueError("Failed sources must remain independently owned and cannot be absorbed into a merged event")
        if proposal.scene_continuity != "unknown":
            ordered_shots = sorted(shots, key=lambda shot: (shot.start_time, shot.number, shot.id))
            first_index = source_order[members[0]]
            if not first_index:
                raise ValueError("A scene boundary judgment requires a preceding whole source")
            left, right = ordered_shots[first_index - 1], shot_map[members[0]]
            for ref in proposal.scene_evidence:
                _validate_ref(ref, {left.id, right.id}, frame_map, shot_map)
            if {frame_map[ref.frame_id].shot_id for ref in proposal.scene_evidence} != {left.id, right.id}:
                raise ValueError("Scene continuity requires real frame evidence from both adjacent boundary sources")
            if proposal.scene_continuity == "same_scene" and (
                    (left.id, right.id) in boundaries or
                    (_actual_scene_id(left) and _actual_scene_id(right)
                     and _actual_scene_id(left) != _actual_scene_id(right))):
                raise ValueError("Model scene continuity cannot override a trusted scene or time-elision boundary")
            if proposal.continuation_of_anchor_shot_id and proposal.scene_continuity in ("scene_change", "time_elision"):
                raise ValueError("An event continuation cannot cross its own scene-change or time-elision judgment")
        positions = {sid: index for index, sid in enumerate(members)}
        decisions = {decision.source_shot_id: decision for decision in proposal.presentation}
        if len(decisions) != len(proposal.presentation) or set(decisions) != set(members):
            raise ValueError("Each owned source must have exactly one presentation decision")
        if any(decisions[sid].disposition != "preserve" for sid in set(members) & protected):
            raise ValueError("Trusted protected reveal/reaction sources must preserve presentation; model relabeling cannot authorize omission")
        facts = {fact.id: fact for fact in proposal.causal_facts}
        if len(facts) != len(proposal.causal_facts):
            raise ValueError("Causal fact IDs must be unique within an event")
        evidenced_sources = set()
        for fact in proposal.causal_facts:
            if not fact.source_shot_ids or len(set(fact.source_shot_ids)) != len(fact.source_shot_ids):
                raise ValueError("Causal fact sources must be unique")
            if any(sid not in positions for sid in fact.source_shot_ids):
                raise ValueError("Causal facts may only refer to owned event sources")
            if [positions[sid] for sid in fact.source_shot_ids] != sorted(positions[sid] for sid in fact.source_shot_ids):
                raise ValueError("Causal fact source IDs must be ordered")
            for ref in fact.evidence:
                _validate_ref(ref, set(fact.source_shot_ids), frame_map, shot_map)
                evidenced_sources.add(frame_map[ref.frame_id].shot_id)
            saved = [text for sid in fact.source_shot_ids for text in _source_texts(shot_map[sid])]
            # A model-authored frame description is not a separately verified
            # atomic observation. Extra images can validate grouping/continuity,
            # but cannot authorize inventing new saved facts in this MVP.
            if _normal(fact.text) not in {_normal(text) for text in saved}:
                raise ValueError(f"causal_facts[{fact.id}].text: Causal text must literally quote a saved source observation; image descriptions are not independent verification; allowed observation_ids=" + ",".join(row["observation_id"] for sid in fact.source_shot_ids for row in saved_observations(shot_map[sid])))
            for sid in fact.source_shot_ids:
                catalog = saved_observations(shot_map[sid])
                matches = [row for row in catalog if _normal(row["text"]) == _normal(fact.text)]
                if fact.saved_observation_ids and not set(fact.saved_observation_ids).issubset({row["observation_id"] for owner in fact.source_shot_ids for row in saved_observations(shot_map[owner]) if _normal(row["text"]) == _normal(fact.text)}):
                    raise ValueError(f"causal_facts[{fact.id}].saved_observation_ids: unknown or mismatched saved atom")
                if fact.kind in ("action", "handoff", "reaction") and matches and all(row["observation_type"] == "static" for row in matches):
                    raise ValueError(f"causal_facts[{fact.id}].kind: static observation cannot establish an action; use context/state/detail")
                observation = shot_map[sid].visual_observation_v2
                if observation is None:
                    continue
                matching_fields = [field for group in (observation.facts, observation.dynamics)
                                   for field in iter_observed_fields(group) if field.observable
                                   and any(_normal(fact.text) == _normal(value)
                                           for value in (field.value if isinstance(field.value, list) else [field.value])
                                           if isinstance(value, str))]
                if matching_fields:
                    saved_evidence_ids = {ref.frame_id for field in matching_fields for ref in field.evidence}
                    claimed_ids = {ref.frame_id for ref in fact.evidence if frame_map[ref.frame_id].shot_id == sid}
                    if not claimed_ids.intersection(saved_evidence_ids):
                        raise ValueError(f"Causal frame evidence must include the saved literal observation's verified frame: {sid}")
            fact_evidence_sources = {frame_map[ref.frame_id].shot_id for ref in fact.evidence}
            if fact_evidence_sources != set(fact.source_shot_ids):
                raise ValueError("Each causal fact requires frame evidence from every claimed whole source")
            if fact.kind == "reveal":
                if not fact.critical or any(decisions[sid].disposition != "preserve" for sid in fact.source_shot_ids):
                    raise ValueError("Important reveals must remain critical and preserve presentation")
            if fact.kind == "reaction":
                if not fact.critical or any(decisions[sid].disposition == "omit" for sid in fact.source_shot_ids):
                    raise ValueError("Observed reactions must remain critical and presented within their event")
            if fact.critical and any(decisions[sid].disposition == "omit" for sid in fact.source_shot_ids):
                raise ValueError("Critical causal facts cannot be omitted")
        for decision in proposal.presentation:
            for ref in decision.evidence:
                _validate_ref(ref, {decision.source_shot_id}, frame_map, shot_map)
                evidenced_sources.add(frame_map[ref.frame_id].shot_id)
            if decision.disposition == "omit" and not decision.evidence:
                raise ValueError("Omission requires explicit source frame evidence")
        if not set(members).issubset(evidenced_sources):
            raise ValueError("Every owned source needs frame evidence, including omitted inserts")
        assigned, presented_facts = [], set()
        stage_ids = set()
        for index, stage in enumerate(proposal.stages):
            if stage.action_summary is not None:
                if not stage.action_summary.strip():
                    raise ValueError("Generated action summary must contain a nonblank action description")
                if _SUMMARY_SPEECH.search(stage.action_summary):
                    raise ActionSummaryError("Generated action summary cannot assert speech or dialogue; source audio was not analyzed", stage, "speech")
            if stage.id in stage_ids:
                raise ValueError("Generation stage IDs must be unique")
            stage_ids.add(stage.id)
            if any(sid not in positions for sid in stage.source_shot_ids):
                raise ValueError("Generation stages reference an unknown source")
            if any(decisions[sid].disposition == "omit" for sid in stage.source_shot_ids):
                raise ValueError("Omitted inserts cannot become generated action stages")
            if index == 0 and stage.transition != "initial":
                raise ValueError("The first generated stage must use transition=initial")
            if index > 0 and stage.transition == "initial":
                raise ValueError("Only the first generated stage can be initial")
            if proposal.generation_mode == "continuous" and stage.transition == "cut":
                raise ValueError("Continuous generation cannot contain physical cuts")
            for fid in stage.fact_ids:
                if fid not in facts or not set(facts[fid].source_shot_ids).issubset(set(stage.source_shot_ids)):
                    raise ValueError("Generated action must reference facts from its whole source stage")
                presented_facts.add(fid)
            if stage.action_summary is not None:
                for term in _SUMMARY_INFERENCE_TERMS:
                    if term not in stage.action_summary:
                        continue
                    supporting_facts = [facts[fid].text for fid in stage.fact_ids if term in facts[fid].text]
                    if not supporting_facts:
                        raise ActionSummaryError(f"Unsupported inferred action summary term '{term}': no literal support in this stage's validated fact.text", stage, "unsupported")
                    if (all(_SUMMARY_UNCERTAINTY.search(text) for text in supporting_facts)
                            and not _SUMMARY_UNCERTAINTY.search(stage.action_summary)):
                        raise ActionSummaryError(f"Action summary term '{term}' only has uncertain fact.text support; preserve uncertainty or describe visible motions instead", stage, "uncertain")
            fact_order = [min(positions[sid] for sid in facts[fid].source_shot_ids) for fid in stage.fact_ids]
            if fact_order != sorted(fact_order):
                raise ValueError("Generated facts must keep whole-source chronology; semantic phases cannot reorder saved actions")
            assigned.extend(stage.source_shot_ids)
        visible = [sid for sid in members if decisions[sid].disposition != "omit"]
        if assigned != visible:
            raise ValueError("Generated stages must cover ordered non-omitted whole sources exactly once")
        if any(fact.critical and fact.id not in presented_facts for fact in proposal.causal_facts):
            raise ValueError("Every critical causal fact must appear in a generated stage")
        for sid in visible:
            saved_action = shot_map[sid].action
            saved_steps = [step for step in shot_map[sid].action_details.steps if step.strip() not in ("", "未知", "无")]
            rendered_texts = [fact.text for fact in proposal.causal_facts
                              if sid in fact.source_shot_ids and fact.id in presented_facts
                              and (sid not in protected or fact.critical)]
            rendered_keys = {_normal(text) for text in rendered_texts}
            if saved_action and saved_action not in ("未知", "无") and _normal(saved_action) not in rendered_keys:
                if not saved_steps or not all(_normal(step) in rendered_keys for step in saved_steps):
                    raise ValueError(f"Saved source action or all its observed steps must remain presented as required critical facts: {sid}")
    return proposals


def _event_id(job_id, anchor):
    return "event-" + digest({"job_id": job_id, "anchor_shot_id": anchor})[:16]


def _make_event(proposal, window, job_id, shots, config):
    shot_map = {shot.id: shot for shot in shots}
    sources = [shot_map[sid] for sid in proposal.source_shot_ids]
    stages = proposal.stages
    if config.timing_policy == "source-faithful":
        visible_ids = {decision.source_shot_id for decision in proposal.presentation if decision.disposition != "omit"}
        total = sum(shot.duration for shot in sources)
        visible_total = sum(shot_map[sid].duration for sid in visible_ids)
        if visible_total > 0:
            stages = [stage.model_copy(update={"duration_seconds": sum(shot_map[sid].duration for sid in stage.source_shot_ids)
                                               * total / visible_total}) for stage in proposal.stages]
    event_hash = digest({"version": VERSION, "proposal": proposal.model_dump(mode="json"),
                         "sources": [{"id": shot.id, "start": shot.start_time, "end": shot.end_time,
                                      "observations": _source_texts(shot)} for shot in sources],
                         "generation_max_seconds": config.generation_max_seconds, "timing_policy": config.timing_policy})
    return NarrativeEvent(
        id=_event_id(job_id, sources[0].id), anchor_shot_id=sources[0].id,
        title=proposal.title, goal=proposal.goal, source_shot_ids=proposal.source_shot_ids,
        generation_subjects=proposal.generation_subjects, generation_objects=proposal.generation_objects,
        initial_state=sources[0].action_details.initial_state,
        final_state=sources[-1].action_details.final_state,
        causal_facts=proposal.causal_facts, presentation=proposal.presentation,
        generation_mode=proposal.generation_mode, stages=stages,
        confidence=proposal.confidence, window_id=window.id, version_hash=event_hash,
        photography=proposal.photography, timing_policy=config.timing_policy,
        scene_continuity=proposal.scene_continuity, scene_evidence=proposal.scene_evidence,
    )


def _saved_anchors(shot):
    participants = [shot.action_details.primary_subject, *shot.visual_facts.people]
    props = [shot.action_details.interaction_target, *shot.visual_facts.objects]
    clean = lambda values: {_normal(value) for value in values if value and value not in ("未知", "无")}
    return clean(participants), clean(props)


def _continue_event(previous, current, proposal, window, shots, frames, all_frames, protected_source_shot_ids=None, protected_boundaries=None):
    """Validate a proposed continuation without exact narrative-label matching."""
    if proposal.continuation_of_anchor_shot_id != previous.anchor_shot_id:
        raise ValueError("续接锚点不属于上一个事件")
    shot_map = {shot.id: shot for shot in shots}
    left, right = shot_map[previous.source_shot_ids[-1]], shot_map[current.source_shot_ids[0]]
    if current.status != "ready" or left.status == "failed" or right.status == "failed":
        raise ValueError("分析失败的来源不能与已验证事件续接")
    if left.id not in window.context_shot_ids:
        raise ValueError("续接来源不在只读尾部上下文中")
    if abs(left.end_time - right.start_time) > 0.1:
        raise ValueError("续接必须使用时间相邻的完整来源镜头")
    if _actual_scene_id(left) and _actual_scene_id(right) and _actual_scene_id(left) != _actual_scene_id(right):
        raise ValueError("已保存场景归属发生变化，保守保留事件边界")
    if (left.id, right.id) in _protected_boundaries(shots, protected_boundaries):
        raise ValueError("已核实场景变化或时间省略，保守保留事件边界")
    frame_map = {frame.id: frame for frame in frames}
    evidence_sources = set()
    for ref in proposal.continuation_evidence:
        _validate_ref(ref, {left.id, right.id}, frame_map, shot_map)
        evidence_sources.add(frame_map[ref.frame_id].shot_id)
    if evidence_sources != {left.id, right.id}:
        raise ValueError("续接必须同时提供前事件尾部和当前起点的真实帧证据")
    left_people, left_props = _saved_anchors(left)
    right_people, right_props = _saved_anchors(right)
    if not left_people & right_people:
        raise ValueError("续接缺少保存观察中一致的人物锚点")
    if left_props and right_props and not left_props & right_props:
        raise ValueError("续接道具锚点不一致，保守保留事件边界")
    # Canonical shared participants/props and two-sided frames support continuity.
    # Different camera views can describe the same endpoint differently, so natural
    # language equality is deliberately not treated as a compatibility classifier.
    def allocate(base, used):
        # A model can choose any ID, including our apparent namespace. Allocate
        # against the actual existing IDs rather than assuming prefixes are safe.
        for suffix in range(len(used) + 2):
            candidate = base if suffix == 0 else f"{base}~{suffix}"
            if candidate not in used:
                used.add(candidate)
                return candidate
        raise AssertionError("Could not allocate a unique derived ID")

    prefix = current.anchor_shot_id + ":"
    used_fact_ids = {fact.id for fact in previous.causal_facts}
    used_stage_ids = {stage.id for stage in previous.stages}
    fact_ids = {fact.id: allocate(prefix + fact.id, used_fact_ids) for fact in current.causal_facts}
    new_facts = [fact.model_copy(update={"id": fact_ids[fact.id]}) for fact in current.causal_facts]
    mode = "internal_cuts" if "internal_cuts" in (previous.generation_mode, current.generation_mode) else "continuous"
    new_stages = []
    for index, stage in enumerate(current.stages):
        transition = stage.transition
        if index == 0:
            revealing = any(fact.kind == "reveal" and fact.id in stage.fact_ids for fact in current.causal_facts)
            transition = "cut" if mode == "internal_cuts" and revealing else "continuation"
        new_stages.append(stage.model_copy(update={
            "id": allocate(prefix + stage.id, used_stage_ids), "fact_ids": [fact_ids[fid] for fid in stage.fact_ids],
            "transition": transition,
        }))
    def merged_selection(left, right, limit):
        if left is None or right is None:
            return None
        values = list(dict.fromkeys([*left, *right]))
        return values if len(values) <= limit else None

    merged = previous.model_copy(update={
        "title": current.title, "goal": current.goal,
        "source_shot_ids": [*previous.source_shot_ids, *current.source_shot_ids],
        "generation_subjects": merged_selection(previous.generation_subjects, current.generation_subjects, 4),
        "generation_objects": merged_selection(previous.generation_objects, current.generation_objects, 6),
        "final_state": current.final_state,
        "causal_facts": [*previous.causal_facts, *new_facts],
        "presentation": [*previous.presentation, *current.presentation],
        "stages": [*previous.stages, *new_stages], "generation_mode": mode,
        "confidence": min(previous.confidence, current.confidence),
        "warnings": [*previous.warnings, *current.warnings],
    })
    merged.version_hash = digest({"version": VERSION, "previous": previous.version_hash,
                                  "continued": current.version_hash,
                                  "evidence": [ref.model_dump(mode="json") for ref in proposal.continuation_evidence]})
    # Re-run semantic coverage checks after merging. model_construct is internal:
    # the original bounded response already validated; a cross-window event can
    # legitimately contain more than one response's 40-fact / 20-stage envelope.
    merged_proposal = EventProposal.model_construct(
        title=merged.title, goal=merged.goal, source_shot_ids=merged.source_shot_ids,
        generation_subjects=merged.generation_subjects, generation_objects=merged.generation_objects,
        causal_facts=merged.causal_facts, presentation=merged.presentation,
        generation_mode=merged.generation_mode, stages=merged.stages,
    )
    merged_window = EventWindow(id=window.id, start_time=shot_map[merged.source_shot_ids[0]].start_time,
                               end_time=right.end_time, source_shot_ids=merged.source_shot_ids)
    validate_event_proposals([merged_proposal], merged_window, shots, all_frames,
                            protected_source_shot_ids=protected_source_shot_ids, protected_boundaries=protected_boundaries)
    return merged


def _fallback_event(shot, window, job_id, frames, config, warning):
    source_frames = [frame for frame in frames if frame.shot_id == shot.id
                     and shot.start_time - 0.1 <= frame.timestamp <= shot.end_time + 0.1]
    text = shot.action or (shot.action_details.steps[0] if shot.action_details.steps else "保留当前来源镜头的已见状态")
    facts, stages = [], []
    if source_frames and text.strip() not in ("", "未知"):
        dynamics = shot.visual_observation_v2.dynamics if shot.visual_observation_v2 else None
        action_ids = {ref.frame_id for field in (dynamics.subject_action, dynamics.action_steps)
                      for ref in field.evidence} if dynamics else set()
        frame = next((candidate for candidate in source_frames if candidate.id in action_ids), source_frames[0])
        facts = [CausalFact(id="saved-action", text=text, source_shot_ids=[shot.id],
                            evidence=[EvidenceRef(frame_id=frame.id, timestamp=frame.timestamp, description=text)])]
        duration = max(0.05, shot.duration or 1.0)
        stages = [GeneratedStage(id="saved-source", source_shot_ids=[shot.id], fact_ids=["saved-action"],
                                 duration_seconds=duration, transition="initial")]
    payload = {"version": VERSION, "fallback": True, "source": shot.id,
               "observations": _source_texts(shot), "start": shot.start_time, "end": shot.end_time}
    return NarrativeEvent(
        id=_event_id(job_id, shot.id), anchor_shot_id=shot.id,
        title=text[:160], goal=text[:300], source_shot_ids=[shot.id],
        initial_state=shot.action_details.initial_state, final_state=shot.action_details.final_state,
        causal_facts=facts, presentation=[PresentationDecision(source_shot_id=shot.id,
                                                               disposition="preserve", reason="保守逐来源回退")],
        generation_mode="continuous", stages=stages, confidence=shot.confidence,
        status="fallback", window_id=window.id, version_hash=digest(payload), warnings=[warning],
        timing_policy=config.timing_policy,
        photography=GenerationPhotography(
            shot_size=shot.shot_size if shot.shot_size in ("大远景", "远景", "全景", "中景", "近景", "特写", "大特写") else "未知",
            lens=shot.lens if shot.lens in ("超广角", "广角", "标准", "长焦") else "未知",
            camera_movement=shot.camera_movement if shot.camera_movement in (
                "固定", "摇镜", "俯仰", "推近", "拉远", "跟拍", "手持", "升降", "环绕", "变焦", "航拍"
            ) else "未知", composition=shot.composition or "保留已见主体与空间关系",
        ),
    )


def _select_frames(window, frames, limit, shots=()):
    allowed = set(window.source_shot_ids + window.context_shot_ids)
    scope_start = window.context_start_time if window.context_start_time is not None else window.start_time
    selected = sorted((frame for frame in frames if frame.shot_id in allowed
                       and scope_start <= frame.timestamp < window.end_time),
                      key=lambda frame: (frame.timestamp, frame.id))
    if len(selected) <= limit:
        return selected
    # Every owner first, then verified dynamic endpoints, then read-only context
    # and dense samples. A first-frame grid must not crowd out action results.
    chosen = []
    shot_map = {shot.id: shot for shot in shots}
    frame_map = {frame.id: frame for frame in selected}
    endpoints = []
    for sid in window.source_shot_ids:
        candidates = [frame for frame in selected if frame.shot_id == sid]
        if candidates:
            shot = shot_map.get(sid)
            dynamics = shot.visual_observation_v2.dynamics if shot and shot.visual_observation_v2 else None
            action_ids, state_ids = set(), set()
            if dynamics:
                for name in ("subject_action", "action_steps", "initial_state", "final_state"):
                    field = getattr(dynamics, name)
                    if not field.observable:
                        continue
                    for ref in field.evidence:
                        try:
                            _validate_ref(ref, {sid}, frame_map, shot_map)
                        except ValueError:
                            continue
                        (action_ids if name in ("subject_action", "action_steps") else state_ids).add(ref.frame_id)
            # A source can start on the previous person's reaction before the
            # saved action appears. Prefer its verified dynamic evidence, not
            # blindly the first keyframe, when only one image will fit.
            chosen.append(next((frame for frame in candidates if frame.id in action_ids),
                               next((frame for frame in candidates if frame.id in state_ids),
                                    next((frame for frame in candidates if frame.selected), candidates[0]))))
            dynamic = [frame for frame in candidates if frame.id in action_ids | state_ids]
            if dynamic:
                endpoints.append(dynamic[-1])
    for endpoint in endpoints:
        if len(chosen) >= limit:
            break
        if endpoint.id not in {frame.id for frame in chosen}:
            chosen.append(endpoint)
    for sid in reversed(window.context_shot_ids):
        if len(chosen) >= limit:
            break
        candidates = [frame for frame in selected if frame.shot_id == sid]
        if candidates:
            chosen.append(candidates[-1])
    chosen_ids = {frame.id for frame in chosen}
    remainder = [frame for frame in selected if frame.id not in chosen_ids]
    count = limit - len(chosen)
    if count > 0 and remainder:
        indices = {round(index * (len(remainder) - 1) / max(1, count - 1)) for index in range(count)}
        chosen.extend(remainder[index] for index in sorted(indices))
    return sorted(chosen, key=lambda frame: (frame.timestamp, frame.id))


def _normalize_protocol(response, shots=(), frames=(), protected_source_shot_ids=None):
    """Compile saved atoms and protocol metadata without regrouping or retiming.

    A first-stage cut/continuation has no preceding stage inside this event.
    Canonicalizing it does not delete any internal physical cut; all later
    transitions remain untouched and undergo the normal strict validation.
    Likewise a literal, evidenced reaction/action already fully presented in a
    stage can be made critical without inventing or adding anything to a clip.
    Missing mandatory atoms can only come from a verified saved IR action and
    its delivered frames, inside the stage the model already assigned to it.
    """
    repairs = []
    shot_map = {shot.id: shot for shot in shots}
    frame_map = {frame.id: frame for frame in frames}
    protected = set(protected_source_shot_ids or ())
    if isinstance(response, FinalizeResponse):
        for proposal in response.events:
            # Optional display selections cannot invalidate correct event/fact
            # ownership. Keep exact saved labels only; never guess an alias.
            known_subjects, known_objects = _generation_labels(proposal.source_shot_ids, shot_map)
            removed_generation_labels = set()
            for name, allowed in (("generation_subjects", known_subjects), ("generation_objects", known_objects)):
                before = getattr(proposal, name)
                if before is None or (name == "generation_objects" and before == []):
                    continue
                after = list(dict.fromkeys(value for value in before if value in allowed)) or None
                removed = list(dict.fromkeys(value for value in before if value not in allowed))
                removed_generation_labels.update(removed)
                if after != before:
                    setattr(proposal, name, after)
                    display_name = "生成主体名单" if name == "generation_subjects" else "生成道具名单"
                    warning = f"事件 {proposal.source_shot_ids[0]} 的{display_name}已按本事件保存标签筛选并去重；无有效选择时使用完整来源描述，主体/道具展示需复核。"
                    repairs.append({"event_anchor_shot_id": proposal.source_shot_ids[0], "field": name,
                        "from": before, "to": after, "removed_labels": removed,
                        "reason": "optional display selection retains only exact saved owned-source labels without alias inference",
                        "warning": warning})
            if proposal.stages and proposal.stages[0].transition != "initial":
                first = proposal.stages[0]
                repairs.append({"event_anchor_shot_id": proposal.source_shot_ids[0],
                                "stage_id": first.id, "from": first.transition, "to": "initial"})
                proposal.stages[0] = first.model_copy(update={"transition": "initial"})
            decisions = {decision.source_shot_id: decision for decision in proposal.presentation}
            presented_ids = {fid for stage in proposal.stages for fid in stage.fact_ids}
            retained = []
            for fact in proposal.causal_facts:
                catalog = [row for sid in fact.source_shot_ids if sid in shot_map for row in saved_observations(shot_map[sid])]
                optional_unused = not fact.critical and fact.id not in presented_ids and fact.kind in ("context", "state", "detail")
                if optional_unused and _normal(fact.text) not in {_normal(row["text"]) for row in catalog}:
                    repairs.append({"event_anchor_shot_id": proposal.source_shot_ids[0], "fact_id": fact.id,
                        "field": "unused_context_fact", "reason": "unsupported optional unpresented context removed; required facts unchanged"})
                    continue
                retained.append(fact)
            proposal.causal_facts = retained
            for fact in proposal.causal_facts:
                matches = [row for sid in fact.source_shot_ids if sid in shot_map
                           for row in saved_observations(shot_map[sid]) if _normal(row["text"]) == _normal(fact.text)]
                if (fact.kind in ("action", "handoff", "reaction") and matches
                        and all(row["observation_type"] == "static" for row in matches)):
                    repairs.append({"event_anchor_shot_id": proposal.source_shot_ids[0], "fact_id": fact.id,
                        "field": "fact_kind", "from": fact.kind, "to": "context",
                        "reason": "saved static atom retained as context, never promoted to observed action"})
                    fact.kind = "context"
            hydrated = False
            # The model owns grouping/timing, not copying every verified atom.
            # Restore a dropped saved action only inside the unique source stage
            # it already assigned, using that action's actually delivered refs.
            for sid in proposal.source_shot_ids:
                shot = shot_map.get(sid)
                decision = decisions.get(sid)
                stages = [stage for stage in proposal.stages if sid in stage.source_shot_ids]
                if shot is None or decision is None or decision.disposition == "omit" or len(stages) != 1:
                    continue
                stage = stages[0]
                present = {_normal(fact.text) for fact in proposal.causal_facts
                           if sid in fact.source_shot_ids and fact.id in stage.fact_ids}
                action = shot.action
                steps = [step for step in shot.action_details.steps if step.strip() not in ("", "未知", "无")]
                if (_normal(action) in present or steps and all(_normal(step) in present for step in steps)):
                    continue
                observation = shot.visual_observation_v2
                field = observation.dynamics.subject_action if observation is not None else None
                if (field is None or not field.observable or not field.evidence
                        or _normal(field.value) != _normal(action) or not 0 < len(field.value) <= 600):
                    continue
                evidence = []
                for saved_ref in field.evidence:
                    try:
                        _validate_ref(saved_ref, {sid}, frame_map, shot_map)
                    except ValueError:
                        continue
                    evidence.append(saved_ref.model_copy(deep=True))
                if not evidence:
                    continue
                used_ids = {fact.id for fact in proposal.causal_facts}
                fact_id = "saved-source-action-" + sid
                while fact_id in used_ids:
                    fact_id += "~"
                proposal.causal_facts.append(CausalFact(id=fact_id, text=field.value, source_shot_ids=[sid],
                    evidence=evidence[:12], critical=True, kind="action"))
                stage.fact_ids.append(fact_id)
                hydrated = True
                repairs.append({"event_anchor_shot_id": proposal.source_shot_ids[0], "source_shot_id": sid,
                    "fact_id": fact_id, "field": "hydrated_saved_action", "frame_ids": [ref.frame_id for ref in evidence[:12]],
                    "reason": "verified saved literal action restored inside its already assigned source stage"})
            if hydrated:
                # Source 003 restored between 002 and 004 must not narrate after
                # 004 merely because compilation appended a fact. Stable keys
                # preserve each source's original model order and keep a joint
                # multi-source fact indivisible at its first source's position.
                source_rank = {sid: index for index, sid in enumerate(proposal.source_shot_ids)}
                proposal.causal_facts.sort(key=lambda fact: min(
                    source_rank.get(sid, len(source_rank)) for sid in fact.source_shot_ids))
                facts = {fact.id: fact for fact in proposal.causal_facts}
                for stage in proposal.stages:
                    previous_ids = list(stage.fact_ids)
                    stage_rank = {sid: index for index, sid in enumerate(stage.source_shot_ids)}
                    stage.fact_ids.sort(key=lambda fid: min(
                        (stage_rank.get(sid, len(stage_rank)) for sid in facts[fid].source_shot_ids),
                        default=len(stage_rank)) if fid in facts else len(stage_rank))
                    if stage.fact_ids != previous_ids:
                        repairs.append({"event_anchor_shot_id": proposal.source_shot_ids[0],
                            "stage_id": stage.id, "field": "fact_order", "from": previous_ids,
                            "to": list(stage.fact_ids), "reason": "restored fact ordered by first whole-source owner; same-source model order preserved"})
            # Strengthen existing literal dynamic atoms using only their own
            # verified, delivered references. No final-state text, inferred
            # action, or substitute for invalid model evidence is introduced.
            for fact in proposal.causal_facts:
                added_ids = []
                for sid in fact.source_shot_ids:
                    shot = shot_map.get(sid)
                    if shot is None or shot.visual_observation_v2 is None:
                        continue
                    fields = [field for field in iter_observed_fields(shot.visual_observation_v2.dynamics)
                              if field.observable and any(_normal(fact.text) == _normal(value)
                                  for value in (field.value if isinstance(field.value, list) else [field.value])
                                  if isinstance(value, str))]
                    saved_refs = []
                    for field in fields:
                        for ref in field.evidence:
                            try:
                                _validate_ref(ref, {sid}, frame_map, shot_map)
                            except ValueError:
                                continue
                            saved_refs.append(ref)
                    saved_refs.sort(key=lambda ref: (ref.timestamp, ref.frame_id))
                    if len({ref.frame_id for ref in saved_refs}) < 2:
                        continue
                    for ref in (saved_refs[0], saved_refs[-1]):
                        if len(fact.evidence) >= 12 or ref.frame_id in {item.frame_id for item in fact.evidence}:
                            continue
                        fact.evidence.append(ref.model_copy(deep=True, update={"description": "已验证动态帧定位"}))
                        added_ids.append(ref.frame_id)
                if added_ids:
                    repairs.append({"event_anchor_shot_id": proposal.source_shot_ids[0], "fact_id": fact.id,
                        "field": "dynamic_evidence_endpoints", "added_frame_ids": added_ids,
                        "reason": "same saved literal atom and whole source; verified delivered endpoint evidence only"})
            # An optional scene interpretation cannot invalidate a fully grounded
            # event. Unavailable references mean unknown, never replacement
            # evidence or an inferred new scene. Trusted-boundary contradictions
            # still go through the strict validator below.
            if proposal.scene_continuity != "unknown":
                ordered = sorted(shots, key=lambda shot: (shot.start_time, shot.number, shot.id))
                source_order = {shot.id: index for index, shot in enumerate(ordered)}
                first_index = source_order.get(proposal.source_shot_ids[0], 0)
                try:
                    if not first_index:
                        raise ValueError("no preceding source for scene interpretation")
                    boundary = {ordered[first_index - 1].id, proposal.source_shot_ids[0]}
                    for ref in proposal.scene_evidence:
                        _validate_ref(ref, boundary, frame_map, shot_map)
                    if {frame_map[ref.frame_id].shot_id for ref in proposal.scene_evidence} != boundary:
                        raise ValueError("scene interpretation lacks both adjacent source frames")
                except ValueError as exc:
                    repairs.append({"event_anchor_shot_id": proposal.source_shot_ids[0], "field": "scene_continuity",
                        "from": proposal.scene_continuity, "to": "unknown", "discarded_scene_evidence":
                        [ref.model_dump(mode="json") for ref in proposal.scene_evidence], "warning": str(exc) if first_index else "",
                        "reason": "unverified optional scene interpretation discarded; event ownership/timing unchanged"})
                    proposal.scene_continuity, proposal.scene_evidence = "unknown", []
            for index, fact in enumerate(proposal.causal_facts):
                if fact.critical or any(sid not in shot_map or sid not in decisions
                                       or decisions[sid].disposition == "omit" for sid in fact.source_shot_ids):
                    continue
                required = fact.kind in ("reaction", "reveal") or any(
                    _normal(fact.text) == _normal(shot_map[sid].action)
                    or any(_normal(fact.text) == _normal(step) for step in shot_map[sid].action_details.steps)
                    for sid in fact.source_shot_ids)
                presented = any(fact.id in stage.fact_ids and set(fact.source_shot_ids).issubset(set(stage.source_shot_ids))
                                for stage in proposal.stages)
                literal = any(_normal(fact.text) == _normal(text) for sid in fact.source_shot_ids
                              for text in _source_texts(shot_map[sid]))
                if not (required and presented and literal):
                    continue
                try:
                    for ref in fact.evidence:
                        _validate_ref(ref, set(fact.source_shot_ids), frame_map, shot_map)
                    if {frame_map[ref.frame_id].shot_id for ref in fact.evidence} != set(fact.source_shot_ids):
                        continue
                except ValueError:
                    continue
                proposal.causal_facts[index] = fact.model_copy(update={"critical": True})
                repairs.append({"event_anchor_shot_id": proposal.source_shot_ids[0], "fact_id": fact.id,
                                "field": "critical", "from": False, "to": True,
                                "reason": "literal evidenced fact already fully presented in generation stage"})
            for sid in proposal.source_shot_ids:
                decision = decisions.get(sid)
                if sid not in protected or sid not in shot_map or decision is None or decision.disposition != "integrate":
                    continue
                stages = [stage for stage in proposal.stages if sid in stage.source_shot_ids]
                if len(stages) != 1:
                    continue
                stage, shot = stages[0], shot_map[sid]
                literal_keys, fact_ids = set(), []
                for fact in proposal.causal_facts:
                    if (not fact.critical or fact.id not in stage.fact_ids or sid not in fact.source_shot_ids
                            or not set(fact.source_shot_ids).issubset(set(stage.source_shot_ids))
                            or _normal(fact.text) not in {_normal(text) for text in _source_texts(shot)}):
                        continue
                    try:
                        for ref in fact.evidence:
                            _validate_ref(ref, set(fact.source_shot_ids), frame_map, shot_map)
                        if {frame_map[ref.frame_id].shot_id for ref in fact.evidence} != set(fact.source_shot_ids):
                            continue
                    except ValueError:
                        continue
                    if shot.visual_observation_v2 is not None:
                        fields = [field for group in (shot.visual_observation_v2.facts, shot.visual_observation_v2.dynamics)
                                  for field in iter_observed_fields(group) if field.observable and any(
                                      _normal(fact.text) == _normal(value) for value in (
                                          field.value if isinstance(field.value, list) else [field.value])
                                      if isinstance(value, str))]
                        saved_ids = {ref.frame_id for field in fields for ref in field.evidence}
                        cited_ids = {ref.frame_id for ref in fact.evidence if frame_map[ref.frame_id].shot_id == sid}
                        if fields and not cited_ids.intersection(saved_ids):
                            continue
                    literal_keys.add(_normal(fact.text))
                    fact_ids.append(fact.id)
                steps = [step for step in shot.action_details.steps if step.strip() not in ("", "未知", "无")]
                action_present = shot.action.strip() not in ("", "未知", "无") and _normal(shot.action) in literal_keys
                steps_present = bool(steps) and all(_normal(step) in literal_keys for step in steps)
                if not (action_present or steps_present):
                    continue
                # Preserve is a content-protection marker, not a new event or
                # camera cut. Integrate already presents these verified atoms.
                decision.disposition = "preserve"
                repairs.append({"event_anchor_shot_id": proposal.source_shot_ids[0], "source_shot_id": sid,
                    "stage_id": stage.id, "fact_ids": fact_ids, "field": "protected_presentation",
                    "from": "integrate", "to": "preserve",
                    "reason": "trusted protected source already fully presented with critical saved literal facts and delivered evidence"})
            # A removed novel label must not survive via free-form delivery text.
            # A label present in this stage's saved facts remains valid wording,
            # even if it was not an exact primary_subject/people display label.
            fact_map = {fact.id: fact for fact in proposal.causal_facts}
            for stage in proposal.stages:
                if stage.action_summary is None:
                    continue
                stage_subjects, stage_objects = _generation_labels(stage.source_shot_ids, shot_map)
                texts = [fact_map[fid].text for fid in stage.fact_ids if fid in fact_map]
                unsupported = sorted(label for label in removed_generation_labels
                    if label in stage.action_summary and label not in (stage_subjects | stage_objects)
                    and not any(label in text for text in texts))
                if unsupported:
                    warning = f"事件 {proposal.source_shot_ids[0]} 阶段 {stage.id} 的动作摘要引用无本阶段支持的展示标签，已回到完整原事实，摘要需复核。"
                    repairs.append({"event_anchor_shot_id": proposal.source_shot_ids[0], "stage_id": stage.id,
                        "field": "action_summary", "from": stage.action_summary, "to": None,
                        "unsupported_labels": unsupported,
                        "reason": "removed optional display labels have no support in this stage's saved labels or fact.text",
                        "warning": warning})
                    stage.action_summary = None
    return repairs


def _frame_input(frame):
    return FrameInput(id=frame.id, timestamp=frame.timestamp, path=frame.path,
                      width=frame.width, height=frame.height, artifact_kind=frame.artifact_kind,
                      parent_frame_id=frame.parent_frame_id,
                      bbox=tuple(frame.bbox) if frame.bbox else None)


def _run_event_agent(*, job_id: str, run_id: str, shots: List[Shot], frames: List[FrameArtifact],
                    provider, tool_factory, config: EventAgentConfig, style_text="",
                    cancelled=lambda: False, emit=lambda type, payload: None,
                    protected_source_shot_ids=None, protected_boundaries=None, candidate_boundaries=None) -> EventAgentResult:
    """No provider/tool retries are hidden in this orchestration.

    Provider transport must honor timeout_seconds; late returns are discarded and
    cannot initiate additional requests. Cancellation is checked before and after
    every external operation, and audit is emitted before returning a partial run.
    """
    config = EventAgentConfig.model_validate(config)
    start_run = time.monotonic()
    windows = build_event_windows(shots, config)
    shot_map = {shot.id: shot for shot in shots}
    protected_source_shot_ids = set(protected_source_shot_ids or ()) & set(shot_map)
    protected_boundaries = _protected_boundaries(shots, protected_boundaries)
    events, clips, audits, extra_frames, warnings = [], [], [], [], []
    run_cancelled = False
    all_frame_ids = {frame.id for frame in frames}
    for window_index, window in enumerate(windows):
        start_window = time.monotonic()
        deadline = start_window + config.window_timeout_seconds
        usage = WindowUsage()
        raw_responses, tool_results, errors, window_warnings = [], [], [], []
        fact_reviews = {}
        source_frames = [frame for frame in [*frames, *extra_frames]
                         if frame.shot_id in shot_map
                         and shot_map[frame.shot_id].start_time - 0.1 <= frame.timestamp
                         <= shot_map[frame.shot_id].end_time + 0.1]
        available = {frame.shot_id for frame in source_frames}
        isolated = {sid for sid in window.source_shot_ids
                    if shot_map[sid].status == "failed" or not source_has_saved_observations(shot_map[sid])
                    or sid not in available}
        analyzable = [sid for sid in window.source_shot_ids if sid not in isolated]
        # Keep the persisted window and its ownership unchanged. Only the model
        # view excludes sources that cannot possibly produce a literal fact.
        # They remain read-only visual context and are independently restored.
        model_window = window.model_copy(update={
            "source_shot_ids": analyzable,
            "context_shot_ids": [*window.context_shot_ids, *[sid for sid in window.source_shot_ids if sid in isolated]],
        })
        for sid in window.source_shot_ids:
            if sid in isolated:
                reason = ("分析失败" if shot_map[sid].status == "failed" else
                          "缺少可用真实帧" if sid not in available else "全部内容观察未知")
                window_warnings.append(f"来源 {sid} {reason}，独立保守保留；不填造动作，不影响其余有证据来源的事件合并。")
        selected = _select_frames(model_window, source_frames, config.max_initial_frames_per_window, shots)
        known = list(selected)
        scope_frames = [frame for frame in [*frames, *extra_frames]
                        if frame.shot_id in set(window.source_shot_ids + window.context_shot_ids)]
        proposals = None
        fallback_reason = ""
        registry = None
        last_finalized = None

        def check_budget():
            if run_cancelled or cancelled():
                raise WindowStopped("cancelled")
            if time.monotonic() >= deadline:
                raise WindowStopped("window deadline exceeded")

        def model_call(phase, previous="", validation_errors=None):
            nonlocal last_finalized
            check_budget()
            if usage.model_calls >= config.max_model_calls_per_window:
                raise WindowStopped("model call budget exhausted")
            usage.model_calls += 1  # failures and blank responses consume the call
            call_start = time.monotonic()
            prompt = build_window_prompt(model_window, shots, known, config, phase=phase,
                                         previous_response=previous, errors=validation_errors,
                                         tool_results=tool_results,
                                         previous_tail_event=events[-1] if events else None,
                                         protected_source_shot_ids=protected_source_shot_ids,
                                         protected_boundaries=protected_boundaries,
                                         system_fallback_source_shot_ids=isolated,
                                         candidate_boundaries=candidate_boundaries)
            raw = ""
            record = {"window_id": window.id, "phase": f"event_agent.{phase}", "response": ""}
            try:
                raw = provider.analyze_event_window(
                    prompt, [_frame_input(frame) for frame in known], phase=f"event_agent.{phase}",
                    schema=EventResponseSchema, timeout_seconds=max(0.001, deadline - time.monotonic()),
                    max_tokens=config.max_tokens,
                )
                if not isinstance(raw, str):
                    raise ValueError("Provider must return a JSON string")
                record["response"] = raw
                check_budget()
                if not raw.strip():
                    raise ValueError("Provider returned a blank response")
                response = EVENT_RESPONSE_ADAPTER.validate_json(raw)
                # A review suggestion is not permission to modify facts or start
                # another request. Invalid suggestions cannot discard a good plan.
                for review in response.fact_review_requests:
                    try:
                        if review.source_shot_id not in window.source_shot_ids:
                            raise ValueError("只能复核当前窗口所属来源，不能改写只读上下文")
                        for ref in review.evidence:
                            _validate_ref(ref, {review.source_shot_id}, {f.id: f for f in known}, shot_map)
                        key = (review.source_shot_id, review.field_group)
                        if key in fact_reviews or len(fact_reviews) < 2:
                            fact_reviews[key] = review
                    except ValueError as exc:
                        window_warnings.append(f"已拒绝无效事实复核建议：{exc}")
                if isinstance(response, FinalizeResponse):
                    record["local_repairs"] = _normalize_protocol(response, shots, known, protected_source_shot_ids)
                    for repair in record["local_repairs"]:
                        if repair.get("field") == "scene_continuity" and repair.get("warning"):
                            window_warnings.append(f"事件 {repair['event_anchor_shot_id']} 的场次判断证据不可验证，已降为未知；事件动作/归属保持：{repair['warning']}")
                        elif repair.get("field") in ("generation_subjects", "generation_objects", "action_summary") and repair.get("warning"):
                            window_warnings.append(repair["warning"])
                    if [sid for proposal in response.events for sid in proposal.source_shot_ids] == analyzable:
                        last_finalized = response
                    validate_event_proposals(response.events, model_window, shots, known,
                                             protected_source_shot_ids=protected_source_shot_ids, protected_boundaries=protected_boundaries)
                return response, raw
            except Exception as exc:
                record["error"] = str(exc)
                raise
            finally:
                record["elapsed_seconds"] = round(time.monotonic() - call_start, 4)
                raw_responses.append(record)

        try:
            check_budget()
            if window_index >= config.max_windows:
                raise WindowStopped("configured maximum agent windows reached")
            if window.end_time - window.start_time > config.window_seconds + 1e-7:
                raise WindowStopped("whole source exceeds bounded analysis window")
            if not analyzable:
                raise WindowStopped("all whole sources lack usable saved observations or image evidence; no model request")
            if not set(model_window.source_shot_ids).issubset({frame.shot_id for frame in known}):
                raise WindowStopped("one or more whole sources have no available image evidence")
            emit("event_agent.progress", {"run_id": run_id, "window_id": window.id,
                                          "window_index": window_index + 1, "window_count": len(windows),
                                          "progress": int(window_index * 100 / max(1, len(windows))),
                                          "window_completed": window_index, "window_total": len(windows)})
            response = None
            try:
                response, raw = model_call("initial")
            except WindowStopped:
                raise
            except Exception as exc:
                errors.append(str(exc))
                check_budget()
                if not isinstance(exc, ValueError):
                    raise WindowStopped(f"provider request failed; no transport retry: {exc}")
                # Exactly one repair, using the already supplied evidence.
                if usage.model_calls < config.max_model_calls_per_window:
                    response, raw = model_call("repair", raw_responses[-1].get("response", ""), errors)
                else:
                    raise WindowStopped("model call budget exhausted after invalid response")
            if isinstance(response, RequestEvidenceResponse):
                if usage.model_calls >= config.max_model_calls_per_window:
                    raise WindowStopped("final model response requested more evidence")
                registry = tool_factory(window, list(known), config.max_extra_frames_per_window)
                if hasattr(registry, "deadline"):
                    registry.deadline = deadline
                new_evidence = False
                for request in response.tool_requests:
                    check_budget()
                    if usage.tool_calls >= config.max_tool_calls_per_window:
                        window_warnings.append("工具调用预算已耗尽。")
                        break
                    usage.tool_calls += 1
                    before_count = int(getattr(registry, "extra_frames", usage.extra_frames))
                    call_start = time.monotonic()
                    try:
                        result = registry.call(request)
                        result = ToolResult.model_validate(result)
                    except Exception as exc:
                        result = ToolResult(name=request.name, status="error", error=str(exc),
                                            elapsed_seconds=round(time.monotonic() - call_start, 4))
                    reported_count = int(getattr(registry, "extra_frames", before_count))
                    successful = result.status in ("success", "ok")
                    new_ids = {frame.id for frame in result.frames if frame.id not in all_frame_ids} if successful else set()
                    usage.extra_frames += max(max(0, reported_count - before_count), len(new_ids))
                    tool_results.append(result)
                    check_budget()
                    if usage.extra_frames > config.max_extra_frames_per_window:
                        raise WindowStopped("tool exceeded extra-frame budget")
                    for frame in result.frames:
                        if frame.id in all_frame_ids:
                            continue
                        if not successful:
                            continue
                        if frame.shot_id not in set(window.source_shot_ids + window.context_shot_ids):
                            raise ValueError("Tool returned a frame outside window sources")
                        if usage.extra_frames > config.max_extra_frames_per_window:
                            raise WindowStopped("tool exceeded extra-frame budget")
                        all_frame_ids.add(frame.id)
                        extra_frames.append(frame)
                        known.append(frame)
                        scope_frames.append(frame)
                        new_evidence = True
                    if successful and request.name == "compare_frames" and any(
                        isinstance(value, (int, float)) and not isinstance(value, bool)
                        for key, value in result.output.items()
                        if key in ("brightness_change", "mean_absolute_difference", "feature_flow_x", "feature_flow_y", "tracked_features")
                    ):
                        new_evidence = True
                    # Measured comparison metrics are new evidence even on already
                    # supplied images. Repeated saved neighbor metadata is not.
                    if result.error:
                        window_warnings.append(f"工具 {result.name} 失败：{result.error}")
                if not new_evidence:
                    raise WindowStopped("tools yielded no new visual evidence or measured comparison; refinement skipped")
                response, raw = model_call("refine", raw)
            if not isinstance(response, FinalizeResponse):
                raise ValueError("The last model call must finalize")
            proposals = response.events
        except Exception as exc:
            fallback_reason = str(exc)
            if fallback_reason == "cancelled" or cancelled():
                run_cancelled = True
                fallback_reason = "cancelled"
            if fallback_reason not in errors:
                errors.append(fallback_reason)
            # A malformed/transport/cancelled/deadline response cannot be used.
            # A parsed final response with exact source ownership, however, can
            # be checked event-by-event after the single repair is exhausted.
            if (not run_cancelled and isinstance(exc, ValueError) and last_finalized is not None
                    and [sid for proposal in last_finalized.events for sid in proposal.source_shot_ids] == analyzable
                    and time.monotonic() < deadline):
                proposals = last_finalized.events
                window_warnings.append(f"{window.id} 最终输出存在局部校验错误，将逐事件验证并仅回退不安全的事件：{fallback_reason}")
            else:
                window_warnings.append(f"{window.id} 已保守逐来源回退：{fallback_reason}")

        if proposals is not None:
            window_events = []
            for proposal in proposals:
                try:
                    proposal_window = model_window.model_copy(update={"source_shot_ids": proposal.source_shot_ids,
                        "context_shot_ids": [sid for sid in model_window.context_shot_ids if sid not in proposal.source_shot_ids]})
                    recovered_summary_warnings = []
                    # Only the final local verification can deactivate unsafe
                    # delivery summaries. The initial error still receives the
                    # one existing model repair; facts/ownership are never waived.
                    for _ in range(len(proposal.stages) + 1):
                        try:
                            validate_event_proposals([proposal], proposal_window, shots, known,
                                protected_source_shot_ids=protected_source_shot_ids, protected_boundaries=protected_boundaries)
                            break
                        except ActionSummaryError as exc:
                            if exc.kind not in ("unsupported", "uncertain", "speech") or exc.stage.action_summary is None:
                                raise
                            warning = f"事件 {proposal.source_shot_ids[0]} 阶段 {exc.stage.id} 的动作摘要已停用并回到完整原事实，摘要需复核：{exc}"
                            repair = {"event_anchor_shot_id": proposal.source_shot_ids[0], "stage_id": exc.stage.id,
                                      "field": "action_summary", "from": exc.stage.action_summary, "to": None,
                                      "reason": str(exc), "recovery": "complete_source_fact_text", "warning": warning}
                            raw_responses[-1].setdefault("local_repairs", []).append(repair)
                            exc.stage.action_summary = None
                            recovered_summary_warnings.append(warning)
                            window_warnings.append(warning)
                    candidate = _make_event(proposal, window, job_id, shots, config)
                    missing_motion = [sid for sid in proposal.source_shot_ids if shot_map[sid].visual_observation_v2 is not None
                        and not shot_map[sid].visual_observation_v2.dynamics.subject_action.observable]
                    if missing_motion:
                        candidate.status = "fallback"
                        warning = "动态待复核：" + "、".join(missing_motion) + " 缺少已验证主要动作，静态内容与事件归属已保留，视频方案为草案。"
                        candidate.warnings.append(warning)
                        window_warnings.append(warning)
                    candidate.warnings.extend(recovered_summary_warnings)
                    candidate.warnings.extend(repair["warning"] for repair in raw_responses[-1].get("local_repairs", [])
                        if repair.get("event_anchor_shot_id") == proposal.source_shot_ids[0]
                        and repair.get("field") in ("generation_subjects", "generation_objects", "action_summary")
                        and repair.get("warning") and repair["warning"] not in candidate.warnings)
                    render_event_clips(candidate, shots, config, style_text=style_text, frames=[*frames, *extra_frames])
                    window_events.append(candidate)
                except ValueError as exc:
                    if str(exc) not in errors:
                        errors.append(str(exc))
                    warning = f"事件 {proposal.source_shot_ids[0]} 无法安全通过证据/完整来源校验，仅该事件逐来源回退：{exc}"
                    window_warnings.append(warning)
                    window_events.extend(_fallback_event(shot_map[sid], window, job_id, scope_frames, config, warning)
                                         for sid in proposal.source_shot_ids)
            window_events.extend(_fallback_event(shot_map[sid], window, job_id, scope_frames, config,
                                  next(warning for warning in window_warnings if warning.startswith(f"来源 {sid} ")))
                                 for sid in window.source_shot_ids if sid in isolated)
            owned_order = {sid: index for index, sid in enumerate(window.source_shot_ids)}
            window_events.sort(key=lambda event: owned_order[event.source_shot_ids[0]])
        if proposals is None:
            window_events = [_fallback_event(shot_map[sid], window, job_id, scope_frames, config,
                                             window_warnings[-1] if window_warnings else "证据不足，保守回退")
                             for sid in window.source_shot_ids]
        continued = False
        if (proposals is not None and proposals[0].continuation_of_anchor_shot_id
                and window_events[0].status == "ready"
                and window_events[0].anchor_shot_id == proposals[0].source_shot_ids[0]):
            try:
                if not events or events[-1].status != "ready":
                    raise ValueError("上一窗口没有可续接的已验证事件")
                window_events[0] = _continue_event(events[-1], window_events[0], proposals[0], window, shots, known,
                                                   [*frames, *extra_frames], protected_source_shot_ids, protected_boundaries)
                events[-1] = window_events[0]
                continued = True
            except ValueError as exc:
                window_warnings.append(f"窗口续接未通过，保留完整独立事件：{exc}")
        if proposals is not None and any(proposal.continuation_of_anchor_shot_id for proposal in proposals[1:]):
            window_warnings.append("仅窗口首事件可续接；其余事件保留独立归属。")
        events.extend(window_events[1:] if continued else window_events)
        usage.elapsed_seconds = round(time.monotonic() - start_window, 4)
        if fact_reviews:
            window_warnings.append("事件 Agent 发现来源事实疑点，已记录待复核建议；未修改事实或自动调用复核模型。")
        audit = WindowAudit(
            window_id=window.id, source_shot_ids=window.source_shot_ids,
            context_shot_ids=window.context_shot_ids,
            status="cancelled" if run_cancelled else ("ready" if proposals is not None and all(event.status == "ready" for event in window_events) else "fallback"),
            raw_responses=raw_responses, tool_results=tool_results, validation_errors=errors,
            warnings=window_warnings, usage=usage, fact_review_requests=list(fact_reviews.values()),
        )
        audits.append(audit)
        warnings.extend(window_warnings)
        # Persist audit/raw output before the final result, even after cancellation.
        emit("event_agent.window", {"run_id": run_id, **audit.model_dump(mode="json"),
                                    "event_ids": [event.id for event in window_events]})
        emit("event_agent.progress", {"run_id": run_id, "progress": int((window_index + 1) * 100 / max(1, len(windows))),
                                      "window_completed": window_index + 1, "window_total": len(windows)})

    clips = [clip for event in events for clip in render_event_clips(event, shots, config, style_text=style_text, frames=[*frames, *extra_frames])]
    coverage = [SourceOwnership(source_shot_id=sid, event_id=event.id)
                for event in events for sid in event.source_shot_ids]
    ordered_ids = [sid for window in windows for sid in window.source_shot_ids]
    if [item.source_shot_id for item in coverage] != ordered_ids:
        raise AssertionError("Internal error: source ownership is not exact")
    usage_summary = {"model_calls": sum(audit.usage.model_calls for audit in audits),
                     "tool_calls": sum(audit.usage.tool_calls for audit in audits),
                     "extra_frames": sum(audit.usage.extra_frames for audit in audits),
                     "windows": len(audits), "elapsed_seconds": round(time.monotonic() - start_run, 4)}
    version_hash = digest({"version": VERSION, "config": config.model_dump(mode="json"),
                           "events": [event.version_hash for event in events],
                           "clips": [clip.version_hash for clip in clips],
                           "fact_review_requests": [r.model_dump(mode="json") for a in audits for r in a.fact_review_requests]})
    return EventAgentResult(
        job_id=job_id, run_id=run_id, status="cancelled" if run_cancelled else ("degraded" if warnings else "ready"),
        config=config, windows=windows, events=events, generation_clips=clips,
        frames=extra_frames, audit=audits,
        raw_responses=[record for audit in audits for record in audit.raw_responses],
        warnings=list(dict.fromkeys(warnings)), usage_summary=usage_summary,
        source_coverage=coverage, version_hash=version_hash,
        fact_review_requests=[r for a in audits for r in a.fact_review_requests],
    )


def run_event_agent(*, shots, frames, tool_factory, config, **kwargs):
    """Use bounded interval views internally and publish original source IDs.

    Source spans are computation/provenance metadata, not DB Shot records or
    additional canvas cards. Only interval coverage may repeat an original ID.
    """
    from app.event_agent.spans import analysis_views
    config = EventAgentConfig.model_validate(config)
    views, view_frames, spans = analysis_views(shots, frames, config)
    if not spans:
        return _run_event_agent(shots=shots, frames=frames, tool_factory=tool_factory, config=config, **kwargs)
    original = {shot.id: shot for shot in shots}
    original_id = lambda sid: spans[sid]["source_shot_id"] if sid in spans else sid
    unique = lambda ids: list(dict.fromkeys(original_id(sid) for sid in ids))
    def merge_decisions(items):
        merged = {}
        rank = {"omit": 0, "integrate": 1, "preserve": 2}
        for item in items:
            current = merged.get(item.source_shot_id)
            if current is None or rank[item.disposition] > rank[current.disposition]:
                merged[item.source_shot_id] = item
        return list(merged.values())
    def frame_view(frame):
        sid = next((key for key, span in spans.items() if span["source_shot_id"] == original_id(frame.shot_id)
                    and span["start_time"] <= frame.timestamp < span["end_time"]), frame.shot_id)
        return frame.model_copy(update={"shot_id": sid})
    def factory(window, known, remaining):
        real_window = window.model_copy(update={"source_shot_ids": unique(window.source_shot_ids),
            "context_shot_ids": [sid for sid in unique(window.context_shot_ids) if sid not in unique(window.source_shot_ids)]})
        registry = tool_factory(real_window, [frame.model_copy(update={"shot_id": original_id(frame.shot_id)}) for frame in known], remaining)
        class Adapter:
            def __getattr__(self, key):
                return getattr(registry, key)
            @property
            def deadline(self):
                return registry.deadline
            @deadline.setter
            def deadline(self, value):
                registry.deadline = value
            def call(self, request):
                result = registry.call(request)
                return result.model_copy(update={"frames": [frame_view(frame) for frame in result.frames]})
        return Adapter()
    protected = kwargs.get("protected_source_shot_ids") or ()
    kwargs["protected_source_shot_ids"] = {sid for sid in spans if original_id(sid) in protected}
    boundaries = kwargs.get("protected_boundaries") or ()
    kwargs["protected_boundaries"] = [(left.id, right.id) for left, right in zip(views, views[1:])
        if (original_id(left.id), original_id(right.id)) in boundaries]
    result = _run_event_agent(shots=views, frames=view_frames, tool_factory=factory, config=config, **kwargs)
    # Namespace stage/fact IDs before adjacent spans of one original source are
    # folded into the same narrative owner. No source fact or interval is lost.
    merged, event_map = [], {}
    for saved_event in result.events:
        # Renderer decisions may share objects with the narrative event. Remap
        # a deep event copy so clip provenance keeps its internal span owners.
        event = saved_event.model_copy(deep=True)
        internal_id = event.id
        prefix = event.anchor_shot_id + ":"
        fact_map = {fact.id: prefix + fact.id for fact in event.causal_facts}
        for fact in event.causal_facts:
            fact.id = fact_map[fact.id]
            fact.source_shot_ids = unique(fact.source_shot_ids)
        for stage in event.stages:
            stage.id = prefix + stage.id
            stage.source_shot_ids = unique(stage.source_shot_ids)
            stage.fact_ids = [fact_map[fid] for fid in stage.fact_ids]
        event.source_shot_ids = unique(event.source_shot_ids)
        event.anchor_shot_id = original_id(event.anchor_shot_id)
        for decision in event.presentation:
            decision.source_shot_id = original_id(decision.source_shot_id)
        event.presentation = merge_decisions(event.presentation)
        if merged and set(merged[-1].source_shot_ids) & set(event.source_shot_ids):
            owner = merged[-1]
            owner.source_shot_ids = list(dict.fromkeys([*owner.source_shot_ids, *event.source_shot_ids]))
            owner.causal_facts.extend(event.causal_facts)
            owner.stages.extend(event.stages)
            owner.presentation = merge_decisions([*owner.presentation, *event.presentation])
            owner.final_state = event.final_state
            if event.status == "fallback":
                owner.status = "fallback"
            owner.warnings = list(dict.fromkeys([*owner.warnings, *event.warnings]))
        else:
            event.id = _event_id(result.job_id, event.anchor_shot_id)
            merged.append(event)
            owner = event
        event_map[internal_id] = (owner.id, fact_map, prefix)
    result.events = merged
    for clip in result.generation_clips:
        raw_ids = list(clip.source_shot_ids)
        event_id, fact_map, prefix = event_map[clip.event_id]
        clip.event_id = event_id
        clip.delivery["source_spans"] = [spans[sid] for sid in raw_ids]
        clip.delivery["span_presentation"] = [{**spans[decision.source_shot_id], "disposition": decision.disposition,
            "reason": decision.reason, "evidence": [ref.model_dump(mode="json") for ref in decision.evidence]}
            for decision in clip.presentation]
        stage_spans = {}
        for stage in clip.timeline:
            stage_spans[prefix + stage.id] = [spans[sid] for sid in stage.source_shot_ids]
            stage.id = prefix + stage.id
            stage.source_shot_ids = unique(stage.source_shot_ids)
            stage.fact_ids = [fact_map[fid] for fid in stage.fact_ids]
        clip.delivery["stage_source_spans"] = stage_spans
        clip.source_shot_ids = unique(raw_ids)
        for decision in clip.presentation:
            decision.source_shot_id = original_id(decision.source_shot_id)
        clip.presentation = merge_decisions(clip.presentation)
    for window in result.windows:
        window.source_spans = [spans[sid] for sid in window.source_shot_ids]
        window.source_shot_ids = unique(window.source_shot_ids)
        window.context_shot_ids = [sid for sid in unique(window.context_shot_ids) if sid not in window.source_shot_ids]
    for artifact in result.frames:
        artifact.shot_id = original_id(artifact.shot_id)
    for review in result.fact_review_requests:
        review.source_shot_id = original_id(review.source_shot_id)
    result.source_coverage = [SourceOwnership(source_shot_id=sid, event_id=event.id) for event in merged for sid in event.source_shot_ids]
    result.usage_summary["analysis_spans"] = len(spans)
    result.usage_summary["physical_source_shots"] = len(shots)
    result.version_hash = digest({"version": VERSION, "spans": spans, "result": result.version_hash})
    return result
