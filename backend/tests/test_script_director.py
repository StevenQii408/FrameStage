import json

import pytest
from pydantic import ValidationError

from app.schemas import ProviderHealth, ShotContract
from app.script_director.document import parse_script_document
from app.script_director.engine import _characters_for, _prop_owner_id, conservative_plan, dialogue_units, director_windows, plan_window, validate_decision
from app.script_director.plan_models import CameraPlan, DirectorDecision, PropState, ToolRequest
from app.script_director.tools import ScriptToolRegistry, estimate_dialogue_seconds


class FakeDirector:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def health(self):
        return ProviderHealth(provider="bailian", model="qwen3.8-flash", ready=True)

    def plan_script_director(self, prompt, *, schema, max_tokens):
        self.calls.append((prompt, schema, max_tokens))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


def example():
    document = parse_script_document("【人物】\n程远（40岁，刑警）\n老K（50岁，线人）\n外景：天桥 夜\n程远走入桥下。\n老K（压低声音）：别回头。\n程远：东西带来了吗？\n老K拿出硬盘。")
    window = director_windows(document)[0]
    return document, window, conservative_plan(document, window)


def test_director_covers_only_content_and_keeps_raw_dialogue():
    document, window, plan = example()
    validate_decision(plan, document, window)
    assert not any("40岁" in sid for event in plan.events for sid in event.source_span_ids)
    ranges = [part for event in plan.events for shot in event.shots for part in shot.dialogue_ranges]
    spans = {span.id: span for span in document.spans}
    assert [spans[part.span_id].text[part.start:part.end] for part in ranges] == ["别回头。", "东西带来了吗？"]
    assert {sid for event in plan.events for sid in event.source_span_ids} == {span.id for span in document.content_spans}


def test_director_does_not_reapply_legacy_two_beat_or_five_second_grouping():
    document = parse_script_document("甲打开门。甲走到桌边。甲拿起杯子。甲放下杯子。")
    window = director_windows(document)[0]
    plan = conservative_plan(document, window)
    first = plan.events[0].shots[0]
    first.source_span_ids = window.source_span_ids
    first.duration_seconds = 8
    first.action_steps = ["甲进门走到桌边，拿起杯子后放下"]
    plan.events = [plan.events[0].model_copy(update={"source_span_ids": window.source_span_ids, "shots": [first]})]
    provider = FakeDirector([plan.model_dump_json()])
    result = plan_window(provider, document, window)
    assert not result.fallback and len(result.events[0].shots) == 1
    assert result.events[0].shots[0].duration_seconds == 8
    assert len(provider.calls) == 1


def test_one_optional_tool_pass_then_final_plan_with_total_two_model_calls():
    document, window, final = example()
    provider = FakeDirector([DirectorDecision(action="use_tools", tool_requests=[ToolRequest(name="get_source_span", span_ids=[window.source_span_ids[0]])]).model_dump_json(), final.model_dump_json()])
    result = plan_window(provider, document, window)
    assert result.model_calls == 2 and not result.fallback
    assert result.tool_calls[0]["name"] == "get_source_span"
    assert "本地工具结果" in provider.calls[1][0]


def test_invalid_structure_is_corrected_once_and_all_raw_responses_saved():
    document, window, final = example()
    provider = FakeDirector(["not json", final.model_dump_json()])
    result = plan_window(provider, document, window)
    assert not result.fallback and result.model_calls == 2
    assert result.raw_responses[0] == "not json"
    assert "上次校验错误" in provider.calls[1][0]
    assert '"not json"' in provider.calls[1][0] and "上次响应" in provider.calls[1][0]


def test_two_invalid_results_do_not_loop_or_lose_dialogue():
    document, window, _ = example()
    result = plan_window(FakeDirector(["bad", "bad again"]), document, window)
    assert result.fallback and result.model_calls == 2
    assert result.raw_responses == ["bad", "bad again"]
    validate_decision(DirectorDecision(action="finalize", events=result.events), document, window)


def test_network_error_does_not_trigger_another_paid_attempt():
    document, window, _ = example()
    result = plan_window(FakeDirector([RuntimeError("network unavailable")]), document, window)
    assert result.model_calls == 1 and result.fallback
    assert any("请求未完成" in warning for warning in result.warnings)


def test_missing_source_or_invented_character_is_rejected():
    document, window, plan = example()
    plan.events[0].shots[0].character_ids = ["invented-assassin"]
    with pytest.raises(ValueError, match="人物"):
        validate_decision(plan, document, window)
    plan = conservative_plan(document, window)
    plan.events[0].source_span_ids.pop()
    with pytest.raises(ValueError, match="恰好覆盖"):
        validate_decision(plan, document, window)


def test_dialogue_repetition_is_rejected_and_source_speaker_reference_is_compiled():
    document, window, plan = example()
    shot = next(shot for event in plan.events for shot in event.shots if shot.dialogue_ranges)
    shot.dialogue_ranges[0].start = 1
    with pytest.raises(ValueError, match="无损覆盖"):
        validate_decision(plan, document, window)
    plan = conservative_plan(document, window)
    shot = next(shot for event in plan.events for shot in event.shots if shot.dialogue_ranges)
    shot.character_ids = []
    validate_decision(plan, document, window)
    assert shot.character_ids == [next(span.character_id for span in document.spans if span.id == shot.dialogue_ranges[0].span_id)]


def test_two_person_interaction_keeps_both_known_character_references():
    document = parse_script_document("【人物】\n程远（40岁，刑警）\n老K（50岁，线人）\n老K把硬盘塞给程远。")
    window = director_windows(document)[0]
    plan = conservative_plan(document, window)
    shot = plan.events[0].shots[0]
    shot.character_ids = [document.characters[0].id]
    validate_decision(plan, document, window)
    assert set(shot.character_ids) == {character.id for character in document.characters}


def test_different_dialogue_spans_cannot_reverse_question_and_response():
    document = parse_script_document("甲：你先说。\n乙：我再回答。")
    window = director_windows(document)[0]
    plan = conservative_plan(document, window)
    plan.events[0].shots.reverse()
    with pytest.raises(ValueError, match="回答先于提问"):
        validate_decision(plan, document, window)


def test_obvious_future_action_chain_in_first_frame_uses_the_same_repair_budget():
    document, window, plan = example()
    broken = plan.model_copy(deep=True)
    broken.events[0].shots[0].first_frame_state = "程远站在门外，然后跑入桥下，最后把硬盘交给老K。"
    provider = FakeDirector([broken.model_dump_json(), plan.model_dump_json()])
    result = plan_window(provider, document, window)
    assert not result.fallback and result.model_calls == 2
    assert "首帧必须" in provider.calls[1][0]


def test_long_dialogue_is_losslessly_split_by_punctuation_not_four_equal_phases():
    text = "老K：" + "你拿着这些证据先离开，我留在这里处理后面的事情。" * 8
    document = parse_script_document(text)
    window = director_windows(document)[0]
    final = conservative_plan(document, window)
    validate_decision(final, document, window)
    span = document.content_spans[0]
    ranges = [part for event in final.events for shot in event.shots for part in shot.dialogue_ranges]
    assert len(ranges) > 1
    assert "".join(span.text[part.start:part.end] for part in ranges) == span.text
    assert all(shot.duration_seconds <= 12 for event in final.events for shot in event.shots)
    assert all(not shot.action_timeline for event in final.events for shot in event.shots)


def test_dialogue_duration_can_be_locally_corrected_without_extra_model_call():
    document, window, plan = example()
    shot = next(shot for event in plan.events for shot in event.shots if shot.dialogue_ranges)
    shot.duration_seconds = .5
    fixes, _ = validate_decision(plan, document, window)
    assert fixes and shot.duration_seconds > .5


def test_local_timing_normalization_is_audited_without_marking_window_for_review():
    document, window, plan = example()
    shot = next(shot for event in plan.events for shot in event.shots if shot.dialogue_ranges)
    shot.duration_seconds = .5
    provider = FakeDirector([plan.model_dump_json()])
    result = plan_window(provider, document, window)
    assert not result.fallback and result.model_calls == 1
    assert result.warnings == []
    assert "对白表演预算已本地校正，未改变台词或事件" in result.normalization_notes
    from app.script_director.source_support import NORMALIZATION_NOTE
    assert set(result.normalization_notes) <= {"对白表演预算已本地校正，未改变台词或事件", NORMALIZATION_NOTE}
    assert any(directed.duration_seconds > .5 for event in result.events for directed in event.shots if directed.dialogue_ranges)


def test_axis_and_prop_continuity_require_explicit_directing_plan():
    document, window, plan = example()
    plan.events[0].shots[0].camera.axis_side = "B"
    with pytest.raises(ValueError, match="跨轴"):
        validate_decision(plan, document, window)
    plan.events[0].shots[0].camera.reestablishes_axis = True
    for event in plan.events:
        for shot in event.shots:
            shot.camera.axis_side = "B"
    _, state = validate_decision(plan, document, window)
    assert state["axis"] in ("A", "B")


@pytest.mark.parametrize("holder", [
    "老K", "老K(手中)", "老K(怀中)", "老K递出/程远触碰",
    "老K(递出)/程远(触碰)", "老K(手中)，程远指尖接触",
    "程远触碰老K手中硬盘", "老K(尚未松手)/程远(准备接)",
])
def test_prop_owner_ignores_body_location_and_touching_participants(holder):
    document, _, _ = example()
    expected = next(character.id for character in document.characters if character.name == "老K")
    assert _prop_owner_id(holder, document) == expected


@pytest.mark.parametrize("holder", ["无", "未知", "陌生人手中", "程远(触碰)", "老K/程远", "老K(已松手)", "不在老K手中"])
def test_prop_owner_unknown_or_contact_does_not_prove_a_transfer(holder):
    document, window, plan = example()
    assert _prop_owner_id(holder, document) is None
    shot = plan.events[0].shots[0]
    shot.prop_states_before = [PropState(prop="硬盘", holder=holder)]
    fixes, state = validate_decision(plan, document, window, {"props": {"硬盘": "老K(手中)"}})
    assert any("持有主体证据不足" in fix for fix in fixes)
    assert state["props"]["硬盘"] == "老K(手中)"
    assert shot.prop_states_before[0].holder == holder


def test_prop_owner_resolves_existing_id_and_alias_without_mutating_original_evidence():
    document, window, plan = example()
    owner = next(character for character in document.characters if character.name == "老K")
    owner.aliases.append("老王")
    shot = plan.events[0].shots[0]
    shot.prop_states_before = [PropState(prop="硬盘", holder="老王(怀中)")]
    shot.prop_states_after = [PropState(prop="硬盘", holder="老K(递出)/程远(触碰)")]
    fixes, state = validate_decision(plan, document, window, {"props": {"硬盘": owner.id}})
    assert not fixes
    assert shot.prop_states_before[0].holder == "老王(怀中)"
    assert state["props"]["硬盘"] == "老K(递出)/程远(触碰)"


def test_unexplained_change_of_known_prop_holder_still_rejected():
    document, window, plan = example()
    plan.events[0].shots[0].prop_states_before = [PropState(prop="硬盘", holder="程远(手中)")]
    with pytest.raises(ValueError, match="持有状态与上一镜不一致"):
        validate_decision(plan, document, window, {"props": {"硬盘": "老K(手中)，程远指尖接触"}})


def test_owner_normalization_prevents_paid_semantic_retries_for_location_variants():
    document, window, plan = example()
    shot = plan.events[0].shots[0]
    shot.prop_states_before = [PropState(prop="硬盘", holder="老K(递出)/程远(触碰)")]
    shot.prop_states_after = [PropState(prop="硬盘", holder="老K(手中)，程远指尖接触")]
    provider = FakeDirector([plan.model_dump_json()])
    result = plan_window(provider, document, window, {"props": {"硬盘": "老K(手中)"}})
    assert not result.fallback and result.model_calls == 1
    assert result.events[0].shots[0].prop_states_after[0].holder == "老K(手中)，程远指尖接触"


def test_read_only_tools_report_unknown_ids_and_no_plan_mutation():
    document, _, _ = example()
    registry = ScriptToolRegistry(document)
    before = document.model_dump()
    result = registry.run(ToolRequest(name="get_source_span", span_ids=["missing"]))
    assert result.error and document.model_dump() == before
    grammar = registry.run(ToolRequest(name="lookup_film_grammar", rule_ids=["angle"]))
    assert grammar.result["version"] == "film-grammar.v1"
    assert "人物视点" in grammar.result["rules"]["angle"]


def test_video_duration_guard_still_strict_and_script_timing_explicit():
    assert ShotContract(generation_duration_seconds=8).generation_duration_seconds == 8
    assert ShotContract(timing_origin="script-performance", generation_duration_seconds=8).generation_duration_seconds == 8


def test_dialogue_ids_are_compiled_locally_without_model_character_counting():
    document, window, plan = example()
    for event in plan.events:
        for shot in event.shots:
            shot.dialogue_ids = [part.span_id for part in shot.dialogue_ranges]
            shot.dialogue_ranges = []
    validate_decision(plan, document, window)
    validate_decision(plan, document, window)
    assert [part.end for event in plan.events for shot in event.shots for part in shot.dialogue_ranges] == [len(span.text) for span in document.content_spans if span.role == "dialogue"]


def test_long_dialogue_units_are_precompiled_and_lossless():
    document = parse_script_document("程远：" + "你先走，我留在这里。" * 20)
    window = director_windows(document)[0]
    units = list(dialogue_units(document, window).values())
    assert len(units) > 1 and all(".part-" in unit["id"] for unit in units)
    assert "".join(unit["text"] for unit in units) == document.content_spans[0].text
    assert all(unit["estimated_seconds"] <= 11.5 for unit in units)


def test_serialization_fragments_in_schema_valid_strings_are_rejected():
    document, window, plan = example()
    plan.events[0].shots[0].action_steps = ["], action_timeline: [], duration_seconds: 3.86, shot_function: "]
    with pytest.raises(ValueError, match="代码碎片"):
        validate_decision(plan, document, window)


def test_empty_dialogue_actions_compile_to_pose_without_forcing_schema_garbage():
    document = parse_script_document("老K：带着证据活下去！")
    window = director_windows(document)[0]
    plan = conservative_plan(document, window)
    shot = plan.events[0].shots[0]
    shot.action_steps = []
    validate_decision(plan, document, window)
    assert shot.action_steps == ["主体保持当前姿态，按原对白或旁白自然表达"]


def test_missing_action_restatement_compiles_assigned_source_without_extra_call():
    document = parse_script_document("男孩拿起杯子。男孩将杯子递给女人。")
    window = director_windows(document)[0]
    plan = conservative_plan(document, window)
    shot = plan.events[0].shots[0]
    shot.action_steps = []
    provider = FakeDirector([plan.model_dump_json()])
    result = plan_window(provider, document, window)
    assert not result.fallback and result.model_calls == 1
    assert result.events[0].shots[0].action_steps == [span.text for span in document.content_spans]


def test_gunshot_only_cannot_invent_a_visible_collapse_and_keeps_previous_camera():
    document = parse_script_document("老K：快走！\n枪声伴随着惊雷炸响。")
    window = director_windows(document)[0]
    plan = conservative_plan(document, window)
    previous, sound = plan.events[0].shots
    previous.camera = CameraPlan(shot_size="近景", lens="长焦")
    previous.end_state = "老K仍站在原地，朝程远离开的方向注视"
    sound.end_state = "老K受到冲击向后倒下"
    sound.action_steps = ["老K遭击倒地"]
    validate_decision(plan, document, window)
    assert sound.first_frame_state == sound.end_state == previous.end_state
    assert sound.camera == previous.camera
    assert "倒下" not in sound.end_state
    assert sound.action_steps == ["保持当前画面与人物姿态，原文声音仅在画外发生"]


def continuation_example():
    document = parse_script_document("甲打开门。甲走到桌边。甲拿起杯子。甲放下杯子。")
    previous, current = director_windows(document, maximum_spans=2)
    plan = conservative_plan(document, current)
    state = {"open_event": {"id": "script-event-001", "window_number": previous.number,
        "scene_id": previous.scene_id, "last_source_span_id": previous.source_span_ids[-1],
        "source_span_ids": previous.source_span_ids, "successful": True}}
    return document, current, plan, state


def test_adjacent_successful_same_scene_event_continuation_covers_only_new_source():
    document, window, plan, state = continuation_example()
    plan.events[0].continues_event_id = "script-event-001"
    validate_decision(plan, document, window, state)
    assert [sid for event in plan.events for sid in event.source_span_ids] == window.source_span_ids
    provider = FakeDirector([plan.model_dump_json()])
    result = plan_window(provider, document, window, state)
    assert not result.fallback and result.model_calls == 1
    assert result.events[0].continues_event_id == "script-event-001"
    payload = json.JSONDecoder().raw_decode(provider.calls[0][0].split("\n输入：\n", 1)[1])[0]
    assert payload["previous_state"]["open_event"] == state["open_event"]
    assert payload["target_span_ids"] == window.source_span_ids


@pytest.mark.parametrize("change", [
    {"id": "script-event-older"}, {"scene_id": "scene_other"},
    {"window_number": 0}, {"successful": False},
    {"last_source_span_id": "span_0001"},
])
def test_event_continuation_rejects_older_cross_scene_failed_or_nonadjacent_state(change):
    document, window, plan, state = continuation_example()
    plan.events[0].continues_event_id = "script-event-001"
    state["open_event"].update(change)
    with pytest.raises(ValueError, match="相邻成功窗口"):
        validate_decision(plan, document, window, state)


def test_continuation_requires_open_event_and_is_never_allowed_in_fallback():
    document, window, plan, state = continuation_example()
    plan.events[0].continues_event_id = "script-event-001"
    with pytest.raises(ValueError, match="相邻成功窗口"):
        validate_decision(plan, document, window)
    with pytest.raises(ValueError, match="保守兜底"):
        validate_decision(plan, document, window, state, allow_continuation=False)
    result = plan_window(FakeDirector(["bad", "bad again"]), document, window, state)
    assert result.fallback and not any(event.continues_event_id for event in result.events)


def test_later_event_in_window_cannot_reopen_previous_event():
    document, window, plan, state = continuation_example()
    first = plan.events[0]
    shot = first.shots[0]
    plan.events = [first.model_copy(update={"source_span_ids": [sid],
        "shots": [shot.model_copy(update={"source_span_ids": [sid]})],
        "continues_event_id": "script-event-001" if index else None})
        for index, sid in enumerate(window.source_span_ids)]
    with pytest.raises(ValueError, match="第一个事件"):
        validate_decision(plan, document, window, state)


def test_continuation_does_not_allow_prior_sources_to_be_repeated():
    document, window, plan, state = continuation_example()
    plan.events[0].continues_event_id = "script-event-001"
    plan.events[0].source_span_ids.insert(0, document.content_spans[0].id)
    with pytest.raises(ValueError, match="恰好覆盖"):
        validate_decision(plan, document, window, state)


def test_resolved_pronoun_reference_keeps_existing_character_profile_on_its_action():
    document = parse_script_document("【人物】\n老K（50岁，线人）\n老K站在门口。他伸出手。")
    pronoun_action = document.content_spans[-1]
    assert _characters_for([pronoun_action], document) == [document.characters[0].id]
    window = director_windows(document)
    plan = conservative_plan(document, window[0])
    plan.events[0].shots[0].character_ids = []
    validate_decision(plan, document, window[0])
    assert plan.events[0].shots[0].character_ids == [document.characters[0].id]


def test_exact_two_person_group_preserves_both_profiles_without_inventing_a_single_identity():
    document = parse_script_document("【人物】\n程远（40岁，刑警）\n老K（50岁，线人）\n程远和老K站在桥下。两人同时转头。")
    group_span = document.content_spans[-1]
    group_ref = next(ref for ref in group_span.entity_refs if ref.text == "两人")
    assert group_ref.character_id is None
    assert _characters_for([group_span], document) == [character.id for character in document.characters]


def test_prompt_limits_event_titles_and_goals_to_explicit_source_results():
    document, window, plan = example()
    provider = FakeDirector([plan.model_dump_json()])
    plan_window(provider, document, window)
    prompt = provider.calls[0][0]
    assert "事件title和goal也必须忠于明确原文" in prompt
    assert "枪声不等于中枪" in prompt and "事件结果保持未说明" in prompt
