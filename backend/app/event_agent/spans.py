"""Bounded analysis views. Computation spans never become physical source shots."""
from __future__ import annotations

import math
from types import SimpleNamespace

from app.perception_models import VisualObservationV2, merge_stages, project_v1, unknown_field


def span_plan(shots, config):
    limit = min(config.window_seconds, math.floor(config.generation_max_seconds * 2) / 2)
    if limit <= 0:
        raise ValueError("生成上限不足半秒，不能建立有效分析区间")
    views, spans = [], {}
    for shot in sorted(shots, key=lambda row: (row.start_time, row.number, row.id)):
        count = max(1, math.ceil((shot.end_time - shot.start_time) / limit))
        for index in range(count):
            start, end = shot.start_time + index * limit, min(shot.end_time, shot.start_time + (index + 1) * limit)
            sid = shot.id if count == 1 else f"{shot.id}::analysis-{index + 1:03d}"
            spans[sid] = {"source_shot_id": shot.id, "start_time": start, "end_time": end,
                          "source_raw_shot_ids": list(getattr(shot, "source_raw_shot_ids", []))}
            views.append(SimpleNamespace(id=sid, number=len(views) + 1, start_time=start, end_time=end))
    return views, spans


def analysis_views(shots, frames, config):
    plan, spans = span_plan(shots, config)
    source = {shot.id: shot for shot in shots}
    if all(view.id == spans[view.id]["source_shot_id"] for view in plan):
        return list(shots), list(frames), {}
    views = []
    for row in plan:
        span = spans[row.id]
        original = source[span["source_shot_id"]]
        owned = [frame for frame in frames if frame.shot_id == original.id and row.start_time <= frame.timestamp < row.end_time]
        ids = {frame.id for frame in owned}
        view = original.model_copy(deep=True, update={"id": row.id, "number": row.number,
            "start_time": row.start_time, "end_time": row.end_time, "duration": row.end_time-row.start_time,
            "generation_duration_seconds": row.end_time-row.start_time,
            "frame_ids": [frame.id for frame in owned],
            "source_edit_times": [value for value in original.source_edit_times if row.start_time < value < row.end_time]})
        saved_spans = getattr(original, "analysis_span_observations", []) or []
        saved = next((item for item in saved_spans
                      if abs(item.get("start_time", -1) - row.start_time) <= 0.000001
                      and abs(item.get("end_time", -1) - row.end_time) <= 0.000001), None)
        if row.id != original.id and saved is not None:
            # Each interval has its own perception and four registered images.
            # A pending interval must not borrow the whole-source motion summary.
            view.visual_observation_v2 = (VisualObservationV2.model_validate(saved["observation"])
                if saved.get("observation") is not None else merge_stages(None, None))
            projection = project_v1(view.visual_observation_v2)
            view.action = projection.subject_action
            view.action_details = projection.action_details
            view.visual_facts = projection.visual_facts
            view.spatial_evidence = projection.spatial_evidence
            view.shot_size, view.lens = projection.shot_size, projection.lens
            view.camera_movement, view.composition = projection.camera_movement, projection.composition
            view.confidence = projection.confidence
            view.source_text = ""
            view.status = "ready" if saved.get("status") == "ready" else "warning"
        elif row.id != original.id and view.visual_observation_v2 is not None:
            observation = view.visual_observation_v2
            for group in (observation.facts, observation.dynamics):
                for name in type(group).model_fields:
                    field = getattr(group, name)
                    if not hasattr(field, "evidence"):
                        continue
                    refs = [ref for ref in field.evidence if ref.frame_id in ids]
                    # A changing action cannot be assigned to a subinterval using
                    # only one endpoint from an action spanning the whole raw shot.
                    motion = group is observation.dynamics and name in ("subject_action", "action_steps", "emotion_change")
                    if not refs or motion and len(refs) != len(field.evidence):
                        setattr(group, name, type(field).model_validate(unknown_field([] if isinstance(field.value, list) else "未知", "该分析区间缺少独立动作证据")))
                    else:
                        field.evidence = refs
                if group is observation.dynamics:
                    group.action_phases = []
                    group.step_evidence = []
            projected = project_v1(observation)
            view.action = projected.subject_action
            view.action_details = projected.action_details
            view.visual_facts = projected.visual_facts
        elif row.id != original.id:
            # Legacy free text has no interval evidence; preserve visual context
            # only, rather than claiming a full-source action repeats per span.
            view.action = "未知"
            view.source_text = ""
            view.action_details.evidence = []
            view.action_details.accompanying_states = []
            view.action_details.emotion_change = "未知"
            view.action_details.steps = []
            view.action_details.initial_state = "未知"
            view.action_details.final_state = "未知"
        views.append(view)
    def owner(frame):
        return next((row.id for row in plan if spans[row.id]["source_shot_id"] == frame.shot_id and row.start_time <= frame.timestamp < row.end_time), frame.shot_id)
    return views, [frame.model_copy(update={"shot_id": owner(frame)}) for frame in frames], spans
