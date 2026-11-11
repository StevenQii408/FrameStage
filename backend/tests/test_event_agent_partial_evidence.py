"""Offline regressions for the nine-source real failure, never touch its DB."""
import copy
import json

import pytest

from app.event_agent import EventAgentConfig, EventProposal, build_event_windows, source_has_saved_observations, validate_event_proposals
from app.perception_models import MotionObservationV2, StaticObservationV2, empty_stage, merge_stages
from app.schemas import ActionDetails, VisualFacts
from test_event_agent import Provider, event, final, frame, ref, run, source


def unknown(shot):
    shot.status = "warning"  # The original failed requests were projected as warning.
    shot.action = "未知"
    shot.source_text = ""
    shot.action_details = ActionDetails()
    shot.visual_facts = VisualFacts()
    shot.visual_observation_v2 = merge_stages(None, None)
    shot.analysis_warnings = ["静态网络连接超时", "动态网络连接超时"]
    return shot


def nine_sources():
    # Synthetic sequence with a missing first observation. The first
    # observation failed; 2..8 are a cup handoff/drinking with attached feedback;
    # 9 is a changed, reclining state, not evidence of poisoning or causality.
    actions = ["未知", "卷发女性看着前景中递出的橙色液体，面部表情从微张嘴唇变为微笑",
               "卷发男孩双手捧着装有粉色液体的玻璃杯，面部表情从惊讶转为微笑。",
               "小孩双手捧着装有粉色液体的玻璃杯，另一双较大的手从右侧伸出并握住杯子",
               "卷发女性双手持杯与男孩交谈，随后举起杯子饮用杯中粉色液体",
               "卷发男孩面带微笑，头部轻微晃动",
               "卷发女性双手持杯，闭眼撅嘴做出吹气或品尝动作，随后睁眼微笑",
               "卷发女性举起玻璃杯饮用杯中液体，随后放下杯子",
               "躺卧的卷发女性角色面部泛红且带有汗珠，头部枕在靠垫上，嘴巴进行张合动作并伴有眨眼。"]
    shots = [source(index, (index - 1) * 1.4, 1.4, action=action)
             for index, action in enumerate(actions, 1)]
    unknown(shots[0])
    for shot in shots:
        shot.scene_reason = "剧情时空与事件连续"
    shots[0].scene_reason = "视觉模型判断剧情时空与事件连续"
    shots[-1].scene_group_id = "scene-2"
    shots[-1].scene_reason = "视觉模型判断剧情时空或连续事件变化"
    return shots, [frame(shot) for shot in shots]


def test_real_nine_source_structure_isolates_unknown_but_merges_seven_valid_sources():
    shots, frames = nine_sources()
    before = copy.deepcopy([shot.model_dump() for shot in shots])
    proposal = event(shots[1:8], frames[1:8])
    for index in (0, 1, 4):
        proposal["causal_facts"][index]["kind"] = "reaction"
    # The actual response used three semantic stages rather than one shot per
    # action. Local repair must not re-bin or rewrite the timing/durations.
    proposal["stages"] = [
        {"id": "offer", "source_shot_ids": [shot.id for shot in shots[1:3]],
         "fact_ids": ["fact-1", "fact-2"], "duration_seconds": 3.866667, "transition": "initial"},
        {"id": "handoff", "source_shot_ids": [shot.id for shot in shots[3:5]],
         "fact_ids": ["fact-3", "fact-4"], "duration_seconds": 2.733333, "transition": "continuation"},
        {"id": "drink-feedback", "source_shot_ids": [shot.id for shot in shots[5:8]],
         "fact_ids": ["fact-5", "fact-6", "fact-7"], "duration_seconds": 4.633333, "transition": "continuation"},
    ]
    reveal = event(shots[8:], frames[8:], duration=1.2, mode="internal_cuts", dispositions={shots[8].id: "preserve"})
    reveal["goal"] = "展示已观察到的躺卧状态"
    reveal["causal_facts"][0]["kind"] = "reveal"
    reveal["stages"][0]["transition"] = "cut"  # Actual second hidden error.
    reveal["scene_continuity"] = "scene_change"
    reveal["scene_evidence"] = [ref(frames[7], "饮用后仍坐立"), ref(frames[8], "躺卧在靠垫上")]
    provider = Provider([final(proposal, reveal)])
    result = run(shots, frames, provider)
    assert result.status == "degraded"  # Only the genuinely unknown source.
    assert [saved.status for saved in result.events] == ["fallback", "ready", "ready"]
    assert [saved.source_shot_ids for saved in result.events] == [
        [shots[0].id], [shot.id for shot in shots[1:8]], [shots[8].id]]
    assert len(provider.calls) == 1 and not result.audit[0].validation_errors
    assert result.events[1].stages[1].duration_seconds == 2.733333
    assert result.events[2].stages[0].transition == "initial"
    assert result.events[2].scene_continuity == "scene_change"
    assert result.raw_responses[0]["local_repairs"][0]["from"] == "cut"
    text = "\n".join(clip.seedance_prompt.positive_prompt for clip in result.generation_clips)
    assert all(shot.action in text for shot in shots[1:]) and "中毒" not in text
    assert not result.events[0].causal_facts
    payload = json.loads(provider.calls[0][0].split("\n输入：\n", 1)[1])
    assert payload["owned_source_shot_ids"] == [shot.id for shot in shots[1:]]
    assert payload["system_fallback_source_shot_ids"] == [shots[0].id]
    assert [item.source_shot_id for item in result.source_coverage] == [shot.id for shot in shots]
    assert [shot.model_dump() for shot in shots] == before


def test_live_validated_handoff_and_drinking_does_not_need_repair_for_noncritical_reaction_markers():
    shots, frames = nine_sources()
    restored = source(1, 0, 1.4, action="条纹衫男孩双手捧着装有粉色液体的玻璃杯，将其向前递向卷发女性")
    shots[0] = restored
    proposal = event(shots[:8], frames[:8])
    for index in (2, 5, 6):
        proposal["causal_facts"][index].update(kind="reaction", critical=False)
    proposal["stages"] = [
        {"id": "handoff", "source_shot_ids": [shot.id for shot in shots[:4]],
         "fact_ids": [f"fact-{i}" for i in range(1, 5)], "duration_seconds": 4.0, "transition": "initial"},
        {"id": "drinking-feedback", "source_shot_ids": [shot.id for shot in shots[4:8]],
         "fact_ids": [f"fact-{i}" for i in range(5, 9)], "duration_seconds": 4.0, "transition": "continuation"},
    ]
    reveal = event(shots[8:], frames[8:], duration=1.2)
    reveal["goal"] = "展示躺卧状态，不推测原因"
    raw = final(proposal, reveal)
    provider = Provider([raw])
    result = run(shots, frames, provider)
    assert result.status == "ready" and len(provider.calls) == 1 and len(result.events) == 2
    assert result.raw_responses[0]["response"] == raw
    assert len(result.raw_responses[0]["local_repairs"]) == 3
    assert all(fact.critical for fact in result.events[0].causal_facts)
    assert [stage.duration_seconds for stage in result.events[0].stages] == [4.0, 4.0]
    assert "中毒" not in result.events[0].goal


@pytest.mark.parametrize("mutation", ["invented", "no-stage", "partial-source-stage", "omit", "bad-frame"])
def test_safe_marker_upgrade_cannot_authorize_missing_actions_or_invalid_evidence(mutation):
    shots = [source(1, 0, action="男孩微笑"), source(2, 2, action="女子饮用")]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames)
    proposal["causal_facts"][0].update(kind="reaction", critical=False)
    if mutation == "invented":
        proposal["causal_facts"][0]["text"] = "男孩因女子中毒而微笑"
    elif mutation == "no-stage":
        proposal["stages"][0]["fact_ids"].remove("fact-1")
    elif mutation == "partial-source-stage":
        proposal["causal_facts"][0]["source_shot_ids"].append(shots[1].id)
        proposal["causal_facts"][0]["evidence"].append(ref(frames[1], "另一来源"))
        proposal["stages"] = [
            {"id": "first", "source_shot_ids": [shots[0].id], "fact_ids": ["fact-1"],
             "duration_seconds": 2.0, "transition": "initial"},
            {"id": "next", "source_shot_ids": [shots[1].id], "fact_ids": ["fact-2"],
             "duration_seconds": 2.0, "transition": "continuation"}]
    elif mutation == "omit":
        proposal["presentation"][0]["disposition"] = "omit"
    else:
        proposal["causal_facts"][0]["evidence"][0]["frame_id"] = "missing-frame"
    result = run(shots, frames, Provider([final(proposal), final(proposal)]))
    assert result.status == "degraded" and all(saved.status == "fallback" for saved in result.events)


def test_all_unknown_sources_make_zero_model_calls_even_when_frames_exist():
    shots = [unknown(source(index, index - 1, 1.0)) for index in range(1, 4)]
    provider = Provider([])
    result = run(shots, [frame(shot) for shot in shots], provider)
    assert not provider.calls and result.usage_summary["model_calls"] == 0
    assert all(saved.status == "fallback" and not saved.causal_facts for saved in result.events)
    assert [item.source_shot_id for item in result.source_coverage] == [shot.id for shot in shots]


def test_unknown_source_between_valid_sources_cannot_be_bridged():
    shots = [source(index, index - 1, 1.0) for index in range(1, 4)]
    unknown(shots[1])
    frames = [frame(shot) for shot in shots]
    unsafe = event([shots[0], shots[2]], [frames[0], frames[2]])
    provider = Provider([final(unsafe), final(unsafe)])
    result = run(shots, frames, provider)
    assert all(saved.status == "fallback" for saved in result.events)
    assert "cannot bridge independently retained unknown" in " ".join(result.audit[0].validation_errors)
    assert [saved.source_shot_ids for saved in result.events] == [[shot.id] for shot in shots]


def test_invalid_local_event_does_not_discard_independent_verified_event_after_one_repair():
    shots = [source(index, index - 1, 1.0) for index in range(1, 4)]
    frames = [frame(shot) for shot in shots]
    good = event(shots[:2], frames[:2])
    bad = event(shots[2:], frames[2:])
    bad["causal_facts"][0]["text"] = "女子喝水后中毒"
    provider = Provider([final(good, bad), final(good, bad)])
    result = run(shots, frames, provider)
    assert len(provider.calls) == 2
    assert [saved.status for saved in result.events] == ["ready", "fallback"]
    assert result.events[0].source_shot_ids == [shot.id for shot in shots[:2]]
    assert result.events[1].causal_facts[0].text == shots[2].action
    assert "中毒" not in "\n".join(clip.seedance_prompt.positive_prompt for clip in result.generation_clips)


@pytest.mark.parametrize("repair", ["truncated-json", "foreign-ownership"])
def test_bad_repair_preserves_independently_verified_initial_events(repair):
    shots = [source(index, index - 1, 1.0) for index in range(1, 4)]
    frames = [frame(shot) for shot in shots]
    good, bad = event(shots[:2], frames[:2]), event(shots[2:], frames[2:])
    bad["causal_facts"][0]["text"] = "未观察到的原因"
    if repair == "truncated-json":
        repair_response = final(good, bad)[:-25]
    else:
        wrong = copy.deepcopy(good)
        wrong["source_shot_ids"][0] = "not-owned-source"
        repair_response = final(wrong, bad)
    result = run(shots, frames, Provider([final(good, bad), repair_response]))
    assert result.status == "degraded" and [saved.status for saved in result.events] == ["ready", "fallback"]
    assert result.events[0].source_shot_ids == [shots[0].id, shots[1].id]
    assert result.usage_summary["model_calls"] == 2


def test_semantic_validator_still_rejects_unprotected_noncritical_reaction_without_safe_normalization():
    shots = [source(1, 0, action="男孩微笑")]
    frames = [frame(shots[0])]
    proposal = event(shots, frames)
    proposal["causal_facts"][0].update(kind="reaction", critical=False)
    with pytest.raises(ValueError, match="Observed reactions"):
        validate_event_proposals([EventProposal.model_validate(proposal)], build_event_windows(shots, EventAgentConfig())[0], shots, frames)


def test_presentation_evidence_is_sufficient_for_incidental_source_without_a_causal_fact():
    shots = [source(1, 0), source(2, 2), source(3, 4)]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames, dispositions={shots[1].id: "omit"})
    proposal["causal_facts"].pop(1)
    proposal["stages"][0]["source_shot_ids"] = [shots[0].id, shots[2].id]
    proposal["stages"][0]["fact_ids"].remove("fact-2")
    result = run(shots, frames, Provider([final(proposal)]))
    assert result.status == "ready" and len(result.events) == 1
    assert len(result.source_coverage) == 3
    assert shots[1].action not in result.generation_clips[0].seedance_prompt.positive_prompt


@pytest.mark.parametrize("mutation", ["missing", "foreign", "bad-time"])
def test_ownership_evidence_in_presentation_remains_strict(mutation):
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames, dispositions={shots[1].id: "omit"})
    proposal["causal_facts"].pop(1)
    proposal["stages"][0]["source_shot_ids"].pop()
    proposal["stages"][0]["fact_ids"].pop()
    evidence = proposal["presentation"][1]["evidence"]
    if mutation == "missing":
        evidence.clear()
    elif mutation == "foreign":
        evidence[0] = ref(frames[0], "不属于省略镜头")
    else:
        evidence[0]["timestamp"] = frames[1].timestamp + 0.1
    result = run(shots, frames, Provider([final(proposal), final(proposal)]))
    assert result.status == "degraded" and all(saved.status == "fallback" for saved in result.events)


def observed_motion(shot, artifact):
    static = empty_stage(StaticObservationV2, "静态未知")
    motion = empty_stage(MotionObservationV2, "动态未知")
    field = motion.dynamics.subject_action
    field.value, field.observable, field.confidence = shot.action, True, 0.9
    from app.perception_models import EvidenceRef
    field.evidence = [EvidenceRef(**ref(artifact, shot.action))]
    shot.visual_observation_v2 = merge_stages(static, motion)
    return shot


def test_frame_budget_prefers_saved_dynamic_evidence_instead_of_previous_person_first_frame():
    shots = [source(index, index - 1, 1.0) for index in range(1, 10)]
    frames = [frame(shot, suffix, offset) for shot in shots
              for suffix, offset in [("first", 0.1), ("action", 0.4), ("last", 0.8)]]
    for shot in shots:
        observed_motion(shot, next(item for item in frames if item.id == f"{shot.id}-action"))
    def selected_reply():
        shown = {image.id for image in provider.calls[-1][1]}
        evidence = [next(item for item in frames if item.id == f"{shot.id}-action") for shot in shots]
        assert all(item.id in shown for item in evidence)
        return final(event(shots, evidence))
    provider = Provider([selected_reply])
    result = run(shots, frames, provider, config=EventAgentConfig(max_initial_frames_per_window=16))
    assert result.status == "ready" and len(provider.calls[0][1]) == 16
    assert {item.id.rsplit("-", 1)[0] for item in provider.calls[0][1]} == {shot.id for shot in shots}
    payload = json.loads(provider.calls[0][0].split("\n输入：\n", 1)[1])
    assert all(any(row["text"] == shot.action and row["evidence"]
                   for row in observation["saved_literal_observations"])
               for shot, observation in zip(shots, payload["source_observations"]))


def test_fact_cannot_attach_saved_action_to_the_wrong_frame_of_its_own_source():
    shot = source(1, 0)
    first, action = frame(shot, "first", 0.1), frame(shot, "action", 0.8)
    observed_motion(shot, action)
    proposal = event([shot], [first])
    result = run([shot], [first, action], Provider([final(proposal), final(proposal)]))
    assert result.status == "degraded"
    assert "verified frame" in " ".join(result.audit[0].validation_errors)
    assert result.events[0].causal_facts[0].evidence[0].frame_id == action.id


def test_iris_unknown_cannot_be_counted_as_successful_legacy_projection():
    shot = unknown(source(1, 0))
    shot.action = "保留已见内容但分析失败"  # A generic compatibility projection.
    shot.visual_facts.scene = "厨房"
    assert not source_has_saved_observations(shot)


def test_scene_boundary_judgment_keeps_new_goal_in_same_scene_and_requires_both_frames():
    shots = [source(1, 0, action="女子喝水"), source(2, 2, action="女子打开窗户")]
    frames = [frame(shot) for shot in shots]
    first, second = event(shots[:1], frames[:1]), event(shots[1:], frames[1:])
    second["scene_continuity"] = "same_scene"
    second["scene_evidence"] = [ref(artifact, "同一室内空间") for artifact in frames]
    result = run(shots, frames, Provider([final(first, second)]))
    assert result.status == "ready" and len(result.events) == 2
    assert result.events[1].scene_continuity == "same_scene"
    second["scene_evidence"].pop(0)
    reply = final(first, second)
    result = run(shots, frames, Provider([reply, reply]))
    assert [saved.status for saved in result.events] == ["ready", "ready"]
    assert result.events[1].scene_continuity == "unknown" and not result.events[1].scene_evidence
    assert not result.audit[0].validation_errors and result.usage_summary["model_calls"] == 1
    assert "场次判断证据不可验证" in " ".join(result.warnings)


def test_visual_pending_scene_ids_do_not_force_a_real_scene_boundary():
    shots = [source(1, 0), source(2, 2)]
    shots[0].scene_reason = "视觉保守分组，待语义复核"
    shots[1].scene_reason = "视觉边界保守划分，需复核场次"
    shots[1].scene_group_id = "scene-2"
    frames = [frame(shot) for shot in shots]
    result = run(shots, frames, Provider([final(event(shots, frames))]))
    assert result.status == "ready" and len(result.events) == 1


def test_model_same_scene_cannot_override_a_trusted_time_elision():
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    first, second = event(shots[:1], frames[:1]), event(shots[1:], frames[1:])
    second["scene_continuity"] = "same_scene"
    second["scene_evidence"] = [ref(artifact, "相同空间") for artifact in frames]
    reply = final(first, second)
    result = run(shots, frames, Provider([reply, reply]), protected_boundaries={(shots[0].id, shots[1].id)})
    assert [saved.status for saved in result.events] == ["ready", "fallback"]
    assert "cannot override a trusted scene" in " ".join(result.audit[0].validation_errors)


def test_second_live_response_hydrates_dropped_source3_inside_its_existing_stage_without_repair():
    shots, frames = nine_sources()
    shots[0] = source(1, 0, 1.4, action="男孩将粉色液体递向女性")
    observed_motion(shots[2], frames[2])
    before = copy.deepcopy([shot.model_dump() for shot in shots])
    proposal = event(shots[:8], frames[:8])
    proposal["causal_facts"].pop(2)
    proposal["stages"][0]["fact_ids"].remove("fact-3")
    proposal["presentation"][2]["evidence"] = []  # Initial response also omitted this.
    reveal = event(shots[8:], frames[8:], duration=1.2)
    unseen = frame(shots[7], "unshown", 0.9)
    reveal["scene_continuity"] = "time_elision"
    reveal["scene_evidence"] = [ref(unseen, "原场景结尾"), ref(frames[8], "已见躺卧")]
    raw = final(proposal, reveal)
    provider = Provider([raw])
    result = run(shots, [*frames, unseen], provider, config=EventAgentConfig(timing_policy="authored", max_initial_frames_per_window=9))
    assert len(provider.calls) == 1 and not result.audit[0].validation_errors
    assert [len(saved.source_shot_ids) for saved in result.events] == [8, 1]
    assert [saved.status for saved in result.events] == ["ready", "ready"]
    assert result.events[1].scene_continuity == "unknown" and not result.events[1].scene_evidence
    recovered = next(fact for fact in result.events[0].causal_facts if fact.source_shot_ids == [shots[2].id])
    assert recovered.text == shots[2].action and recovered.critical
    assert recovered.evidence[0].frame_id == frames[2].id
    assert recovered.id in result.events[0].stages[0].fact_ids
    assert result.events[0].stages[0].fact_ids.index(recovered.id) < result.events[0].stages[0].fact_ids.index("fact-4")
    action_line = result.generation_clips[0].timeline[0].action
    assert action_line.index(shots[1].action) < action_line.index(shots[2].action) < action_line.index(shots[3].action)
    assert result.events[0].stages[0].duration_seconds == proposal["stages"][0]["duration_seconds"]
    assert result.events[0].stages[0].source_shot_ids == proposal["stages"][0]["source_shot_ids"]
    assert result.raw_responses[0]["response"] == raw
    assert any(repair.get("field") == "hydrated_saved_action" for repair in result.raw_responses[0]["local_repairs"])
    assert [shot.model_dump() for shot in shots] == before


def test_hydrated_middle_action_keeps_joint_facts_indivisible_and_same_source_fact_order_stable():
    shots = [source(index, index - 1, 1.0, action=action) for index, action in enumerate(
        ["双方接触同一杯子", "双方接触同一杯子", "男孩微笑", "女子接稳水杯", "女子接稳水杯"], 1)]
    frames = [frame(shot) for shot in shots]
    observed_motion(shots[2], frames[2])
    shots[3].action_details.steps = [shots[3].action, "女子仍握杯子"]
    proposal = event(shots, frames)
    first = copy.deepcopy(proposal["causal_facts"][0])
    first.update(id="joint-first", source_shot_ids=[shots[0].id, shots[1].id],
                 evidence=[ref(frames[0], "共同杯子"), ref(frames[1], "共同杯子")])
    joint = copy.deepcopy(proposal["causal_facts"][3])
    joint.update(id="joint-last", source_shot_ids=[shots[3].id, shots[4].id],
                 evidence=[ref(frames[3], "接稳杯子"), ref(frames[4], "接稳杯子")])
    detail = copy.deepcopy(proposal["causal_facts"][3])
    detail.update(id="same-owner-detail", text="女子仍握杯子")
    proposal["causal_facts"] = [first, joint, detail]
    # Deliberately keep this owner's detail ahead of the joint summary in the
    # model's stage order; hydration may not reorder within the same source.
    proposal["stages"][0]["fact_ids"] = ["joint-first", "same-owner-detail", "joint-last"]
    result = run(shots, frames, Provider([final(proposal)]))
    assert result.status == "ready" and len(result.events) == len(result.generation_clips) == 1
    saved = result.events[0]
    recovered = next(fact for fact in saved.causal_facts if fact.source_shot_ids == [shots[2].id])
    assert saved.stages[0].fact_ids == ["joint-first", recovered.id, "same-owner-detail", "joint-last"]
    assert next(fact for fact in saved.causal_facts if fact.id == "joint-first").source_shot_ids == [shots[0].id, shots[1].id]
    assert next(fact for fact in saved.causal_facts if fact.id == "joint-last").source_shot_ids == [shots[3].id, shots[4].id]
    assert saved.stages[0].duration_seconds == 8.0
    action = result.generation_clips[0].timeline[0].action
    assert action == "双方接触同一杯子；男孩微笑；女子仍握杯子；女子接稳水杯"


@pytest.mark.parametrize("mutation", ["no-ir", "unobservable", "unshown-ref", "ir-does-not-match-edit", "duplicate-stage", "unowned-stage"])
def test_local_hydration_requires_exact_verified_action_seen_refs_and_unique_existing_stage(mutation):
    shots = [source(1, 0), source(2, 2, action="男孩微笑")]
    frames = [frame(shot) for shot in shots]
    observed_motion(shots[1], frames[1])
    proposal = event(shots, frames)
    proposal["causal_facts"].pop(1)
    proposal["stages"][0]["fact_ids"].pop()
    if mutation == "no-ir":
        shots[1].visual_observation_v2 = None
    elif mutation == "unobservable":
        shots[1].visual_observation_v2.dynamics.subject_action.observable = False
        # Keep another trustworthy observable so this source remains model-owned.
        shots[1].visual_observation_v2.dynamics.initial_state = shots[1].visual_observation_v2.dynamics.subject_action.model_copy()
        shots[1].visual_observation_v2.dynamics.initial_state.observable = True
    elif mutation == "unshown-ref":
        shots[1].visual_observation_v2.dynamics.subject_action.evidence[0].frame_id = "not-delivered"
    elif mutation == "ir-does-not-match-edit":
        shots[1].action = "男孩收起笑容"
        shots[1].action_details.steps = [shots[1].action]
    elif mutation == "duplicate-stage":
        proposal["stages"].append(copy.deepcopy(proposal["stages"][0]))
    else:
        proposal["stages"][0]["source_shot_ids"].remove(shots[1].id)
    result = run(shots, frames, Provider([final(proposal), final(proposal)]))
    assert result.status == "degraded" and all(saved.status == "fallback" for saved in result.events)
    assert all(not any(repair.get("field") == "hydrated_saved_action" for repair in raw.get("local_repairs", []))
               for raw in result.raw_responses)


def test_model_invented_fact_is_not_removed_or_authorized_by_restoring_a_saved_missing_fact():
    shots = [source(1, 0), source(2, 2, action="男孩微笑")]
    frames = [frame(shot) for shot in shots]
    observed_motion(shots[1], frames[1])
    proposal = event(shots, frames)
    proposal["causal_facts"][1]["text"] = "男孩知道女子中毒，所以微笑"
    result = run(shots, frames, Provider([final(proposal), final(proposal)]))
    assert all(saved.status == "fallback" for saved in result.events)
    assert "Causal text must literally quote" in " ".join(result.audit[0].validation_errors)
    assert "中毒" not in "\n".join(clip.seedance_prompt.positive_prompt for clip in result.generation_clips)


def test_literal_matching_ignores_only_whitespace_and_punctuation_not_paraphrases_or_numbers():
    shot = source(1, 0, action="女子接稳水杯，随后微笑。")
    artifact = frame(shot)
    observed_motion(shot, artifact)
    proposal = event([shot], [artifact])
    proposal["causal_facts"][0]["text"] = "女子 接稳水杯随后微笑"
    result = run([shot], [artifact], Provider([final(proposal)]))
    assert result.status == "ready" and not result.raw_responses[0]["local_repairs"]
    assert result.events[0].causal_facts[0].text == proposal["causal_facts"][0]["text"]


@pytest.mark.parametrize("saved,changed", [
    ("女子接稳水杯，随后微笑。", "女子把水杯放稳随后微笑"),
    ("女子饮用粉色液体", "女子饮用橙色液体"),
    ("男孩递出1.5升液体", "男孩递出15升液体"),
    ("男孩递出1/5升液体", "男孩递出15升液体"),
    ("男孩递出液体", "男孩递出液体导致中毒"),
])
def test_literal_normalization_never_changes_words_numbers_or_causality(saved, changed):
    shot = source(1, 0, action=saved)
    artifact = frame(shot)
    proposal = event([shot], [artifact])
    proposal["causal_facts"][0]["text"] = changed
    result = run([shot], [artifact], Provider([final(proposal), final(proposal)]))
    assert result.status == "degraded" and result.events[0].status == "fallback"


def test_full_observed_steps_have_the_same_punctuation_matching_as_saved_action():
    shot = source(1, 0, action="女子接杯并喝水")
    shot.action_details.steps = ["女子接杯。", "女子喝水。"]
    artifact = frame(shot)
    proposal = event([shot], [artifact])
    proposal["causal_facts"][0]["text"] = "女子 接杯"
    second = copy.deepcopy(proposal["causal_facts"][0])
    second.update(id="drink", text="女子喝水")
    proposal["causal_facts"].append(second)
    proposal["stages"][0]["fact_ids"].append("drink")
    result = run([shot], [artifact], Provider([final(proposal)]))
    assert result.status == "ready"


def test_prompt_exposes_only_delivered_frame_references_and_copyable_adjacent_boundary_catalog():
    from app.perception_models import ActionPhase, EvidenceRef
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    unseen = frame(shots[0], "unsent", 1.8)
    refs = [EvidenceRef(**ref(artifact, "保存的动作观察")) for artifact in (frames[0], unseen)]
    shots[0].action_details.evidence = [f"{unseen.id} | 1.8s：未送达帧"]
    shots[0].visual_facts.evidence = [f"{unseen.id} | 未送达人物证据"]
    shots[0].action_details.step_evidence = [refs]
    shots[0].action_details.action_phases = [ActionPhase(description="已保存阶段", source_step_indices=[1], evidence=refs, confidence=0.9)]
    before = copy.deepcopy([shot.model_dump() for shot in shots])
    provider = Provider([final(event(shots, frames))])
    result = run(shots, [*frames, unseen], provider, config=EventAgentConfig(max_initial_frames_per_window=2))
    assert result.status == "ready"
    payload_text = provider.calls[0][0].split("\n输入：\n", 1)[1]
    assert unseen.id not in payload_text
    payload = json.loads(payload_text)
    catalog = payload["boundary_frame_catalog"]
    assert len(catalog) == 1
    assert catalog[0]["left_source_shot_id"] == shots[0].id and catalog[0]["right_source_shot_id"] == shots[1].id
    assert catalog[0]["recommended_evidence"] == [ref(frames[0], "边界左侧"), ref(frames[1], "边界右侧")]
    assert not payload["source_observations"][0]["action_details"]["evidence"]
    assert payload["source_observations"][0]["action_details"]["step_evidence"][0][0]["frame_id"] == frames[0].id
    assert [shot.model_dump() for shot in shots] == before


def motion_with_endpoints(shot, artifacts):
    from app.perception_models import EvidenceRef
    observed_motion(shot, artifacts[0])
    shot.visual_observation_v2.dynamics.subject_action.evidence = [EvidenceRef(**ref(item, "保存动作证据")) for item in artifacts]
    return shot


def test_default_twenty_frame_budget_stays_below_the_twenty_four_frame_hard_limit():
    from pydantic import ValidationError
    assert EventAgentConfig().max_initial_frames_per_window == 20
    assert EventAgentConfig(max_initial_frames_per_window=24).max_initial_frames_per_window == 24
    with pytest.raises(ValidationError):
        EventAgentConfig(max_initial_frames_per_window=25)


def test_dynamic_endpoints_take_priority_over_context_and_dense_samples_after_all_owners():
    from app.event_agent import EventWindow
    from app.event_agent.engine import _select_frames
    shots = [source(index, index, 1.0) for index in range(3)]
    frames = [frame(shot, suffix, offset) for shot in shots for suffix, offset in (("first", 0.1), ("last", 0.8))]
    for shot in shots[1:]:
        motion_with_endpoints(shot, [item for item in frames if item.shot_id == shot.id])
    window = EventWindow(id="window", start_time=1.0, end_time=3.0,
                         source_shot_ids=[shot.id for shot in shots[1:]], context_shot_ids=[shots[0].id], context_start_time=0.0)
    selected = _select_frames(window, frames, 3, shots)
    assert len(selected) == 3
    assert {item.shot_id for item in selected} == {shots[1].id, shots[2].id}
    assert f"{shots[1].id}-last" in {item.id for item in selected}
    selected = _select_frames(window, frames, 4, shots)
    assert len(selected) == 4 and not any(item.shot_id == shots[0].id for item in selected)
    selected = _select_frames(window, frames, 5, shots)
    assert len(selected) == 5 and f"{shots[0].id}-last" in {item.id for item in selected}


def test_nine_dynamic_sources_receive_endpoints_and_existing_atoms_gain_only_their_verified_end_refs():
    shots = [source(index, index - 1, 1.0) for index in range(1, 10)]
    frames = [frame(shot, suffix, offset) for shot in shots
              for suffix, offset in (("01", 0.1), ("02", 0.3), ("03", 0.6), ("04", 0.8))]
    originals = []
    for shot in shots:
        artifacts = [item for item in frames if item.shot_id == shot.id]
        if shot.number == 6:
            artifacts = artifacts[1:]  # First image belongs to the preceding woman.
        motion_with_endpoints(shot, artifacts)
        originals.append(artifacts[0])
    proposal = event(shots, originals)
    before = copy.deepcopy([shot.model_dump() for shot in shots])
    provider = Provider([final(proposal)])
    result = run(shots, frames, provider)
    assert result.status == "ready" and len(provider.calls[0][1]) == 20
    delivered = {image.id for image in provider.calls[0][1]}
    assert "shot-6-02" in delivered and "shot-6-04" in delivered and "shot-6-01" not in delivered
    for fact, shot, first in zip(result.events[0].causal_facts, shots, originals):
        assert {first.id, f"{shot.id}-04"}.issubset({item.frame_id for item in fact.evidence})
        assert {item.frame_id for item in fact.evidence}.issubset(delivered)
        assert fact.text == shot.action
    assert result.events[0].stages[0].duration_seconds == proposal["stages"][0]["duration_seconds"]
    assert result.events[0].stages[0].fact_ids == proposal["stages"][0]["fact_ids"]
    assert all(repair["field"] == "dynamic_evidence_endpoints" for repair in result.raw_responses[0]["local_repairs"])
    assert [shot.model_dump() for shot in shots] == before


def test_dynamic_endpoint_augmentation_does_not_use_unshown_or_different_literal_atoms():
    from app.perception_models import EvidenceRef
    shot = source(1, 0, action="闭眼撅嘴，呈现吹气或品尝的动作")
    first, last = frame(shot, "first", 0.1), frame(shot, "last", 0.8)
    motion_with_endpoints(shot, [first, last])
    proposal = event([shot], [first])
    provider = Provider([final(proposal)])
    result = run([shot], [first, last], provider, config=EventAgentConfig(max_initial_frames_per_window=1))
    assert result.status == "ready" and {item.frame_id for item in result.events[0].causal_facts[0].evidence} == {first.id}
    assert "或" in result.events[0].causal_facts[0].text
    assert "不得将“吹气或品尝”写成确定“品尝”" in provider.calls[0][0]
    shot.visual_observation_v2.dynamics.subject_action.evidence = [EvidenceRef(**ref(first, "原动作"))]
    state = shot.visual_observation_v2.dynamics.final_state
    state.value, state.observable, state.confidence = "女子仍握杯子", True, 0.9
    state.evidence = [EvidenceRef(**ref(last, "另外的末状态观察"))]
    result = run([shot], [first, last], Provider([final(proposal)]))
    assert result.status == "ready"
    assert {item.frame_id for item in result.events[0].causal_facts[0].evidence} == {first.id}
    assert all(fact.text == shot.action for fact in result.events[0].causal_facts)


def test_trusted_protected_integrated_reaction_already_presented_can_preserve_without_regrouping():
    shots = [source(1, 0, action="男孩递杯"), source(2, 2, action="男孩微笑"), source(3, 4, action="女子喝水")]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames)
    proposal["causal_facts"][1]["kind"] = "reaction"
    raw = final(proposal)
    provider = Provider([raw])
    result = run(shots, frames, provider, protected_source_shot_ids={shots[1].id})
    assert result.status == "ready" and len(provider.calls) == len(result.events) == 1
    assert result.events[0].presentation[1].disposition == "preserve"
    assert result.events[0].presentation[0].disposition == "integrate"  # Not a protected source.
    assert result.events[0].stages[0].source_shot_ids == proposal["stages"][0]["source_shot_ids"]
    assert result.events[0].stages[0].fact_ids == proposal["stages"][0]["fact_ids"]
    assert result.events[0].stages[0].duration_seconds == 8.0
    assert result.events[0].generation_mode == "continuous"
    assert result.raw_responses[0]["response"] == raw
    assert result.raw_responses[0]["local_repairs"][0]["field"] == "protected_presentation"


@pytest.mark.parametrize("mutation", ["omit", "missing-fact", "unpresented", "invented", "bad-evidence", "static-only", "duplicate-stage"])
def test_protected_presentation_upgrade_cannot_authorize_omission_missing_actions_or_invalid_proofs(mutation):
    shots = [source(1, 0), source(2, 2, action="男孩微笑")]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames)
    if mutation == "omit":
        proposal["presentation"][1]["disposition"] = "omit"
    elif mutation == "missing-fact":
        proposal["causal_facts"].pop(1)
        proposal["stages"][0]["fact_ids"].pop()
    elif mutation == "unpresented":
        proposal["stages"][0]["fact_ids"].pop()
    elif mutation == "invented":
        proposal["causal_facts"][1]["text"] = "男孩因女子中毒而微笑"
    elif mutation == "bad-evidence":
        proposal["causal_facts"][1]["evidence"][0]["frame_id"] = frames[0].id
    elif mutation == "static-only":
        proposal["causal_facts"][1]["text"] = shots[1].visual_facts.scene
    else:
        proposal["stages"].append(copy.deepcopy(proposal["stages"][0]))
    result = run(shots, frames, Provider([final(proposal), final(proposal)]), protected_source_shot_ids={shots[1].id})
    assert result.status == "degraded" and all(saved.status == "fallback" for saved in result.events)
    assert all(not any(repair.get("field") == "protected_presentation" for repair in raw.get("local_repairs", []))
               for raw in result.raw_responses)
