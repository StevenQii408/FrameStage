from dataclasses import dataclass, field
import re
from typing import Iterable, List, Sequence, Tuple

from app.schemas import ScriptBeat, ScriptPlan, ScriptShotPlan


SCENE_HEADING = re.compile(r"^(?:场景\s*[:：]|(?:INT|EXT)\.|内景|外景|第[一二三四五六七八九十百零0-9]+场)", re.IGNORECASE)
DIALOGUE_ONLY = re.compile(r"^[\s‘’“”\"']*[^：:]{0,12}[：:]?[‘“\"].+[’”\"]\s*$")
ACTION_CUE = re.compile(r"(?=(?:突然|随后|然后|接着|这时|同时|而后|最后|紧接着|转身|却|并(?:立刻|马上|缓慢|快速)))")
NON_VISUAL = re.compile(r"(?:心想|想起|意识到|觉得|认为|回忆起|内心|旁白|独白)")
VISIBLE_ACTION = re.compile(r"(?:走|跑|看|拿|推|转|站|坐|抱|吃|说|进入|离开|伸|停|哭|笑|摇|点头|抬|放|打开|关上|挥|抓|扇)")


@dataclass
class ScriptWindow:
    beats: List[ScriptBeat]
    target_beat_ids: List[str]


@dataclass
class ScriptPlanningBundle:
    shots: List[ScriptShotPlan]
    beats: List[ScriptBeat]
    mode: str
    raw_responses: List[str] = field(default_factory=list)
    fallback_windows: List[int] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    window_count: int = 0


def _clean_piece(value: str) -> str:
    return value.strip(" \t\n，,；;。")


def _split_long_sentence(sentence: str) -> List[str]:
    sentence = _clean_piece(sentence)
    if not sentence:
        return []
    cue_parts = [_clean_piece(part) for part in ACTION_CUE.split(sentence) if _clean_piece(part)]
    pieces: List[str] = []
    for part in cue_parts:
        if len(part) <= 34:
            pieces.append(part)
            continue
        comma_parts = [_clean_piece(item) for item in re.split(r"[，,]+", part) if _clean_piece(item)]
        pieces.extend(comma_parts if len(comma_parts) > 1 else [part])
    return pieces


def extract_script_beats(script_text: str, maximum: int = 480) -> List[ScriptBeat]:
    """Split prose or screenplay text into traceable visual/action candidates."""

    normalized = script_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    raw_lines = normalized.split("\n")
    current_scene = "scene-001"
    current_scene_label = ""
    scene_number = 1
    paragraph_number = 1
    previous_was_blank = False
    pieces: List[Tuple[str, str, str, str]] = []
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            previous_was_blank = True
            continue
        if previous_was_blank and pieces:
            paragraph_number += 1
        previous_was_blank = False
        paragraph_hint = f"paragraph-{paragraph_number:03d}"
        if SCENE_HEADING.match(line):
            if pieces:
                scene_number += 1
                current_scene = f"scene-{scene_number:03d}"
            current_scene_label = _clean_piece(line)
            continue
        if re.match(r"^[^：:\n]{1,12}[：:]\s*[‘“\"']", line):
            if pieces:
                prior, scene, scene_label, paragraph = pieces[-1]
                pieces[-1] = (f"{prior}；{_clean_piece(line)}", scene, scene_label, paragraph)
            else:
                pieces.append((_clean_piece(line), current_scene, current_scene_label, paragraph_hint))
            continue
        sentences = [_clean_piece(item) for item in re.split(r"(?<=[。！？!?；;])", line) if _clean_piece(item)]
        for sentence in sentences or [line]:
            for piece in _split_long_sentence(sentence):
                if DIALOGUE_ONLY.match(piece) and pieces:
                    prior, scene, scene_label, paragraph = pieces[-1]
                    pieces[-1] = (f"{prior}；{piece}", scene, scene_label, paragraph)
                elif NON_VISUAL.search(piece) and not VISIBLE_ACTION.search(piece) and pieces:
                    prior, scene, scene_label, paragraph = pieces[-1]
                    pieces[-1] = (f"{prior}；{piece}", scene, scene_label, paragraph)
                else:
                    pieces.append((piece, current_scene, current_scene_label, paragraph_hint))
    if not pieces:
        pieces = [(normalized, current_scene, current_scene_label, "paragraph-001")]
    return [
        ScriptBeat(id=f"B{index:03d}", text=text, scene_hint=scene, scene_label=scene_label, paragraph_hint=paragraph)
        for index, (text, scene, scene_label, paragraph) in enumerate(pieces[:maximum], start=1)
    ]


def script_windows(beats: Sequence[ScriptBeat], size: int = 12, overlap: int = 2) -> List[ScriptWindow]:
    if not beats:
        return []
    size = max(2, size)
    overlap = max(0, min(overlap, size - 1))
    step = size - overlap
    windows: List[ScriptWindow] = []
    for start in range(0, len(beats), step):
        window = list(beats[start:start + size])
        if not window:
            break
        context_count = overlap if start else 0
        targets = window[context_count:]
        if targets:
            windows.append(ScriptWindow(window, [beat.id for beat in targets]))
        if start + size >= len(beats):
            break
    return windows


def _camera_defaults(text: str) -> Tuple[str, str, str, str, str, str]:
    if re.search(r"眼神|表情|手中|手里|照片|信封|泪|嘴|手机|细节", text):
        return "特写", "长焦", "固定", "固定镜头突出细节", "实景光源", "聚焦主体局部与互动对象"
    if re.search(r"走|跑|追|穿过|进入|离开|跟着|驶|飞", text):
        return "中景", "标准", "跟拍", "沿主体移动方向平稳跟随", "自然柔光", "主体位于运动方向的反侧留出空间"
    if re.search(r"城市|街道|房间|广场|远处|全景|天空|海边|森林", text):
        return "全景", "广角", "固定", "固定镜头交代环境", "自然柔光", "环境建立镜头，主体与背景分层"
    return "中景", "标准", "固定", "保持稳定视角", "自然柔光", "主体清晰，前景与背景分层"


def estimate_beat_seconds(beat: ScriptBeat) -> float:
    """Conservative visual-time budget; dialogue and long movement consume more."""
    text = beat.text
    dialogue = len(re.findall(r'[“"].+?[”"]', text))
    movements = len(re.findall(r'走|跑|追|穿过|进入|离开|抱|推|转身', text))
    interactions = len(re.findall(r'拿|抓|塞|递|敲|打开|关上|扇|亲', text))
    return min(4.5, max(1.5, 1.25 + len(text) * 0.025 + movements * 0.35 + interactions * 0.3 + dialogue * 0.7))


def fallback_window_plan(beats: Sequence[ScriptBeat], target_beat_ids: Sequence[str], maximum_shots: int = 120) -> ScriptPlan:
    target_set = set(target_beat_ids)
    targets = [beat for beat in beats if beat.id in target_set]
    if not targets:
        raise ValueError("剧本窗口没有待规划节拍")
    groups: List[List[ScriptBeat]] = []
    for beat in targets:
        if not groups:
            groups.append([beat])
            continue
        current = groups[-1]
        scene_changed = current[-1].scene_hint != beat.scene_hint
        paragraph_changed = current[-1].paragraph_hint != beat.paragraph_hint
        length_after_merge = sum(len(item.text) for item in current) + len(beat.text)
        strong_transition = bool(re.match(r"^(?:突然|这时|随后|然后|接着|最后|紧接着)", beat.text))
        exceeds_time = sum(estimate_beat_seconds(item) for item in [*current, beat]) > 5.0
        if scene_changed or paragraph_changed or strong_transition or len(current) >= 2 or length_after_merge > 72 or exceeds_time:
            groups.append([beat])
        else:
            current.append(beat)
    if len(groups) > maximum_shots:
        flattened = [beat for group in groups for beat in group]
        groups = [flattened[index:index + 4] for index in range(0, len(flattened), 4)][:maximum_shots]
    shots: List[ScriptShotPlan] = []
    for group in groups:
        text = "；".join(beat.text for beat in group)
        shot_size, lens, movement, movement_detail, light, composition = _camera_defaults(text)
        subject_match = re.search(r"([^，。；]{1,16}?)(?:走|跑|看|拿|推|转|站|坐|抱|吃|说|进入|离开|伸|停)", text)
        subject = _clean_piece(subject_match.group(1)) if subject_match else "剧本中的主要主体"
        steps = [beat.text for beat in group][:4]
        shots.append(ScriptShotPlan(
            source_beat_ids=[beat.id for beat in group], scene_group_id=group[0].scene_hint,
            primary_subject=subject or "剧本中的主要主体",
            first_frame_state=f"{subject or '主要主体'}位于场景中，尚未执行本镜动作",
            action_steps=steps, interaction_target="按剧本文本中的互动对象",
            end_state=f"{steps[-1]}完成后的稳定状态", emotion_change="按剧本文本呈现，不额外推断",
            shot_size=shot_size, lens=lens, camera_movement_type=movement,
            camera_movement_detail=movement_detail, lighting_type=light,
            lighting_detail="保持场景既有光线逻辑", composition=composition,
            foreground="场景中的近处环境元素", midground=subject or "主要主体",
            background=group[0].scene_label or "剧本文本明确的环境", spatial_relationships="按动作对象与移动方向建立",
            continuity_anchors=[subject or "主要主体", "服装、道具与方向保持连续"],
            duration_seconds=4.0 if sum(estimate_beat_seconds(item) for item in group) <= 3.3 else 4.5 if sum(estimate_beat_seconds(item) for item in group) <= 4.3 else 5.0, confidence=0.45,
            warnings=["使用确定性剧本拆镜兜底，需要人工复核摄影参数"],
        ))
    return ScriptPlan(shots=shots)


def validate_window_plan(plan: ScriptPlan, beats: Sequence[ScriptBeat], target_beat_ids: Sequence[str]) -> ScriptPlan:
    target_ids = list(target_beat_ids)
    valid_ids = {beat.id for beat in beats}
    flattened = [beat_id for shot in plan.shots for beat_id in shot.source_beat_ids]
    if flattened != target_ids:
        raise ValueError(f"source_beat_ids 必须按顺序恰好覆盖 {target_ids}，实际为 {flattened}")
    if any(beat_id not in valid_ids for beat_id in flattened):
        raise ValueError("计划引用了不存在的剧本节拍")
    conservative_groups = fallback_window_plan(beats, target_beat_ids).shots
    protected_group = {
        beat_id: group_index
        for group_index, group in enumerate(conservative_groups)
        for beat_id in group.source_beat_ids
    }
    for shot in plan.shots:
        if len({protected_group[beat_id] for beat_id in shot.source_beat_ids}) > 1:
            raise ValueError(f"镜头 {shot.source_beat_ids} 跨越了动作转折或段落保护边界，请拆开")
        estimate = sum(estimate_beat_seconds(beat) for beat in beats if beat.id in shot.source_beat_ids)
        if estimate > 5.0:
            raise ValueError(f"镜头 {shot.source_beat_ids} 估计动作时长 {estimate:.1f} 秒，超过五秒，请拆开")
    return plan


def compact_oversplit_plan(plan: ScriptPlan, beats: Sequence[ScriptBeat], target_beat_ids: Sequence[str]) -> ScriptPlan:
    """Conservatively merge an over-segmented plan with one shot per beat.

    Model-authored states and photography survive; deterministic beat boundaries
    only decide which adjacent one-beat records belong to the same 4–5s shot.
    """

    if len(target_beat_ids) < 6 or len(plan.shots) != len(target_beat_ids):
        return plan
    if any(len(shot.source_beat_ids) != 1 for shot in plan.shots):
        return plan
    grouping = fallback_window_plan(beats, target_beat_ids).shots
    if len(grouping) >= len(plan.shots):
        return plan
    by_beat = {shot.source_beat_ids[0]: shot for shot in plan.shots}
    merged: List[ScriptShotPlan] = []
    for group in grouping:
        members = [by_beat[beat_id] for beat_id in group.source_beat_ids]
        first, last = members[0], members[-1]
        actions = [step for member in members for step in member.action_steps][:4]
        anchors = list(dict.fromkeys(anchor for member in members for anchor in member.continuity_anchors))
        warnings = list(dict.fromkeys(warning for member in members for warning in member.warnings))
        merged.append(first.model_copy(update={
            "source_beat_ids": group.source_beat_ids,
            "action_steps": actions,
            "interaction_target": "；".join(dict.fromkeys(member.interaction_target for member in members if member.interaction_target != "无")) or "无",
            "end_state": last.end_state,
            "emotion_change": last.emotion_change,
            "continuity_anchors": anchors,
            "confidence": min(member.confidence for member in members),
            "warnings": warnings,
        }))
    return ScriptPlan(shots=merged)


def reconcile_protected_boundaries(plan: ScriptPlan, beats: Sequence[ScriptBeat], target_beat_ids: Sequence[str]) -> Tuple[ScriptPlan, bool]:
    """Split model groups that crossed deterministic narrative protection.

    The rule layer restores source/action boundaries, while model-authored subject
    and cinematography remain attached to every resulting child shot.
    """

    conservative = fallback_window_plan(beats, target_beat_ids).shots
    protected_group = {
        beat_id: group_index
        for group_index, group in enumerate(conservative)
        for beat_id in group.source_beat_ids
    }
    crossing = any(len({protected_group[beat_id] for beat_id in shot.source_beat_ids}) > 1 for shot in plan.shots)
    if not crossing:
        return plan, False
    model_by_beat = {beat_id: shot for shot in plan.shots for beat_id in shot.source_beat_ids}
    reconciled: List[ScriptShotPlan] = []
    for rule_shot in conservative:
        donors = list(dict.fromkeys(model_by_beat[beat_id].source_beat_ids[0] for beat_id in rule_shot.source_beat_ids))
        donor = model_by_beat[donors[0]]
        donor_first = donor.source_beat_ids[0] == rule_shot.source_beat_ids[0]
        donor_last = donor.source_beat_ids[-1] == rule_shot.source_beat_ids[-1]
        reconciled.append(rule_shot.model_copy(update={
            "primary_subject": donor.primary_subject,
            "first_frame_state": donor.first_frame_state if donor_first else rule_shot.first_frame_state,
            "end_state": donor.end_state if donor_last else rule_shot.end_state,
            "shot_size": donor.shot_size,
            "lens": donor.lens,
            "camera_movement_type": donor.camera_movement_type,
            "camera_movement_detail": donor.camera_movement_detail,
            "lighting_type": donor.lighting_type,
            "lighting_detail": donor.lighting_detail,
            "composition": donor.composition,
            "foreground": donor.foreground,
            "midground": donor.midground,
            "background": donor.background,
            "spatial_relationships": donor.spatial_relationships,
            "continuity_anchors": donor.continuity_anchors,
            "confidence": min(donor.confidence, 0.65),
            "warnings": list(dict.fromkeys([*donor.warnings, "模型合并跨越动作转折，已按保护边界拆分"])),
        }))
    return ScriptPlan(shots=reconciled), True


def renumber_and_limit(shots: Iterable[ScriptShotPlan], maximum_shots: int) -> List[ScriptShotPlan]:
    result = list(shots)
    if len(result) <= maximum_shots:
        return result
    raise ValueError(f"剧本规划产生 {len(result)} 镜，超过 {maximum_shots} 镜限制")


def parse_script(script_text: str, max_shots: int = 120) -> List[ScriptShotPlan]:
    """Compatibility entry point: deterministic v2 output without a provider."""

    beats = extract_script_beats(script_text, max_shots * 4)
    if not beats:
        return []
    return fallback_window_plan(beats, [beat.id for beat in beats], max_shots).shots
