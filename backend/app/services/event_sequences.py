"""Evidence-gated event coverage and local, traceable generation sequences.

This layer never alters a source Shot, generation-plan.v1, or its prompt/cache.
All derivation and rendering is deterministic and makes no provider requests.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from typing import Any, Iterable, Optional

from app.db import utc_now
from app.services.sequence_delivery import VERSION as RENDER_VERSION, style_line, video_spec, video_spec_line
from app.schemas import (
    FrameArtifact, GenerationSequence, GenerationSequenceContract, MontageRelation,
    NarrativeEvent, PromptBundle, SequencePrompt, SequenceStrategy,
    SequenceTimelineSegment, Shot,
)


VERSION = "event-sequences.v4"
_UNKNOWN = {"", "未知", "无", "无明确", "待判断", "待确认", "待设定"}


def _dict(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def _norm(value: str) -> str:
    return re.sub(r"[\s，。；、,.!！?？:：]", "", value or "").casefold()


def _known(value: str) -> bool:
    return _norm(value) not in _UNKNOWN and not (value or "").startswith(("未知", "待人工"))


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _unique(values: Iterable) -> list:
    return list(dict.fromkeys(values))


def _supports(value: str, observations: Iterable[str]) -> bool:
    """Only confirm a claimed context against saved visible fields, not actions."""
    needle = _norm(value)
    return _known(value) and any(_known(fact) and needle in _norm(fact) for fact in observations)


def _strategy(value=None) -> SequenceStrategy:
    config = value if isinstance(value, SequenceStrategy) else SequenceStrategy.model_validate(value or {})
    if not config.target_min_seconds <= config.target_max_seconds <= config.hard_max_seconds:
        raise ValueError("序列时长必须满足 target_min <= target_max <= hard_max <= 12")
    return config


def _context(left: Shot, right: Shot, relation: MontageRelation) -> Optional[dict]:
    """Require state/context evidence, including for older optional-field payloads."""
    if left.status not in ("ready", "warning") or right.status not in ("ready", "warning"):
        return None
    scene_ids = [s.scene_group_id for s in (left, right)]
    if all(scene_ids) and scene_ids[0] != scene_ids[1] and not any(
        (sid or "").startswith(("legacy-", "presentation-")) or s.scene_reason == "legacy-presentation"
        for sid, s in zip(scene_ids, (left, right))
    ):
        return None
    if relation.relation_type != "连续动作" or relation.scene_change or relation.merge_eligible is False:
        return None
    if relation.boundary_role in ("independent-reaction", "reveal"):
        return None
    if not relation.evidence or not _known(left.action_details.final_state) or not _known(right.action_details.initial_state):
        return None
    final, initial = left.action_details.final_state, right.action_details.initial_state
    linkage = relation.state_linkage
    if linkage and linkage.compatible is False:
        return None
    if linkage and linkage.compatible is True:
        # A claimed state link must refer to the actual saved endpoints.
        if not linkage.evidence or _norm(linkage.left_final_state) != _norm(final) or _norm(linkage.right_initial_state) != _norm(initial):
            return None
    elif _norm(final) != _norm(initial):
        return None
    goal = relation.event_goal
    if not _known(goal):
        # Compatibility is exact evidence/state/action identity, never keyword matching.
        if relation.merge_eligible is not None or _norm(left.action) != _norm(right.action) or not _known(left.action):
            return None
        goal = left.action
    if relation.merge_eligible is True and relation.boundary_role not in ("continuation", "action-detail"):
        return None
    prop = relation.shared_prop
    no_prop = prop in ("", "无") and left.action_details.interaction_target == right.action_details.interaction_target == "无"
    if no_prop:
        prop = "无"
    if not _known(prop):
        if not no_prop and _norm(left.action_details.interaction_target) != _norm(right.action_details.interaction_target):
            return None
        prop = left.action_details.interaction_target
    place = relation.shared_location
    if not _known(place):
        if _norm(left.visual_facts.scene) != _norm(right.visual_facts.scene):
            return None
        place = left.visual_facts.scene
    if (not _known(prop) and not no_prop) or not _known(place):
        return None
    quoted = {'subject': set(), 'prop': set(), 'location': set()}
    allowed_fields = {'subject': {'primary_subject', 'people', 'initial_state', 'final_state', 'interaction_target'},
                      'prop': {'objects', 'interaction_target', 'initial_state', 'final_state'},
                      'location': {'scene'}}
    for ref in relation.context_evidence:
        if ref.source_field not in allowed_fields[ref.kind]:
            return None
        shot = next((s for s in (left, right) if s.id == ref.shot_id), None)
        if shot is None:
            return None
        value = getattr(shot.visual_facts, ref.source_field, None) if ref.source_field in ('people', 'objects', 'scene') else getattr(shot.action_details, ref.source_field, None)
        texts = value if isinstance(value, list) else [value]
        if not any(isinstance(text, str) and ref.quote in text for text in texts):
            return None
        quoted[ref.kind].add(_norm(ref.value))
    for shot in (left, right):
        if not no_prop and not _supports(prop, [shot.action_details.interaction_target, *shot.visual_facts.objects,
                               shot.action_details.initial_state, shot.action_details.final_state]) and _norm(prop) not in quoted['prop']:
            return None
        if not _supports(place, [shot.visual_facts.scene]) and _norm(place) not in quoted['location']:
            return None
    subjects = [s for s in relation.shared_subjects if _known(s)]
    if not subjects:
        # Different subjects need explicit shared participants (e.g. cup handoff).
        if _norm(left.action_details.primary_subject) != _norm(right.action_details.primary_subject):
            return None
        subjects = [left.action_details.primary_subject]
    if not any(_known(s) for s in subjects):
        return None
    visible_subjects = [s.action_details.primary_subject for s in (left, right)] + [p for s in (left, right) for p in s.visual_facts.people]
    if any(not _supports(subject, visible_subjects) and _norm(subject) not in quoted['subject'] for subject in subjects):
        return None
    return {"goal": goal, "prop": prop, "place": place, "subjects": subjects,
            "location_continuity": bool(relation.merge_eligible is True and linkage and linkage.compatible is True)}


def _chain_context_matches(anchor, candidate):
    """Same-place relations are transitive through the actual shared source shot.

    '室内居家环境' and '家庭室内（客厅与开放式厨房）' need not be identical
    text if BOTH adjacent links independently passed saved-state/context checks.
    This is evidence linkage, not a location-keyword fuzzy match. Older payloads
    without an explicit merge/state verdict retain exact-location behavior.
    """
    if any(_norm(candidate[k]) != _norm(anchor[k]) for k in ('goal', 'prop')):
        return False
    if (_norm(candidate['place']) != _norm(anchor['place'])
            and not (anchor.get('location_continuity') and candidate.get('location_continuity'))):
        return False
    return set(map(_norm, candidate['subjects'])).issubset(set(map(_norm, anchor['subjects'])))


def _event_groups(shots: list[Shot], relations: list[MontageRelation], config: SequenceStrategy):
    edges = {(r.left_shot_id, r.right_shot_id): r for r in relations}
    # A reaction/reveal is a protected SOURCE shot, not merely an incoming edge.
    # Otherwise it can be swallowed by the next apparently continuous pair.
    protected = {r.right_shot_id for r in relations if r.boundary_role in ('independent-reaction', 'reveal')}
    groups, group, anchor, evidence = [], [], None, []
    for shot in shots:
        relation = edges.get((group[-1].id, shot.id)) if group else None
        candidate = _context(group[-1], shot, relation) if relation and relation.confidence >= config.minimum_confidence else None
        if candidate and (shot.id in protected or group[-1].id in protected):
            candidate = None
        # Number/time adjacency is mandatory; missing/failed shots cannot be crossed.
        if candidate and (shot.number != group[-1].number + 1 or abs(shot.start_time - group[-1].end_time) > 0.1):
            candidate = None
        if candidate and anchor:
            if not _chain_context_matches(anchor, candidate):
                candidate = None
        if group and not candidate:
            groups.append((group, anchor, evidence))
            group, anchor, evidence = [], None, []
        if candidate:
            if anchor is None:
                anchor = candidate
            evidence.extend(relation.evidence)
            if relation.state_linkage:
                evidence.extend(relation.state_linkage.evidence)
        group.append(shot)
    if group:
        groups.append((group, anchor, evidence))
    return groups, edges


def _duration(shot: Shot) -> float:
    # Source duration remains untouched; these are proposed generator timings.
    return max(0.05, shot.generation_duration_seconds or shot.duration or 1.0)


def _steps(shot: Shot) -> int:
    # Count evidenced semantic phases, not repeated micro-movements or still poses.
    # An unphased source summary is one stage; raw steps remain in the contract.
    from app.services.action_presentation import build_action_presentation
    from types import SimpleNamespace
    observation = SimpleNamespace(subject_action=shot.action, action_details=shot.action_details)
    phases = build_action_presentation(observation, _duration(shot))['phases']
    return max(1, len(phases))


def _partition(shots: list[Shot], edges, config: SequenceStrategy) -> list[list[Shot]]:
    """Choose real shot boundaries; prefer 6–10s, never stretch/fabricate phases."""
    @lru_cache(None)
    def solve(start):
        if start == len(shots):
            return 0.0, ()
        best = None
        duration, steps = 0.0, 0
        for end in range(start + 1, min(len(shots), start + config.max_source_shots) + 1):
            current = shots[end - 1]
            duration += _duration(current)
            steps += _steps(current)
            from app.services.generation_timing import ceil_half_seconds
            if ceil_half_seconds(duration) > config.hard_max_seconds + 1e-7 or (steps > config.max_event_steps and end > start + 1):
                break
            # A sub-target complete action is legitimate; fewer outputs wins ties.
            cost = 3.0 + max(0, config.target_min_seconds - duration) * 0.5 + max(0, duration - config.target_max_seconds)
            if end < len(shots):
                boundary = edges.get((current.id, shots[end].id))
                # Split at the least tightly connected explicit stage boundary.
                cost += 1.0 if boundary and boundary.boundary_role == "action-detail" else 0.0
            tail_cost, tail = solve(end)
            proposal = (round(cost + tail_cost, 6), (end, *tail))
            if best is None or proposal[0] < best[0]:
                best = proposal
        if best is None:
            raise ValueError(f"来源镜头 {shots[start].id} 超过生成序列上限，不能修改来源或静默丢弃")
        return best
    _, boundaries = solve(0)
    result, start = [], 0
    for end in boundaries:
        result.append(shots[start:end])
        start = end
    return result


def _source_hash(shots, relations, config):
    # Exclude derived prompts/styles so style application cannot regroup source coverage.
    observations = [_dict(s) for s in shots]
    for value in observations:
        value.pop("prompts", None)
        value.pop("visual_observation_v2", None)
    return _digest({"version": VERSION, "shots": observations, "montage": [_dict(r) for r in relations], "strategy": config.model_dump()})


def _source_prompts(shots, prompts):
    result = {}
    member_ids = {s.id for s in shots}
    for prompt in [*(p for s in shots for p in s.prompts), *prompts]:
        p = prompt if isinstance(prompt, PromptBundle) else PromptBundle.model_validate(prompt)
        if p.contract and p.shot_id in member_ids:
            result[p.shot_id] = p
    return result


def _style_layers(prompt: Optional[PromptBundle]) -> dict:
    """Retain owned delivery text and scope; never infer or expand a style."""
    if not prompt or not prompt.contract:
        return {"global": "", "scene": "", "shot": "", "local_effect": "", "avoid": "", "recipe_constraint": ""}
    contract = prompt.contract
    structured = contract.delivery.get("style_layers")
    if isinstance(structured, dict):
        return {key: value if isinstance(value, str) else "" for key, value in (
            ("global", structured.get("global", contract.observed_style)),
            ("scene", structured.get("scene", "")), ("shot", structured.get("shot", "")),
            ("local_effect", structured.get("local_effect", "")),
            ("avoid", structured.get("avoid", "")), ("recipe_constraint", structured.get("recipe_constraint", "")),
        )}
    # Compatibility only with complete lines from our own previous renderers.
    lines = _unique(line.strip() for key in ("first_frame_prompt", "seedance_video_prompt")
        for line in str(contract.delivery.get(key) or "").splitlines() if line.strip())
    return {"global": contract.observed_style,
        "scene": "\n".join(line for line in lines if line.startswith("本场景视觉变化：")),
        "local_effect": "\n".join(line for line in lines if line.startswith(("本镜局部效果：", "局部视觉效果："))),
        "shot": "", "avoid": "", "recipe_constraint": ""}


def _render(sequence: GenerationSequence, shots: list[Shot], prompts, frames=()) -> GenerationSequence:
    source = {s.id: s for s in shots}
    member_shots = [source[sid] for sid in sequence.source_shot_ids]
    first, last = member_shots[0], member_shots[-1]
    bundle = _source_prompts(member_shots, prompts)
    original = bundle.get(first.id)
    source_contract = original.contract if original else None
    member_style_layers = {shot.id: _style_layers(bundle.get(shot.id)) for shot in member_shots}
    first_layers = member_style_layers[first.id]
    style = first_layers["global"] if source_contract else "；".join(
        value for value in (first.visual_style_evidence.medium, first.visual_style_evidence.rendering,
                           first.visual_style_evidence.texture, first.visual_style_evidence.color_palette) if _known(value)) or "画风待确认"
    known_style_hashes = {p.contract.style_profile_hash for p in bundle.values() if p.contract.style_profile_hash}
    if len(known_style_hashes) > 1:
        raise ValueError("序列成员画风版本不一致，请先统一应用全局画风")
    initial = first.action_details.initial_state if _known(first.action_details.initial_state) else "首帧状态待人工设定"
    final = last.action_details.final_state if _known(last.action_details.final_state) else "结束状态待人工设定"
    source_cuts = _unique([t for shot in member_shots for t in shot.source_edit_times] + [
        segment.source_start_time for segment in sequence.timeline if segment.transition == "cut"])
    anchors = _unique([v for shot in member_shots for v in (
        shot.action_details.primary_subject, shot.action_details.interaction_target, shot.visual_facts.scene) if _known(v)])
    # Describe the whole event's saved participants, not just its first camera angle.
    subjects = _unique(v for shot in member_shots for v in (
        shot.action_details.primary_subject, *shot.visual_facts.people) if _known(v))
    props = _unique(v for shot in member_shots for v in (
        shot.action_details.interaction_target, *shot.visual_facts.objects) if _known(v))
    subject = "；".join(subjects) or "按来源首帧保留可见主体"
    if props:
        subject += "；固定道具：" + "、".join(props)
    spec = video_spec(sequence.generation_duration_seconds,
        aspect_ratio=source_contract.aspect_ratio if source_contract else "待设定",
        frames=[f for f in frames if (f.get('shot_id') if isinstance(f, dict) else f.shot_id) == first.id],
        resolution=source_contract.generation_suggestions.get("resolution", "1080p") if source_contract else "1080p")
    contract = GenerationSequenceContract(
        purpose=sequence.main_action, first_frame_state=initial, end_state=final,
        generation_duration_seconds=sequence.generation_duration_seconds,
        source_shot_ids=sequence.source_shot_ids, source_edit_times=sorted(source_cuts),
        continuity_anchors=anchors,
        aspect_ratio=spec["aspect_ratio"], resolution=spec["resolution"], orientation=spec["orientation"],
        style_profile_version=source_contract.style_profile_version if source_contract else "",
        style_profile_hash=source_contract.style_profile_hash if source_contract else "", observed_style=style,
    )
    guard = "无台词、无人物说话、无旁白；不生成文字、字幕、logo或水印。手部、物体接触关系和可见身份保持稳定。"
    scene_values = [member_style_layers[shot.id]["scene"] for shot in member_shots]
    common_scene = scene_values[0] if len(set(scene_values)) == 1 else ""
    first_frame = "\n".join([
        "【生成序列首帧图片提示词】",
        f"主体：{first.action_details.primary_subject}；场景：{first.visual_facts.scene}。",
        f"0 秒定格：{initial}。",
        f"镜头：{first.shot_size}；视觉焦段：{first.lens}；构图：{first.composition or '按来源首帧'}；光线：{first.lighting_vibe}；画幅：{contract.aspect_ratio}。",
        style_line(style),
        first_layers["scene"], first_layers["shot"], first_layers["avoid"],
        "冻结在第一个来源镜头的动作起点，不提前呈现后续镜头或最终结果。" + guard,
    ])
    motion_lines = []
    for segment in sequence.timeline:
        shot = source[segment.source_shot_id]
        local = bundle.get(shot.id)
        presentation = local.contract.delivery.get("action_presentation", {}) if local else {}
        steps = presentation.get("main_action") or shot.action or "；".join(shot.action_details.steps) or "动作待人工补充"
        label = "起始镜头" if segment.transition == "initial" else "切至" if segment.transition == "cut" else "保持当前机位，连续进入"
        focal = local.contract.generation_suggestions.get("focal_length") if local else None
        camera = f"{shot.shot_size}，{focal or '视觉焦段' + shot.lens}，{shot.camera_movement}"
        # Every complete event is delivered as one ordered schedule, including a
        # singleton. Keep the saved stage budgets; never divide into equal bins.
        prefix = f"{segment.generation_start_seconds:g}–{segment.generation_end_seconds:g} 秒："
        motion_lines.append(f"{prefix}{label}{camera}；{steps}。")
        if segment.source_edit_times:
            cuts = "、".join(f"生成 {t:g} 秒" for t in segment.generation_edit_times)
            motion_lines.append(f"此时间段内在{cuts}保留机位切换；按已见景别与构图衔接，切后画面不足时待确认，不补造单镜运镜。")
        # Scene/local style is scoped to its actual member, without overwriting global style.
        if local and local.contract.style_profile_hash != contract.style_profile_hash:
            motion_lines.append(f"此镜头可见画面风格：{local.contract.observed_style}。")
        layers = member_style_layers[shot.id]
        scoped_lines = [layers["scene"] if layers["scene"] != common_scene else "",
                        layers["shot"], layers["local_effect"], layers["avoid"], layers["recipe_constraint"]]
        if any(scoped_lines):
            motion_lines.append(f"此段画面要求（{shot.id}）：" + "\n" + "\n".join(line for line in scoped_lines if line))
    video = "\n".join([
        "【Seedance 生成序列提示词】",
        f"主体：{subject}。",
        f"场景：{first.visual_facts.scene}。",
        video_spec_line(spec),
        f"以首帧画面为起点：{initial}；保持角色、服装、道具、场景和光线一致；连续性锚点：{'；'.join(anchors)}。",
        "动作时间线（生成编排建议，不是原片动作时间实测）：",
        *motion_lines,
        style_line(style),
        common_scene,
        "声音：未分析原片音轨；仅提供无对白、无旁白的生成约束，具体拟音待设定。",
        f"收束：{final}。",
        guard,
    ])
    contract.delivery = {"schema_version": RENDER_VERSION,
                         "order": ["subject", "scene", "video_specs", "initial_state", "action_timeline", "overall_style", "sound", "ending"],
                         "source_documents": ["Prompts/seedance_video_prompt_skill.md", "Prompts/action_delivery_v1.md", "Prompts/event_sequence_delivery_v2.md"],
                         "first_frame_prompt": first_frame, "seedance_video_prompt": video,
                         "source_timeline": [segment.model_dump() for segment in sequence.timeline],
                         "style_layers": {"global": style, "first_frame_scene": first_layers["scene"],
                             "common_scene": common_scene, "source_shots": member_style_layers},
                         "video_spec": spec, "render_version": RENDER_VERSION}
    status = sequence.status
    sequence.first_frame_prompt = SequencePrompt(id=sequence.id + "-first-frame", prompt_type="first_frame", platform="image-generation",
        version=sequence.version, positive_prompt=first_frame, status=status, contract=contract.model_copy(deep=True))
    sequence.seedance_prompt = SequencePrompt(id=sequence.id + "-seedance", prompt_type="video", platform="Seedance",
        version=sequence.version, positive_prompt=video, status=status, contract=contract.model_copy(deep=True))
    return sequence


def derive_sequences(shots, frames=(), prompts=(), montage=None, *, strategy=None, mode="preview") -> dict:
    """Return JSON-compatible coverage; exact ordered source coverage is enforced."""
    source = [s if isinstance(s, Shot) else Shot.model_validate(s) for s in shots]
    source.sort(key=lambda s: s.number)
    if len({s.id for s in source}) != len(source):
        raise ValueError("来源镜头 ID 重复")
    config = _strategy(strategy)
    raw_relations = montage.get("relations", []) if isinstance(montage, dict) else montage or []
    relations = [r if isinstance(r, MontageRelation) else MontageRelation.model_validate(r) for r in raw_relations]
    groups, edges = _event_groups(source, relations, config)
    build_hash = _source_hash(source, relations, config)
    artifacts = [f if isinstance(f, FrameArtifact) else FrameArtifact.model_validate(f) for f in frames]
    events, sequences = [], []
    for members, anchor, evidence in groups:
        first, last = members[0], members[-1]
        event_id = "event-" + _digest([s.id for s in members])[:16]
        goal = anchor["goal"] if anchor else first.action or "动作待确认"
        status = "failed" if first.status == "failed" else "warning" if any(s.status != "ready" for s in members) else "ready"
        if status != "failed" and any(not _known(s.action) or not _known(s.action_details.initial_state) or not _known(s.action_details.final_state) for s in members):
            status = "warning"
        event = NarrativeEvent(id=event_id, goal=goal, source_shot_ids=[s.id for s in members],
            source_raw_shot_ids=_unique(rid for s in members for rid in s.source_raw_shot_ids),
            primary_subject=first.action_details.primary_subject,
            participants=anchor["subjects"] if anchor else [first.action_details.primary_subject],
            interaction_target=anchor["prop"] if anchor else first.action_details.interaction_target,
            location=anchor["place"] if anchor else first.visual_facts.scene,
            initial_state=first.action_details.initial_state, final_state=last.action_details.final_state,
            evidence=_unique(evidence), status=status)
        events.append(event)
        for partition in _partition(members, edges, config):
            ids = [s.id for s in partition]
            sequence_id = "sequence-" + _digest(ids)[:16]
            timeline, cursor = [], 0.0
            from app.services.generation_timing import allocate_phase_durations
            phase_durations = allocate_phase_durations([_duration(shot) for shot in partition])
            for index, shot in enumerate(partition):
                duration = phase_durations[index]
                transition = "initial"
                if index:
                    previous = partition[index - 1]
                    # The same raw segment split below 5s stays continuous; raw cuts stay cuts.
                    boundary_cut = any(abs(t - shot.start_time) <= 0.05
                        for t in [*previous.source_edit_times, *shot.source_edit_times])
                    transition = "continuation" if set(previous.source_raw_shot_ids) & set(shot.source_raw_shot_ids) and not boundary_cut else "cut"
                end = round(cursor + duration, 4)
                cuts = [t for t in shot.source_edit_times if shot.start_time < t < shot.end_time]
                timeline.append(SequenceTimelineSegment(source_shot_id=shot.id, source_start_time=shot.start_time,
                    source_end_time=shot.end_time, generation_start_seconds=cursor, generation_end_seconds=end,
                    shot_size=shot.shot_size, lens=shot.lens, camera_movement=shot.camera_movement, composition=shot.composition,
                    action=shot.action, action_steps=shot.action_details.steps, initial_state=shot.action_details.initial_state,
                    final_state=shot.action_details.final_state, source_edit_times=cuts,
                    generation_edit_times=[round(cursor + (t - shot.start_time) / max(shot.duration, 0.0001) * duration, 4) for t in cuts],
                    transition=transition, cut_reason=shot.cut_reason))
                cursor = end
            # Placeholder prompts satisfy typed construction, replaced by deterministic rendering.
            placeholder = GenerationSequenceContract(purpose=goal, first_frame_state=partition[0].action_details.initial_state,
                end_state=partition[-1].action_details.final_state, generation_duration_seconds=cursor, source_shot_ids=ids)
            cover = min((f for f in artifacts if f.shot_id == partition[0].id and f.selected),
                        key=lambda f: f.timestamp, default=None)
            warnings = _unique(w for s in partition for w in s.analysis_warnings)
            if status == "failed":
                warnings.append("来源镜头分析失败；单独保留覆盖，不补造动作，不可直接生成")
            if any(not _known(s.action) or not _known(s.action_details.initial_state) or not _known(s.action_details.final_state) for s in partition):
                warnings.append("来源动作或端点状态缺少已保存观察，保留单独覆盖，需人工确认")
            if sum(_steps(s) for s in partition) > config.max_event_steps:
                warnings.append("单个来源镜头动作步骤超过策略上限，保留来源，需人工简化")
            sequence = GenerationSequence(id=sequence_id, event_id=event_id, title=goal, main_action=goal,
                scene_group_id=partition[0].scene_group_id, cover_frame_id=cover.id if cover else next(iter(partition[0].frame_ids), None),
                source_shot_ids=ids, source_raw_shot_ids=_unique(rid for s in partition for rid in s.source_raw_shot_ids),
                source_start_time=partition[0].start_time, source_end_time=partition[-1].end_time,
                source_duration_seconds=round(sum(s.duration for s in partition), 4), generation_duration_seconds=cursor,
                timeline=timeline, first_frame_prompt=SequencePrompt(id="", prompt_type="first_frame", platform="image-generation", positive_prompt="", contract=placeholder),
                seedance_prompt=SequencePrompt(id="", prompt_type="video", platform="Seedance", positive_prompt="", contract=placeholder),
                status=status, build_hash=_digest({"source": build_hash, "ids": ids}), warnings=warnings)
            sequences.append(_render(sequence, source, prompts, artifacts))
    expected = [s.id for s in source]
    covered = [sid for sequence in sequences for sid in sequence.source_shot_ids]
    if covered != expected or len(set(covered)) != len(covered):
        raise ValueError("生成序列没有按顺序恰好覆盖所有来源镜头")
    membership = {sid: seq.id for seq in sequences for sid in seq.source_shot_ids}
    event_membership = {sid: event.id for event in events for sid in event.source_shot_ids}
    protected = {r.right_shot_id for r in relations if r.boundary_role in ('independent-reaction', 'reveal')}
    decisions = []
    for left, right in zip(source, source[1:]):
        relation = edges.get((left.id, right.id))
        merged = membership[left.id] == membership[right.id]
        if merged:
            reason = '连续事件、状态和上下文证据通过，保留内部剪辑合并'
        elif not relation:
            reason = '相邻关系未完成或无有效结果，保守保留边界'
        elif left.id in protected or right.id in protected:
            reason = '独立反应或线索揭示镜头单独保留'
        elif relation.scene_change or relation.relation_type != '连续动作' or relation.merge_eligible is False:
            reason = '模型证据判定不是可合并的连续动作'
        elif relation.confidence < config.minimum_confidence:
            reason = '连续关系置信度未达门槛'
        elif event_membership[left.id] == event_membership[right.id]:
            reason = '同一事件按生成时长、来源镜头数或语义阶段预算分段'
        elif _context(left, right, relation) is None:
            reason = '保存的端点状态、人物、道具或地点引用尚不完整'
        else:
            reason = '相邻关系与当前事件目标或连续上下文不一致'
        decisions.append({'left_shot_id': left.id, 'right_shot_id': right.id,
            'merged': merged, 'reason': reason, 'relation_type': relation.relation_type if relation else '未知'})
    warnings = []
    if isinstance(montage, dict) and montage.get('status') in ('partial', 'failed'):
        warnings.append('部分跨镜关系未完成，已保留有效合并，其余边界需复核；不代表所有镜头必须独立')
    return {"narrative_events": [event.model_dump(mode="json") for event in events],
            "generation_sequences": [sequence.model_dump(mode="json") for sequence in sequences],
            "sequence_metadata": {"version": VERSION, "render_version": RENDER_VERSION, "mode": mode, "build_hash": build_hash,
                "strategy": config.model_dump(), "source_shot_count": len(source), "event_count": len(events),
                "sequence_count": len(sequences), "version_number": 1, "extra_model_calls": 0,
                "action_budget_unit": "semantic-phases-not-source-microsteps",
                "relation_analysis_status": montage.get('status', 'legacy') if isinstance(montage, dict) else 'legacy',
                "missing_pair_count": len(montage.get('rejected_pairs', [])) if isinstance(montage, dict) else 0,
                "merge_decisions": decisions, "warnings": warnings}}


def derive_sequences_safe(shots, frames=(), prompts=(), montage=None, *, strategy=None, mode="preview") -> dict:
    """Optional historical preview must never invalidate an otherwise valid result."""
    try:
        return derive_sequences(shots, frames, prompts, montage, strategy=strategy, mode=mode)
    except (ValueError, TypeError, KeyError) as exc:
        # Keep every source as independent coverage using only valid saved facts.
        # Omit unreliable relations/config, and isolate inconsistent member styles.
        try:
            result = derive_sequences(shots, frames, prompts, [], mode=mode)
        except (ValueError, TypeError, KeyError) as fallback_error:
            # A long raw source cannot be squeezed into an old whole-source
            # generator contract. Preserve source cards and explicitly record
            # pending coverage rather than silently trimming it to twelve seconds.
            source = [s if isinstance(s, Shot) else Shot.model_validate(s) for s in shots]
            return {"narrative_events": [], "generation_sequences": [], "sequence_metadata": {
                "version": VERSION, "render_version": RENDER_VERSION, "mode": mode, "version_number": 1,
                "degraded": True, "source_shot_count": len(source), "event_count": 0, "sequence_count": 0,
                "build_hash": _digest({"version": VERSION, "pending_sources": [_dict(s) for s in source]}),
                "unplanned_source_shot_ids": [s.id for s in source], "status": "pending_review",
                "warnings": ["生成区间尚未规划，原始镜头与静态描述已完整保留；未压缩或截断时长：" + str(fallback_error)[:240]]}}
        warning = f"生成序列派生需复核，已保留单源覆盖：{str(exc)[:240]}"
        requested_strategy = _dict(strategy) if strategy is not None else SequenceStrategy().model_dump()
        raw_montage = _dict(montage) if hasattr(montage, "model_dump") else montage
        if isinstance(raw_montage, list):
            raw_montage = [_dict(r) for r in raw_montage]
        source = [s if isinstance(s, Shot) else Shot.model_validate(s) for s in shots]
        attempted_hash = _digest({"version": VERSION, "source": [_dict(s) for s in source],
                                  "montage": raw_montage, "strategy": requested_strategy})
        result["sequence_metadata"].update(degraded=True, warnings=[warning], build_hash=attempted_hash,
                                            requested_strategy=requested_strategy)
        for sequence in result["generation_sequences"]:
            sequence["build_hash"] = _digest({"source": attempted_hash, "ids": sequence["source_shot_ids"]})
            sequence["warnings"].append(warning)
            if sequence["status"] == "ready":
                sequence["status"] = "warning"
                sequence["first_frame_prompt"]["status"] = sequence["seedance_prompt"]["status"] = "warning"
        return result


def _sequence_prompt_edit_reason(payload: dict) -> str:
    for sequence in payload.get("generation_sequences", []):
        for key, delivery_key in (("first_frame_prompt", "first_frame_prompt"), ("seedance_prompt", "seedance_video_prompt")):
            prompt = sequence.get(key) or {}
            delivery = (prompt.get("contract") or {}).get("delivery") or {}
            delivered = delivery.get(delivery_key)
            if not isinstance(delivered, str):
                return "生成序列 Prompt 缺少原始文本基线，无法确认是否经过手动修改，已保留原文"
            if delivered != prompt.get("positive_prompt"):
                return "检测到手动修改的生成序列 Prompt，未覆盖；请先保存或导出当前编辑"
            negative_key = "first_frame_negative_prompt" if key == "first_frame_prompt" else "seedance_negative_prompt"
            negative_baseline = delivery.get(negative_key, delivery.get("negative_prompt", ""))
            if negative_baseline != (prompt.get("negative_prompt") or ""):
                return "检测到手动修改的生成序列负面 Prompt，未覆盖；请先保存或导出当前编辑"
    return ""


def assert_sequence_prompts_unedited(payload: dict) -> None:
    reason = _sequence_prompt_edit_reason(payload)
    if reason:
        raise ValueError(reason)


def sequence_payload_view(payload: dict, shots, prompts=(), frames=()) -> dict:
    """Refresh only verifiably unedited saved prompt formatting in memory.

    Source coverage, timeline, hashes, IDs and persisted revision numbers belong
    to the saved plan. Reading it must neither replan nor write that history.
    """
    result = copy.deepcopy(payload)
    # Agent stage ownership and generated choreography are authoritative. The
    # historical pair renderer reads source actions and must not flatten them.
    if result.get("sequence_metadata", {}).get("event_grouping_policy") in ("event-agent-v2", "script-director-v3"):
        return result
    sequences = result.get("generation_sequences", [])
    if not sequences:
        return result
    current = all((((sequence.get(key) or {}).get("contract") or {}).get("delivery") or {}).get("render_version") == RENDER_VERSION
                  for sequence in sequences for key in ("first_frame_prompt", "seedance_prompt"))
    if current:
        return result
    reason = _sequence_prompt_edit_reason(result)
    if reason:
        result.setdefault("sequence_metadata", {})["render_refresh"] = {"status": "skipped", "reason": reason}
        return result
    try:
        source = [s if isinstance(s, Shot) else Shot.model_validate(s) for s in shots]
        refreshed = []
        for saved in sequences:
            rendered = _render(GenerationSequence.model_validate(saved), source, prompts, frames=frames).model_dump(mode="json")
            updated = copy.deepcopy(saved)
            for key in ("first_frame_prompt", "seedance_prompt"):
                original = saved[key]
                # Preserve the canonical IDs, versions, status and negative text.
                # Only the renderer-owned positive text and its contract change.
                updated[key] = {**original, "positive_prompt": rendered[key]["positive_prompt"],
                                "contract": rendered[key]["contract"]}
                negative_key = "first_frame_negative_prompt" if key == "first_frame_prompt" else "seedance_negative_prompt"
                updated[key]["contract"]["delivery"][negative_key] = original.get("negative_prompt") or ""
            refreshed.append(updated)
    except (ValueError, TypeError, KeyError) as exc:
        result.setdefault("sequence_metadata", {})["render_refresh"] = {
            "status": "skipped", "reason": f"生成序列格式刷新未完成，已保留原文：{str(exc)[:240]}"}
        return result
    result["generation_sequences"] = refreshed
    result.setdefault("sequence_metadata", {}).update(render_version=RENDER_VERSION,
        render_refresh={"status": "refreshed", "reason": "仅刷新提示词格式；持久化版本、来源归属和时序保持不变"})
    return result


def rerender_sequence_payload(payload: dict, shots, prompts=(), frames=()) -> dict:
    """Pure style rerender; caller can update it in the source-prompt transaction."""
    assert_sequence_prompts_unedited(payload)
    result = copy.deepcopy(payload)
    source = [s if isinstance(s, Shot) else Shot.model_validate(s) for s in shots]
    if result.get("sequence_metadata", {}).get("event_grouping_policy") == "script-director-v3":
        from app.script_director.sequences import rerender_script_sequences
        return rerender_script_sequences(result, source, prompts)
    if result.get("sequence_metadata", {}).get("event_grouping_policy") == "event-agent-v2":
        return _rerender_agent_style(result, source, prompts)
    rendered = []
    version = int(result.get("sequence_metadata", {}).get("version_number", 1)) + 1
    for saved in result.get("generation_sequences", []):
        sequence = GenerationSequence.model_validate(saved)
        sequence.version = version
        updated = _render(sequence, source, prompts, frames=frames).model_dump(mode="json")
        for key in ("first_frame_prompt", "seedance_prompt"):
            negative = (saved.get(key) or {}).get("negative_prompt") or ""
            updated[key]["negative_prompt"] = negative
            negative_key = "first_frame_negative_prompt" if key == "first_frame_prompt" else "seedance_negative_prompt"
            updated[key]["contract"]["delivery"][negative_key] = negative
        rendered.append(updated)
    result["generation_sequences"] = rendered
    result.setdefault("sequence_metadata", {}).update(version_number=version, mode="persisted", render_version=RENDER_VERSION)
    result["sequence_metadata"].pop("render_refresh", None)
    return result


def _rerender_agent_style(payload, shots, prompts):
    """Style changes cannot recreate event boundaries, stages or camera plans."""
    version = int(payload.get("sequence_metadata", {}).get("version_number", 1)) + 1
    by_id = {shot.id: shot for shot in shots}
    for sequence in payload.get("generation_sequences", []):
        members = [by_id[sid] for sid in sequence["source_shot_ids"]]
        bundles = _source_prompts(members, prompts)
        hashes = {item.contract.style_profile_hash for item in bundles.values() if item.contract.style_profile_hash}
        if len(hashes) > 1:
            raise ValueError("事件成员画风版本不一致，请先统一应用全局画风")
        first = bundles.get(members[0].id)
        if first is None or first.contract is None:
            raise ValueError("事件画风缺少来源合同，已保留原 Prompt")
        contract = first.contract
        line = style_line(_style_layers(first)["global"])
        new_style_lines = line.splitlines()
        if any(re.match(r"^(?:声音(?:与约束)?|结束状态|画面约束)[：:]", value) for value in new_style_lines):
            raise ValueError("全局画风不能包含交付段边界，已保留事件 Prompt")
        sequence["version"] = version
        base_warnings = [warning for warning in sequence.get("warnings", []) if not warning.startswith("Prompt校验：")]
        current_lint_warnings = []
        for key, baseline in (("first_frame_prompt", "first_frame_prompt"), ("seedance_prompt", "seedance_video_prompt")):
            item = sequence[key]
            lines = item["positive_prompt"].splitlines()
            indices = [index for index, value in enumerate(lines) if value.startswith("整体画面风格：")]
            compact = item["contract"]["delivery"].get("render_version") in ("event-agent-render.v4", "event-agent-render.v5")
            if compact and (sum(value.startswith("声音与约束：") for value in lines) > 1
                            or any(value.startswith("声音：") for value in lines)):
                raise ValueError("事件 Prompt 画风段不可唯一定位，未改写事件时序")
            if compact:
                from app.services.prompt_delivery import is_known
                new_style_lines = line.splitlines() if is_known(contract.observed_style) else []
            endings = [index for index, value in enumerate(lines) if (
                re.match(r"^(?:结束状态|画面约束|声音与约束)[：:]", value) if compact else value.startswith("声音："))]
            end = endings[0] if endings else None
            if len(indices) > 1 or end is None or (indices and end <= indices[0]) or (not indices and not compact):
                raise ValueError("事件 Prompt 画风段不可唯一定位，未改写事件时序")
            # Both Agent prompts use this explicit renderer-owned block order.
            # Replace the entire block, including editable multiline style text.
            lines[indices[0] if indices else end:end] = new_style_lines
            item["positive_prompt"] = "\n".join(lines)
            item["version"] = version
            item["contract"].update(style_profile_version=contract.style_profile_version,
                style_profile_hash=contract.style_profile_hash, observed_style=contract.observed_style)
            item["contract"]["delivery"][baseline] = item["positive_prompt"]
            if compact:
                from app.services.prompt_delivery import lint_prompt
                item["contract"]["delivery"]["style_text"] = contract.observed_style
                issues = lint_prompt(item["positive_prompt"], prompt_type=item["prompt_type"], contract=item["contract"],
                    profile={"required_contract_fields": ["first_frame_state", "observed_style"]
                             + (["end_state"] if item["prompt_type"] == "video" else [])})
                item["contract"]["delivery"].setdefault("prompt_lint", {})[item["prompt_type"]] = {
                    "passed": not issues, "issues": issues}
                current_lint_warnings.extend("Prompt校验：" + issue for issue in issues)
                if issues:
                    item["status"] = "warning"
                elif not base_warnings and item["contract"]["delivery"].get("agent_event", {}).get("status") != "fallback":
                    item["status"] = "ready"
        if compact:
            sequence["warnings"] = list(dict.fromkeys([*base_warnings, *current_lint_warnings]))
            sequence["status"] = "warning" if sequence["warnings"] or any(sequence[key]["status"] == "warning"
                for key in ("first_frame_prompt", "seedance_prompt")) else "ready"
    payload.setdefault("sequence_metadata", {}).update(version_number=version, mode="persisted")
    return payload


def store_sequence_payload(conn, job_id: str, payload: dict, *, expected_version=None) -> dict:
    """Archive and replace only this derived layer, in a caller-owned transaction."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT event_sequence_json FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("任务不存在")
    previous = json.loads(row["event_sequence_json"] or "{}")
    prior_version = int(previous.get("sequence_metadata", {}).get("version_number", 0))
    if expected_version is not None and prior_version != expected_version:
        raise ValueError("生成序列版本冲突，请刷新后重建")
    assert_sequence_prompts_unedited(previous)
    now = utc_now()
    if previous:
        conn.execute("INSERT INTO event_sequence_revisions(job_id,version,payload_json,created_at) VALUES(?,?,?,?)",
                     (job_id, prior_version, row["event_sequence_json"], now))
    saved = copy.deepcopy(payload)
    version = prior_version + 1
    saved.setdefault("sequence_metadata", {}).update(mode="persisted", version_number=version)
    for sequence in saved.get("generation_sequences", []):
        sequence["version"] = version
        for name in ("first_frame_prompt", "seedance_prompt"):
            sequence[name]["version"] = version
    conn.execute("UPDATE jobs SET event_sequence_json=?,updated_at=? WHERE id=?", (json.dumps(saved, ensure_ascii=False), now, job_id))
    return saved


def sequence_payload_for_job(service, job_id: str, *, persist=False, strategy=None, expected_version=None) -> dict:
    """Preview uses saved facts only; explicit rebuild is the sole history write."""
    def derive_from_saved(row, mode):
        result = service.result(job_id)
        config = json.loads(row["config_json"])
        return derive_sequences_safe(result.shots, result.frames, result.prompts, json.loads(row["montage_json"] or "{}"),
            strategy=strategy if strategy is not None else config.get("sequence_strategy"), mode=mode)

    if not persist:
        row = service.db.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None:
            raise ValueError("任务不存在")
        saved = json.loads(row["event_sequence_json"] or "{}")
        if saved and strategy is None:
            # JobService.result renders the saved layer once from its already
            # loaded source facts. Reuse that view; never recurse through here.
            result = service.result(job_id)
            return {**saved, "narrative_events": [event.model_dump(mode="json") for event in (result.narrative_events or [])],
                    "generation_sequences": [sequence.model_dump(mode="json") for sequence in (result.generation_sequences or [])],
                    "sequence_metadata": result.sequence_metadata or {}}
        return derive_from_saved(row, "preview")
    with service.db.connect() as conn:
        # Lock before reading source prompts. A first rebuild whose old derived
        # value is NULL must not race a global style application with the same NULL.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("任务不存在")
        if row["status"] != "completed":
            raise ValueError("仅已完成任务允许显式重建生成序列")
        saved = json.loads(row["event_sequence_json"] or "{}")
        prior_version = int(saved.get("sequence_metadata", {}).get("version_number", 0))
        if expected_version is not None and expected_version != prior_version:
            raise ValueError("生成序列版本冲突，请刷新后重建")
        if saved.get("sequence_metadata", {}).get("event_grouping_policy") == "event-agent-v2":
            # Local rebuilding is not permission for paid Agent inference or
            # replacement by the old per-pair grouping strategy.
            payload = saved
        else:
            payload = derive_from_saved(row, "persisted")
        payload = store_sequence_payload(conn, job_id, payload, expected_version=prior_version)
    service._emit(job_id, "sequence.ready", {"sequence_count": len(payload["generation_sequences"]), "mode": "persisted",
        "version": payload["sequence_metadata"]["version_number"], "build_hash": payload["sequence_metadata"]["build_hash"]})
    return payload
