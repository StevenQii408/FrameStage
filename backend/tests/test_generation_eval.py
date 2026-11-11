import json
from pathlib import Path

import pytest

from evals.manifest import ManifestError, load_manifest
from evals.segmentation import score_generation_annotation


def test_generation_score_checks_cut_insert_scene_and_raw_coverage():
    reference = [
        {"start_time": 0.0, "end_time": 1.0, "scene_id": "a", "role": "salient_insert"},
        {"start_time": 1.0, "end_time": 3.0, "scene_id": "a", "role": "regular"},
        {"start_time": 3.0, "end_time": 4.0, "scene_id": "b", "role": "regular"},
    ]
    result = {
        "shots": [
            {"number": 1, "start_time": 0.0, "end_time": 1.0, "scene_group_id": "scene-001", "source_raw_shot_ids": ["raw-1"]},
            {"number": 2, "start_time": 1.0, "end_time": 3.0, "scene_group_id": "scene-001", "source_raw_shot_ids": ["raw-2"]},
            {"number": 3, "start_time": 3.0, "end_time": 4.0, "scene_group_id": "scene-002", "source_raw_shot_ids": ["raw-3"]},
        ],
        "raw_shots": [{"id": f"raw-{index}"} for index in range(1, 4)],
    }
    score = score_generation_annotation(reference, result)
    assert score["generation_boundary"]["f1"] == 1.0
    assert score["scene_boundary"]["f1"] == 1.0
    assert score["salient_insert"]["recall"] == 1.0
    assert score["all_under_five_seconds"] is True
    assert score["timeline_complete"] is True
    assert score["raw_source_coverage"] == 1.0
    result["shots"][2]["scene_group_id"] = "scene-001"
    assert score_generation_annotation(reference, result)["scene_boundary"]["recall"] == 0.0


def test_manifest_accepts_optional_human_generation_labels(tmp_path):
    example = Path(__file__).resolve().parents[1] / "evals" / "manifest.example.json"
    manifest = json.loads(example.read_text(encoding="utf-8"))
    generation = [
        {"start_time": 0.0, "end_time": 4.5, "scene_id": "scene-a", "role": "regular"},
        {"start_time": 4.5, "end_time": 9.0, "scene_id": "scene-b", "role": "salient_insert"},
    ]
    manifest["clips"][0]["generation_shots"] = generation
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert load_manifest(path)["clips"][0]["generation_shots"] == generation
    generation[1]["scene_id"] = "scene-a"
    generation.append({"start_time": 9.0, "end_time": 9.1, "scene_id": "scene-b", "role": "regular"})
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ManifestError, match="未覆盖完整素材|时间线"):
        load_manifest(path)
