"""Compatibility-safe presentation contract derived from persisted shots and plans."""
from __future__ import annotations

from typing import Any, Iterable

from app.schemas import ActionAtom, SceneGroup, Shot


def _ids_for_shot(shot: Shot, count: int) -> list[str]:
    return [f"{shot.id}-atom-{index + 1:02d}" for index in range(count)]


def build_action_atoms(shots: Iterable[Shot], plan_payload: dict[str, Any] | None = None) -> list[ActionAtom]:
    plan_by_number = {index + 1: segment for index, segment in enumerate((plan_payload or {}).get("shots", []))}
    atoms: list[ActionAtom] = []
    for shot in sorted(shots, key=lambda item: item.number):
        plan = plan_by_number.get(shot.number) or {}
        beat_ids = plan.get("source_beat_ids") or []
        # The text planner preserves the exact source beat assignment. A video
        # observation may contain several ordered visible steps from its four frames.
        steps = [str(step).strip() for step in shot.action_details.steps if str(step).strip()] or [shot.action or "未知"]
        # Video step timestamps are not directly observed; do not invent equal-time atoms.
        count = min(4, max(len(beat_ids), len(steps))) if beat_ids else 1
        if shot.script_direction:
            # A directed performance is not four measured frame intervals.
            count = 1
        count = max(1, count)
        atom_ids = _ids_for_shot(shot, count)
        shot.source_action_atom_ids = atom_ids
        for index, atom_id in enumerate(atom_ids):
            start = shot.start_time + shot.duration * index / count
            end = shot.start_time + shot.duration * (index + 1) / count
            action = steps[min(index, len(steps) - 1)] if beat_ids else "；".join(steps)
            warnings = list(shot.analysis_warnings)
            atoms.append(ActionAtom(
                id=atom_id,
                number=len(atoms) + 1,
                start_time=start,
                end_time=end,
                duration=end - start,
                source_raw_shot_ids=list(shot.source_raw_shot_ids),
                frame_ids=list(shot.frame_ids),
                primary_subject=shot.action_details.primary_subject,
                initial_state=shot.action_details.initial_state if index == 0 else "上一动作步骤后的可见状态",
                action=action,
                interaction_target=shot.action_details.interaction_target,
                final_state=shot.action_details.final_state if index == count - 1 else "进入下一动作步骤",
                confidence=shot.confidence if not warnings else min(shot.confidence, 0.5),
                warnings=warnings,
                generation_shot_id=shot.id,
            ))
    return atoms


def build_scene_groups(shots: Iterable[Shot]) -> list[SceneGroup]:
    ordered = sorted(shots, key=lambda item: item.number)
    groups: list[list[Shot]] = []
    for shot in ordered:
        if groups and groups[-1][-1].scene_group_id == shot.scene_group_id and shot.scene_group_id:
            groups[-1].append(shot)
        else:
            groups.append([shot])
    result: list[SceneGroup] = []
    for index, members in enumerate(groups, start=1):
        first, last = members[0], members[-1]
        result.append(SceneGroup(
            id=first.scene_group_id or f"scene-unassigned-{index:03d}",
            order=index,
            title=f"场次 {index:02d}",
            shot_ids=[shot.id for shot in members],
            cover_frame_ids=[shot.frame_ids[0] for shot in members if shot.frame_ids][:7],
            start_time=first.start_time,
            end_time=last.end_time,
            warning_count=sum(shot.status == "warning" or bool(shot.analysis_warnings) for shot in members),
            grouping_mode=("visual-fallback" if first.scene_reason.startswith("视觉") else "semantic") if first.scene_group_id else "unassigned",
        ))
    return result


def validate_action_coverage(shots: Iterable[Shot], atoms: Iterable[ActionAtom]) -> None:
    ordered_shots = sorted(shots, key=lambda item: item.number)
    by_id = {atom.id: atom for atom in atoms}
    all_ids = [atom_id for shot in ordered_shots for atom_id in shot.source_action_atom_ids]
    if len(all_ids) != len(set(all_ids)) or set(all_ids) != set(by_id):
        raise ValueError("原子动作归属重复或缺失")
    for shot in ordered_shots:
        source = [by_id[atom_id] for atom_id in shot.source_action_atom_ids]
        if not 1 <= len(source) <= 4:
            raise ValueError(f"镜头 {shot.id} 的原子动作数不在 1–4 范围内")
        if shot.duration <= 0 or shot.end_time <= shot.start_time:
            raise ValueError(f"镜头 {shot.id} 的来源时间无效")
        if source and (abs(source[0].start_time - shot.start_time) > 0.001 or abs(source[-1].end_time - shot.end_time) > 0.001):
            raise ValueError(f"镜头 {shot.id} 的原子动作时间范围不完整")
        if any(abs(left.end_time - right.start_time) > 0.001 for left, right in zip(source, source[1:])):
            raise ValueError(f"镜头 {shot.id} 的原子动作时间不连续")
