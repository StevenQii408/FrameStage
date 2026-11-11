import copy
import json
import sqlite3
from dataclasses import replace

import pytest
from PIL import Image
from pydantic import ValidationError

from app.config import load_settings
from app.db import Database
from app.events import EventBus
from app.providers.bailian import BailianVisionProvider
from app.providers.base import FrameInput
from app.schemas import ActionDetails, PromptBundle, Shot, ShotContract, VisualFacts
from app.services.event_sequences import (
    derive_sequences, derive_sequences_safe, rerender_sequence_payload, sequence_payload_for_job, store_sequence_payload,
)
from app.services.job_service import JobService


def shot(number, start, duration, *, subject="男孩", initial="杯子在男孩手中", final="杯子在男孩手中", action="男孩递杯", steps=None, raw=None):
    return Shot(id=f"job-shot-{number:03d}", number=number, start_time=start, end_time=start + duration,
        duration=duration, generation_duration_seconds=duration, time=f"{start}–{start + duration}",
        status="ready", confidence=0.9, action=action, shot_size="中景", lens="标准", camera_movement="固定",
        lighting_vibe="暖光", composition="人物居中", visual_facts=VisualFacts(scene="厨房", objects=["杯子"]),
        action_details=ActionDetails(primary_subject=subject, interaction_target="杯子", initial_state=initial,
            final_state=final, steps=steps or [action]), source_raw_shot_ids=raw or [f"raw-{number:03d}"])


def relation(left, right, *, goal="把同一杯子从男孩交给女人", role="continuation", kind="连续动作", scene_change=False, eligible=True, compatible=True, confidence=0.92):
    return {"left_shot_id": left.id, "right_shot_id": right.id,
        "observed_edit_fact": "保留来源角度切换，杯子状态衔接", "relation_type": kind,
        "evidence": [f"{left.id} {left.end_time}s与{right.id} {right.start_time}s边界杯子状态"],
        "confidence": confidence, "scene_change": scene_change, "merge_eligible": eligible,
        "event_goal": goal, "boundary_role": role, "reason": "同一接力动作",
        "state_linkage": {"left_final_state": left.action_details.final_state,
            "right_initial_state": right.action_details.initial_state, "compatible": compatible,
            "evidence": ["两帧同一杯子与接触姿态匹配"]},
        "shared_subjects": [p for p in ("男孩", "女人") if p in left.action_details.primary_subject + right.action_details.primary_subject],
        "shared_prop": "杯子", "shared_location": "厨房"}


def handoff():
    a = shot(1, 0, 2.1, final="双方接触杯子", action="男孩递出杯子")
    b = shot(2, 2.1, 1.4, subject="男孩和女人的手", initial="双方接触杯子", final="女人握住杯子", action="双方的手接触杯子")
    b.shot_size = "特写"
    c = shot(3, 3.5, 2.5, subject="女人", initial="女人握住杯子", final="女人持杯，男孩松手", action="女人接稳杯子，男孩松手")
    return [a, b, c], [relation(a, b, role="action-detail"), relation(b, c)]


def test_handoff_combines_action_detail_and_subject_transfer_without_erasing_cuts():
    shots, edges = handoff()
    saved = [s.model_dump() for s in shots]
    payload = derive_sequences(shots, montage={"relations": edges})
    assert len(payload["narrative_events"]) == len(payload["generation_sequences"]) == 1
    sequence = payload["generation_sequences"][0]
    assert sequence["source_shot_ids"] == [s.id for s in shots]
    assert sequence["generation_duration_seconds"] == 6
    assert [s["transition"] for s in sequence["timeline"]] == ["initial", "cut", "cut"]
    assert sequence["timeline"][1]["shot_size"] == "特写"
    video = sequence["seedance_prompt"]["positive_prompt"]
    assert "切至特写" in video and "2–3.5 秒" in video
    assert "女人接稳杯子" in video
    first = sequence["first_frame_prompt"]["positive_prompt"]
    assert "杯子在男孩手中" in first and "女人持杯，男孩松手" not in first
    assert sequence["seedance_prompt"]["contract"]["source_edit_times"] == [2.1, 3.5]
    assert [s.model_dump() for s in shots] == saved
    assert payload["sequence_metadata"]["extra_model_calls"] == 0


@pytest.mark.parametrize("kind,role,scene_change,compatible", [
    ("反应镜头", "independent-reaction", False, True),
    ("连续动作", "independent-reaction", False, True),
    ("连续动作", "reveal", False, True),
    ("时间省略", "continuation", False, True),
    ("场景转换", "continuation", True, True),
    ("未知", "unknown", False, True),
    ("连续动作", "continuation", False, False),
])
def test_protected_narrative_boundaries_stay_separate(kind, role, scene_change, compatible):
    a, b = handoff()[0][:2]
    edge = relation(a, b, kind=kind, role=role, scene_change=scene_change, compatible=compatible)
    payload = derive_sequences([a, b], montage=[edge])
    assert [s["source_shot_ids"] for s in payload["generation_sequences"]] == [[a.id], [b.id]]


def test_pair_chain_cannot_drift_to_a_different_goal_or_prop_or_place():
    shots, edges = handoff()
    for key, value in (("event_goal", "女人喝水"), ("shared_prop", "勺子"), ("shared_location", "客厅"), ("shared_subjects", ["陌生人"])):
        modified = copy.deepcopy(edges)
        modified[1][key] = value
        payload = derive_sequences(shots, montage=modified)
        assert [s["source_shot_ids"] for s in payload["generation_sequences"]] == [[shots[0].id, shots[1].id], [shots[2].id]]


def test_low_confidence_or_missing_evidence_cannot_authorize_merging():
    a, b = handoff()[0][:2]
    for change in ({"confidence": 0.79}, {"evidence": []}, {"merge_eligible": False}, {"state_linkage": None}):
        edge = relation(a, b)
        edge.update(change)
        if change == {"state_linkage": None}:
            b.action_details.initial_state = "女人独自持杯"
        assert len(derive_sequences([a, b], montage=[edge])["generation_sequences"]) == 2


def test_relation_context_claims_are_grounded_in_saved_observations_and_meaningful_scenes():
    a, b = handoff()[0][:2]
    edge = relation(a, b)
    for key, value in (("shared_location", "客厅"), ("shared_prop", "勺子"), ("shared_subjects", ["陌生人"])):
        unsupported = copy.deepcopy(edge)
        unsupported[key] = value
        assert len(derive_sequences([a, b], montage=[unsupported])["generation_sequences"]) == 2
    a.scene_group_id, b.scene_group_id = "scene-001", "scene-002"
    assert len(derive_sequences([a, b], montage=[edge])["generation_sequences"]) == 2
    b.scene_reason = "legacy-presentation"
    assert len(derive_sequences([a, b], montage=[edge])["generation_sequences"]) == 1


def test_large_event_splits_at_real_source_boundaries_and_full_coverage_is_stable():
    shots = [shot(n, (n - 1) * 4.5, 4.5) for n in range(1, 7)]
    edges = [relation(a, b) for a, b in zip(shots, shots[1:])]
    payload = derive_sequences(shots, montage=edges)
    assert len(payload["narrative_events"]) == 1
    sequences = payload["generation_sequences"]
    assert len(sequences) == 3
    assert all(6 <= s["generation_duration_seconds"] <= 10 for s in sequences)
    assert [sid for s in sequences for sid in s["source_shot_ids"]] == [s.id for s in shots]
    assert all(len(s["source_shot_ids"]) <= 4 for s in sequences)
    assert [s["id"] for s in derive_sequences(shots, montage=edges)["generation_sequences"]] == [s["id"] for s in sequences]
    assert all(s["seedance_prompt"]["contract"]["generation_duration_seconds"] <= 12 for s in sequences)
    assert ShotContract(generation_duration_seconds=6).generation_duration_seconds == 6


def test_source_shot_and_action_step_bounds_do_not_force_artificial_temporal_bins():
    shots = [shot(n, (n - 1) * 1.0, 1.0, steps=["递出", "接触", "松手"]) for n in range(1, 7)]
    payload = derive_sequences(shots, montage=[relation(a, b) for a, b in zip(shots, shots[1:])])
    # Three micro-steps do not consume three semantic stages. Source-shot and
    # duration safeguards still apply; no fabricated equal-time bins are added.
    assert max(len(s["source_shot_ids"]) for s in payload["generation_sequences"]) == 4
    assert payload['sequence_metadata']['action_budget_unit'] == 'semantic-phases-not-source-microsteps'
    single = derive_sequences(shots[:1])["generation_sequences"][0]
    assert single["generation_duration_seconds"] == 1
    assert "0–1 秒：" in single["seedance_prompt"]["positive_prompt"]
    assert "动作：" not in single["seedance_prompt"]["positive_prompt"]


def test_failed_shot_is_singleton_and_successful_shots_never_cross_it():
    shots = [shot(n, (n - 1) * 2.0, 2.0) for n in range(1, 6)]
    shots[2].status = "failed"
    payload = derive_sequences(shots, montage=[relation(a, b) for a, b in zip(shots, shots[1:])])
    sequences = payload["generation_sequences"]
    assert [s["source_shot_ids"] for s in sequences] == [[shots[0].id, shots[1].id], [shots[2].id], [shots[3].id, shots[4].id]]
    assert sequences[1]["status"] == sequences[1]["seedance_prompt"]["status"] == "failed"
    assert "分析失败" in sequences[1]["warnings"][-1]


def test_reaction_is_not_swallowed_by_outgoing_continuation():
    shots, edges = handoff()
    edges[0].update(boundary_role='independent-reaction', relation_type='反应镜头', merge_eligible=False)
    payload = derive_sequences(shots, montage=edges)
    assert [s['source_shot_ids'] for s in payload['generation_sequences']] == [[s.id] for s in shots]
    assert all('独立反应' in d['reason'] for d in payload['sequence_metadata']['merge_decisions'])


def test_context_alias_requires_exact_saved_quote_and_correct_field():
    a, b = handoff()[0][:2]
    a.visual_facts.scene = '室内起居区域'
    b.visual_facts.scene = '只有手部特写，环境不可见'
    edge = relation(a, b)
    edge['context_evidence'] = [{'kind': 'location', 'value': '厨房', 'shot_id': a.id,
        'source_field': 'scene', 'quote': '室内起居区域'}]
    assert len(derive_sequences([a, b], montage=[edge])['generation_sequences']) == 1
    edge['context_evidence'][0].update(source_field='primary_subject', quote='男孩')
    assert len(derive_sequences([a, b], montage=[edge])['generation_sequences']) == 2


def test_partial_relations_surface_warning_without_discarding_valid_merges():
    shots, edges = handoff()
    output = derive_sequences(shots, montage={'status': 'partial', 'relations': edges[:1],
        'rejected_pairs': [[shots[1].id, shots[2].id]]})
    assert output['generation_sequences'][0]['source_shot_ids'] == [shots[0].id, shots[1].id]
    assert output['sequence_metadata']['missing_pair_count'] == 1
    assert output['sequence_metadata']['warnings']
    assert not output['sequence_metadata']['merge_decisions'][1]['merged']


def test_old_relations_require_exact_saved_state_and_context_not_keywords():
    a, b = shot(1, 0, 2), shot(2, 2, 2)
    old = {"left_shot_id": a.id, "right_shot_id": b.id, "observed_edit_fact": "同一男孩与杯子位置衔接",
        "relation_type": "连续动作", "confidence": 0.9, "evidence": ["边界帧杯子姿态匹配"]}
    assert len(derive_sequences([a, b], montage=[old])["generation_sequences"]) == 1
    b.action = "男孩喝杯中水"
    assert len(derive_sequences([a, b], montage=[old])["generation_sequences"]) == 2
    b.action = a.action
    b.action_details.initial_state = "未知"
    assert len(derive_sequences([a, b], montage=[old])["generation_sequences"]) == 2


def test_confirmed_absence_of_an_interaction_prop_does_not_forbid_continuous_action():
    a = shot(1, 0, 2, initial="男孩站在门前", final="男孩跨过门槛", action="男孩走进厨房")
    b = shot(2, 2, 2, initial="男孩跨过门槛", final="男孩走到桌旁", action="男孩走进厨房")
    a.action_details.interaction_target = b.action_details.interaction_target = "无"
    a.visual_facts.objects = b.visual_facts.objects = []
    edge = relation(a, b, goal="男孩从门口走到桌旁")
    edge["shared_prop"] = "无"
    assert len(derive_sequences([a, b], montage=[edge])["generation_sequences"]) == 1


def test_source_internal_cut_timings_remain_distinct_from_generator_schedule():
    a = shot(1, 10, 4)
    a.end_time, a.duration = 13, 3
    a.source_edit_times = [11.5]
    sequence = derive_sequences([a])["generation_sequences"][0]
    assert sequence["timeline"][0]["generation_edit_times"] == [2.0]
    assert "生成 2 秒保留机位切换" in sequence["seedance_prompt"]["positive_prompt"]
    assert "来源 11.5 秒" not in sequence["seedance_prompt"]["positive_prompt"]
    assert sequence["source_duration_seconds"] == 3
    assert sequence["generation_duration_seconds"] == 4


def test_shared_raw_lineage_keeps_continuation_unless_source_boundary_records_a_cut():
    a, b = shot(1, 0, 2, raw=["raw-1"]), shot(2, 2, 2, raw=["raw-1"])
    edge = relation(a, b)
    sequence = derive_sequences([a, b], montage=[edge])["generation_sequences"][0]
    assert sequence["timeline"][1]["transition"] == "continuation"
    b.source_edit_times = [2]
    sequence = derive_sequences([a, b], montage=[edge])["generation_sequences"][0]
    assert sequence["timeline"][1]["transition"] == "cut"
    assert sequence["seedance_prompt"]["contract"]["source_edit_times"] == [2]


def style_prompt(shot_id, style="暖色动画", style_hash="hash-a"):
    return PromptBundle(id=shot_id + "-style", shot_id=shot_id, prompt_type="first_frame", platform="image",
        positive_prompt="来源首帧", negative_prompt="", contract=ShotContract(observed_style=style,
            style_profile_hash=style_hash, style_profile_version="style-profile.v3:2", generation_duration_seconds=2))


def test_style_rerender_preserves_membership_and_source_and_protects_edits():
    shots, edges = handoff()
    payload = derive_sequences(shots, montage=edges)
    updated = rerender_sequence_payload(payload, shots, [style_prompt(s.id) for s in shots])
    seq = updated["generation_sequences"][0]
    assert seq["id"] == payload["generation_sequences"][0]["id"]
    assert seq["timeline"] == payload["generation_sequences"][0]["timeline"]
    assert seq["version"] == 2 and seq["first_frame_prompt"]["version"] == 2
    assert "暖色动画" in seq["first_frame_prompt"]["positive_prompt"]
    assert "暖色动画" in seq["seedance_prompt"]["positive_prompt"]
    assert seq["seedance_prompt"]["contract"]["style_profile_hash"] == "hash-a"
    edited = copy.deepcopy(payload)
    edited["generation_sequences"][0]["seedance_prompt"]["positive_prompt"] = "手工编辑"
    with pytest.raises(ValueError, match="手动修改"):
        rerender_sequence_payload(edited, shots, [])


def test_saved_action_presentation_is_used_instead_of_replaying_micro_steps():
    a = shot(1, 0, 2, steps=["伸手", "手掌接触杯壁", "手指握住杯子"], action="男孩端起杯子")
    prompt = style_prompt(a.id)
    prompt.contract.delivery["action_presentation"] = {"main_action": "男孩顺势端起杯子"}
    video = derive_sequences([a], prompts=[prompt])["generation_sequences"][0]["seedance_prompt"]["positive_prompt"]
    assert "男孩顺势端起杯子" in video
    assert "手掌接触杯壁" not in video


def test_structured_scene_style_survives_single_source_sequence_without_changing_global():
    a = shot(1, 0, 2)
    prompt = style_prompt(a.id)
    prompt.contract.delivery["style_layers"] = {"global": "暖色动画", "scene": "本场景视觉变化：窗侧冷色边缘光。",
        "local_effect": "本镜局部效果：杯沿微光。", "avoid": "", "recipe_constraint": ""}
    sequence = derive_sequences([a], prompts=[prompt])["generation_sequences"][0]
    for key in ("first_frame_prompt", "seedance_prompt"):
        assert sequence[key]["positive_prompt"].count("本场景视觉变化：窗侧冷色边缘光。") == 1
        assert sequence[key]["contract"]["observed_style"] == "暖色动画"
    assert "杯沿微光" not in sequence["first_frame_prompt"]["positive_prompt"]
    assert sequence["seedance_prompt"]["positive_prompt"].count("杯沿微光") == 1


def test_same_style_hash_scene_is_rendered_once_and_later_local_effect_stays_in_its_segment():
    shots, edges = handoff()
    prompts = [style_prompt(s.id) for s in shots]
    for p in prompts:
        p.contract.delivery["style_layers"] = {"global": "暖色动画", "scene": "本场景视觉变化：厨房窗侧暖光。",
            "local_effect": "", "avoid": "", "recipe_constraint": ""}
    prompts[1].contract.delivery["style_layers"]["local_effect"] = "本镜局部效果：交接瞬间杯沿粉色光点。"
    sequence = derive_sequences(shots, prompts=prompts, montage=edges)["generation_sequences"][0]
    first = sequence["first_frame_prompt"]["positive_prompt"]
    video = sequence["seedance_prompt"]["positive_prompt"]
    assert "粉色光点" not in first
    assert first.count("厨房窗侧暖光") == video.count("厨房窗侧暖光") == 1
    assert video.count("粉色光点") == 1
    assert video.index("2–3.5 秒") < video.index("粉色光点") < video.index("3.5–6 秒")
    assert "此段画面要求（job-shot-002）" in video
    assert "此段画面要求（job-shot-001）" not in video and "此段画面要求（job-shot-003）" not in video
    assert sequence["seedance_prompt"]["contract"]["observed_style"] == "暖色动画"


@pytest.mark.parametrize("effect_prefix", ["本镜局部效果：", "局部视觉效果："])
def test_legacy_delivery_owned_style_lines_are_recovered_once_and_scoped(effect_prefix):
    shots, edges = handoff()
    prompts = [style_prompt(s.id) for s in shots]
    scene = "本场景视觉变化：厨房晨间柔光。"
    effect = effect_prefix + "杯中粉色光点。"
    for p in prompts:
        p.contract.delivery["first_frame_prompt"] = "【首帧图片提示词】\n暖色动画\n" + scene
        p.contract.delivery["seedance_video_prompt"] = "【Seedance 中文图生视频提示词】\n暖色动画\n" + scene
    prompts[1].contract.delivery["seedance_video_prompt"] += "\n" + effect + "\n" + effect
    sequence = derive_sequences(shots, prompts=prompts, montage=edges)["generation_sequences"][0]
    video = sequence["seedance_prompt"]["positive_prompt"]
    assert video.count(scene) == 1 and video.count(effect) == 1
    assert effect not in sequence["first_frame_prompt"]["positive_prompt"]
    assert video.index("2–3.5 秒") < video.index(effect) < video.index("3.5–6 秒")


def test_different_member_scene_style_is_not_copied_to_other_members_or_the_initial_frame():
    shots, edges = handoff()
    prompts = [style_prompt(s.id) for s in shots]
    for p in prompts:
        p.contract.delivery["style_layers"] = {"global": "暖色动画", "scene": "", "local_effect": ""}
    prompts[1].contract.delivery["style_layers"]["scene"] = "本场景视觉变化：中间特写的杯沿反光。"
    sequence = derive_sequences(shots, prompts=prompts, montage=edges)["generation_sequences"][0]
    video = sequence["seedance_prompt"]["positive_prompt"]
    assert "杯沿反光" not in sequence["first_frame_prompt"]["positive_prompt"]
    assert video.count("杯沿反光") == 1
    assert video.index("2–3.5 秒") < video.index("杯沿反光") < video.index("3.5–6 秒")


def test_structured_shot_style_uses_first_member_in_still_and_actual_member_in_video():
    shots, edges = handoff()
    prompts = [style_prompt(s.id) for s in shots]
    for p in prompts:
        p.contract.delivery["style_layers"] = {"global": "暖色动画", "scene": "", "shot": "", "local_effect": ""}
    prompts[0].contract.delivery["style_layers"]["shot"] = "本镜画面特征：人物轮廓柔和。"
    prompts[1].contract.delivery["style_layers"]["shot"] = "本镜画面特征：交接手部边缘明亮。"
    sequence = derive_sequences(shots, prompts=prompts, montage=edges)["generation_sequences"][0]
    first = sequence["first_frame_prompt"]["positive_prompt"]
    video = sequence["seedance_prompt"]["positive_prompt"]
    assert "人物轮廓柔和" in first and "交接手部边缘明亮" not in first
    assert video.count("人物轮廓柔和") == video.count("交接手部边缘明亮") == 1
    assert video.index("0–2 秒") < video.index("人物轮廓柔和") < video.index("2–3.5 秒")
    assert video.index("2–3.5 秒") < video.index("交接手部边缘明亮") < video.index("3.5–6 秒")


def test_causal_handoff_receiving_and_drinking_can_share_goal_while_reaction_stays_independent():
    shots, edges = handoff()
    goal = "让女人接过并饮用男孩递来的水"
    for edge in edges:
        edge["event_goal"] = goal
    drink = shot(4, 6, 2, subject="女人", initial=shots[-1].action_details.final_state,
        final="女人饮下杯中的水", action="女人举杯饮水")
    reaction = shot(5, 8, 2, subject="女人", initial=drink.action_details.final_state,
        final="女人骤然皱眉", action="女人喝下后异常反应")
    edges.extend([relation(shots[-1], drink, goal=goal), relation(drink, reaction, goal=goal, role="independent-reaction")])
    # The existing event has both participants; later woman-only coverage is a subset.
    payload = derive_sequences([*shots, drink, reaction], montage=edges)
    assert [s["source_shot_ids"] for s in payload["generation_sequences"]] == [[s.id for s in [*shots, drink]], [reaction.id]]


def test_inconsistent_styles_and_missing_legacy_facts_degrade_preview_without_a_result_error():
    shots, edges = handoff()
    prompts = [style_prompt(s.id, style_hash=f"different-{n}") for n, s in enumerate(shots)]
    payload = derive_sequences_safe(shots, prompts=prompts, montage=edges)
    assert len(payload["generation_sequences"]) == 3
    assert payload["sequence_metadata"]["degraded"] is True
    assert all(s["status"] == "warning" for s in payload["generation_sequences"])
    legacy = Shot(id="legacy", number=1, start_time=0, end_time=20, duration=20, time="0–20", status="ready")
    pending = derive_sequences_safe([legacy])
    assert pending["generation_sequences"] == []
    assert pending["sequence_metadata"]["unplanned_source_shot_ids"] == ["legacy"]
    assert "未压缩或截断" in " ".join(pending["sequence_metadata"]["warnings"])
    assert legacy.duration == 20


def service_with_saved_job(tmp_path):
    cfg = replace(load_settings(), provider="mock", data_dir=tmp_path / "data")
    db = Database(tmp_path / "jobs.sqlite3")
    db.init()
    service = JobService(cfg, db, EventBus())
    db.insert_job({"id": "job", "status": "completed", "stage": "completed", "progress": 100,
        "config": service.config().model_dump(), "created_at": "now", "updated_at": "unchanged"})
    shots, edges = handoff()
    for s in shots:
        observation = {"subject_action": s.action, "shot_size": s.shot_size, "lens": s.lens,
            "camera_movement": s.camera_movement, "lighting_vibe": s.lighting_vibe, "confidence": s.confidence,
            "action_details": s.action_details.model_dump(), "visual_facts": s.visual_facts.model_dump()}
        db.execute("INSERT INTO shots(id,job_id,number,start_time,end_time,duration,time,status,observation_json,source_raw_shot_ids_json,generation_duration_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (s.id, "job", s.number, s.start_time, s.end_time, s.duration, s.time, s.status, json.dumps(observation), json.dumps(s.source_raw_shot_ids), s.duration))
    db.update_job("job", montage_json=json.dumps({"relations": edges}), generation_plan_json='{"version":"generation-plan.v1","sentinel":"frozen"}')
    return service


def test_old_completed_result_and_sequence_preview_are_read_only_and_explicit_rebuild_is_scoped(tmp_path, monkeypatch):
    service = service_with_saved_job(tmp_path)
    monkeypatch.setattr(service.provider, "analyze_shot", lambda *args: pytest.fail("preview must not call model"))
    before = dict(service.db.fetchone("SELECT * FROM jobs WHERE id='job'"))
    result = service.result("job")
    assert result.sequence_metadata["mode"] == "preview"
    assert len(result.generation_sequences) == 1
    payload = sequence_payload_for_job(service, "job")
    assert payload["sequence_metadata"]["mode"] == "preview"
    assert dict(service.db.fetchone("SELECT * FROM jobs WHERE id='job'")) == before
    assert service.db.fetchall("SELECT * FROM event_sequence_revisions") == []
    rebuilt = sequence_payload_for_job(service, "job", persist=True, expected_version=0)
    assert rebuilt["sequence_metadata"]["mode"] == "persisted"
    assert service.result("job").sequence_metadata["mode"] == "persisted"
    assert service.db.fetchone("SELECT generation_plan_json FROM jobs WHERE id='job'")[0] == before["generation_plan_json"]
    assert len(service.db.events_since("job")) == 1
    with pytest.raises(ValueError, match="版本冲突"):
        sequence_payload_for_job(service, "job", persist=True, expected_version=0)
    second = sequence_payload_for_job(service, "job", persist=True, expected_version=1)
    assert second["sequence_metadata"]["version_number"] == 2
    archived = service.db.fetchone("SELECT payload_json FROM event_sequence_revisions WHERE job_id='job' AND version=1")
    assert json.loads(archived[0]) == rebuilt
    service.executor.shutdown(wait=True)


def test_sequence_style_transaction_can_roll_back_archive_and_source_update(tmp_path):
    service = service_with_saved_job(tmp_path)
    original = sequence_payload_for_job(service, "job", persist=True)
    with pytest.raises(RuntimeError):
        with service.db.connect() as conn:
            store_sequence_payload(conn, "job", original, expected_version=1)
            conn.execute("UPDATE shots SET status='failed' WHERE job_id='job'")
            raise RuntimeError("style transaction failed")
    assert json.loads(service.db.fetchone("SELECT event_sequence_json FROM jobs WHERE id='job'")[0]) == original
    assert service.db.fetchall("SELECT * FROM event_sequence_revisions") == []
    assert all(row[0] == "ready" for row in service.db.fetchall("SELECT status FROM shots WHERE job_id='job'"))
    service.executor.shutdown(wait=True)


def test_optional_derived_failure_emits_warning_without_failing_completed_source_job(tmp_path, monkeypatch):
    service = service_with_saved_job(tmp_path)
    def explode(*args, **kwargs):
        raise ValueError("optional sequence renderer failed")
    monkeypatch.setattr("app.services.sequence_orchestration.derive_sequences_safe", explode)
    service._build_event_sequences("job")
    assert service.status("job")["status"] == "completed"
    assert service.db.events_since("job")[-1]["type"] == "job.warning"
    service.executor.shutdown(wait=True)


@pytest.mark.parametrize("path", ["explicit-rebuild", "new-job-worker"])
def test_first_sequence_persistence_locks_before_reading_and_rendering_source_prompts(tmp_path, monkeypatch, path):
    service = service_with_saved_job(tmp_path)
    original_result = service.result
    reads = []
    def locked_result(job_id):
        with sqlite3.connect(str(service.db.path), timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        reads.append(job_id)
        return original_result(job_id)
    monkeypatch.setattr(service, "result", locked_result)
    if path == "explicit-rebuild":
        result = sequence_payload_for_job(service, "job", persist=True, expected_version=0)
    else:
        service._build_event_sequences("job")
        result = json.loads(service.status("job")["event_sequence_json"])
    assert reads == ["job"]
    assert result["sequence_metadata"]["version_number"] == 1
    service.executor.shutdown(wait=True)


def test_sequence_preview_does_not_hold_a_database_write_lock(tmp_path, monkeypatch):
    service = service_with_saved_job(tmp_path)
    original_result = service.result
    def unlocked_result(job_id):
        with sqlite3.connect(str(service.db.path), timeout=0) as other:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        return original_result(job_id)
    monkeypatch.setattr(service, "result", unlocked_result)
    result = sequence_payload_for_job(service, "job", persist=False)
    assert result["sequence_metadata"]["mode"] == "preview"
    assert service.status("job")["event_sequence_json"] is None
    service.executor.shutdown(wait=True)


def test_montage_extensions_and_failure_gaps_reuse_original_bounded_calls(tmp_path, monkeypatch):
    cfg = replace(load_settings(), provider="bailian", cloud_api_key="test-only", perception_v2_enabled=False)
    provider = BailianVisionProvider(cfg)
    shots = [{"id": f"s{n}", "number": n, "start_time": n - 1, "end_time": n,
        "observation": {}, "status": "failed" if n == 3 else "ready"} for n in range(1, 11)]
    image_path = tmp_path / "frame.jpg"
    Image.new("RGB", (32, 32)).save(image_path)
    frames = {s["id"]: [FrameInput(s["id"] + "-frame", s["start_time"], str(image_path))] for s in shots}
    monkeypatch.setattr(provider.helper, "_contact_sheets", lambda *_args: [image_path])
    calls = []
    def call(prompt, inputs, phase, **kwargs):
        calls.append((prompt, phase, kwargs))
        pairs = json.loads(prompt.split("指定相邻镜头对：", 1)[1].split("\n", 1)[0])
        return json.dumps({"relations": [{"left_shot_id": a, "right_shot_id": b,
            "observed_edit_fact": "可见边界状态不确定", "relation_type": "未知", "confidence": 0.5,
            "evidence": [], "merge_eligible": False, "event_goal": "", "boundary_role": "unknown"} for a, b in pairs]})
    monkeypatch.setattr(provider, "_call", call)
    output = provider.analyze_montage(shots, frames)
    assert len(calls) == 2  # same ceil((10-1)/7) windows as the original path
    assert all(c[2]["max_tokens"] == 4096 for c in calls)
    pairs = [(r["left_shot_id"], r["right_shot_id"]) for r in output["relations"]]
    assert ("s2", "s3") not in pairs and ("s3", "s4") not in pairs and ("s2", "s4") not in pairs
    assert pairs == [("s1", "s2"), ("s4", "s5"), ("s5", "s6"), ("s6", "s7"), ("s7", "s8"), ("s8", "s9"), ("s9", "s10")]
    assert all("state_linkage" in c[0] and "independent-reaction" in c[0] for c in calls)
    provider.unload()


def test_montage_compacts_redundant_evidence_but_keeps_last_saved_endpoints_in_valid_json(tmp_path, monkeypatch):
    cfg = replace(load_settings(), provider="bailian", cloud_api_key="test-only", perception_v2_enabled=False)
    provider = BailianVisionProvider(cfg)
    last_initial, last_final = "最后一镜完整初始状态" * 150, "最后一镜完整结束状态" * 150
    shots = [{"id": f"s{n}", "number": n, "start_time": n - 1, "end_time": n, "observation": {
        "subject_action": "连续动作", "shot_size": "中景", "camera_movement": "固定",
        "visual_style_evidence": {"evidence": ["不需重复的画风证据" * 4000]},
        "spatial_evidence": {"relationships": "不需重复的空间说明" * 4000},
        "visual_facts": {"scene": "厨房", "objects": ["杯子"], "people": ["男孩", "女人"]},
        "action_details": {"primary_subject": "女人", "initial_state": last_initial if n == 8 else "杯子在手中",
            "final_state": last_final if n == 8 else "杯子在手中", "interaction_target": "杯子",
            "steps": ["握住杯子"], "evidence": ["不需重复的动作证据" * 4000], "action_phases": [{"evidence": "巨大冗余" * 4000}]},
    }} for n in range(1, 9)]
    path = tmp_path / "frame.jpg"
    Image.new("RGB", (32, 32)).save(path)
    frames = {s["id"]: [FrameInput(s["id"] + "-frame", s["start_time"], str(path))] for s in shots}
    monkeypatch.setattr(provider.helper, "_contact_sheets", lambda *_args: [path])
    calls = []
    def call(prompt, inputs, phase, **kwargs):
        calls.append(prompt)
        saved = json.loads(prompt.split("逐镜观察：", 1)[1])
        assert len(saved) == 8
        assert saved[-1]["observation"]["action_details"]["initial_state"] == last_initial
        assert saved[-1]["observation"]["action_details"]["final_state"] == last_final
        assert "visual_style_evidence" not in saved[0]["observation"]
        assert "action_phases" not in saved[0]["observation"]["action_details"]
        pairs = json.loads(prompt.split("指定相邻镜头对：", 1)[1].split("\n", 1)[0])
        return json.dumps({"relations": [{"left_shot_id": a, "right_shot_id": b,
            "observed_edit_fact": "边界状态未知", "relation_type": "未知", "confidence": 0.5} for a, b in pairs]})
    monkeypatch.setattr(provider, "_call", call)
    provider.analyze_montage(shots, frames)
    assert len(calls) == 1
    assert "递给、接过特写、继而饮用可服务同一明确目标" in calls[0]
    assert "独立情绪/线索揭示必须另立事件" in calls[0]
    provider.unload()


def test_montage_repairs_wire_types_and_keeps_valid_pairs_when_one_item_fails(tmp_path, monkeypatch):
    provider = BailianVisionProvider(replace(load_settings(), cloud_api_key='test-only'))
    path = tmp_path / 'frame.jpg'
    Image.new('RGB', (32, 32)).save(path)
    shots = [{'id': f's{n}', 'number': n, 'start_time': n - 1, 'end_time': n,
        'observation': {}, 'status': 'ready'} for n in range(1, 4)]
    frames = {s['id']: [FrameInput(s['id'] + '-frame', s['start_time'], path)] for s in shots}
    monkeypatch.setattr(provider.helper, '_contact_sheets', lambda *_: [path])
    valid = {'left_shot_id': 's1', 'right_shot_id': 's2', 'observed_edit_fact': '画面状态不明',
        'relation_type': '未知', 'confidence': .4, 'evidence': 's1结尾和s2开头', 'event_goal': None,
        'shared_prop': ['玻璃杯'], 'state_linkage': {'left_final_state': '', 'right_initial_state': '',
            'compatible': False, 'evidence': '无法判断边界'}}
    invalid = {**valid, 'left_shot_id': 's2', 'right_shot_id': 's3', 'confidence': '不可解析'}
    calls = []
    def call(prompt, inputs, phase, **options):
        calls.append((phase, options))
        return json.dumps({'relations': [valid, invalid]} if len(calls) == 1 else {'relations': [invalid]})
    monkeypatch.setattr(provider, '_call', call)
    output = provider.analyze_montage(shots, frames)
    assert [r['left_shot_id'] for r in output['relations']] == ['s1']
    saved = output['relations'][0]
    assert saved['evidence'] == ['s1结尾和s2开头'] and saved['event_goal'] == '' and saved['shared_prop'] == '玻璃杯'
    assert saved['state_linkage']['evidence'] == ['无法判断边界']
    assert output['status'] == 'partial' and output['rejected_pairs'] == [('s2', 's3')]
    assert len(output['raw_responses']) == len(calls) == 2
    assert all(options['max_attempts'] == 1 for _, options in calls)
    provider.unload()
