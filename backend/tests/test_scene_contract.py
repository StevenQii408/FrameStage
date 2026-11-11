from app.schemas import ActionDetails, Shot
from app.services.scene_contract import build_action_atoms, build_scene_groups, validate_action_coverage
from app.services.video_processor import DetectedShot, RawDetectedShot, assign_scene_groups, split_long_shots


def test_long_video_source_keeps_original_time_without_implicit_cuts():
    segments = split_long_shots([DetectedShot(1, 0.0, 12.2, [1], "scene-001")])
    assert len(segments) == 1
    assert [(item.start_time, item.end_time) for item in segments] == [(0.0, 12.2)]
    assert all(item.source_raw_numbers == [1] for item in segments)


def test_scene_grouping_can_keep_a_hard_cut_but_breaks_at_fade():
    shots = [DetectedShot(1, 0, 2, [1]), DetectedShot(2, 2, 4, [2]), DetectedShot(3, 4, 6, [3])]
    raw = [
        RawDetectedShot(1, 0, 2, {"protected": True, "visual_similarity": .55, "jump_ratio": 1.8}),
        RawDetectedShot(2, 2, 4, {"protected": True, "fade_or_black": True}),
        RawDetectedShot(3, 4, 6),
    ]
    assign_scene_groups(shots, raw)
    assert [shot.scene_group_id for shot in shots] == ["scene-001", "scene-001", "scene-002"]


def test_atom_coverage_and_scene_contract_keep_every_ordered_step():
    shot = Shot(
        id="shot-1", number=1, start_time=0, end_time=4.5, duration=4.5, time="0 – 4.5",
        scene_group_id="scene-001", frame_ids=["f1", "f2", "f3", "f4"],
        action="女孩握住门把，推门进入", confidence=.8,
        action_details=ActionDetails(primary_subject="女孩", initial_state="站在门外", steps=["握住门把", "推门进入"], final_state="站在室内"),
    )
    atoms = build_action_atoms([shot])
    assert [atom.action for atom in atoms] == ["握住门把；推门进入"]
    assert [atom.generation_shot_id for atom in atoms] == ["shot-1"]
    assert all(atom.frame_ids == ["f1", "f2", "f3", "f4"] for atom in atoms)
    validate_action_coverage([shot], atoms)
    groups = build_scene_groups([shot])
    assert groups[0].shot_ids == ["shot-1"]
    assert groups[0].cover_frame_ids == ["f1"]
