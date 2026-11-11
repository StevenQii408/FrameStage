"""Deterministic event delivery for authored scripts, without video inference.

The director's event IDs own grouping; source shots own their duration, dialogue
and choreography. Style rerendering reads saved snapshots so it cannot replan
those decisions or reinterpret authored material as observed video evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Dict, Iterable, List

from app.schemas import (
    GenerationSequence, GenerationSequenceContract, NarrativeEvent, PromptBundle,
    SequencePrompt, SequenceTimelineSegment, Shot,
)
from app.services.prompt_delivery import display_timing, is_known, lint_prompt


VERSION = "script-sequence.v2"
LEGACY_VERSION = "script-sequence.v1"
POLICY = "script-director-v3"
MAX_SECONDS = 12.0
MAX_SHOTS = 4
_UNKNOWN = {"", "未知", "无", "待确认", "待设定", "画风待确认"}
_GUARD = "不生成文字、字幕、Logo或水印；角色身份、服装、道具与接触关系保持连续。"
_NEGATIVE = "模糊，低分辨率，水印，字幕，文字覆盖，logo，畸形面部，多余手指，手部变形，主体漂移"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _unique(values: Iterable) -> list:
    return list(dict.fromkeys(values))


def _known(value) -> bool:
    return isinstance(value, str) and value.strip() not in _UNKNOWN


def _sources(shots) -> List[Shot]:
    source = [shot if isinstance(shot, Shot) else Shot.model_validate(shot) for shot in shots]
    source.sort(key=lambda shot: shot.number)
    if len({shot.id for shot in source}) != len(source):
        raise ValueError("剧本来源镜头 ID 重复")
    for shot in source:
        if shot.timing_origin != "script-performance":
            raise ValueError("剧本序列仅接受 script-performance 来源，不解释原视频")
        _duration(shot)
        if not all(math.isfinite(value) for value in (shot.start_time, shot.end_time, shot.duration)):
            raise ValueError("剧本建议时间必须为有限数值")
        if shot.duration <= 0 or shot.end_time <= shot.start_time:
            raise ValueError("剧本来源镜头的建议时长和时间范围必须为正")
    return source


def _duration(shot: Shot) -> float:
    duration = shot.generation_duration_seconds
    duration = float(shot.duration if duration is None else duration)
    if not math.isfinite(duration) or not 0 < duration <= MAX_SECONDS:
        raise ValueError("剧本单镜表演时长必须在 0–12 秒内；不得截断原文压缩兜底")
    return duration


def _bundles(shots, prompts) -> Dict[str, PromptBundle]:
    selected = {}
    ranking = {}
    valid = {shot.id for shot in shots}
    for index, item in enumerate([*(prompt for shot in shots for prompt in shot.prompts), *prompts]):
        prompt = item if isinstance(item, PromptBundle) else PromptBundle.model_validate(item)
        if prompt.shot_id not in valid or prompt.contract is None:
            continue
        rank = (prompt.version, int(prompt.prompt_type == "video"), index)
        if prompt.shot_id not in ranking or rank >= ranking[prompt.shot_id]:
            selected[prompt.shot_id], ranking[prompt.shot_id] = prompt, rank
    return selected


def _direction(shot: Shot, bundle=None) -> dict:
    if shot.script_direction is not None:
        return copy.deepcopy(shot.script_direction)
    if bundle and bundle.contract:
        saved = bundle.contract.delivery.get("script_direction")
        if isinstance(saved, dict):
            return copy.deepcopy(saved)
    return {}


def _snapshot(shots, bundles) -> dict:
    records = []
    for shot in shots:
        record = shot.model_dump(mode="json", exclude={"prompts"})
        record["script_direction"] = _direction(shot, bundles.get(shot.id))
        records.append(record)
    return {"shots": records, "prompts": [bundles[shot.id].model_dump(mode="json")
            for shot in shots if shot.id in bundles]}


def _status(shots) -> str:
    if any(shot.status == "failed" for shot in shots):
        return "failed"
    return "warning" if any(shot.status != "ready" or shot.analysis_warnings for shot in shots) else "ready"


def _groups(shots, bundles):
    groups = []
    previous = None
    for shot in shots:
        requested = str(_direction(shot, bundles.get(shot.id)).get("event_id") or "")
        # Missing authored ownership never authorizes merging adjacent shots.
        key = (bool(requested), requested or shot.id)
        if not groups or key != previous:
            groups.append([])
        groups[-1].append(shot)
        previous = key
    return groups


def _partitions(shots):
    partitions, current, seconds = [], [], 0.0
    for shot in shots:
        duration = _duration(shot)
        if current and (len(current) >= MAX_SHOTS or seconds + duration > MAX_SECONDS + 1e-9):
            partitions.append(current)
            current, seconds = [], 0.0
        current.append(shot)
        seconds += duration
    if current:
        partitions.append(current)
    return partitions


def _photography(shot, direction) -> dict:
    return {"shot_size": shot.shot_size, "lens": shot.lens,
            "camera_angle": direction.get("camera_angle", ""),
            "camera_movement": shot.camera_movement, "composition": shot.composition,
            "lighting": shot.lighting_vibe}


def _camera_line(camera) -> str:
    return "；".join(value for value in [camera.get("shot_size"), camera.get("lens"),
        camera.get("camera_angle"), camera.get("camera_movement"),
        "构图：" + camera["composition"] if _known(camera.get("composition")) else "",
        "光线：" + camera["lighting"] if _known(camera.get("lighting")) else ""] if _known(value)) or "摄影方案待确认"


def _style(bundle, direction) -> dict:
    contract = bundle.contract if bundle else None
    layers = contract.delivery.get("style_layers", {}) if contract else {}
    layers = layers if isinstance(layers, dict) else {}
    return {"global": layers.get("global") or (contract.observed_style if contract else "")
                or direction.get("global_style_prompt", ""),
            **{name: layers.get(name, "") for name in ("scene", "shot", "local_effect", "avoid", "recipe_constraint")},
            "version": contract.style_profile_version if contract else "",
            "hash": contract.style_profile_hash if contract else ""}


def _style_line(value) -> str:
    if not _known(value):
        return "整体画面风格：待确认，沿用已保存的剧本视觉设定。"
    return value if value.startswith("整体画面风格") else "整体画面风格：" + value


def _characters(members) -> list:
    profiles, seen = [], set()
    for shot in members:
        for profile in (shot.script_direction or {}).get("character_profiles", []):
            if not isinstance(profile, dict):
                continue
            key = _digest(profile)
            if key not in seen:
                profiles.append(copy.deepcopy(profile))
                seen.add(key)
    return profiles


def _dialogue_lines(direction) -> list:
    lines = []
    for item in direction.get("dialogue", []):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ValueError("剧本对白必须包含原文 text，不得静默跳过")
        speaker = item.get("speaker_name") or item.get("speaker_id") or "说话人未明确"
        parenthetical = "（" + str(item["parenthetical"]) + "）" if item.get("parenthetical") else ""
        # Preserve every character, punctuation mark and newline of the source.
        lines.append(f"{speaker}{parenthetical}：“{item['text']}”")
    return lines


def _sound_lines(direction) -> list:
    values = direction.get("sound_cues", [])
    if isinstance(values, str):
        values = [values]
    return [value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            for value in values]


def _render_v1(sequence, shots, bundles):
    by_id = {shot.id: shot for shot in shots}
    try:
        members = [by_id[shot_id] for shot_id in sequence.source_shot_ids]
    except KeyError as exc:
        raise ValueError("剧本序列缺少保存的来源镜头") from exc
    first, last = members[0], members[-1]
    directions = {shot.id: _direction(shot) for shot in members}
    styles = {shot.id: _style(bundles.get(shot.id), directions[shot.id]) for shot in members}
    first_style = styles[first.id]
    profiles = _characters(members)
    character_lines = [f"{item.get('name') or item.get('id') or '人物'}：{item['description']}"
                       for item in profiles if item.get("description")]
    subject = "、".join(_unique(shot.action_details.primary_subject for shot in members
                               if _known(shot.action_details.primary_subject))) or "主体待确认"
    scene = "；".join(_unique(shot.visual_facts.scene for shot in members
                             if _known(shot.visual_facts.scene))) or "场景待确认"
    initial = sequence.timeline[0].initial_state
    final = sequence.timeline[-1].final_state
    anchors = _unique(anchor for shot in members for anchor in (
        directions[shot.id].get("continuity_notes", []) +
        (bundles[shot.id].contract.continuity_anchors if shot.id in bundles else [])))
    first_camera = sequence.timeline[0].photography or _photography(first, directions[first.id])
    first_frame = "\n".join(filter(None, [
        "【剧本事件首帧图片提示词】", f"主体：{first.action_details.primary_subject}。",
        "人物设定（剧本创作）：" + "；".join(character_lines) if character_lines else "",
        f"场景：{first.visual_facts.scene if _known(first.visual_facts.scene) else '场景待确认'}。",
        f"0 秒定格：{initial}。", "导演摄影建议：" + _camera_line(first_camera) + "。",
        _style_line(first_style["global"]), first_style["scene"], first_style["shot"], first_style["avoid"],
        "生成设置：分辨率 1080p；竖屏 9:16。",
        "冻结在本片段动作起点，不提前展示后续对白、动作结果或结束状态。" + _GUARD,
    ]))
    motion_lines, cameras, dialogue_records, sound_records, assumptions = [], [], [], [], []
    for index, stage in enumerate(sequence.timeline):
        direction = directions[stage.source_shot_id]
        label = "起始镜头" if index == 0 else "切至下一导演镜头"
        prefix = f"{stage.generation_start_seconds:g}–{stage.generation_end_seconds:g} 秒"
        action = "；".join(stage.action_steps) or stage.action
        motion_lines.append(f"{prefix}：{label}；{action}。")
        voices = _dialogue_lines(direction)
        if voices:
            motion_lines.append("本时段原文对白／旁白（独立声音层，不生成字幕）：\n" + "\n".join(voices))
        sounds = _sound_lines(direction)
        if sounds:
            motion_lines.append("本时段声音设定（剧本文本／导演建议）：" + "；".join(sounds))
        camera = stage.photography or _photography(by_id[stage.source_shot_id], direction)
        cameras.append(f"{prefix}：" + _camera_line(camera) + "。")
        local_style = styles[stage.source_shot_id]
        scoped = [local_style["global"] if local_style["global"] != first_style["global"] else "",
                  *(local_style[key] for key in ("scene", "shot", "local_effect", "avoid", "recipe_constraint"))]
        if any(_known(value) for value in scoped):
            motion_lines.append("本时段画面要求：" + "；".join(value for value in scoped if _known(value)))
        dialogue_records.extend({**copy.deepcopy(item), "source_shot_id": stage.source_shot_id,
            "generation_start_seconds": stage.generation_start_seconds,
            "generation_end_seconds": stage.generation_end_seconds} for item in direction.get("dialogue", []))
        sound_records.append({"source_shot_id": stage.source_shot_id, "sound_cues": copy.deepcopy(direction.get("sound_cues", []))})
        assumptions.extend(direction.get("creative_assumptions", []))
    video = "\n".join(filter(None, [
        "【Seedance 剧本事件提示词】", f"主体：{subject}。",
        "人物设定（剧本创作）：" + "；".join(character_lines) if character_lines else "",
        f"场景：{scene}。", f"以首帧画面为起点：{initial}。",
        "动作与对白时间线（剧本表演建议）：", *motion_lines,
        f"结束状态：{final}。", "导演摄影建议（创作方案）：", *cameras,
        _style_line(first_style["global"]),
        f"生成设置：时长 {sequence.generation_duration_seconds:g} 秒；分辨率 1080p；竖屏 9:16。",
        "声音与对白是拟定的生成声音层，本轮未生成音频；原文对白／旁白完整保留，不自行补写。",
        "创作补充（非原文拍摄事实）：" + "；".join(_unique(assumptions)) if assumptions else "",
        "本计划来自剧本文本与导演创作；秒数、运镜与光线是建议，不是原视频测量事实。",
        _GUARD,
    ]))
    contract = GenerationSequenceContract(
        purpose=sequence.main_action, first_frame_state=initial, end_state=final,
        generation_duration_seconds=sequence.generation_duration_seconds,
        source_shot_ids=list(sequence.source_shot_ids), source_edit_times=[], continuity_anchors=anchors,
        editorial_note="剧本导演编排；内部切镜和表演秒数为创作建议，没有原视频切点或已生成音频",
        aspect_ratio="9:16", resolution="1080p", orientation="竖屏",
        style_profile_version=first_style["version"], style_profile_hash=first_style["hash"],
        observed_style=first_style["global"] or "画风待确认",
        delivery={"schema_version": LEGACY_VERSION, "render_version": LEGACY_VERSION,
            "timing_origin": "script-performance", "audio_policy": "preserve-source-dialogue",
            "audio_generated": False,
            "order": ["subject", "scene", "initial_state", "action_dialogue_timeline", "ending", "photography", "style", "video_specs"],
            "first_frame_prompt": first_frame, "seedance_video_prompt": video,
            "first_frame_negative_prompt": _NEGATIVE, "seedance_negative_prompt": "",
            "dialogue": dialogue_records, "sound_cues": sound_records,
            "creative_assumptions": _unique(assumptions), "character_profiles": profiles,
            "source_texts": {shot.id: shot.source_text for shot in members},
            "source_span_ids": _unique(span_id for shot in members
                for span_id in directions[shot.id].get("source_span_ids", [])),
            "source_directions": copy.deepcopy(directions),
            "source_timeline": [stage.model_dump(mode="json") for stage in sequence.timeline],
            "style_layers": {"global": first_style["global"], "source_shots": styles},
            "source_prompt_template_ids": {shot.id: bundles[shot.id].template_id
                for shot in members if shot.id in bundles},
            "source_documents": ["Prompts/script_director_v3.md", "Prompts/short_drama_storyboard_skill.md"],
            "video_spec": {"duration_seconds": sequence.generation_duration_seconds,
                "resolution": "1080p", "aspect_ratio": "9:16", "orientation": "竖屏",
                "aspect_source": "script-generation-setting", "resolution_source": "generation_setting"}})
    sequence.first_frame_prompt = SequencePrompt(id=sequence.id + "-first-frame", prompt_type="first_frame",
        platform="image-generation", version=sequence.version, positive_prompt=first_frame,
        negative_prompt=_NEGATIVE, status=sequence.status, contract=contract.model_copy(deep=True))
    sequence.seedance_prompt = SequencePrompt(id=sequence.id + "-seedance", prompt_type="video",
        platform="Seedance", version=sequence.version, positive_prompt=video,
        status=sequence.status, contract=contract.model_copy(deep=True))
    return sequence


def _explicit_spec(shot, bundle):
    direction = _direction(shot, bundle)
    contract = bundle.contract if bundle else None
    settings = contract.delivery.get("video_spec", {}) if contract else {}
    settings = settings if isinstance(settings, dict) else {}
    values = {}
    for name in ("aspect_ratio", "resolution"):
        candidates = [settings.get(name), direction.get(name)]
        if name == "aspect_ratio" and contract:
            candidates.insert(0, contract.aspect_ratio)
        known = _unique(value for value in candidates if is_known(value))
        if len(known) > 1:
            raise ValueError(f"剧本来源镜头 {shot.id} 的 {name} 显式设置冲突")
        values[name] = known[0] if known else None
    return values


def _sequence_spec(members, bundles):
    specs = [_explicit_spec(shot, bundles.get(shot.id)) for shot in members]
    for name in ("aspect_ratio", "resolution"):
        if len(_unique(spec[name] for spec in specs if spec[name])) > 1:
            raise ValueError(f"剧本合并片段的 {name} 显式设置冲突，不能合并")
    first = specs[0]
    # Use the first source's settings; a later explicit setting may fill only a gap.
    ratio = first["aspect_ratio"] or next((spec["aspect_ratio"] for spec in specs if spec["aspect_ratio"]), "9:16")
    resolution = first["resolution"] or next((spec["resolution"] for spec in specs if spec["resolution"]), "1080p")
    try:
        width, height = (float(value) for value in ratio.split(":"))
        orientation = "横屏" if width > height else "竖屏" if width < height else "方形"
    except (TypeError, ValueError):
        orientation = "待设定"
    return {"aspect_ratio": ratio, "resolution": resolution, "orientation": orientation,
            "aspect_source": "first-source-setting" if first["aspect_ratio"] else "source-setting" if any(spec["aspect_ratio"] for spec in specs) else "script-default",
            "resolution_source": "first-source-setting" if first["resolution"] else "source-setting" if any(spec["resolution"] for spec in specs) else "script-default"}


def _compact_camera(camera, *, still=False):
    keys = ["shot_size", "lens", "camera_angle"]
    if not still:
        keys.append("camera_movement")
    parts = [camera.get(key) for key in keys]
    parts.extend(label + camera[key] for key, label in (("composition", "构图："), ("lighting", "光线："))
                 if is_known(camera.get(key)))
    return "；".join(value for value in parts if is_known(value))


def _sentence(value):
    # Structural punctuation only; source dialogue never passes through this.
    return str(value).rstrip("。；;.!！") + "。"


def _delivery_phases(sequence, directions):
    """Project authored within-shot phases without changing source ownership."""
    phases = []
    for stage_index, stage in enumerate(sequence.timeline):
        authored = directions[stage.source_shot_id].get("action_timeline", [])
        if len(authored) > 1:
            for index, phase in enumerate(authored):
                phases.append({"source_shot_id": stage.source_shot_id, "source_stage_index": stage_index,
                    "start_seconds": stage.generation_start_seconds + phase["start_seconds"],
                    "end_seconds": stage.generation_start_seconds + phase["end_seconds"],
                    "action": phase["description"], "first_in_source": not index,
                    "last_in_source": index == len(authored) - 1})
        else:
            steps = [value for value in stage.action_steps if is_known(value)]
            action = "；".join(steps) or (stage.action if is_known(stage.action) else "")
            phases.append({"source_shot_id": stage.source_shot_id, "source_stage_index": stage_index,
                "start_seconds": stage.generation_start_seconds, "end_seconds": stage.generation_end_seconds,
                "action": action, "first_in_source": True, "last_in_source": True})
    return phases


def _render(sequence, shots, bundles, *, render_version=VERSION):
    if render_version == LEGACY_VERSION:
        return _render_v1(sequence, shots, bundles)
    # Retain the full source, dialogue, audit and style records in the contract.
    _render_v1(sequence, shots, bundles)
    by_id = {shot.id: shot for shot in shots}
    members = [by_id[shot_id] for shot_id in sequence.source_shot_ids]
    first = members[0]
    directions = {shot.id: _direction(shot, bundles.get(shot.id)) for shot in members}
    styles = {shot.id: _style(bundles.get(shot.id), directions[shot.id]) for shot in members}
    first_style = styles[first.id]
    spec = _sequence_spec(members, bundles)
    phases = _delivery_phases(sequence, directions)
    timing = display_timing(sequence.generation_duration_seconds,
        [(phase["start_seconds"], phase["end_seconds"]) for phase in phases])
    contract = sequence.seedance_prompt.contract
    initial, final = contract.first_frame_state, contract.end_state
    first_profiles = _characters([first])
    first_characters = "；".join(f"{item.get('name') or item.get('id') or '人物'}：{item['description']}"
                               for item in first_profiles if item.get("description"))
    all_characters = "；".join(f"{item.get('name') or item.get('id') or '人物'}：{item['description']}"
                             for item in contract.delivery["character_profiles"] if item.get("description"))
    subject = "、".join(_unique(shot.action_details.primary_subject for shot in members
                               if is_known(shot.action_details.primary_subject)))
    scene = "；".join(_unique(shot.visual_facts.scene for shot in members if is_known(shot.visual_facts.scene)))
    initial_line = _sentence("起始画面：" + initial) if is_known(initial) else ""
    first_camera = _compact_camera(sequence.timeline[0].photography or _photography(first, directions[first.id]), still=True)
    first_frame = "\n".join(filter(None, [
        "【剧本事件首帧图片提示词】",
        _sentence("主体：" + first.action_details.primary_subject) if is_known(first.action_details.primary_subject) else "",
        "人物设定：" + first_characters if first_characters else "",
        _sentence("场景：" + first.visual_facts.scene) if is_known(first.visual_facts.scene) else "",
        initial_line, _sentence("摄影：" + first_camera) if first_camera else "",
        _style_line(first_style["global"]) if is_known(first_style["global"]) else "",
        first_style["scene"], first_style["shot"], first_style["avoid"],
        f"图像规格：分辨率 {spec['resolution']}；{spec['orientation']} {spec['aspect_ratio']}。",
        "冻结在动作起点，不提前展示动作结果。" + _GUARD,
    ]))
    timeline = []
    for index, phase in enumerate(phases):
        stage = sequence.timeline[phase["source_stage_index"]]
        direction = directions[stage.source_shot_id]
        camera = _compact_camera(stage.photography or _photography(by_id[stage.source_shot_id], direction))
        action = phase["action"] if is_known(phase["action"]) else ""
        timed = len(phases) > 1 or stage.transition not in {"", "initial", "continuous"}
        interval = timing["intervals"][index]
        prefix = (f"{interval['display_start_seconds']}–{interval['display_end_seconds']} 秒："
                  if timed else "动作：")
        parts = ["切至下一镜头" if phase["source_stage_index"] and phase["first_in_source"] and stage.transition == "cut" else "",
                 camera if phase["first_in_source"] else "", action]
        if any(parts):
            timeline.append(_sentence(prefix + "；".join(value for value in parts if value)))
        if not phase["last_in_source"]:
            continue
        voices, sounds = _dialogue_lines(direction), _sound_lines(direction)
        if voices:
            timeline.append("对白／旁白：\n" + "\n".join(voices))
        if sounds:
            timeline.append("声音：" + "；".join(sounds))
        local_style = styles[stage.source_shot_id]
        scoped = [local_style["global"] if local_style["global"] != first_style["global"] else "",
                  *(local_style[key] for key in ("scene", "shot", "local_effect", "avoid", "recipe_constraint"))]
        if any(is_known(value) for value in scoped):
            timeline.append("本时段画面要求：" + "；".join(value for value in scoped if is_known(value)))
    video = "\n".join(filter(None, ["【Seedance 剧本事件提示词】",
        _sentence("主体：" + subject) if subject else "", "人物设定：" + all_characters if all_characters else "",
        _sentence("场景：" + scene) if scene else "", initial_line,
        "动作与对白时间线：" if len(phases) > 1 else "", *timeline,
        _sentence("结束状态：" + final) if is_known(final) else "",
        _style_line(first_style["global"]) if is_known(first_style["global"]) else "",
        f"视频规格：时长 {timing['display_duration_seconds']} 秒；分辨率 {spec['resolution']}；{spec['orientation']} {spec['aspect_ratio']}。",
        _GUARD]))
    core = [("主体", subject), ("场景", scene), ("起始状态", initial), ("结束状态", final)]
    core.extend((f"动作（{phase['source_shot_id']}）", phase["action"]) for phase in phases)
    for name, value in core:
        if not is_known(value):
            warning = f"剧本事件{name}未知，需复核"
            if warning not in sequence.warnings:
                sequence.warnings.append(warning)
            if sequence.status == "ready":
                sequence.status = "warning"
    contract.aspect_ratio, contract.resolution, contract.orientation = spec["aspect_ratio"], spec["resolution"], spec["orientation"]
    contract.delivery.update(schema_version=VERSION, render_version=VERSION,
        first_frame_prompt=first_frame, seedance_video_prompt=video,
        first_frame_character_profiles=first_profiles,
        provenance={"timing_origin": "script-performance", "photography_origin": "director-creative-plan",
                    "note": "秒数、运镜与光线为导演创作建议，不是原视频测量事实"},
        order=["subject", "scene", "initial_state", "action_camera_dialogue_timeline", "ending", "style", "video_specs"],
        display_timing=timing,
        render_timeline=[{**phase,
            "display_start": interval["display_start_seconds"],
            "display_end": interval["display_end_seconds"]} for phase, interval in zip(phases, timing["intervals"])],
        video_spec={"duration_seconds": sequence.generation_duration_seconds, **spec})
    for prompt, text in ((sequence.first_frame_prompt, first_frame), (sequence.seedance_prompt, video)):
        prompt.positive_prompt, prompt.status = text, sequence.status
        prompt.contract = contract.model_copy(deep=True)
        issues = lint_prompt(text, prompt_type=prompt.prompt_type, contract=prompt.contract)
        prompt.contract.delivery["prompt_lint"] = {prompt.prompt_type: {
            "version": "prompt-delivery.v2", "passed": not issues, "issues": issues}}
        for issue in issues:
            warning = "Prompt校验：" + issue
            if warning not in sequence.warnings:
                sequence.warnings.append(warning)
    if sequence.warnings and sequence.status == "ready":
        sequence.status = "warning"
    sequence.first_frame_prompt.status = sequence.seedance_prompt.status = sequence.status
    return sequence


def _hash_payload(payload):
    metadata = payload["sequence_metadata"]
    snapshot = metadata["source_snapshot"]
    metadata["source_hash"] = _digest(snapshot["shots"])
    metadata["build_hash"] = _digest({"version": metadata.get("render_version", metadata.get("version", VERSION)), "snapshot": snapshot})
    for sequence in payload["generation_sequences"]:
        sequence["build_hash"] = _digest({"source": metadata["build_hash"],
            "ids": sequence["source_shot_ids"], "timeline": sequence["timeline"],
            "first_frame": sequence["first_frame_prompt"]["positive_prompt"],
            "video": sequence["seedance_prompt"]["positive_prompt"]})
        sequence["version_hash"] = sequence["build_hash"]
    return payload


def build_script_sequences(shots, prompts=()) -> dict:
    """Cover every script shot once, grouping only adjacent authored event IDs."""
    source = _sources(shots)
    bundles = _bundles(source, prompts)
    snapshot = _snapshot(source, bundles)
    # Resolve any saved direction from source contracts before rendering.
    source = _sources(snapshot["shots"])
    events, sequences, ownership = [], [], []
    for members in _groups(source, bundles):
        first, last = members[0], members[-1]
        direction = _direction(first)
        ids = [shot.id for shot in members]
        authored_event_id = str(direction.get("event_id") or "")
        event_id = "script-event-" + _digest([authored_event_id, ids])[:16]
        goal = direction.get("event_goal") or direction.get("event_title") or first.action or "剧本事件待确认"
        title = direction.get("event_title") or goal
        event = NarrativeEvent(id=event_id, goal=goal, source_shot_ids=ids,
            source_raw_shot_ids=_unique(raw_id for shot in members for raw_id in shot.source_raw_shot_ids),
            primary_subject=first.action_details.primary_subject,
            participants=_unique(shot.action_details.primary_subject for shot in members
                                 if _known(shot.action_details.primary_subject)),
            interaction_target=first.action_details.interaction_target, location=first.visual_facts.scene,
            initial_state=first.action_details.initial_state, final_state=last.action_details.final_state,
            evidence=_unique(span_id for shot in members for span_id in _direction(shot).get("source_span_ids", [])),
            status=_status(members))
        events.append(event.model_dump(mode="json"))
        ownership.append({"event_id": event_id, "source_event_id": authored_event_id, "source_shot_ids": ids})
        for partition in _partitions(members):
            partition_ids = [shot.id for shot in partition]
            sequence_id = "script-sequence-" + _digest([event_id, partition_ids])[:16]
            from app.services.generation_timing import allocate_phase_durations
            allocation = allocate_phase_durations([_duration(shot) for shot in partition])
            timeline, cursor = [], 0.0
            for index, shot in enumerate(partition):
                end = cursor + allocation[index]
                timeline.append(SequenceTimelineSegment(id=sequence_id + f"-stage-{index + 1:02d}",
                    source_shot_id=shot.id, source_shot_ids=[shot.id],
                    source_start_time=shot.start_time, source_end_time=shot.end_time,
                    generation_start_seconds=cursor, generation_end_seconds=end,
                    shot_size=shot.shot_size, lens=shot.lens, camera_movement=shot.camera_movement,
                    composition=shot.composition, action=shot.action, action_steps=list(shot.action_details.steps),
                    initial_state=shot.action_details.initial_state, final_state=shot.action_details.final_state,
                    photography=_photography(shot, _direction(shot)), transition="initial" if not index else "cut",
                    cut_reason=shot.cut_reason))
                cursor = end
            placeholder = GenerationSequenceContract(purpose=goal,
                first_frame_state=partition[0].action_details.initial_state,
                end_state=partition[-1].action_details.final_state,
                generation_duration_seconds=cursor, source_shot_ids=partition_ids)
            sequence = GenerationSequence(id=sequence_id, event_id=event_id, title=title, main_action=goal,
                scene_group_id=partition[0].scene_group_id, source_shot_ids=partition_ids,
                source_raw_shot_ids=_unique(raw_id for shot in partition for raw_id in shot.source_raw_shot_ids),
                source_start_time=partition[0].start_time, source_end_time=partition[-1].end_time,
                source_duration_seconds=round(sum(shot.duration for shot in partition), 8),
                generation_duration_seconds=cursor, timeline=timeline,
                generation_mode="internal_cuts" if len(partition) > 1 else "continuous",
                presentation=[{"source_shot_id": shot.id,
                    "source_span_ids": _direction(shot).get("source_span_ids", []),
                    "source_event_id": authored_event_id} for shot in partition],
                first_frame_prompt=SequencePrompt(id="", prompt_type="first_frame", platform="image-generation",
                    positive_prompt="", contract=placeholder),
                seedance_prompt=SequencePrompt(id="", prompt_type="video", platform="Seedance",
                    positive_prompt="", contract=placeholder),
                status=_status(partition), build_hash="",
                warnings=_unique(warning for shot in partition for warning in shot.analysis_warnings))
            sequences.append(_render(sequence, source, bundles).model_dump(mode="json"))
    covered = [shot_id for sequence in sequences for shot_id in sequence["source_shot_ids"]]
    if covered != [shot.id for shot in source] or len(covered) != len(set(covered)):
        raise ValueError("剧本序列必须按顺序恰好覆盖每个来源镜头")
    return _hash_payload({"narrative_events": events, "generation_sequences": sequences,
        "sequence_metadata": {"version": VERSION, "render_version": VERSION, "event_grouping_policy": POLICY,
            "mode": "preview", "version_number": 1, "extra_model_calls": 0,
            "timing_origin": "script-performance", "audio_generated": False,
            "source_shot_count": len(source), "event_count": len(events), "sequence_count": len(sequences),
            "strategy": {"hard_max_seconds": MAX_SECONDS, "max_source_shots": MAX_SHOTS},
            "source_snapshot": snapshot, "event_ownership": ownership,
            "warnings": [], "action_budget_unit": "authored-event-shots"}})


def rerender_script_sequences(payload, shots, prompts=()) -> dict:
    """Update delivery styles and versions while preserving saved ownership/time."""
    if payload.get("sequence_metadata", {}).get("event_grouping_policy") != POLICY:
        raise ValueError("仅 script-director-v3 序列可以进行剧本风格重渲染")
    result = copy.deepcopy(payload)
    current = _sources(shots)
    metadata = result["sequence_metadata"]
    snapshot = metadata.get("source_snapshot")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("shots"), list):
        raise ValueError("剧本序列缺少来源快照，不能重建已保存的导演编排")
    saved_source = _sources(snapshot["shots"])
    current_ids = {shot.id for shot in current}
    if any(shot.id not in current_ids for shot in saved_source):
        raise ValueError("剧本重渲染缺少来源镜头，已保存分组不能修改")
    bundles = _bundles(current, prompts)
    previous_bundles = _bundles(saved_source, snapshot.get("prompts", []))
    # Missing optional current contracts keep their saved style rather than erasing it.
    effective = dict(previous_bundles)
    for shot_id, current_bundle in bundles.items():
        if shot_id not in previous_bundles:
            effective[shot_id] = current_bundle
            continue
        styled = previous_bundles[shot_id].model_copy(deep=True)
        styled.version = current_bundle.version
        for field in ("observed_style", "style_profile_version", "style_profile_hash"):
            setattr(styled.contract, field, getattr(current_bundle.contract, field))
        for field in ("style_layers", "style_provenance"):
            if field in current_bundle.contract.delivery:
                styled.contract.delivery[field] = copy.deepcopy(current_bundle.contract.delivery[field])
            else:
                styled.contract.delivery.pop(field, None)
        effective[shot_id] = styled
    version = int(metadata.get("version_number", 1)) + 1
    rendered = []
    for saved in result.get("generation_sequences", []):
        for key, baseline in (("first_frame_prompt", "first_frame_prompt"), ("seedance_prompt", "seedance_video_prompt")):
            original = saved[key]
            delivery = original.get("contract", {}).get("delivery", {})
            if delivery.get(baseline) != original.get("positive_prompt"):
                raise ValueError("检测到手动修改的剧本序列 Prompt，保留原文")
            negative_key = "first_frame_negative_prompt" if key == "first_frame_prompt" else "seedance_negative_prompt"
            if delivery.get(negative_key, "") != (original.get("negative_prompt") or ""):
                raise ValueError("检测到手动修改的剧本序列负面 Prompt，保留原文")
        sequence = GenerationSequence.model_validate(saved)
        sequence.version = version
        render_version = saved["seedance_prompt"]["contract"]["delivery"].get("render_version", metadata.get("render_version", LEGACY_VERSION))
        update = _render(sequence, saved_source, effective, render_version=render_version).model_dump(mode="json")
        # The rest of each saved sequence, including future ownership metadata,
        # remains byte-for-byte equivalent as ordinary JSON data.
        updated = copy.deepcopy(saved)
        updated["version"] = version
        for key in ("first_frame_prompt", "seedance_prompt"):
            negative = saved[key].get("negative_prompt") or ""
            updated[key] = {**saved[key], "version": version,
                "positive_prompt": update[key]["positive_prompt"], "contract": update[key]["contract"],
                "negative_prompt": negative}
            negative_key = "first_frame_negative_prompt" if key == "first_frame_prompt" else "seedance_negative_prompt"
            updated[key]["contract"]["delivery"][negative_key] = negative
        rendered.append(updated)
    result["generation_sequences"] = rendered
    metadata.update(version_number=version, mode="persisted")
    metadata["source_snapshot"]["prompts"] = [effective[shot.id].model_dump(mode="json")
        for shot in saved_source if shot.id in effective]
    metadata.pop("render_refresh", None)
    return _hash_payload(result)
