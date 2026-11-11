"""Lossless, local projection of committed Agent results into the main workbench.

The Agent owns event grouping, stage timing and generated camera cuts. This
adapter validates ownership and projects those decisions without invoking the
legacy pair classifier, rendering prompts, requesting models or writing data.
"""
from __future__ import annotations

import copy
import math
import re

from app.event_agent.models import EventAgentResult
from app.event_agent.engine import _actual_scene_id
from app.event_agent.rendering import VERSION as RENDER_VERSION
from app.schemas import (
    FrameArtifact, GenerationSequence, GenerationSequenceContract, NarrativeEvent,
    PromptBundle, SequencePrompt, SequenceTimelineSegment, Shot,
)
from app.services.sequence_delivery import video_spec


VERSION = "event-agent-projection.v1"


def _unique(values):
    return list(dict.fromkeys(values))


def _known(value):
    return isinstance(value, str) and value.strip() not in ("", "未知", "无", "待设定", "待判断", "待确认")


def _exact_coverage(actual, expected, label):
    if actual != expected or len(set(actual)) != len(actual):
        raise ValueError(f"{label}必须按时间顺序恰好覆盖全部来源镜头，不得遗漏、重复或改写来源归属")


def _real_scene(shot):
    # Planning and publishing must agree: a provisional visual/presentation
    # group cannot invalidate a fully evidenced event after paid inference.
    return _actual_scene_id(shot)


def _decisions(values, owned, label):
    ids = [decision.source_shot_id for decision in values]
    _exact_coverage(ids, owned, label)
    return {decision.source_shot_id: decision for decision in values}


def _validate(result, sources):
    if result.status == "cancelled":
        raise ValueError("已取消的事件 Agent 结果不能发布到主工作台")
    ids = [shot.id for shot in sources]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("适配来源镜头不能为空或重复")
    shot_map = {shot.id: shot for shot in sources}
    for shot in sources:
        if not all(math.isfinite(value) for value in (shot.start_time, shot.end_time, shot.duration)) or shot.end_time <= shot.start_time or shot.duration <= 0:
            raise ValueError(f"来源镜头时间无效：{shot.id}")
    event_ids = [event.id for event in result.events]
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("Agent 事件 ID 重复")
    _exact_coverage([sid for event in result.events for sid in event.source_shot_ids], ids, "Agent 事件")
    _exact_coverage([item.source_shot_id for item in result.source_coverage], ids, "Agent 来源归属")
    events = {event.id: event for event in result.events}
    owners = {sid: event.id for event in result.events for sid in event.source_shot_ids}
    if any(item.event_id != owners[item.source_shot_id] for item in result.source_coverage):
        raise ValueError("Agent source_coverage 与事件来源归属不一致")
    for event in result.events:
        if event.anchor_shot_id not in event.source_shot_ids:
            raise ValueError("Agent 事件锚点不属于其来源")
        _decisions(event.presentation, event.source_shot_ids, "Agent 事件呈现决策")
        scenes = {_real_scene(shot_map[sid]) for sid in event.source_shot_ids}
        scenes.discard(None)
        if len(scenes) > 1:
            raise ValueError("Agent 事件不能跨越已保存的真实场次边界")
        fact_ids = [fact.id for fact in event.causal_facts]
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("Agent 事件事实 ID 重复")
        for fact in event.causal_facts:
            if not set(fact.source_shot_ids).issubset(set(event.source_shot_ids)):
                raise ValueError("Agent 事实引用其他事件的来源")
    clips = result.generation_clips
    clip_ids = [clip.id for clip in clips]
    if len(set(clip_ids)) != len(clip_ids):
        raise ValueError("Agent 生成片段 ID 重复")
    span_mode = any(clip.delivery.get("source_spans") for clip in clips)
    if span_mode:
        cursor = {sid: shot_map[sid].start_time for sid in ids}
        ordered_spans = [span for clip in clips for span in clip.delivery.get("source_spans", [])]
        previous_source_order = -1
        source_order = {sid: index for index, sid in enumerate(ids)}
        for span in ordered_spans:
            sid, start, end = span.get("source_shot_id"), span.get("start_time"), span.get("end_time")
            if (sid not in cursor or type(start) not in (int, float) or type(end) not in (int, float)
                    or not all(math.isfinite(value) for value in (start, end))
                    or abs(start - cursor[sid]) > 0.0001 or end <= start or end > shot_map[sid].end_time + 0.0001):
                raise ValueError("Agent 来源区间必须完整连续覆盖原镜头，不能重复、越界或遗漏")
            if source_order[sid] < previous_source_order:
                raise ValueError("Agent 来源区间不能改变原镜头时间顺序")
            previous_source_order = source_order[sid]
            cursor[sid] = end
        if any(abs(cursor[sid] - shot_map[sid].end_time) > 0.0001 for sid in ids):
            raise ValueError("Agent 来源区间未完整覆盖原镜头")
    else:
        _exact_coverage([sid for clip in clips for sid in clip.source_shot_ids], ids, "Agent 生成片段")
    prompt_ids = [prompt.id for clip in clips for prompt in (clip.first_frame_prompt, clip.seedance_prompt)]
    if len(set(prompt_ids)) != len(prompt_ids):
        raise ValueError("Agent 交付 Prompt ID 重复")
    for clip in clips:
        if not all(math.isfinite(value) for value in (
            clip.source_start_time, clip.source_end_time, clip.source_duration_seconds, clip.generation_duration_seconds,
        )):
            raise ValueError("Agent 片段时间必须为有限数值")
        if clip.event_id not in events or any(owners[sid] != clip.event_id for sid in clip.source_shot_ids):
            raise ValueError("Agent 生成片段引用了其他事件的来源")
        event = events[clip.event_id]
        if clip.generation_mode != event.generation_mode:
            raise ValueError("Agent 事件与生成片段的摄影模式不一致")
        decisions = _decisions(clip.presentation, clip.source_shot_ids, "Agent 片段呈现决策")
        event_decisions = {decision.source_shot_id: decision for decision in event.presentation}
        if span_mode:
            rank = {"omit": 0, "integrate": 1, "preserve": 2}
            expected_dispositions = {}
            for span in clip.delivery.get("span_presentation", []):
                sid, disposition = span["source_shot_id"], span["disposition"]
                if sid not in expected_dispositions or rank[disposition] > rank[expected_dispositions[sid]]:
                    expected_dispositions[sid] = disposition
            if any(decision.disposition != expected_dispositions.get(sid) or
                   rank[decision.disposition] > rank[event_decisions[sid].disposition] for sid, decision in decisions.items()):
                raise ValueError("Agent 片段改写了来源区间的呈现决策")
        elif any(decision != event_decisions[sid] for sid, decision in decisions.items()):
            raise ValueError("Agent 片段改写了事件的呈现决策")
        expected_staged = [sid for sid in clip.source_shot_ids if decisions[sid].disposition != "omit"]
        if span_mode:
            staged = list(dict.fromkeys(sid for stage in clip.timeline for sid in stage.source_shot_ids))
            _exact_coverage(staged, expected_staged, "Agent 生成阶段区间")
            key = lambda span: (span["source_shot_id"], span["start_time"], span["end_time"])
            span_decisions = clip.delivery.get("span_presentation", [])
            expected_spans = [key(span) for span in span_decisions if span["disposition"] != "omit"]
            assigned_spans = [key(span) for stage in clip.timeline
                              for span in clip.delivery.get("stage_source_spans", {}).get(stage.id, [])]
            if expected_spans != assigned_spans or len(set(assigned_spans)) != len(assigned_spans):
                raise ValueError("Agent 生成阶段区间必须恰好覆盖全部非省略来源区间")
        else:
            _exact_coverage([sid for stage in clip.timeline for sid in stage.source_shot_ids], expected_staged, "Agent 生成阶段")
        if not clip.timeline or len(clip.timeline) > 20 or len(clip.source_shot_ids) > 120:
            raise ValueError("Agent 主工作台片段最多支持120个完整来源和20个生成阶段")
        if len({stage.id for stage in clip.timeline}) != len(clip.timeline):
            raise ValueError("Agent 生成阶段 ID 重复")
        facts = {fact.id: fact for fact in event.causal_facts}
        cursor = 0.0
        for index, stage in enumerate(clip.timeline):
            if not all(math.isfinite(value) for value in (stage.generation_start_seconds, stage.generation_end_seconds)):
                raise ValueError("Agent 生成阶段时间必须为有限数值")
            if abs(stage.generation_start_seconds - cursor) > 0.0001 or stage.generation_end_seconds <= stage.generation_start_seconds:
                raise ValueError("Agent 生成阶段时间必须从0秒连续递进，不能出现空隙或重叠")
            if (stage.transition == "initial" and index != 0) or (index == 0 and stage.transition != "initial"):
                raise ValueError("Agent 生成阶段必须保留唯一的首段起点")
            if clip.generation_mode == "continuous" and stage.transition == "cut":
                raise ValueError("Agent 连续摄影片段不能凭空加入生成切角")
            for fid in stage.fact_ids:
                if fid not in facts or not set(facts[fid].source_shot_ids).issubset(set(stage.source_shot_ids)):
                    raise ValueError("Agent 生成阶段事实不属于其完整来源")
            cursor = stage.generation_end_seconds
        if abs(cursor - clip.generation_duration_seconds) > 0.0001 or not 0 < cursor <= result.config.generation_max_seconds:
            raise ValueError("Agent 生成时间预算必须与阶段一致且不超过12秒")
        first, last = shot_map[clip.source_shot_ids[0]], shot_map[clip.source_shot_ids[-1]]
        spans = clip.delivery.get("source_spans", [])
        expected_start = spans[0]["start_time"] if spans else first.start_time
        expected_end = spans[-1]["end_time"] if spans else last.end_time
        expected_duration = sum(span["end_time"] - span["start_time"] for span in spans) if spans else sum(shot_map[sid].duration for sid in clip.source_shot_ids)
        if spans and list(dict.fromkeys(span["source_shot_id"] for span in spans)) != clip.source_shot_ids:
            raise ValueError("Agent 片段来源区间与来源ID不一致")
        if abs(clip.source_start_time - expected_start) > 0.0001 or abs(clip.source_end_time - expected_end) > 0.0001:
            raise ValueError("Agent 片段不能改写来源区间时间")
        if abs(clip.source_duration_seconds - expected_duration) > 0.0001:
            raise ValueError("Agent 片段来源时长与来源区间不一致")
    return shot_map, events


def _source_contract(first, prompts):
    for prompt in [*first.prompts, *prompts]:
        if prompt.shot_id == first.id and prompt.contract and video_spec(1.0, aspect_ratio=prompt.contract.aspect_ratio)["aspect_source"] == "source_contract":
            return prompt.contract
    return next((prompt.contract for prompt in [*first.prompts, *prompts]
                 if prompt.shot_id == first.id and prompt.contract), None)


def _rendered_style(text):
    """Read the Agent-owned style block without altering the delivered text."""
    marker = "整体画面风格："
    if marker not in text:
        return "未知"
    return re.split(r"\n(?:声音(?:与约束)?|收束|结束状态|画面约束)[：:]", text.split(marker, 1)[1], maxsplit=1)[0].strip()


def _narrative(event, source):
    members = [source[sid] for sid in event.source_shot_ids]
    subjects = _unique(value for shot in members for value in (
        shot.action_details.primary_subject, *shot.visual_facts.people) if _known(value))
    targets = _unique(shot.action_details.interaction_target for shot in members if _known(shot.action_details.interaction_target))
    evidence = _unique(f"{ref.frame_id} @ {ref.timestamp:g}s：{ref.description}"
        for fact in event.causal_facts for ref in fact.evidence)
    return NarrativeEvent(id=event.id, goal=event.goal, source_shot_ids=event.source_shot_ids,
        source_raw_shot_ids=_unique(rid for shot in members for rid in shot.source_raw_shot_ids),
        primary_subject=members[0].action_details.primary_subject, participants=subjects,
        interaction_target="；".join(targets) or "无", location=members[0].visual_facts.scene,
        initial_state=event.initial_state, final_state=event.final_state, evidence=evidence,
        status="warning" if event.status == "fallback" else "ready")


def project_event_agent_result(result, shots, frames=(), prompts=()) -> dict:
    """Return a store_sequence_payload-compatible projection, without any I/O."""
    result = result if isinstance(result, EventAgentResult) else EventAgentResult.model_validate(result)
    sources = [shot if isinstance(shot, Shot) else Shot.model_validate(shot) for shot in shots]
    sources.sort(key=lambda shot: (shot.start_time, shot.number, shot.id))
    source, events = _validate(result, sources)
    artifacts = [frame if isinstance(frame, FrameArtifact) else FrameArtifact.model_validate(frame)
                 for frame in [*frames, *result.frames]]
    bundles = [prompt if isinstance(prompt, PromptBundle) else PromptBundle.model_validate(prompt) for prompt in prompts]
    sequences = []
    for clip in result.generation_clips:
        event = events[clip.event_id]
        members = [source[sid] for sid in clip.source_shot_ids]
        visible_ids = [decision.source_shot_id for decision in clip.presentation if decision.disposition != "omit"]
        first, last = source[(visible_ids or clip.source_shot_ids)[0]], source[(visible_ids or clip.source_shot_ids)[-1]]
        first_frames = [frame for frame in artifacts if frame.shot_id == first.id]
        source_contract = _source_contract(first, bundles)
        spec = video_spec(clip.generation_duration_seconds,
            aspect_ratio=source_contract.aspect_ratio if source_contract else "待设定", frames=first_frames, resolution="1080p")
        if clip.delivery.get("render_version") in ("event-agent-render.v4", "event-agent-render.v5"):
            saved_spec = clip.delivery.get("video_spec", {})
            planned = video_spec(clip.generation_duration_seconds,
                aspect_ratio=saved_spec.get("aspect_ratio", "待设定"), resolution=saved_spec.get("resolution", "1080p"))
            if (not saved_spec or planned["aspect_source"] != "source_contract"
                    or saved_spec.get("duration_seconds") != clip.generation_duration_seconds
                    or planned["orientation"] != saved_spec.get("orientation")):
                raise ValueError("Agent 生成规格与已渲染片段不一致")
            # A later source/bundle update cannot silently alter already rendered
            # generator settings. Their original selection provenance is retained.
            spec = copy.deepcopy(saved_spec)
        facts = {fact.id: fact for fact in event.causal_facts}
        projected_facts = [fact.model_dump(mode="json") for fact in event.causal_facts
                           if set(fact.source_shot_ids) & set(clip.source_shot_ids)]
        timeline = []
        for stage in clip.timeline:
            stage_sources = [source[sid] for sid in stage.source_shot_ids]
            stage_spans = clip.delivery.get("stage_source_spans", {}).get(stage.id, [])
            timeline.append(SequenceTimelineSegment(
                id=stage.id, semantic_phase=stage.semantic_phase,
                source_shot_id=stage.source_shot_ids[0], source_shot_ids=stage.source_shot_ids,
                fact_ids=stage.fact_ids, photography=stage.photography.model_dump(mode="json"),
                source_start_time=stage_spans[0]["start_time"] if stage_spans else stage_sources[0].start_time,
                source_end_time=stage_spans[-1]["end_time"] if stage_spans else stage_sources[-1].end_time,
                generation_start_seconds=stage.generation_start_seconds, generation_end_seconds=stage.generation_end_seconds,
                shot_size=stage.photography.shot_size, lens=stage.photography.lens,
                camera_movement=stage.photography.camera_movement, composition=stage.photography.composition,
                action=stage.action, action_steps=[facts[fid].text for fid in stage.fact_ids],
                initial_state=stage_sources[0].action_details.initial_state, final_state=stage_sources[-1].action_details.final_state,
                source_edit_times=sorted(set(t for shot in stage_sources for t in shot.source_edit_times)),
                generation_edit_times=[stage.generation_start_seconds] if stage.transition == "cut" else [],
                transition=stage.transition,
                cut_reason="事件 Agent 的显式生成摄影切角" if stage.transition == "cut" else "",
            ))
        physical_edits = sorted(set(t for shot in members for t in shot.source_edit_times))
        anchors = _unique(value for sid in visible_ids for value in (
            source[sid].action_details.primary_subject, source[sid].action_details.interaction_target,
            source[sid].visual_facts.scene) if _known(value))
        delivery = {
            "first_frame_prompt": clip.first_frame_prompt.positive_prompt,
            "seedance_video_prompt": clip.seedance_prompt.positive_prompt,
            "first_frame_negative_prompt": clip.first_frame_prompt.negative_prompt,
            "seedance_negative_prompt": clip.seedance_prompt.negative_prompt,
            "render_version": clip.delivery.get("render_version", "event-agent-render.v3"), "projection_version": VERSION,
            "source_timeline": [segment.model_dump(mode="json") for segment in timeline],
            "video_spec": spec, "agent_event": event.model_dump(mode="json"), "agent_clip": clip.model_dump(mode="json"),
            "photography": event.photography.model_dump(mode="json"),
            "causal_facts": projected_facts, "presentation": [decision.model_dump(mode="json") for decision in clip.presentation],
            "generation_mode": clip.generation_mode,
            "source_trace": [{"source_shot_id": shot.id, "source_raw_shot_ids": shot.source_raw_shot_ids,
                "start_time": shot.start_time, "end_time": shot.end_time, "duration": shot.duration,
                "source_edit_times": shot.source_edit_times, "scene_group_id": shot.scene_group_id,
                "cut_reason": shot.cut_reason} for shot in members],
        }
        # Project saved renderer diagnostics verbatim; projection never edits the
        # body or upgrades historical clips to the current renderer implicitly.
        delivery.update({key: copy.deepcopy(value) for key, value in clip.delivery.items()
                         if key in {"display_timing", "prompt_lint", "style_text", "audio_policy",
                                    "source_audio_analyzed", "photography_origin", "timing_origin", "timing_policy", "source_spans", "stage_source_spans", "span_presentation"}})
        contract = GenerationSequenceContract(purpose=event.goal,
            first_frame_state=first.action_details.initial_state, end_state=last.action_details.final_state,
            generation_duration_seconds=clip.generation_duration_seconds, source_shot_ids=clip.source_shot_ids,
            source_edit_times=physical_edits, continuity_anchors=anchors,
            editorial_note="事件 Agent 的生成摄影与阶段编排；来源剪辑单独记录用于追溯",
            aspect_ratio=spec["aspect_ratio"], resolution=spec["resolution"], orientation=spec["orientation"],
            style_profile_version=source_contract.style_profile_version if source_contract else "",
            style_profile_hash=source_contract.style_profile_hash if source_contract else "",
            observed_style=clip.delivery.get("style_text", _rendered_style(clip.first_frame_prompt.positive_prompt)), delivery=delivery)
        cover = min((frame for frame in first_frames if frame.selected and not frame.parent_frame_id
                     and frame.artifact_kind == "keyframe"), key=lambda frame: frame.timestamp, default=None)
        first_prompt = SequencePrompt(**clip.first_frame_prompt.model_dump(mode="json"), contract=contract.model_copy(deep=True))
        video_prompt = SequencePrompt(**clip.seedance_prompt.model_dump(mode="json"), contract=contract.model_copy(deep=True))
        sequence = GenerationSequence(id=clip.id, event_id=clip.event_id, title=clip.title, main_action=event.goal,
            scene_group_id=first.scene_group_id, cover_frame_id=cover.id if cover else next(iter(first.frame_ids), None),
            source_shot_ids=clip.source_shot_ids,
            source_raw_shot_ids=_unique(rid for shot in members for rid in shot.source_raw_shot_ids),
            source_start_time=clip.source_start_time, source_end_time=clip.source_end_time,
            source_duration_seconds=clip.source_duration_seconds, generation_duration_seconds=clip.generation_duration_seconds,
            timeline=timeline, generation_mode=clip.generation_mode, presentation=delivery["presentation"],
            causal_facts=projected_facts, photography=delivery["photography"], version_hash=clip.version_hash,
            first_frame_prompt=first_prompt, seedance_prompt=video_prompt, status=clip.status,
            build_hash=clip.version_hash, warnings=clip.warnings)
        sequences.append(sequence.model_dump(mode="json"))
    return {"narrative_events": [_narrative(event, source).model_dump(mode="json") for event in result.events],
        "generation_sequences": sequences,
        "sequence_metadata": {"version": VERSION, "render_version": next(iter({
            clip.delivery.get("render_version", "event-agent-render.v3") for clip in result.generation_clips
        }), RENDER_VERSION), "mode": "preview",
            "build_hash": result.version_hash, "version_number": 1, "source_shot_count": len(sources),
            "event_count": len(result.events), "sequence_count": len(sequences), "extra_model_calls": 0,
            "event_grouping_policy": "event-agent-v2", "agent_job_id": result.job_id, "agent_run_id": result.run_id,
            "agent_schema_version": result.schema_version, "agent_status": result.status,
            "agent_config": result.config.model_dump(mode="json"), "agent_usage_summary": copy.deepcopy(result.usage_summary),
            "fact_review_requests": [{**request.model_dump(mode="json"), "status": "pending",
                "suggested_operation": {"mode": "review", "field_group": request.field_group,
                                        "reason": request.reason}}
                for request in result.fact_review_requests],
            "warnings": list(result.warnings)}}
