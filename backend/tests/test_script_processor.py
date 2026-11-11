import pytest

from app.schemas import ScriptPlan
from app.services.script_processor import (
    compact_oversplit_plan,
    extract_script_beats,
    fallback_window_plan,
    reconcile_protected_boundaries,
    script_windows,
    validate_window_plan,
)


GOLDEN_STORY = (
    "一个女人和一个肥胖中年男人在路上走路，突然身边经过一个十分健壮的穿着健身背心的肌肉男，"
    "女人的目光被走过的肌肉男吸引了，看向肌肉男流出了口水。女人身边的男人十分生气，扇了女人一个巴掌，"
    "这时女人拿出两粒棕色胶囊塞到男人嘴里，男人吃了后瞬间变成更加健壮的肌肉男形象，"
    "然后直接抱起女人走旁边的房子里关上门。"
)


def test_single_paragraph_story_extracts_actions_and_falls_back_to_five_to_seven_shots():
    beats = extract_script_beats(GOLDEN_STORY)
    plan = fallback_window_plan(beats, [beat.id for beat in beats])
    assert len(beats) == 8
    assert 5 <= len(plan.shots) <= 7
    assert [beat_id for shot in plan.shots for beat_id in shot.source_beat_ids] == [beat.id for beat in beats]
    assert all(4.0 <= shot.duration_seconds <= 5.0 for shot in plan.shots)
    assert all(1 <= len(shot.action_steps) <= 4 for shot in plan.shots)


def test_paragraphs_and_scene_headings_remain_traceable_boundaries():
    beats = extract_script_beats("内景：客厅 日\n女孩推门进入。\n\n女孩在桌边坐下。\n外景：街道 夜\n汽车驶过。")
    plan = fallback_window_plan(beats, [beat.id for beat in beats])
    assert beats[0].paragraph_hint != beats[1].paragraph_hint
    assert len({beat.scene_hint for beat in beats}) == 2
    assert beats[0].scene_label.startswith("内景") and beats[-1].scene_label.startswith("外景")
    assert [beat_id for shot in plan.shots for beat_id in shot.source_beat_ids] == [beat.id for beat in beats]


def test_dialogue_attaches_to_previous_visible_action():
    beats = extract_script_beats('女孩走到门边。\n女孩：“别走。”')
    assert len(beats) == 1
    assert "别走" in beats[0].text


def test_non_visual_thought_attaches_to_visible_action():
    beats = extract_script_beats("女孩停在门边。她想起昨晚的争吵。女孩伸手敲门。")
    assert len(beats) == 2
    assert "想起昨晚" in beats[0].text


def test_overlapping_windows_assign_every_beat_once():
    beats = extract_script_beats("。".join(f"人物完成动作{i}" for i in range(25)) + "。")
    windows = script_windows(beats, size=12, overlap=2)
    targets = [beat_id for window in windows for beat_id in window.target_beat_ids]
    assert targets == [beat.id for beat in beats]
    assert set(windows[0].target_beat_ids).isdisjoint(windows[1].target_beat_ids)


def test_rule_fallback_respects_shot_cap_without_losing_beats():
    beats = extract_script_beats("。".join(f"人物完成动作{i}" for i in range(10)) + "。")
    plan = fallback_window_plan(beats, [beat.id for beat in beats], maximum_shots=3)
    assert len(plan.shots) == 3
    assert [beat_id for shot in plan.shots for beat_id in shot.source_beat_ids] == [beat.id for beat in beats]
    assert all(len(shot.action_steps) <= 4 for shot in plan.shots)


def test_one_shot_per_beat_model_output_is_compacted_without_losing_model_details():
    beats = extract_script_beats(GOLDEN_STORY)
    individual = []
    for beat in beats:
        template = fallback_window_plan([beat], [beat.id]).shots[0]
        individual.append(template.model_copy(update={"first_frame_state": f"模型首帧 {beat.id}", "end_state": f"模型末帧 {beat.id}", "confidence": 0.8}))
    compacted = compact_oversplit_plan(ScriptPlan(shots=individual), beats, [beat.id for beat in beats])
    assert 5 <= len(compacted.shots) <= 7
    assert [beat_id for shot in compacted.shots for beat_id in shot.source_beat_ids] == [beat.id for beat in beats]
    assert compacted.shots[1].first_frame_state.startswith("模型首帧")
    assert compacted.shots[1].end_state.startswith("模型末帧")


def test_window_validation_rejects_missing_or_reordered_coverage():
    beats = extract_script_beats("人物起身。人物走到门边。")
    valid = fallback_window_plan(beats, [beat.id for beat in beats])
    validate_window_plan(valid, beats, [beat.id for beat in beats])
    broken = ScriptPlan(shots=[valid.shots[0].model_copy(update={"source_beat_ids": [beats[-1].id]})])
    with pytest.raises(ValueError, match="恰好覆盖"):
        validate_window_plan(broken, beats, [beat.id for beat in beats])


def test_window_validation_rejects_model_merge_across_rule_protected_transition():
    beats = extract_script_beats("女孩沿街行走，突然汽车从身边驶过。女孩停下回头。")
    fallback = fallback_window_plan(beats, [beat.id for beat in beats])
    overmerged = fallback.shots[0].model_copy(update={
        "source_beat_ids": [beat.id for beat in beats],
        "action_steps": [beat.text for beat in beats],
    })
    with pytest.raises(ValueError, match="保护边界"):
        validate_window_plan(ScriptPlan(shots=[overmerged]), beats, [beat.id for beat in beats])


def test_protected_boundary_reconciliation_keeps_model_camera_fields():
    beats = extract_script_beats(GOLDEN_STORY)
    fallback = fallback_window_plan(beats, [beat.id for beat in beats])
    one_model_shot = fallback.shots[0].model_copy(update={
        "source_beat_ids": [beat.id for beat in beats], "action_steps": [beat.text for beat in beats[:4]],
        "shot_size": "全景", "lens": "广角", "confidence": 0.9,
    })
    reconciled, changed = reconcile_protected_boundaries(ScriptPlan(shots=[one_model_shot]), beats, [beat.id for beat in beats])
    assert changed is True
    assert len(reconciled.shots) == len(fallback.shots)
    assert all(shot.shot_size == "全景" and shot.lens == "广角" for shot in reconciled.shots)
    assert all(shot.confidence == 0.65 for shot in reconciled.shots)
    validate_window_plan(reconciled, beats, [beat.id for beat in beats])
