"""Read-only replay of saved model responses through the new local parser.

No Provider or network is instantiated. This tests salvage, not model quality.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from app.media_paths import MediaPaths
from app.perception_models import StaticObservationV2, MotionObservationV2, VisualObservationV2, merge_stages, project_v1
from app.providers.base import FrameInput
from app.services.perception_wire import decode_stage


def replay(db_path: Path, job_id: str):
    resolved = db_path.resolve()
    if not resolved.is_file():
        raise ValueError("评测数据库不存在")
    connection = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = MediaPaths(resolved.parent).row
    try:
        if not connection.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone():
            raise ValueError("任务不存在")
        shots = connection.execute("SELECT * FROM shots WHERE job_id=? ORDER BY number", (job_id,)).fetchall()
        rows = []
        for shot in shots:
            if "perception_json" not in shot.keys() or not shot["perception_json"]:
                continue
            record = json.loads(shot["perception_json"])
            frames = [FrameInput(frame["id"], frame["timestamp"], frame["path"], frame["width"], frame["height"],
                                 frame["artifact_kind"], frame["parent_frame_id"])
                      for frame in connection.execute("SELECT * FROM frames WHERE shot_id=? ORDER BY timestamp", (shot["id"],))]
            static = motion = None
            supplement = None
            repairs = []
            errors = []
            for entry in record.get("raw_responses", []):
                phase = entry["phase"]
                raw = entry.get("response")
                if not raw:
                    continue
                model = StaticObservationV2 if phase.startswith("shot.static") else MotionObservationV2 if phase.startswith("shot.motion") else VisualObservationV2 if phase == "shot.supplement" else None
                if model is None:
                    continue
                try:
                    input_ids = {item["frame_id"] for item in entry.get("input_frames", [])}
                    stage_frames = [frame for frame in frames if frame.id in input_ids] if input_ids else frames
                    parsed, audit, _ = decode_stage(raw, model, stage_frames, shot["start_time"], shot["end_time"])
                    repairs.extend(audit)
                    if model is StaticObservationV2:
                        static = parsed
                    elif model is MotionObservationV2:
                        motion = parsed
                    else:
                        supplement = parsed
                except (ValueError, TypeError) as exc:
                    errors.append({"phase": phase, "error": str(exc).splitlines()[0][:180]})
            observation = merge_stages(static, motion)
            if supplement:
                payload = observation.model_dump()
                for group in ("facts", "dynamics", "photography", "artistic_effects"):
                    for name, field in getattr(supplement, group):
                        current = getattr(getattr(observation, group), name)
                        if hasattr(field, "observable") and field.observable and not current.observable:
                            payload[group][name] = field.model_dump()
                observation = VisualObservationV2.model_validate(payload)
            projected = project_v1(observation, partial_failure=static is None or motion is None)
            before = json.loads(shot["observation_json"] or "{}")
            rows.append({"number": shot["number"], "original_action": before.get("subject_action", "未知"),
                         "replayed_action": projected.subject_action, "replayed_subject": projected.action_details.primary_subject,
                         "replayed_scene": projected.visual_facts.scene, "local_repair_count": len(repairs),
                         "stage_errors": errors, "original_model_calls": len(record.get("raw_responses", [])),
                         "original_elapsed_seconds": record.get("elapsed_seconds")})
        return {"job_id": job_id, "database_unchanged": True, "new_model_calls": 0,
                "note": "仅回放已保存响应以验证格式修复，不代表模型重新分析或人工准确性验收。", "shots": rows}
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读回放保存的视觉响应，不调用云端、不修改旧任务")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(replay(args.db, args.job_id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
