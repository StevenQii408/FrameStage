"""Score human-labeled generation shots and narrative scene boundaries."""
from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path
from typing import Any

from evals.manifest import ManifestError, load_manifest
from evals.scoring import match_boundaries


def score_generation_annotation(reference: list[dict[str, Any]], result: dict[str, Any], tolerance: float = 0.3, *, duration_policy="legacy-under-five") -> dict[str, Any]:
    if not reference:
        raise ValueError("缺少人工生成镜头标注")
    shots = sorted(result.get("shots", []), key=lambda shot: int(shot["number"]))
    if not shots:
        raise ValueError("导出结果没有生成镜头")
    predicted_boundaries = [float(shot["start_time"]) for shot in shots[1:]]
    expected_boundaries = [float(shot["start_time"]) for shot in reference[1:]]
    generation = match_boundaries(predicted_boundaries, expected_boundaries, tolerance)
    expected_scene_boundaries = [
        float(shot["start_time"]) for previous, shot in zip(reference, reference[1:])
        if shot["scene_id"] != previous["scene_id"]
    ]
    predicted_scene_boundaries = [
        float(shot["start_time"]) for previous, shot in zip(shots, shots[1:])
        if shot.get("scene_group_id") != previous.get("scene_group_id")
    ]
    scene = match_boundaries(predicted_scene_boundaries, expected_scene_boundaries, tolerance)
    inserts = [shot for shot in reference if shot["role"] == "salient_insert"]
    recovered = sum(
        any(
            abs(float(predicted["start_time"]) - float(insert["start_time"])) <= tolerance
            and abs(float(predicted["end_time"]) - float(insert["end_time"])) <= tolerance
            for predicted in shots
        )
        for insert in inserts
    )
    durations = [float(shot["end_time"]) - float(shot["start_time"]) for shot in shots]
    timeline_complete = (
        abs(float(shots[0]["start_time"]) - float(reference[0]["start_time"])) <= 0.002
        and abs(float(shots[-1]["end_time"]) - float(reference[-1]["end_time"])) <= 0.05
        and all(abs(float(left["end_time"]) - float(right["start_time"])) <= 0.002 for left, right in zip(shots, shots[1:]))
    )
    raw_ids = {raw["id"] for raw in result.get("raw_shots", [])}
    attributed = {raw_id for shot in shots for raw_id in shot.get("source_raw_shot_ids", [])}
    return {
        "generation_boundary": generation,
        "scene_boundary": scene,
        "salient_insert": {"expected": len(inserts), "recovered": recovered, "recall": recovered / len(inserts) if inserts else None},
        "shot_count": len(shots),
        "maximum_duration_seconds": max(durations),
        "duration_policy": duration_policy,
        "all_under_five_seconds": all(0 < duration < 5.0 for duration in durations) if duration_policy == "legacy-under-five" else None,
        "timeline_complete": timeline_complete,
        "raw_source_coverage": len(raw_ids & attributed) / len(raw_ids) if raw_ids else None,
    }


def score_event_groups(groups, result):
    """Human event ownership, independent of source-cut count and render duration."""
    shots = sorted(result.get("shots", []), key=lambda shot: int(shot["number"]))
    numbers = {shot["id"]: int(shot["number"]) for shot in shots}
    expected = [number for group in groups for number in group]
    predicted = []
    for event in result.get("narrative_events") or []:
        ids = event.get("source_shot_ids") or []
        if any(identity not in numbers for identity in ids):
            raise ValueError("预测事件含未知来源镜头")
        predicted.append([numbers[identity] for identity in ids])
    covered = [number for group in predicted for number in group]
    expected_pairs = {pair for group in groups for pair in combinations(group, 2)}
    predicted_pairs = {pair for group in predicted for pair in combinations(group, 2)}
    matched = len(expected_pairs & predicted_pairs)
    precision = matched / len(predicted_pairs) if predicted_pairs else float(not expected_pairs)
    recall = matched / len(expected_pairs) if expected_pairs else float(not predicted_pairs)
    complete = covered == expected and len(set(covered)) == len(covered)
    return {"ownership_complete": complete, "pairwise_precision": precision,
            "pairwise_recall": recall, "pairwise_f1": 2 * precision * recall / (precision + recall) if precision + recall and complete else 0.0,
            "semantic_accuracy": None, "note": "事件归属指标不代替人工动作事实与剧情忠实评审"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="将已导出的 storyboard.v1 与人工生成镜头标注比较")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--clip-id", required=True)
    parser.add_argument("--result-json", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        clip = next((clip for clip in manifest["clips"] if clip["id"] == args.clip_id), None)
        if clip is None or not clip.get("generation_shots"):
            raise ManifestError("指定素材缺少 generation_shots 人工标注")
        result = json.loads(args.result_json.read_text(encoding="utf-8"))
        score = score_generation_annotation(clip["generation_shots"], result,
            duration_policy="source-pts" if manifest["schema_version"] == "storyboard-eval.v2" else "legacy-under-five")
        if clip.get("event_groups"):
            score["event_grouping"] = score_event_groups(clip["event_groups"], result)
    except (ManifestError, ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"拆镜评测失败：{exc}\n")
    print(json.dumps({"clip_id": args.clip_id, "segmentation_profile": result.get("job", {}).get("segmentation_profile"), "score": score}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
