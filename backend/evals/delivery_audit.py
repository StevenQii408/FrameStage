"""Audit exported results offline; semantic quality requires independent gold."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from app.services.delivery_quality import delivery_quality


def audit_result(result):
    shots = result.get("shots", [])
    job = result.get("job", {})
    if not job.get("input_type"):
        raise ValueError("需要包含job.input_type的storyboard导出结果")
    sequences = result.get("generation_sequences") or []
    source_ids = [s["id"] for s in shots]
    covered = [sid for sequence in sequences for sid in sequence.get("source_shot_ids", [])]
    duplicates = [sid for sid, count in Counter(covered).items() if count > 1]
    frames = {f["id"]: f for f in [*result.get("frames", []), *result.get("evidence_frames", [])]}
    invalid_refs = []
    def walk(value):
        if isinstance(value, dict):
            if "frame_id" in value and "timestamp" in value:
                frame = frames.get(value["frame_id"])
                if frame is None or abs(frame.get("timestamp", -1) - value["timestamp"]) > .01:
                    invalid_refs.append(value["frame_id"])
            for part in value.values():
                walk(part)
        elif isinstance(value, list):
            for part in value:
                walk(part)
    for shot in shots:
        walk(shot.get("visual_observation_v2"))
    metadata = result.get("metadata", {})
    quality = delivery_quality(job, shots, result.get("prompts", []),
        {"generation_sequences": sequences, "sequence_metadata": result.get("sequence_metadata") or {}},
        result.get("style_analysis"), metadata.get("script_planning"))
    sequence_durations_valid = all(0 < s.get("generation_duration_seconds", 0) <= 12 for s in sequences)
    coverage_complete = bool(sequences) and covered == source_ids
    return {"version": "delivery-audit.v1", "job_id": job.get("id"), "quality": quality,
            "source_count": len(shots), "sequence_count": len(sequences),
            "coverage_complete_and_ordered": coverage_complete, "duplicate_source_ids": duplicates,
            "missing_source_ids": [sid for sid in source_ids if sid not in covered],
            "unknown_source_ids": [sid for sid in covered if sid not in source_ids],
            "sequence_durations_valid": sequence_durations_valid,
            "invalid_evidence_frame_ids": sorted(set(invalid_refs)),
            "structural_pass": coverage_complete and sequence_durations_valid and not invalid_refs,
            "semantic_accuracy": None, "human_review_required": True,
            "review_dimensions": ["错误合并与漏合并", "动作与物体事实", "首帧提前结果", "画风命名与内容泄漏"],
            "model_calls": 0,
            "note": "结构检查不等于解析准确率；人工标注和模型预测必须分开保存"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_result(json.loads(args.result.read_text(encoding="utf-8")))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if report["structural_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
