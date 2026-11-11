"""Protocol regressions use simulated responses, never paid requests."""
import json

from app.perception_models import MotionObservationV2, observation_review
from app.services.perception import PerceptionOrchestrator, recoverable_stages
from app.services.perception_wire import compact_guide
from app.services.visual_tools import PerceptionBudget
from test_perception import compact_stage, observed, provider_with, setup, stages


def complete_motion(frames):
    _, motion = stages(frames)
    motion["dynamics"]["initial_state"] = observed("女子双手靠近咖啡机操作区域", frames[0])
    motion["dynamics"]["subject_action"] = observed("女子双手操作咖啡机，随后抬起右手", frames[-1])
    motion["dynamics"]["action_steps"] = observed(["女子双手操作咖啡机", "女子抬起右手"], frames[-1])
    motion["dynamics"]["final_state"] = observed("女子右手抬起，左手位于台面附近", frames[-1])
    return motion


def bare_motion(frames):
    motion = complete_motion(frames)
    wire = compact_stage(motion, frames)
    for name, field in list(wire["fields"].items()):
        if name.startswith("dynamics."):
            wire["fields"][name] = field["value"]
    return wire


def test_coffee_bare_dynamic_fields_repair_once_with_original_frame_aliases(setup):
    progress = []
    setup[1].report_perception_progress = lambda phase, state: progress.append((phase, state))
    static, _ = stages(setup[2])
    representative = setup[2][2]
    static_wire = compact_stage(static, [setup[2][0]])
    repaired = compact_stage(complete_motion(setup[2]), setup[2])
    requests = []
    provider = provider_with(setup, [static_wire, bare_motion(setup[2]), repaired], requests)
    provider.helper._sharpest_image = lambda paths: representative.path
    try:
        result = provider.analyze_shot(setup[2], setup[1])
        record = result.perception_record
        assert len(requests) == 3 and record.correction_used and not record.supplemental_call_used
        assert result.subject_action == "女子双手操作咖啡机，随后抬起右手"
        assert result.visual_facts.scene == "厨房"
        assert record.observation.facts.scene.evidence[0].frame_id == representative.id
        assert record.observation.dynamics.initial_state.evidence[0].frame_id == setup[2][0].id
        assert record.stages["motion"].state == "succeeded"
        assert record.stages["motion"].attempts == 2
        assert record.raw_responses[1]["input_frames"] == record.raw_responses[2]["input_frames"]
        repair_prompt = requests[2]["messages"][0]["content"][0]["text"]
        assert "具体错误" in repair_prompt and "dynamics.subject_action" in repair_prompt
        assert "字段不是对象" in repair_prompt and "附图及F编号与原请求完全相同" in repair_prompt
        assert not any("subject_action 未通过" in warning for warning in result.analysis_warnings)
        assert record.budgets["review"]["dynamic"]["status"] == "ready"
        assert record.budgets["review"]["video_ready"]
        assert [entry["phase"] for entry in provider.drain_usage()] == ["shot.static", "shot.motion", "shot.motion.correction"]
        assert ('shot.motion.correction', 'running') in progress
        assert progress[-1] == ('shot.motion.correction', 'completed')
    finally:
        provider.unload()


def test_failed_dynamic_repair_preserves_static_and_stops_at_three_calls(setup):
    static, _ = stages(setup[2])
    requests = []
    provider = provider_with(setup, [static, bare_motion(setup[2]), bare_motion(setup[2])], requests)
    try:
        result = provider.analyze_shot(setup[2], setup[1])
        record = result.perception_record
        assert len(requests) == 3 and not record.tools and not record.supplemental_call_used
        assert result.visual_facts.scene == "厨房" and result.action_details.primary_subject == "男孩"
        assert result.subject_action == "未知"
        review = observation_review(record.observation, record)
        assert review["static"]["status"] == "ready"
        assert review["dynamic"]["status"] == "pending_review" and not review["video_ready"]
        assert recoverable_stages(record) == ["motion"]
    finally:
        provider.unload()


def test_corrected_explicit_unknown_is_schema_valid_without_another_retry(setup):
    static, _ = stages(setup[2])
    unknown_patch = {"fields": {name: {"value": [] if isinstance(value, list) else "未知", "frames": [],
                                      "note": "", "confidence": 0, "uncertainty": "动作被遮挡，无法确定变化"}
                                 for name, value in bare_motion(setup[2])["fields"].items()
                                 if name.startswith("dynamics.")}}
    requests = []
    provider = provider_with(setup, [static, bare_motion(setup[2]), unknown_patch], requests)
    try:
        result = provider.analyze_shot(setup[2], setup[1])
        assert len(requests) == 3 and result.perception_record.correction_used
        assert result.perception_record.stages["motion"].schema_status == "passed"
        assert not recoverable_stages(result.perception_record)
        assert result.analysis_review["dynamic"]["status"] == "pending_review"
        assert not any("subject_action 未通过校验" in item for item in result.analysis_warnings)
    finally:
        provider.unload()


def test_optional_button_unknown_does_not_trigger_visual_supplement(setup):
    static, motion = stages(setup[2])
    motion["uncertainties"] = ["具体按钮和旋钮被手部遮挡，道具细节看不清，无法辨认按钮用途"]
    requests = []
    provider = provider_with(setup, [static, motion], requests)
    try:
        result = provider.analyze_shot(setup[2], setup[1])
        assert len(requests) == 2 and not result.perception_record.tools
        assert result.analysis_review["dynamic"]["status"] == "ready"
        assert "关键视觉信息仍不足，需人工复核" not in result.analysis_warnings
    finally:
        provider.unload()


def test_protocol_patch_locks_successful_fields(setup):
    static, _ = stages(setup[2])
    motion = complete_motion(setup[2])
    wire = compact_stage(motion, setup[2])
    wire["fields"]["dynamics.subject_action"] = "裸字符串动作"
    fixed = compact_stage(motion, setup[2])
    fixed["fields"]["dynamics.final_state"]["value"] = "女子飞出了窗外"
    provider = provider_with(setup, [static, wire, fixed], [])
    try:
        result = provider.analyze_shot(setup[2], setup[1])
        assert result.subject_action == motion["dynamics"]["subject_action"]["value"]
        assert result.action_details.final_state == motion["dynamics"]["final_state"]["value"]
    finally:
        provider.unload()


def test_partial_protocol_stage_can_be_recovered_without_reusing_broken_seed(setup):
    static, _ = stages(setup[2])
    provider = provider_with(setup, [static, bare_motion(setup[2])], [])
    try:
        initial = PerceptionOrchestrator(provider, setup[1], setup[2], PerceptionBudget(max_corrections=0)).run().perception_record
    finally:
        provider.unload()
    assert recoverable_stages(initial) == ["motion"]
    requests = []
    provider = provider_with(setup, [complete_motion(setup[2])], requests)
    try:
        recovered = PerceptionOrchestrator(provider, setup[1], setup[2], seed_record=initial, selected_stages=["motion"]).run()
        assert len(requests) == 1 and recovered.subject_action.startswith("女子")
        assert recovered.perception_record.observation.facts == initial.observation.facts
        assert recovered.analysis_review["dynamic"]["status"] == "ready"
    finally:
        provider.unload()


def test_nested_compact_groups_flatten_without_guessing_evidence(setup):
    static, motion = stages(setup[2])
    wire = compact_stage(motion, setup[2])
    dynamics = {key.split(".")[1]: wire["fields"].pop(key) for key in list(wire["fields"]) if key.startswith("dynamics.")}
    wire["fields"]["dynamics"] = dynamics
    requests = []
    provider = provider_with(setup, [static, wire], requests)
    try:
        result = provider.analyze_shot(setup[2], setup[1])
        assert len(requests) == 2 and result.subject_action.startswith("男孩")
        assert not result.perception_record.correction_used
        assert any(item["repair"] == "flatten_known_group" for item in result.perception_record.raw_responses[1]["field_repairs"])
    finally:
        provider.unload()


def test_held_state_is_a_valid_dynamic_observation(setup):
    static, _ = stages(setup[2])
    motion = complete_motion(setup[2])
    for name in ("initial_state", "subject_action", "final_state"):
        motion["dynamics"][name] = observed("女子保持站立，双手停留在咖啡机旁", setup[2][0] if name == "initial_state" else setup[2][-1])
    motion["dynamics"]["action_steps"] = observed(["女子保持站立，双手停留在咖啡机旁"], setup[2][-1])
    provider = provider_with(setup, [static, motion], [])
    try:
        result = provider.analyze_shot(setup[2], setup[1])
        assert result.analysis_review["dynamic"]["status"] == "ready"
        assert result.analysis_review["video_ready"]
    finally:
        provider.unload()


def test_protocol_example_and_duration_hint_use_real_catalogue_and_half_seconds(setup):
    cfg, context, frames = setup
    context.end_time = 5.483333
    guide = compact_guide(MotionObservationV2, frames[:2])
    example = guide.split("唯一 JSON 对象格式：", 1)[1].split("。每个字段", 1)[0]
    assert json.loads(example)["fields"]["dynamics.subject_action"]["frames"] == ["F1", "F2"]
    provider = provider_with(setup, [], [])
    try:
        prompt = PerceptionOrchestrator(provider, context, frames)._prompt("motion", frames)
        assert "本镜建议生成时长为5.5秒" in prompt and "4.8秒" not in prompt
    finally:
        provider.unload()


def test_protocol_correction_respects_step_budget(setup):
    static, _ = stages(setup[2])
    requests = []
    provider = provider_with(setup, [static, bare_motion(setup[2])], requests)
    try:
        result = PerceptionOrchestrator(provider, setup[1], setup[2], PerceptionBudget(max_steps=3)).run()
        assert len(requests) == 2 and result.perception_record.budgets["used_steps"] == 3
        assert not result.perception_record.correction_used
        assert result.analysis_review["static"]["status"] == "ready"
        assert result.analysis_review["dynamic"]["status"] == "pending_review"
    finally:
        provider.unload()
