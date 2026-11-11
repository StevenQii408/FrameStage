import json

import pytest

from evals.manifest import ManifestError, load_manifest
from evals.segmentation import score_event_groups, score_generation_annotation
from test_evals import manifest_payload


def write_manifest(tmp_path, version):
    payload = manifest_payload("owned.mp4", "0" * 64)
    payload["schema_version"] = version
    clip = payload["clips"][0]
    clip["shots"][0]["end_time"] = 9.23
    clip["generation_shots"] = [{"start_time": 0, "end_time": 9.23, "scene_id": "scene-1", "role": "regular"}]
    if version.endswith("v2"):
        clip["event_groups"] = [[1]]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload))
    return path


def test_v2_keeps_long_source_pts_while_v1_keeps_its_original_rules(tmp_path):
    payload = load_manifest(write_manifest(tmp_path, "storyboard-eval.v2"))
    assert payload["clips"][0]["generation_shots"][0]["end_time"] == 9.23
    with pytest.raises(ManifestError, match="旧版v1"):
        load_manifest(write_manifest(tmp_path, "storyboard-eval.v1"))


def test_v2_does_not_confuse_source_duration_with_generation_budget(tmp_path):
    payload = load_manifest(write_manifest(tmp_path, "storyboard-eval.v2"))
    result = {"shots": [{"id": "s1", "number": 1, "start_time": 0, "end_time": 9.23, "scene_group_id": "scene-1"}]}
    score = score_generation_annotation(payload["clips"][0]["generation_shots"], result, duration_policy="source-pts")
    assert score["timeline_complete"]
    assert score["all_under_five_seconds"] is None
    assert score["maximum_duration_seconds"] == 9.23


@pytest.mark.parametrize("groups", [[[1, 1]], [[2]], [[1], []]])
def test_v2_rejects_duplicate_missing_or_empty_event_truth(tmp_path, groups):
    path = write_manifest(tmp_path, "storyboard-eval.v2")
    payload = json.loads(path.read_text())
    payload["clips"][0]["event_groups"] = groups
    path.write_text(json.dumps(payload))
    with pytest.raises(ManifestError, match="event_groups"):
        load_manifest(path)


def test_event_metric_checks_ownership_as_well_as_pairwise_grouping():
    result = {"shots": [{"id": "s1", "number": 1}, {"id": "s2", "number": 2}],
              "narrative_events": [{"source_shot_ids": ["s1", "s2"]}]}
    assert score_event_groups([[1, 2]], result)["pairwise_f1"] == 1
    result["narrative_events"] = []
    assert not score_event_groups([[1, 2]], result)["ownership_complete"]
    assert score_event_groups([[1, 2]], result)["pairwise_f1"] == 0


def test_v2_refuses_infinite_source_ranges(tmp_path):
    path = write_manifest(tmp_path, "storyboard-eval.v2")
    payload = json.loads(path.read_text())
    payload["clips"][0]["shots"][0]["end_time"] = float("inf")
    path.write_text(json.dumps(payload))
    with pytest.raises(ManifestError, match="有限"):
        load_manifest(path)
