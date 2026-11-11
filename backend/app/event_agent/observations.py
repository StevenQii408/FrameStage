"""One evidence-bearing catalog for both model prompts and local verification."""
from __future__ import annotations

import hashlib

_UNKNOWN = {"", "未知", "无", "待设定", "待判断", "待确认"}


def saved_observations(shot, delivered_ids=None):
    """Expose only saved content facts; never turn photography into an action.

    The wire ID is derived from the source, path and literal value. Filtering
    delivered evidence does not change it, so a repair can quote the same atom.
    Legacy projections have no field evidence and retain literal compatibility.
    """
    rows = []
    observation = shot.visual_observation_v2
    if observation is not None:
        for group_name in ("facts", "dynamics"):
            group = getattr(observation, group_name)
            for name in type(group).model_fields:
                field = getattr(group, name)
                if not hasattr(field, "observable") or not field.observable or not field.evidence:
                    continue
                path = f"{group_name}.{name}"
                for text in field.value if isinstance(field.value, list) else [field.value]:
                    if not isinstance(text, str) or text.strip() in _UNKNOWN:
                        continue
                    evidence = [ref for ref in field.evidence if delivered_ids is None or
                                ref.frame_id in delivered_ids and
                                (not ref.crop_artifact_id or ref.crop_artifact_id in delivered_ids)]
                    if delivered_ids is not None and not evidence:
                        continue
                    key = f"{shot.id}\0{path}\0{text}"
                    rows.append({"observation_id": "obs-" + hashlib.sha256(key.encode()).hexdigest()[:20],
                        "source_shot_id": shot.id, "field_path": path, "text": text,
                        "observation_type": "dynamic" if group_name == "dynamics" and name in ("subject_action", "action_steps", "emotion_change") else "static",
                        "evidence": [ref.model_dump(mode="json") for ref in evidence]})
        return rows
    details, facts = shot.action_details, shot.visual_facts
    fields = {"action": shot.action, "source_text": shot.source_text,
        **{f"action_details.{key}": value for key, value in details.model_dump().items()},
        **{f"visual_facts.{key}": value for key, value in facts.model_dump().items()}}
    for path, value in fields.items():
        if path.endswith((".evidence", ".uncertainties")):
            continue
        for text in value if isinstance(value, list) else [value]:
            if isinstance(text, str) and text.strip() not in _UNKNOWN:
                key = f"{shot.id}\0{path}\0{text}"
                rows.append({"observation_id": "obs-" + hashlib.sha256(key.encode()).hexdigest()[:20],
                    "source_shot_id": shot.id, "field_path": path, "text": text,
                    "observation_type": "legacy", "evidence": []})
    return rows
