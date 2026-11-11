from app.config import load_settings
from app.services.video_processor import DetectedShot, RawDetectedShot, _sample_times, apply_semantic_groups, consolidate_scenes


def test_four_frame_sampling_uses_explicit_timeline_percentages():
    assert _sample_times(DetectedShot(1, 10.0, 20.0), 4) == [11.0, 13.5, 16.5, 19.0]


def test_narrative_segmentation_merges_only_adjacent_unprotected_raw_shots(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    raw = [
        RawDetectedShot(1, 0, 1, {"protected": False, "visual_similarity": .90, "jump_ratio": .8}),
        RawDetectedShot(2, 1, 2, {"protected": True, "visual_similarity": .95, "jump_ratio": .7}),
        RawDetectedShot(3, 2, 3, {"protected": False, "visual_similarity": .88, "jump_ratio": .8}),
        RawDetectedShot(4, 3, 4, {}),
    ]
    narrative = consolidate_scenes(raw, load_settings())

    assert [shot.source_raw_numbers for shot in narrative] == [[1, 2], [3, 4]]
    assert [(shot.start_time, shot.end_time) for shot in narrative] == [(0, 2), (2, 4)]
    assert [item.narrative_number for item in raw] == [1, 1, 2, 2]
    assert raw[1].decision == "keep"
    assert "保护边界" in raw[1].reason


def test_narrative_segmentation_enforces_four_raw_shot_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    raw = [RawDetectedShot(index + 1, index, index + .5, {"protected": False, "visual_similarity": .95, "jump_ratio": .7}) for index in range(6)]
    narrative = consolidate_scenes(raw, load_settings())
    assert max(len(shot.source_raw_numbers) for shot in narrative) <= 4
    assert [number for shot in narrative for number in shot.source_raw_numbers] == list(range(1, 7))


def test_semantic_grouping_never_crosses_a_protected_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    raw = [
        RawDetectedShot(1, 0, 1, {"protected": True}),
        RawDetectedShot(2, 1, 2, {"protected": False}),
        RawDetectedShot(3, 2, 3, {}),
    ]
    narrative = apply_semantic_groups(raw, [[1, 2], [3]], load_settings())
    assert [shot.source_raw_numbers for shot in narrative] == [[1], [2], [3]]
