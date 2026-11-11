"""Deterministic event rendering; physical source edits never imply output cuts."""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache

from app.event_agent.models import (
    ClipTimelineStage, EventAgentConfig, EventPrompt, GenerationClip, NarrativeEvent,
)
from app.services.sequence_delivery import video_spec
from app.services.generation_timing import ceil_half_seconds, allocate_phase_durations
from app.services.prompt_delivery import clean_join, display_timing, lint_prompt


VERSION = "event-agent-render.v5"
_UNKNOWN = {"", "未知", "无", "待设定", "待判断", "待确认"}
IMAGE_GUARD = "画面约束：仅呈现上述首帧状态，不提前完成动作；不生成文字、字幕、Logo或水印。"
VIDEO_GUARD = "声音与约束：无对白、旁白；保持人物外形、服装、道具与空间关系连贯；不生成文字、字幕、Logo或水印。"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _known(value):
    return isinstance(value, str) and value.strip() not in _UNKNOWN


def _initial_text(shot):
    # Use states and saved still-frame photography only, never action/steps/end state.
    details = shot.action_details
    lines = [f"主体：{details.primary_subject}" if _known(details.primary_subject) else "",
             f"场景：{shot.visual_facts.scene}" if _known(shot.visual_facts.scene) else "",
             f"初始状态：{details.initial_state}" if _known(details.initial_state) else "",
             f"景别：{shot.shot_size}" if _known(shot.shot_size) else "",
             f"构图：{shot.composition}" if _known(shot.composition) else ""]
    return "\n".join(line for line in lines if line) or "按来源首帧保留已见主体与初始状态。"


def _subject(shot):
    subject = shot.action_details.primary_subject if _known(shot.action_details.primary_subject) else (
        "、".join(shot.visual_facts.people) or "按来源首帧可见主体")
    details = [value for value in shot.visual_facts.people if _known(value) and value != subject]
    if details:
        subject += "；已见人物：" + "、".join(details)
    objects = [value for value in shot.visual_facts.objects if _known(value)]
    if objects:
        subject += "；已见道具：" + "、".join(objects)
    return subject


def _event_subject(source_shots, selected_subjects=None, selected_objects=None):
    subjects, people, objects, targets = [], [], [], []
    for shot in source_shots:
        if _known(shot.action_details.primary_subject):
            subjects.append(shot.action_details.primary_subject)
        people.extend(value for value in shot.visual_facts.people if _known(value))
        objects.extend(value for value in shot.visual_facts.objects if _known(value))
        if _known(shot.action_details.interaction_target):
            targets.append(shot.action_details.interaction_target)
    subjects = list(dict.fromkeys(subjects))
    people = list(dict.fromkeys(value for value in people if value not in subjects))
    objects = list(dict.fromkeys(objects))
    targets = list(dict.fromkeys(value for value in targets if value not in subjects + people + objects))
    if selected_subjects is not None or selected_objects is not None:
        # Selection is exact-label validated in the event contract, not a new
        # description of people/props. Keep unselected details in source evidence.
        chosen_subjects = [value for value in selected_subjects if value in subjects + people] if selected_subjects is not None else subjects or people
        chosen_objects = [value for value in selected_objects if value in objects] if selected_objects is not None else objects
        parts = ["、".join(chosen_subjects) or "按来源已见主体"]
        if chosen_objects:
            parts.append("事件道具：" + "、".join(chosen_objects))
        return "；".join(parts)
    parts = ["、".join(subjects or people) or "按来源已见主体"]
    if subjects and people:
        parts.append("已见人物：" + "、".join(people))
    if objects:
        parts.append("已见道具：" + "、".join(objects))
    if targets:
        parts.append("已见交互对象：" + "、".join(targets))
    return "；".join(parts)


def _aspect_ratio(first):
    for prompt in first.prompts:
        if prompt.contract and video_spec(1.0, aspect_ratio=prompt.contract.aspect_ratio)["aspect_source"] == "source_contract":
            return prompt.contract.aspect_ratio
    return "待设定"


def _camera(photography, *, static=False, composition=None):
    fields = [("景别", photography.shot_size), ("焦段", photography.lens)]
    if not static:
        fields.append(("运镜", photography.camera_movement))
    fields.append(("构图", composition if composition is not None else photography.composition))
    return "；".join(f"{name}：{value}" for name, value in fields if _known(value))


def _sentence(value):
    value = value.strip()
    return value if value.endswith(("。", "！", "？", ".", "!", "?")) else value + "。"


def _source_timings(event, shots):
    if event.timing_policy == "source-faithful":
        visible = {decision.source_shot_id for decision in event.presentation if decision.disposition != "omit"}
        total = sum(shots[sid].duration for sid in event.source_shot_ids)
        visible_total = sum(shots[sid].duration for sid in visible)
        if visible_total <= 0:
            # Unknown fallback spans still retain the source's real time budget.
            visible = set(event.source_shot_ids)
            visible_total = total
        return {sid: shots[sid].duration * total / visible_total if sid in visible else 0.0
                for sid in event.source_shot_ids}
    durations = {sid: 0.0 for sid in event.source_shot_ids}
    for stage in event.stages:
        weights = [max(0.05, shots[sid].duration) for sid in stage.source_shot_ids]
        total = sum(weights)
        for sid, weight in zip(stage.source_shot_ids, weights):
            durations[sid] += stage.duration_seconds * weight / total
    if not any(durations.values()):
        # A conservative single-source fallback still has a positive output budget.
        for sid in event.source_shot_ids:
            durations[sid] = max(0.05, shots[sid].generation_duration_seconds or shots[sid].duration or 1.0)
    return durations


def _partition(event, timings, config):
    ids = event.source_shot_ids
    # Whole sources are the only permissible partition units. No four-source cap.
    stage_boundaries = {stage.source_shot_ids[-1] for stage in event.stages}
    presented_fact_ids = {fid for stage in event.stages for fid in stage.fact_ids}
    indivisible_facts = [set(fact.source_shot_ids) for fact in event.causal_facts if fact.id in presented_fact_ids]

    @lru_cache(None)
    def solve(start):
        if start == len(ids):
            return 0.0, ()
        duration, best = 0.0, None
        for end in range(start + 1, len(ids) + 1):
            duration += timings[ids[end - 1]]
            if duration <= 0:
                continue
            if ceil_half_seconds(duration) > config.generation_max_seconds + 1e-7:
                break
            member_set = set(ids[start:end])
            if any(member_set & sources and not sources.issubset(member_set) for sources in indivisible_facts):
                continue
            if end < len(ids) and not any(timings[sid] > 0 for sid in ids[end:]):
                continue
            cost = 3.0 + abs(duration - config.generation_target_seconds) * 0.12
            if end < len(ids) and ids[end - 1] not in stage_boundaries:
                cost += 0.8
            try:
                tail_cost, tail = solve(end)
            except ValueError:
                continue
            candidate = (cost + tail_cost, (end, *tail))
            if best is None or candidate[0] < best[0]:
                best = candidate
        if best is None:
            raise ValueError("Whole-source and complete causal-fact ownership cannot fit the generation duration budget")
        return best

    _, ends = solve(0)
    result, start = [], 0
    for end in ends:
        result.append(ids[start:end])
        start = end
    return result


def _coalesce_timeline(timeline, event):
    """Join model-labelled phase slices without guessing actions or retiming.

    Source allocation and whole-source duration partitioning happen beforehand.
    Kept cuts, explicit detail photography and reveals remain separate stages;
    ordinary evidenced reactions can stay in the same goal's continuous phase.
    Legacy unlabelled plans retain every existing interval.
    """
    facts = {fact.id: fact for fact in event.causal_facts}
    details = {stage.id for stage in event.stages
               if event.generation_mode == "internal_cuts" and stage.photography is not None
               and stage.photography.shot_size in ("特写", "大特写")}

    def independent(stage):
        return (stage.transition == "cut" or stage.id in details
                or any(facts[fid].kind == "reveal" for fid in stage.fact_ids if fid in facts))

    phases = []
    for stage in timeline:
        previous = phases[-1] if phases else None
        phase_name = (stage.semantic_phase or "").strip()
        if (previous is not None and phase_name
                and phase_name == (previous.semantic_phase or "").strip()
                and stage.transition == "continuation"
                and not independent(previous) and not independent(stage)
                and previous.photography == stage.photography
                and abs(previous.generation_end_seconds - stage.generation_start_seconds) <= 0.0001):
            phases[-1] = previous.model_copy(update={
                "source_shot_ids": [*previous.source_shot_ids, *stage.source_shot_ids],
                "fact_ids": [*previous.fact_ids, *stage.fact_ids],
                "generation_end_seconds": stage.generation_end_seconds,
                # Keep all actions and their original ordering, including repeated
                # observed actions; a shared phase label is not a summarizer.
                "action": "；".join(action for action in (previous.action, stage.action) if action),
            })
        else:
            phases.append(stage)
    return phases


def _timeline(event, member_ids, timings):
    allowed = set(member_ids)
    facts = {fact.id: fact for fact in event.causal_facts}
    timeline, current = [], 0.0
    for stage in event.stages:
        sources = [sid for sid in stage.source_shot_ids if sid in allowed]
        if not sources:
            continue
        fact_ids = [fid for fid in stage.fact_ids
                    if fid in facts and set(facts[fid].source_shot_ids) & set(sources)]
        duration = sum(timings[sid] for sid in sources)
        if duration <= 0:
            continue
        end = round(current + duration, 4)
        actions = [facts[fid].text for fid in fact_ids]
        # A new semantic phase can include repeated actions (e.g. drinking again).
        # Preserve those observed repetitions; old unlabelled delivery keeps its
        # existing text projection and is not silently rewritten on read.
        if not stage.semantic_phase:
            actions = list(dict.fromkeys(actions))
        action = "；".join(actions)
        # A stage split across clips cannot use the whole-stage summary, since
        # doing so would narrate actions owned by another clip. Evidence-bearing
        # facts remain complete regardless of the chosen delivery description.
        if (stage.action_summary and sources == stage.source_shot_ids and fact_ids == stage.fact_ids):
            action = stage.action_summary.strip()
        timeline.append(ClipTimelineStage(
            id=stage.id, semantic_phase=stage.semantic_phase,
            source_shot_ids=sources, fact_ids=fact_ids,
            generation_start_seconds=current, generation_end_seconds=end,
            transition="initial" if not timeline else ("cut" if event.generation_mode == "internal_cuts" and stage.transition == "cut" else "continuation"),
            action=action,
            photography=stage.photography if event.generation_mode == "internal_cuts" and stage.photography else event.photography,
        ))
        current = end
    if not timeline:
        timeline.append(ClipTimelineStage(
            id="fallback-stage", source_shot_ids=member_ids, fact_ids=[],
            generation_start_seconds=0.0,
            generation_end_seconds=round(sum(timings[sid] for sid in member_ids), 4),
            transition="initial", action=event.goal or "保留已保存的来源观察；动作证据不足，需复核。",
            photography=event.photography,
        ))
    phases = _coalesce_timeline(timeline, event)
    durations = [stage.generation_end_seconds - stage.generation_start_seconds for stage in phases]
    # Quantize the total once, not each phase independently.
    allocated = allocate_phase_durations(durations)
    cursor = 0.0
    for stage, duration in zip(phases, allocated):
        stage.generation_start_seconds = cursor
        cursor += duration
        stage.generation_end_seconds = cursor
    return phases


def render_event_clips(event: NarrativeEvent, source_shots, config: EventAgentConfig, *, style_text="", frames=()):
    style_text = re.sub(r"^整体画面风格[：:]\s*", "", style_text.strip(), count=1)
    frames = list(frames)
    shots = {shot.id: shot for shot in source_shots}
    timings = _source_timings(event, shots)
    warnings = list(event.warnings)
    # Never silently compress a long source. Analysis spans are partitioned
    # before rendering; unsupported legacy plans fail explicitly.
    for sid, duration in timings.items():
        if duration > 0 and ceil_half_seconds(duration) > config.generation_max_seconds:
            raise ValueError(f"来源 {sid} 的生成预算超过上限，需要有来源区间的语义分片；未压缩原片时间")
    groups = _partition(event, timings, config)
    clips = []
    decisions = {decision.source_shot_id: decision for decision in event.presentation}
    for index, member_ids in enumerate(groups, 1):
        visible_ids = [sid for sid in member_ids if decisions[sid].disposition != "omit"]
        first = shots[(visible_ids or member_ids)[0]]
        last = shots[(visible_ids or member_ids)[-1]]
        timeline = _timeline(event, member_ids, timings)
        duration = timeline[-1].generation_end_seconds
        initial_subject = "主体：" + _subject(first)
        subject = "主体：" + _event_subject((shots[sid] for sid in (visible_ids or member_ids)),
            event.generation_subjects, event.generation_objects)
        scene = "场景：" + (first.visual_facts.scene if _known(first.visual_facts.scene) else "按来源首帧已见场景")
        starting_state = first.action_details.initial_state if _known(first.action_details.initial_state) else "保留来源首帧已见的静态状态"
        first_frames = [frame for frame in frames
                        if (frame.get("shot_id") if isinstance(frame, dict) else frame.shot_id) == first.id]
        spec = video_spec(duration, aspect_ratio=_aspect_ratio(first), frames=first_frames, resolution="1080p")
        timing = display_timing(duration, [(stage.generation_start_seconds, stage.generation_end_seconds) for stage in timeline])
        style = "整体画面风格：" + style_text if _known(style_text) else ""
        first_camera = _camera(timeline[0].photography, static=True,
            composition=first.composition if _known(first.composition) else None)
        initial = clean_join([
            initial_subject, scene, f"图像规格：{spec['resolution']}；{spec['orientation']} {spec['aspect_ratio']}。",
            "首帧状态：" + starting_state, "摄影：" + first_camera if first_camera else "",
            style, IMAGE_GUARD,
        ])
        lines = [subject, scene,
                 f"视频规格：时长 {timing['display_duration_seconds']} 秒；分辨率 {spec['resolution']}；{spec['orientation']} {spec['aspect_ratio']}。",
                 "起点：" + starting_state]
        if len(timeline) > 1:
            lines.append("动作时间线：")
        for stage, displayed in zip(timeline, timing["intervals"]):
            transition = "切换画面，" if stage.transition == "cut" else "连续承接，" if stage.transition == "continuation" else ""
            action = stage.action.strip() or "保留该阶段已见主体与状态；动作证据不足，需复核。"
            prefix = f"{displayed['display_start_seconds']}–{displayed['display_end_seconds']} 秒：" if len(timeline) > 1 else "动作："
            line = prefix + transition + action
            if event.generation_mode == "internal_cuts":
                camera = _camera(stage.photography)
                if camera:
                    line += "；摄影：" + camera
            lines.append(line)
        if event.generation_mode == "continuous":
            camera = _camera(event.photography)
            if camera:
                lines.append("摄影：" + camera)
        lines.extend([style, "结束状态：" + _sentence(last.action_details.final_state)
                      if _known(last.action_details.final_state) else "", VIDEO_GUARD])
        video = clean_join(lines)
        delivery = {"render_version": VERSION, "display_timing": timing, "video_spec": spec,
                    "style_text": style_text if _known(style_text) else "未知",
                    "audio_policy": "visual-only-no-dialogue", "source_audio_analyzed": False,
                    "photography_origin": "generation-suggestion", "timing_origin": "generation-plan-not-source-measurement",
                    "timing_policy": event.timing_policy}
        contract_fields = {"first_frame_state": first.action_details.initial_state,
                           "end_state": last.action_details.final_state, "observed_style": delivery["style_text"],
                           "generation_duration_seconds": duration, "aspect_ratio": spec["aspect_ratio"], "delivery": delivery}
        first_issues = lint_prompt(initial, prompt_type="first_frame", contract=contract_fields,
            profile={"first_frame_only": True, "required_contract_fields": ["first_frame_state", "observed_style"]})
        video_issues = lint_prompt(video, prompt_type="video", contract=contract_fields,
            profile={"required_contract_fields": ["first_frame_state", "end_state", "observed_style"]})
        delivery["prompt_lint"] = {kind: {"passed": not issues, "issues": issues}
                                   for kind, issues in (("first_frame", first_issues), ("video", video_issues))}
        clip_warnings = list(dict.fromkeys([*warnings, *("Prompt校验：" + issue for issue in first_issues + video_issues)]))
        payload = {"version": VERSION, "event": event.version_hash, "sources": member_ids,
                   "timeline": [stage.model_dump(mode="json") for stage in timeline],
                   "style_text": style_text.strip(), "video_spec": spec, "initial": initial, "video": video,
                   "delivery": delivery}
        version_hash = digest(payload)
        clip_id = f"{event.id}-clip-{index:02d}"
        clips.append(GenerationClip(
            id=clip_id, event_id=event.id, title=event.title,
            source_shot_ids=member_ids, presentation=[decisions[sid] for sid in member_ids],
            source_start_time=shots[member_ids[0]].start_time,
            source_end_time=shots[member_ids[-1]].end_time,
            source_duration_seconds=round(sum(shots[sid].duration for sid in member_ids), 4),
            generation_duration_seconds=duration, generation_mode=event.generation_mode,
            timeline=timeline,
            first_frame_prompt=EventPrompt(id=clip_id + "-first-frame", prompt_type="first_frame",
                                          platform="generic-image", positive_prompt=initial,
                                          status="warning" if event.status == "fallback" or first_issues else "ready"),
            seedance_prompt=EventPrompt(id=clip_id + "-seedance", prompt_type="video",
                                       platform="seedance", positive_prompt=video,
                                       status="warning" if event.status == "fallback" or video_issues else "ready"),
            status="warning" if event.status == "fallback" or first_issues or video_issues else "ready", version_hash=version_hash,
            warnings=clip_warnings, delivery=delivery,
        ))
    return clips
