"""Keyless recovery and contradiction-review contracts."""
import copy
import time
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from app.perception_models import PerceptionRecord, MotionObservationV2, StaticObservationV2, empty_stage, merge_stages
from app.providers.bailian import BailianVisionProvider
from app.providers.base import FrameInput
from app.services.perception import PerceptionOrchestrator, infer_stage_records, recoverable_stages
from app.services.perception_revision import apply_revision, fresh_evidence_ids, review_observation
from test_perception import observed, provider_with, setup, stages


def seed_record(setup):
    static, motion = stages(setup[2])
    provider = provider_with(setup, [static, motion], [])
    try:
        return provider.analyze_shot(setup[2], setup[1]).perception_record
    finally:
        provider.unload()


def extra_frames(setup):
    path = setup[1].artifact_dir + "/new-motion.jpg"
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (96, 160), (20, 150, 160)).save(path)
    return [*setup[2], FrameInput("additional-motion", 1.1, path, 96, 160, "motion")]


def compact(value, aliases, note="新增帧中可见该具体物体及状态"):
    return dict(value=value, frames=aliases, note=note, confidence=.9)


def scene_patch():
    # Extra at 1.1s is F3, before the existing 1.3s/1.8s frames.
    return {"fields": {"facts.scene": compact("有水槽的洗衣间", ["F3"])},
            "rationale": "新增帧可见洗衣机与水槽，原来的厨房场景判断不符可见证据"}


def test_unknowns_are_valid_stage_results_not_transport_failure(setup):
    requests = []
    provider = provider_with(setup, [empty_stage(StaticObservationV2, "没有足够可见线索").model_dump(),
                                     empty_stage(MotionObservationV2, "无法判断变化").model_dump()], requests)
    try:
        record = provider.analyze_shot(setup[2], setup[1]).perception_record
        assert len(requests) == 2
        assert all(stage.state == "succeeded" and stage.error is None for stage in record.stages.values())
        assert record.stages["static"].unknown_fields
        assert not recoverable_stages(record)
        assert record.stages["static"].usage[0]["usage_available"] is True
        assert record.stages["motion"].attempts == 1
    finally:
        provider.unload()


def test_transport_failure_defers_motion_without_counting_a_request(setup):
    requests = []
    def handler(request):
        requests.append(request)
        raise httpx.ConnectTimeout("secret-proxy-user:password", request=request)
    provider = BailianVisionProvider(setup[0], httpx.MockTransport(handler))
    try:
        record = provider.analyze_shot(setup[2], setup[1]).perception_record
        assert len(requests) == 1
        assert record.stages["static"].state == "failed"
        assert record.stages["static"].error["kind"] == "transport"
        assert record.stages["motion"].state == "deferred" and record.stages["motion"].attempts == 0
        assert "secret-proxy" not in record.model_dump_json()
    finally:
        provider.unload()


def test_motion_only_recovery_preserves_static_style_and_attempt_history(setup):
    static, motion = stages(setup[2])
    requests = []
    provider = provider_with(setup, [static, "broken JSON"], requests)
    # Isolate explicit recovery from the normal pipeline's new one-call repair.
    from app.services.visual_tools import PerceptionBudget
    record = PerceptionOrchestrator(provider, setup[1], setup[2], PerceptionBudget(max_corrections=0)).run().perception_record
    provider.unload()
    saved = copy.deepcopy(record.model_dump())
    recovered_requests = []
    provider = provider_with(setup, [motion], recovered_requests)
    try:
        result = PerceptionOrchestrator(provider, setup[1], setup[2], seed_record=saved, selected_stages=["motion"]).run()
        recovered = result.perception_record
        assert len(recovered_requests) == 1 and recovered.budgets["used_model_calls"] == 1
        assert recovered.budgets["max_model_calls"] == 2
        assert recovered.stages["motion"].attempts == 2 and recovered.stages["motion"].state == "succeeded"
        assert recovered.stages["static"] == record.stages["static"]
        assert recovered.observation.facts == record.observation.facts
        assert recovered.observation.artistic_effects.model_dump(exclude={"motion_signature"}) == record.observation.artistic_effects.model_dump(exclude={"motion_signature"})
        assert len(recovered.raw_responses) == 3 and saved == record.model_dump()
    finally:
        provider.unload()


def test_recovery_has_no_schema_correction_or_automatic_supplement(setup):
    record = seed_record(setup)
    record.stages["motion"].state = "failed"
    record.stages["motion"].error = {"kind": "schema"}
    record.normalized_stages.pop("motion")
    requests = []
    provider = provider_with(setup, ["not JSON"], requests)
    try:
        result = PerceptionOrchestrator(provider, setup[1], setup[2], seed_record=record, selected_stages=["motion"]).run()
        assert len(requests) == 1
        assert result.perception_record.stages["motion"].state == "failed"
        assert result.perception_record.stages["motion"].error["kind"] == "schema"
        assert not result.perception_record.correction_used
        assert not result.perception_record.supplemental_call_used
    finally:
        provider.unload()


def test_failed_static_recovery_keeps_motion_deferred_and_under_two_calls(setup):
    record = seed_record(setup)
    for stage in record.stages.values():
        stage.state = "failed"
    requests = []
    provider = provider_with(setup, ["not JSON"], requests)
    try:
        result = PerceptionOrchestrator(provider, setup[1], setup[2], seed_record=record, selected_stages=["static", "motion"]).run()
        assert len(requests) == 1
        assert result.perception_record.stages["motion"].state == "deferred"
        assert result.perception_record.stages["motion"].error["kind"] == "dependency"
    finally:
        provider.unload()


def test_recovering_both_stages_calls_each_once_and_skips_success_on_repeat(setup):
    record = seed_record(setup)
    for stage in record.stages.values():
        stage.state = "failed"
    static, motion = stages(setup[2])
    requests = []
    provider = provider_with(setup, [static, motion], requests)
    try:
        result = PerceptionOrchestrator(provider, setup[1], setup[2], seed_record=record, selected_stages=["motion", "static"]).run()
        assert len(requests) == 2
        assert result.perception_record.budgets["used_model_calls"] == 2
        second = PerceptionOrchestrator(provider, setup[1], setup[2], seed_record=result.perception_record, selected_stages=["static", "motion"]).run()
        assert len(requests) == 2 and second.perception_record.budgets["used_model_calls"] == 0
    finally:
        provider.unload()


def test_old_record_health_is_optional_and_inferred_from_raw_stages(setup):
    record = seed_record(setup).model_dump()
    for name in ("stages", "input_frames", "revisions", "source_revision"):
        record.pop(name)
    assert PerceptionRecord.model_validate(record).stages == {}
    assert all(stage.state == "succeeded" for stage in infer_stage_records(record).values())


def test_valid_source_revision_keeps_photography_style_and_old_new_rationale(setup):
    record = seed_record(setup)
    record.source_revision = 7
    original = record.model_dump()
    result = apply_revision(record, scene_patch(), extra_frames(setup), setup[1], "scene", "地点识别错了", source_revision=7)
    revised = result.perception_record
    assert revised.observation.facts.scene.value == "有水槽的洗衣间"
    assert revised.observation.photography == record.observation.photography
    assert revised.observation.artistic_effects == record.observation.artistic_effects
    audit = revised.revisions[-1]
    assert audit["status"] == "applied" and audit["reason"] == "地点识别错了"
    assert audit["patches"][0]["old"]["value"] == "厨房"
    assert audit["patches"][0]["new"]["value"] == "有水槽的洗衣间"
    assert audit["patches"][0]["rationale"]
    assert record.model_dump() == original


@pytest.mark.parametrize("problem", ["invented-frame", "outside", "photography", "causal", "same-evidence", "wrong-version", "empty-rationale"])
def test_invalid_revision_preserves_original_with_rejected_audit(setup, problem):
    record = seed_record(setup)
    record.source_revision = 7
    frames = extra_frames(setup)
    proposal = scene_patch()
    source_revision = 7
    if problem == "invented-frame":
        proposal["fields"]["facts.scene"]["frames"] = ["F99"]
    elif problem == "outside":
        frames[-1] = replace(frames[-1], timestamp=2.2)
    elif problem == "photography":
        proposal["fields"]["photography.shot_size"] = compact("特写", ["F3"])
    elif problem == "causal":
        proposal["fields"]["facts.scene"]["value"] = "因为主人想要洗衣服所以进入洗衣间"
    elif problem == "same-evidence":
        proposal["fields"]["facts.scene"]["frames"] = ["F1"]
    elif problem == "wrong-version":
        source_revision = 8
    elif problem == "empty-rationale":
        proposal["rationale"] = ""
    revised = apply_revision(record, proposal, frames, setup[1], "scene", "地点识别错误", source_revision=source_revision).perception_record
    assert revised.observation == record.observation
    assert revised.revisions[-1]["status"] == "rejected"
    assert revised.revisions[-1]["error"]


def test_identical_image_at_new_timestamp_and_id_is_not_new_evidence(setup):
    record = seed_record(setup)
    identical = replace(setup[2][0], id="renamed", timestamp=1.1, artifact_kind="motion")
    assert not fresh_evidence_ids(record, [*setup[2], identical], setup[1])
    requests = []
    provider = provider_with(setup, [], requests)
    try:
        revised = review_observation(provider, setup[1], [*setup[2], identical], record, "scene", "地点错误").perception_record
        assert not requests and revised.observation == record.observation
        assert revised.revisions[-1]["status"] == "rejected"
    finally:
        provider.unload()


def test_legacy_uncited_keyframe_cannot_be_renamed_into_extra_evidence(setup):
    record = PerceptionRecord(observation=seed_record(setup).observation)
    uncited = setup[2][1]
    duplicate = replace(uncited, id="renamed-uncited", timestamp=1.1, artifact_kind="motion")
    assert not fresh_evidence_ids(record, [*setup[2], duplicate], setup[1])


def test_expired_local_budget_does_not_count_an_unsent_recovery_request(setup):
    record = seed_record(setup)
    record.stages["static"].state = "failed"
    requests = []
    provider = provider_with(setup, [], requests)
    try:
        orchestrator = PerceptionOrchestrator(provider, setup[1], setup[2], seed_record=record, selected_stages=["static"])
        orchestrator.started = time.monotonic() - provider.settings.vlm_timeout_seconds - 1
        result = orchestrator.run().perception_record
        assert not requests
        assert result.budgets["used_model_calls"] == 0
        assert len(result.raw_responses) == len(record.raw_responses)
        assert result.stages["static"].attempts == record.stages["static"].attempts
    finally:
        provider.unload()


def test_review_one_request_and_no_hidden_retry_even_for_invalid_json(setup):
    record = seed_record(setup)
    requests = []
    provider = provider_with(setup, ["not JSON"], requests)
    try:
        revised = review_observation(provider, setup[1], extra_frames(setup), record, "scene", "地点错误").perception_record
        assert len(requests) == 1 and revised.budgets["max_model_calls"] == 1
        assert revised.observation == record.observation and revised.revisions[-1]["status"] == "rejected"
        assert revised.raw_responses[-1]["phase"] == "shot.review"
    finally:
        provider.unload()


def test_review_accepts_explicit_new_evidence_once_and_repeated_evidence_is_rejected(setup):
    record = seed_record(setup)
    record.supplemental_call_used = True
    frames = extra_frames(setup)
    requests = []
    provider = provider_with(setup, [scene_patch()], requests)
    try:
        result = review_observation(provider, setup[1], frames, record, "scene", "地点错误")
        assert len(requests) == 1 and result.perception_record.revisions[-1]["status"] == "applied"
        assert result.perception_record.revisions[-1]["usage"][0]["phase"] == "shot.review"
        repeat = review_observation(provider, setup[1], frames, result.perception_record, "scene", "再检查地点")
        assert len(requests) == 1 and repeat.perception_record.revisions[-1]["status"] == "rejected"
    finally:
        provider.unload()


def action_patch():
    return {"fields": {
        "dynamics.subject_action": compact("男孩伸手后把杯子向左推开", ["F1", "F3", "F5"]),
        "dynamics.initial_state": compact("男孩的手靠近杯子", ["F1", "F3"]),
        "dynamics.action_steps": compact(["男孩伸手接触杯子", "男孩把杯子向左推开"], ["F1", "F3", "F5"]),
        "dynamics.final_state": compact("杯子停在台面左侧", ["F3", "F5"]),
    }, "rationale": "新增中间帧可见杯子沿台面向左移动，并没有离开台面，修正旧的拿起动作"}


def test_action_revision_is_atomic_and_requires_ordered_begin_steps_result(setup):
    record = seed_record(setup)
    frames = extra_frames(setup)
    revised = apply_revision(record, action_patch(), frames, setup[1], "action", "杯子没有拿起").perception_record
    assert revised.revisions[-1]["status"] == "applied"
    assert revised.observation.dynamics.subject_action.value.endswith("向左推开")
    invalid = action_patch()
    invalid["fields"].pop("dynamics.final_state")
    denied = apply_revision(record, invalid, frames, setup[1], "action", "杯子没有拿起").perception_record
    assert denied.revisions[-1]["status"] == "rejected" and denied.observation == record.observation
    invalid = action_patch()
    invalid["fields"]["dynamics.final_state"]["frames"] = ["F1", "F3"]
    denied = apply_revision(record, invalid, frames, setup[1], "action", "杯子没有拿起").perception_record
    assert denied.revisions[-1]["status"] == "rejected" and denied.observation == record.observation


def test_cancellation_between_recovery_stages_never_sends_motion_request(setup):
    record = seed_record(setup)
    for stage in record.stages.values():
        stage.state = "failed"
    static, _ = stages(setup[2])
    requests = []
    provider = provider_with(setup, [static], requests)
    try:
        result = PerceptionOrchestrator(provider, setup[1], setup[2], seed_record=record,
            selected_stages=["static", "motion"], cancelled=lambda: bool(requests)).run()
        assert len(requests) == 1
        assert result.perception_record.stages["static"].state == "succeeded"
        assert result.perception_record.stages["motion"].state == "deferred"
        assert result.perception_record.stages["motion"].error["kind"] == "cancelled"
        assert result.perception_record.budgets["cancelled"] is True
        assert result.perception_record.budgets["used_model_calls"] == 1
    finally:
        provider.unload()


def test_reversed_per_step_evidence_is_rejected_atomically(setup):
    record = seed_record(setup)
    frames = extra_frames(setup)
    proposal = action_patch()
    proposal["fields"]["dynamics.action_steps"] = [
        compact("男孩先伸手接触杯子", ["F3", "F5"]),
        compact("男孩随后把杯子向左推开", ["F1", "F3"]),
    ]
    revised = apply_revision(record, proposal, frames, setup[1], "action", "步骤顺序需要复核").perception_record
    assert revised.revisions[-1]["status"] == "rejected"
    assert "逆时序" in revised.revisions[-1]["error"]
    assert revised.observation == record.observation


def test_middle_frame_cannot_be_revised_final_state(setup):
    record = seed_record(setup)
    frames = extra_frames(setup)
    proposal = action_patch()
    proposal["fields"]["dynamics.action_steps"] = compact(["伸手", "将杯子推向左侧"], ["F1", "F3"])
    proposal["fields"]["dynamics.final_state"] = compact("杯子停在台面左侧", ["F3"])
    revised = apply_revision(record, proposal, frames, setup[1], "action", "结果被当成中间状态").perception_record
    assert revised.revisions[-1]["status"] == "rejected"
    assert "最新源帧" in revised.revisions[-1]["error"]
    assert revised.observation == record.observation


def test_partial_action_patch_retains_saved_anchors_and_validates_whole_group(setup):
    record = seed_record(setup)
    record.observation.dynamics.initial_state = type(record.observation.dynamics.initial_state).model_validate(observed("男孩的手靠近杯子", setup[2][0]))
    record.observation.dynamics.final_state = type(record.observation.dynamics.final_state).model_validate(observed("杯子与水壶紧贴", setup[2][-1]))
    frames = extra_frames(setup)
    path = setup[1].artifact_dir + "/second-new-motion.jpg"
    Image.new("RGB", (96, 160), (100, 200, 10)).save(path)
    frames.append(FrameInput("latest-additional-motion", 1.9, path, 96, 160, "motion"))
    proposal = {"fields": {
        "dynamics.action_steps": compact(["男孩把杯子向左推开", "杯子靠近水壶但仍有空隙"], ["F1", "F3", "F5", "F6"], "最后几帧F5-F6中始终有可见空隙"),
        "dynamics.final_state": compact("杯子与水壶之间保留可见空隙", ["F6"]),
    }, "rationale": "新增末帧显示两者之间的可见空隙，旧接触描述缺乏证据"}
    result = apply_revision(record, proposal, frames, setup[1], "action", "没有真的接触").perception_record
    assert result.revisions[-1]["status"] == "applied"
    assert result.observation.dynamics.initial_state == record.observation.dynamics.initial_state
    assert result.observation.dynamics.subject_action == record.observation.dynamics.subject_action
    assert result.observation.photography == record.observation.photography
    assert result.observation.artistic_effects == record.observation.artistic_effects
    assert len(result.revisions[-1]["validated_field_group"]) == 4
    assert {patch["field"] for patch in result.revisions[-1]["patches"]} == {"dynamics.action_steps", "dynamics.final_state"}
    proposal["fields"]["dynamics.action_steps"]["note"] = "两者全程始终保持空隙"
    denied = apply_revision(record, proposal, frames, setup[1], "action", "没有真的接触").perception_record
    assert denied.revisions[-1]["status"] == "rejected" and denied.observation == record.observation
    for unbounded_note in ("F1证明它全程始终接触", "F4-F6证明它始终接触", "最后几帧证明它全程始终接触"):
        proposal["fields"]["dynamics.action_steps"]["note"] = unbounded_note
        denied = apply_revision(record, proposal, frames, setup[1], "action", "没有真的接触").perception_record
        assert denied.revisions[-1]["status"] == "rejected" and denied.observation == record.observation


def test_cancellation_before_recovery_or_review_sends_no_request(setup):
    record = seed_record(setup)
    record.stages["motion"].state = "failed"
    requests = []
    provider = provider_with(setup, [], requests)
    try:
        recovery = PerceptionOrchestrator(provider, setup[1], setup[2], seed_record=record,
            selected_stages=["motion"], cancelled=lambda: True).run()
        review = review_observation(provider, setup[1], extra_frames(setup), record, "scene", "场景判断错了", cancelled=lambda: True)
        assert not requests
        assert recovery.perception_record.budgets["used_model_calls"] == 0
        assert review.perception_record.budgets["used_model_calls"] == 0
        assert review.perception_record.budgets["cancelled"] is True
        assert review.perception_record.observation == record.observation
    finally:
        provider.unload()
