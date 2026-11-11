"""Keyless Flash contract tests; these do NOT measure real model quality."""
import copy
import json
import shutil
import subprocess
import time
from dataclasses import replace

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from app.config import load_settings
from app.perception_models import (
    EvidenceRef, MotionObservationV2, ObservedField, StaticObservationV2,
    empty_stage, merge_stages, project_v1, validate_evidence,
)
from app.providers.bailian import BailianVisionProvider, estimate_list_price_cny
from app.providers.base import FrameInput, ShotContext
from app.services.perception import PerceptionOrchestrator
from app.services.visual_tools import LocalToolRegistry, PerceptionBudget, ToolBudgetExceeded


def observed(value, frame, confidence=0.85):
    return dict(value=value, observable=True, confidence=confidence,
                evidence=[dict(frame_id=frame.id, timestamp=frame.timestamp,
                               bbox=None, crop_artifact_id=None, description="画面中可见主体或状态")],
                uncertainties=[])


def stages(frames):
    static = empty_stage(StaticObservationV2, "不适用").model_dump()
    for key, value in dict(primary_subject="男孩", scene="厨房", objects=["男孩", "杯子"]).items():
        static["facts"][key] = observed(value, frames[0])
    for key, value in dict(shot_size="近景", lens="标准", lighting_vibe="窗边暖光").items():
        static["photography"][key] = observed(value, frames[0])
    motion = empty_stage(MotionObservationV2, "不适用").model_dump()
    motion["dynamics"]["subject_action"] = observed("男孩向前伸手拿起桌上的杯子", frames[-1])
    motion["dynamics"]["action_steps"] = observed(["伸手", "拿起杯子"], frames[-1])
    motion["camera_movement_type"] = observed("固定", frames[-1])
    motion["camera_movement_detail"] = observed("背景和主体尺度保持稳定", frames[-1])
    return static, motion


@pytest.fixture
def setup(tmp_path):
    frames = []
    for n, timestamp in enumerate((0.2, 0.7, 1.3, 1.8)):
        path = tmp_path / f"f{n}.jpg"
        Image.new("RGB", (96, 160), (n * 40, 60, 90)).save(path)
        frames.append(FrameInput(f"frame-{n}", timestamp, str(path), 96, 160))
    cfg = replace(load_settings(), cloud_model="qwen3.8-flash", provider="bailian",
                  cloud_api_key="fixture-not-a-real-key",
                  cloud_base_url="https://fixture.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                  perception_v2_enabled=True)
    context = ShotContext("shot-1", 1, 0, 2, "", artifact_dir=str(tmp_path / "evidence"))
    return cfg, context, frames


def provider_with(setup, replies, requests):
    cfg, _, frames = setup
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        result = replies.pop(0)
        if isinstance(result, int):
            return httpx.Response(result)
        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return httpx.Response(200, json={
            "id": f"request-{len(requests)}", "model": "qwen3.8-flash",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
        })
    provider = BailianVisionProvider(cfg, httpx.MockTransport(handler))
    provider.helper._sharpest_image = lambda paths: frames[0].path
    return provider


def test_flash_default_when_environment_omitted(monkeypatch):
    monkeypatch.delenv("STORYBOARD_CLOUD_MODEL", raising=False)
    assert load_settings().cloud_model == "qwen3.8-flash"


def test_motion_prompt_separates_visible_changes_from_guessed_intent(setup):
    cfg, context, frames = setup
    provider = BailianVisionProvider(cfg, httpx.MockTransport(lambda request: httpx.Response(500)))
    prompt = PerceptionOrchestrator(provider, context, frames)._prompt("motion", frames)
    assert "解释移入 uncertainties" in prompt
    assert "嘴部张合不等于交谈" in prompt
    assert "变化之前和之后" in prompt
    provider.unload()


def test_flash_two_stages_schema_and_deterministic_projection(setup):
    requests = []
    static, motion = stages(setup[2])
    provider = provider_with(setup, [static, motion], requests)
    result = provider.analyze_shot(list(reversed(setup[2])), setup[1])
    assert result.subject_action == motion["dynamics"]["subject_action"]["value"]
    assert result.shot_size == "近景" and result.lens == "标准"
    assert result.perception_record.supplemental_call_used is False
    assert result.perception_record.budgets["used_steps"] == 3
    assert "perception_record" not in result.model_dump()
    for body in requests:
        assert body["model"] == "qwen3.8-flash"
        assert body["enable_thinking"] is False and body["temperature"] == 0.1
        assert body["response_format"]["type"] == "json_object"
        assert "输出 JSON Schema" not in body["messages"][0]["content"][0]["text"]
    phases = provider.drain_usage()
    assert [row["phase"] for row in phases] == ["shot.static", "shot.motion"]
    assert phases[0]["schema_enforcement"] == "json-object"
    assert phases[0]["shot_id"] == setup[1].shot_id
    assert phases[0]["estimated_list_price_cny"] == pytest.approx(0.00134)
    provider.unload()


def test_invalid_enum_only_drops_that_field_without_paid_correction(setup):
    static, motion = stages(setup[2])
    invalid = copy.deepcopy(static)
    invalid["photography"]["shot_size"]["value"] = "超广角"
    requests = []
    provider = provider_with(setup, [invalid, motion], requests)
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.shot_size == "未知" and not result.perception_record.correction_used
    assert result.lens == "标准" and result.subject_action.startswith("男孩")
    assert len(requests) == 2
    assert result.perception_record.raw_responses[0]["field_repairs"]
    assert [row["phase"] for row in provider.drain_usage()] == [
        "shot.static", "shot.motion",
    ]
    provider.unload()


def test_invalid_static_preserves_motion_without_text_correction(setup):
    _, motion = stages(setup[2])
    requests = []
    provider = provider_with(setup, ["not JSON", motion], requests)
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.subject_action.startswith("男孩")
    assert result.shot_size == "未知" and result.confidence <= 0.3
    assert result.analysis_warnings
    assert len(requests) == 2
    assert len(result.perception_record.raw_responses) == 2
    provider.unload()


def compact_stage(stage, frames):
    from app.services.perception_wire import field_paths
    model = StaticObservationV2 if "facts" in stage else MotionObservationV2
    lookup = {frame.id: f"F{n+1}" for n, frame in enumerate(sorted(frames, key=lambda f: f.timestamp))}
    fields = {}
    for key, (group, name, _) in field_paths(model).items():
        value = stage[group][name] if group else stage[name]
        fields[key] = dict(value=value["value"], frames=[lookup[ref["frame_id"]] for ref in value["evidence"]],
                           note="画面可见主体、物体及状态" if value["observable"] else "",
                           confidence=value["confidence"], uncertainty="；".join(value["uncertainties"]))
    return {"fields": fields, "uncertainties": []}


def test_compact_two_stages_evidence_mapping_and_style(setup):
    static, motion = stages(setup[2])
    for key, value in dict(medium="3D动画", character_design="大眼睛卷发男孩，头身比例夸张", rendering="柔光三维渲染", color_palette="暖米色厨房与蓝白衣服", texture="光滑角色表面与织物纹理").items():
        static["artistic_effects"][key] = observed(value, setup[2][0])
    requests = []
    provider = provider_with(setup, [compact_stage(static, setup[2][:1]), compact_stage(motion, setup[2])], requests)
    result = provider.analyze_shot(list(reversed(setup[2])), setup[1])
    assert len(requests) == 2 and not result.perception_record.correction_used
    assert result.visual_style_evidence.medium == "3D动画"
    assert result.visual_style_evidence.character_design.startswith("大眼睛")
    assert result.visual_style_evidence.evidence
    assert result.action_details.evidence[0].startswith(setup[2][-1].id)
    assert requests[0]["max_completion_tokens"] == 2304
    assert requests[1]["max_completion_tokens"] == 1536
    assert "静态主体清单" in requests[1]["messages"][0]["content"][0]["text"]
    assert "bbox 是图像归一化" not in requests[0]["messages"][0]["content"][0]["text"]
    provider.unload()


def test_static_representative_f1_is_not_dynamic_first_frame(setup):
    static, motion = stages(setup[2])
    representative = setup[2][2]
    static["facts"]["primary_subject"] = observed("中间帧卷发男孩", representative)
    # Static response's F1 must map to the only attached representative image.
    static_wire = compact_stage(static, [setup[2][0], representative])
    for field in static_wire["fields"].values():
        if field["frames"]:
            field["frames"] = ["F1"]
    provider = provider_with(setup, [static_wire, compact_stage(motion, setup[2])], [])
    provider.helper._sharpest_image = lambda paths: representative.path
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.perception_record.observation.facts.primary_subject.evidence[0].frame_id == representative.id
    assert result.perception_record.raw_responses[0]["input_frames"][0]["frame_id"] == representative.id
    assert result.perception_record.raw_responses[1]["input_frames"][0]["frame_id"] == setup[2][0].id
    provider.unload()


def test_partial_stage_metrics_do_not_claim_first_response_success(setup):
    from evals.perception_review import technical_summary
    static, motion = stages(setup[2])
    static["photography"]["shot_size"]["value"] = "超广角"
    provider = provider_with(setup, [static, motion], [])
    record = provider.analyze_shot(setup[2], setup[1]).perception_record
    report = {"clips": [{"id": "clip", "model_metrics": [{"number": 1, "perception": record.model_dump()}]}], "model_usage": provider.drain_usage()}
    metrics = technical_summary(report)
    assert metrics["first_response_parse_rate"] == 0.5
    assert metrics["partial_stage_rate"] == 0.5
    assert metrics["field_repair_count"] == 1
    provider.unload()


def test_evidenced_step_list_normalizes_without_fabricating_refs(setup):
    static, motion = stages(setup[2])
    compact = compact_stage(motion, setup[2])
    compact["fields"]["dynamics.action_steps"] = [
        dict(value="男孩伸手", frames=["F1"], note="F1手伸向杯子", confidence=0.9),
        dict(value="男孩拿起杯子", frames=["F4"], note="F4杯子已拿起", confidence=0.85),
    ]
    provider = provider_with(setup, [static, compact], [])
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.action_details.steps == ["男孩伸手", "男孩拿起杯子"]
    assert [[ref.frame_id for ref in refs] for refs in result.action_details.step_evidence] == [[setup[2][0].id], [setup[2][-1].id]]
    assert result.perception_record.budgets["used_model_calls"] == 2
    provider.unload()


def phase_wire(setup):
    static, motion = stages(setup[2])
    motion["dynamics"]["subject_action"] = observed("女子拿起杯子饮用后放下杯子并皱眉", setup[2][-1])
    motion["dynamics"]["action_steps"] = observed(["女子拿起杯子", "女子饮用液体", "女子放下杯子", "女子皱眉"], setup[2][-1])
    wire = compact_stage(motion, setup[2])
    wire["fields"]["dynamics.action_phases"] = [
        dict(description="女子拿起杯子并饮用", source_step_indices=[1, 2], frames=["F1", "F2"], note="早段可见杯子靠近面部", confidence=.9,
             generation_start_seconds=0.0, generation_end_seconds=1.2),
        dict(description="女子放下杯子并皱眉", source_step_indices=[3, 4], frames=["F3", "F4"], note="后段杯子远离面部且眉头收紧", confidence=.9,
             generation_start_seconds=1.2, generation_end_seconds=2.0),
    ]
    wire["fields"]["dynamics.accompanying_states"] = dict(value=["男孩抬头注视女子"], frames=["F2", "F3"], note="这两帧男孩抬眼朝向女子", confidence=.8)
    return static, wire


def test_phase_wire_projects_and_renders_two_not_four_ranges_in_same_calls(setup):
    from app.services.prompt_service import build_prompts
    requests = []
    static, wire = phase_wire(setup)
    provider = provider_with(setup, [static, wire], requests)
    result = provider.analyze_shot(setup[2], setup[1])
    assert len(requests) == 2 and not result.perception_record.supplemental_call_used
    assert len(result.action_details.action_phases) == 2
    assert result.action_details.accompanying_states == ["男孩抬头注视女子"]
    prompts = build_prompts("shot-1", result, generation_duration_seconds=2.0, style_strategy="qwen-direct-v3")
    first, video = prompts
    assert first.template_id == "first-frame-v7" and video.template_id == "seedance-video-v8"
    assert "0–1 秒" in video.positive_prompt and "1–2 秒" in video.positive_prompt
    assert "0.5–1 秒" not in video.positive_prompt
    assert "不是原片动作时间实测" not in video.positive_prompt
    assert video.contract.delivery["display_timing"]["exact_duration_seconds"] == 2.0
    assert "阶段" not in first.positive_prompt and "皱眉" not in first.positive_prompt
    presentation = video.contract.delivery["action_presentation"]
    assert presentation["phases"][0]["evidence"][0]["frame_id"] == setup[2][0].id
    assert result.perception_record.normalized_stages["motion"]["dynamics"]["action_phases"]
    motion_prompt = requests[1]["messages"][0]["content"][0]["text"]
    assert "本镜建议生成时长为2秒" in motion_prompt
    assert "不要强制开始/发展/结果/结束四段" in motion_prompt
    assert "不要按动作数量均分" in motion_prompt
    provider.unload()


@pytest.mark.parametrize("problem", ["bad-frame", "neighbor", "missing-step", "duplicate-step", "bool-index", "string-index", "too-many", "low-confidence"])
def test_invalid_optional_phases_never_erase_motion_or_retry(setup, problem):
    static, wire = phase_wire(setup)
    phases = wire["fields"]["dynamics.action_phases"]
    if problem == "bad-frame":
        phases[0]["frames"] = ["F99"]
    elif problem == "neighbor":
        # This attached frame is outside the shot and cannot support its fact.
        extra = replace(setup[2][0], id="neighbor", timestamp=-0.1)
        from app.services.perception_wire import decode_stage
        with_frames = [*setup[2], extra]
        parsed, _, warnings = decode_stage(json.dumps(wire), MotionObservationV2, with_frames, 0, 2)
        assert not parsed.dynamics.action_phases and warnings
        assert parsed.dynamics.subject_action.observable
        return
    elif problem == "missing-step":
        phases[1]["source_step_indices"] = [4]
    elif problem == "duplicate-step":
        phases[1]["source_step_indices"] = [2, 3, 4]
    elif problem == "bool-index":
        phases[0]["source_step_indices"] = [True, 2]
    elif problem == "string-index":
        phases[0]["source_step_indices"] = ["1", 2]
    elif problem == "too-many":
        phases.extend(copy.deepcopy(phases))
    elif problem == "low-confidence":
        phases[0]["confidence"] = .2
    requests = []
    provider = provider_with(setup, [static, wire], requests)
    result = provider.analyze_shot(setup[2], setup[1])
    assert len(requests) == 2 and result.subject_action.startswith("女子")
    assert result.shot_size == "近景" and len(result.action_details.steps) == 4
    assert not result.action_details.action_phases
    assert any("action_phases" in warning for warning in result.analysis_warnings)
    provider.unload()


@pytest.mark.parametrize("invalid", [5, -1.0, "soon", None])
def test_bad_optional_generation_time_retains_evidenced_phase_order(setup, invalid):
    from app.services.prompt_service import build_prompts
    static, wire = phase_wire(setup)
    # A long but numeric interval is structurally valid and rejected in delivery;
    # malformed optional endpoints are removed locally without another request.
    wire["fields"]["dynamics.action_phases"][0]["generation_end_seconds"] = invalid
    provider = provider_with(setup, [static, wire], [])
    result = provider.analyze_shot(setup[2], setup[1])
    assert len(result.action_details.action_phases) == 2
    video = build_prompts("s", result, generation_duration_seconds=2)[1]
    assert video.contract.delivery["action_presentation"]["timing_mode"] == "sequence"
    assert "0–1.2 秒" not in video.positive_prompt and "阶段 1" in video.positive_prompt
    assert result.perception_record.budgets["used_model_calls"] == 2
    provider.unload()


@pytest.mark.parametrize("field", ["action_steps", "accompanying_states"])
def test_overlong_optional_lists_are_field_local_failures(setup, field):
    static, wire = phase_wire(setup)
    wire["fields"][f"dynamics.{field}"] = dict(value=["女子持杯"] * 5, frames=["F1"], note="持杯状态可见", confidence=.9)
    provider = provider_with(setup, [static, wire], [])
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.subject_action.startswith("女子") and result.shot_size == "近景"
    assert result.perception_record.budgets["used_model_calls"] == 2
    assert any(field in warning for warning in result.analysis_warnings)
    provider.unload()


def test_old_motion_and_perception_records_read_without_new_fields(setup):
    from app.perception_models import PerceptionRecord
    static, motion = stages(setup[2])
    for name in ("accompanying_states", "action_phases", "step_evidence"):
        motion["dynamics"].pop(name, None)
    old_motion = MotionObservationV2.model_validate(motion)
    assert old_motion.dynamics.accompanying_states.value == []
    assert not old_motion.dynamics.accompanying_states.observable
    merged = merge_stages(StaticObservationV2.model_validate(static), old_motion).model_dump()
    for name in ("accompanying_states", "action_phases", "step_evidence"):
        merged["dynamics"].pop(name, None)
    record = PerceptionRecord.model_validate({"observation": merged})
    projected = project_v1(record.observation)
    assert projected.subject_action == "男孩向前伸手拿起桌上的杯子"
    assert projected.action_details.accompanying_states == []
    assert projected.action_details.action_phases == []


def test_sampled_companion_does_not_prove_full_shot_persistence(setup):
    static, wire = phase_wire(setup)
    wire["fields"]["dynamics.accompanying_states"]["value"] = ["男孩全程注视女子"]
    provider = provider_with(setup, [static, wire], [])
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.action_details.accompanying_states == []
    assert len(result.action_details.action_phases) == 2 and result.subject_action.startswith("女子")
    assert result.perception_record.budgets["used_model_calls"] == 2
    provider.unload()


def test_bare_steps_without_evidence_are_not_promoted_to_fact(setup):
    static, motion = stages(setup[2])
    compact = compact_stage(motion, setup[2])
    compact["fields"]["dynamics.action_steps"] = ["伸手", "拿杯子"]
    provider = provider_with(setup, [static, compact], [])
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.action_details.steps == [] and result.subject_action.startswith("男孩")
    assert result.perception_record.budgets["used_model_calls"] == 2
    provider.unload()


def test_rendering_implementation_is_uncertain_not_visual_fact(setup):
    static, motion = stages(setup[2])
    static["artistic_effects"]["medium"] = observed("3D动画", setup[2][0])
    static["artistic_effects"]["rendering"] = observed("次表面散射与全局光照", setup[2][0])
    provider = provider_with(setup, [static, motion], [])
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.visual_style_evidence.medium == "3D动画"
    assert result.visual_style_evidence.rendering == "未知"
    assert "次表面散射与全局光照" in result.visual_style_evidence.uncertain_techniques
    assert result.subject_action.startswith("男孩")
    provider.unload()


def test_real_failure_pattern_1000_bbox_does_not_erase_action(setup):
    static, motion = stages(setup[2])
    motion["dynamics"]["subject_action"]["value"] = "搅拌并舀起粉色液体"
    for group in (motion["dynamics"], {key: value for key, value in motion.items() if key.startswith("camera_")}):
        for field in group.values():
            if not isinstance(field, dict) or "evidence" not in field:
                continue
            for ref in field["evidence"]:
                ref["bbox"] = [240, 60, 850, 950]
    requests = []
    provider = provider_with(setup, [static, motion], requests)
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.subject_action == "搅拌并舀起粉色液体"
    assert len(requests) == 2 and not result.perception_record.supplemental_call_used
    assert result.perception_record.raw_responses[-1]["field_repairs"]
    assert result.perception_record.observation.dynamics.subject_action.evidence[0].bbox is None
    assert not result.analysis_warnings  # Safe bbox repair is informational, not failed perception.
    provider.unload()


@pytest.mark.parametrize("unknown", ["unknown", "未知（看不清）", "UNKNOWN", "待判断"])
def test_unknown_alias_only_normalizes_one_field(setup, unknown):
    static, motion = stages(setup[2])
    motion["dynamics"]["interaction_target"] = dict(value=unknown, observable=False, confidence=0.0, evidence=[], uncertainties=["无可见互动对象"])
    provider = provider_with(setup, [static, motion], [])
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.subject_action.startswith("男孩") and result.action_details.interaction_target == "未知"
    assert result.perception_record.budgets["used_model_calls"] == 2
    provider.unload()


@pytest.mark.parametrize("bad_static,bad_motion", [("[]", '{"$defs":{},"type":"object"}'), ("not JSON", "[]")])
def test_empty_or_schema_echo_never_retries_whole_shot(setup, bad_static, bad_motion):
    requests = []
    provider = provider_with(setup, [bad_static, bad_motion], requests)
    result = provider.analyze_shot(setup[2], setup[1])
    assert len(requests) == 2 and result.subject_action == "未知"
    assert result.analysis_warnings and not result.perception_record.supplemental_call_used
    assert not result.perception_record.correction_used
    provider.unload()


def test_invented_compact_evidence_drops_field_not_stage(setup):
    static, motion = stages(setup[2])
    compact = compact_stage(static, setup[2][:1])
    compact["fields"]["facts.scene"]["frames"] = ["F999"]
    provider = provider_with(setup, [compact, motion], [])
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.visual_facts.scene == "未知" and result.action_details.primary_subject == "男孩"
    assert result.subject_action.startswith("男孩")
    provider.unload()


def test_start_state_cannot_reference_last_frame(setup):
    static, motion = stages(setup[2])
    motion["dynamics"]["initial_state"] = observed("杯子已拿起", setup[2][-1])
    provider = provider_with(setup, [static, motion], [])
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.action_details.initial_state == "未知"
    assert result.subject_action.startswith("男孩")
    provider.unload()


def test_auth_failure_stops_after_one_call(setup):
    requests = []
    provider = provider_with(setup, [401], requests)
    result = provider.analyze_shot(setup[2], setup[1])
    assert len(requests) == 1 and result.confidence == 0
    assert any("鉴权失败" in warning for warning in result.analysis_warnings)
    provider.unload()


def test_connect_timeout_stops_motion_and_supplement(setup):
    requests = []
    def handler(request):
        requests.append(request)
        raise httpx.ConnectTimeout("private proxy details", request=request)
    provider = BailianVisionProvider(setup[0], httpx.MockTransport(handler))
    try:
        result = provider.analyze_shot(setup[2], setup[1])
        assert len(requests) == 1
        assert result.confidence == 0
        assert result.perception_record.budgets["network_failure"]
        assert result.perception_record.budgets["transport_error_category"] == "connect_timeout"
        assert result.perception_record.budgets["used_model_calls"] == 1
        assert not result.perception_record.supplemental_call_used
    finally:
        provider.unload()


def test_motion_timeout_preserves_static_without_supplement(setup):
    static, _ = stages(setup[2])
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps(static, ensure_ascii=False)}}]})
        raise httpx.ReadTimeout("response timed out", request=request)
    provider = BailianVisionProvider(setup[0], httpx.MockTransport(handler))
    provider.helper._sharpest_image = lambda paths: setup[2][0].path
    try:
        result = provider.analyze_shot(setup[2], setup[1])
        assert len(requests) == 2
        assert result.visual_facts.scene == "厨房"
        assert result.subject_action == "未知"
        assert result.perception_record.budgets["network_failure"]
        assert not result.perception_record.supplemental_call_used
    finally:
        provider.unload()


def test_camera_unknown_alone_is_not_a_supplement_trigger(setup):
    static, motion = stages(setup[2])
    motion["camera_movement_type"] = dict(value="未知", observable=False, confidence=0.0, evidence=[], uncertainties=["镜头运动未能判定"])
    provider = provider_with(setup, [static, motion], [])
    result = provider.analyze_shot(setup[2], setup[1])
    assert result.perception_record.budgets["used_model_calls"] == 2
    assert not result.perception_record.supplemental_call_used
    provider.unload()


def test_observable_unknown_and_missing_evidence_rejected(setup):
    invalid = observed("杯子", setup[2][0])
    invalid["observable"] = "true"
    with pytest.raises(ValidationError):
        ObservedField[str].model_validate(invalid)
    with pytest.raises(ValidationError):
        ObservedField[str].model_validate(dict(value="杯子", observable=True,
                                              confidence=0.8, evidence=[], uncertainties=[]))
    with pytest.raises(ValidationError):
        ObservedField[str].model_validate(observed("未知", setup[2][0]))
    with pytest.raises(ValidationError):
        EvidenceRef(frame_id="f", timestamp=1, description="杯子", bbox=(0, 0, 2, 1))


@pytest.mark.parametrize("defect", ["id", "time", "crop"])
def test_invented_evidence_rejected(setup, defect):
    static, _ = stages(setup[2])
    ref = static["facts"]["scene"]["evidence"][0]
    if defect == "id":
        ref["frame_id"] = "invented"
    elif defect == "time":
        ref["timestamp"] += 1
    else:
        ref["crop_artifact_id"] = "missing-crop"
    with pytest.raises(ValueError):
        validate_evidence(StaticObservationV2.model_validate(static), setup[2])


def test_unknown_confidence_cap_and_interpretation_not_projected(setup):
    static, motion = stages(setup[2])
    static["photography"]["lens"] = empty_stage(StaticObservationV2, "未知").model_dump()["photography"]["lens"]
    static["photography"]["shot_size"] = empty_stage(StaticObservationV2, "未知").model_dump()["photography"]["shot_size"]
    v2 = merge_stages(StaticObservationV2.model_validate(static), MotionObservationV2.model_validate(motion))
    projection = project_v1(v2)
    assert projection.confidence <= 0.5
    assert projection.visual_facts.scene == "厨房"


def test_tools_typed_crop_audit_and_budget(setup):
    _, context, frames = setup
    registered = []
    context.register_frame = registered.append
    tools = LocalToolRegistry(context, frames, PerceptionBudget(max_crops=1))
    result = tools.call("crop_frame", {"frame_id": frames[0].id, "bbox": [0.25, 0.25, 0.75, 0.75]})
    assert (result.frames[0].width, result.frames[0].height) == (48, 80)
    assert registered[0].parent_frame_id == frames[0].id
    assert tools.extra_frames == 1 and tools.crops == 1
    with pytest.raises(ToolBudgetExceeded):
        tools.call("crop_frame", {"frame_id": frames[0].id, "bbox": [0, 0, 1, 1]})
    assert tools.audit[-1]["status"] == "failed" and tools.audit[-1]["error"]
    other = LocalToolRegistry(context, frames)
    with pytest.raises(ValueError):
        other.call("crop_frame", {"frame_id": "/etc/passwd", "bbox": [0, 0, 1, 1]})
    assert other.audit[0]["elapsed_seconds"] >= 0


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_real_local_tools_and_single_supplement(setup, tmp_path):
    cfg, context, frames = setup
    video = tmp_path / "video.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=96x160:rate=10", "-t", "2", "-pix_fmt",
                    "yuv420p", "-y", str(video)], check=True)
    context.video_path = str(video)
    static, motion = stages(frames)
    final = merge_stages(StaticObservationV2.model_validate(static), MotionObservationV2.model_validate(motion))
    weak = copy.deepcopy(motion)
    weak["dynamics"]["subject_action"] = observed("人物移动", frames[-1])
    requests = []
    provider = provider_with(setup, [static, weak, final.model_dump()], requests)
    result = provider.analyze_shot(frames, context)
    record = result.perception_record
    assert record.supplemental_call_used and not record.correction_used
    assert record.budgets["used_steps"] <= 6 and record.budgets["used_extra_frames"] == 2
    assert len(requests) == 3 and result.subject_action == final.dynamics.subject_action.value
    assert record.tools[0]["tool"] == "sample_motion_peak_frames"
    tools = LocalToolRegistry(context, frames)
    assert tools.call("get_video_metadata", {}).metadata["duration"] == 2.0
    highres = tools.call("get_high_res_frame", {"frame_id": frames[0].id}).frames[0]
    assert (highres.width, highres.height) == (96, 160)
    provider.unload()


def test_budget_refusal_preserves_base_results(setup):
    static, motion = stages(setup[2])
    motion["dynamics"]["subject_action"] = observed("人物移动", setup[2][-1])
    provider = provider_with(setup, [static, motion], [])
    result = PerceptionOrchestrator(provider, setup[1], setup[2],
                                   PerceptionBudget(max_steps=3)).run()
    assert result.perception_record.budgets["used_steps"] == 3
    assert not result.perception_record.supplemental_call_used
    assert result.analysis_warnings
    with pytest.raises(ValidationError):
        PerceptionBudget(max_extra_frames=7)
    provider.unload()


def test_neighbor_cannot_be_evidence_for_current_shot_fact(setup):
    cfg, context, frames = setup
    outside = FrameInput("neighbor", 3.0, frames[0].path)
    static, _ = stages(frames)
    static["facts"]["scene"] = observed("厨房", outside)
    provider = provider_with(setup, [], [])
    orchestrator = PerceptionOrchestrator(provider, context, frames)
    with pytest.raises(ValueError, match="范围外"):
        orchestrator._parse(json.dumps(static), StaticObservationV2, [*frames, outside])
    provider.unload()


def test_tool_deadline_records_error(setup):
    tools = LocalToolRegistry(setup[1], setup[2], deadline=time.monotonic() - 1)
    with pytest.raises(TimeoutError):
        tools.call("get_shot_frames", {})
    assert tools.audit[0]["status"] == "failed" and tools.audit[0]["error"]


def test_failed_requests_and_missing_usage_not_free(setup):
    provider = provider_with(setup, [403], [])
    with pytest.raises(RuntimeError, match="403"):
        provider._call("json", [], "test", max_attempts=1)
    usage = provider.drain_usage()[0]
    assert usage["status"] == "failed" and usage["error"] == "HTTP 403"
    assert usage["estimated_list_price_cny"] is None
    assert estimate_list_price_cny("unpriced-model", 1000, 200) is None
    provider.unload()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_shadow_record_and_crop_persist_through_artifact_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.perception_models import PerceptionRecord
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    video = tmp_path / "video.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=96x160:rate=10", "-t", "2", "-pix_fmt",
                    "yuv420p", "-y", str(video)], check=True)
    with TestClient(app) as client:
        def analysis(frames, context):
            static, motion = stages(frames)
            tools = LocalToolRegistry(context, frames)
            crop = tools.call("crop_frame", {"frame_id": frames[0].id, "bbox": [0, 0, 0.5, 0.5]}).frames[0]
            v2 = merge_stages(StaticObservationV2.model_validate(static), MotionObservationV2.model_validate(motion))
            projected = project_v1(v2)
            projected.perception_record = PerceptionRecord(observation=v2, tools=tools.audit,
                                                          raw_responses=[{"phase": "fixture", "response": "{}"}])
            return projected
        client.app.state.job_service.provider.analyze_shot = analysis
        with video.open("rb") as handle:
            job = client.post("/api/jobs", files={"video": ("video.mp4", handle, "video/mp4")}).json()["job_id"]
        for _ in range(200):
            status = client.get(f"/api/jobs/{job}").json()
            if status["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        assert status["status"] == "completed", status
        result = client.get(f"/api/jobs/{job}/result").json()
        assert len(result["frames"]) == 4 and len(result["evidence_frames"]) == 1
        assert result["shots"][0]["visual_observation_v2"]["facts"]["scene"]["value"] == "厨房"
        assert result["metadata"]["perception"]
        assert client.get(result["evidence_frames"][0]["path"]).status_code == 200
        assert client.get(f"/api/jobs/{job}/export").json()["shots"][0]["visual_observation_v2"]

def test_technical_summary_does_not_claim_model_quality(setup):
    from evals.perception_review import review_draft, score_review, technical_summary
    static, motion = stages(setup[2])
    provider = provider_with(setup, [static, motion], [])
    observation = provider.analyze_shot(setup[2], setup[1])
    report = {"clips": [{"id": "clip-001", "model_metrics": [
        {"number": 1, "perception": observation.perception_record.model_dump()}]}],
        "model_usage": provider.drain_usage()}
    summary = technical_summary(report)
    assert summary["first_response_parse_rate"] == 1.0
    assert summary["fact_evidence_reference_coverage"] == 1.0
    assert summary["without_supplement_rate"] == 1.0
    assert summary["estimated_list_price_cny"] == pytest.approx(0.00268)
    draft = review_draft(report, "hash")
    assert score_review(report, draft)["status"] == "pending"
    assert score_review(report, draft)["passed"] is False
    draft["shots"].append(copy.deepcopy(draft["shots"][0]))
    with pytest.raises(ValueError, match="重复"):
        score_review(report, draft)
    provider.unload()


def test_human_truth_rejects_model_source_and_missing_evidence():
    from evals.perception_gold import GoldenFields, HumanVisualTruth
    fact = {"value": "未知", "observable": False, "evidence": []}
    payload = dict(reviewer="manual-reviewer", reviewed_at="2026-09-27",
                   fields={key: fact for key in GoldenFields.model_fields},
                   important_closeup=False, scene_id="scene-001")
    truth = HumanVisualTruth.model_validate(payload)
    truth.validate_shot_range(0, 2)
    with pytest.raises(ValidationError):
        HumanVisualTruth.model_validate({**payload, "source": "model"})
    payload["fields"]["scene"] = {"value": "厨房", "observable": True, "evidence": []}
    with pytest.raises(ValidationError):
        HumanVisualTruth.model_validate(payload)


def test_flash_baseline_keyless_preflight_never_calls_network(tmp_path, monkeypatch):
    from evals.flash_baseline import run
    monkeypatch.delenv("STORYBOARD_BAILIAN_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("STORYBOARD_CLOUD_MODEL", "qwen3.8-max-0902")
    with pytest.raises(RuntimeError, match="API_KEY"):
        run(tmp_path / "manifest.json", tmp_path, tmp_path / "results")
    assert not (tmp_path / "results").exists()


def test_missing_usage_is_not_a_zero_cost_result(setup):
    cfg, context, frames = setup
    provider = BailianVisionProvider(cfg, httpx.MockTransport(lambda request: httpx.Response(
        200, json={"choices": [{"message": {"content": "{}"}}]})))
    provider._call("json", [], "fixture", max_attempts=1)
    usage = provider.drain_usage()[0]
    assert usage["usage_available"] is False and usage["estimated_list_price_cny"] is None
    provider.unload()


def test_v2_and_legacy_evaluation_categories_are_compatible():
    from evals.scoring import normalize_shot_size, normalize_movement
    assert normalize_shot_size("大全景") == normalize_shot_size("大远景") == "大远景"
    assert normalize_shot_size("远景") == "远景"
    assert normalize_movement("航拍：缓慢前进") == "航拍"
    assert normalize_movement("航拍：镜头向前推近") == "航拍"
    assert normalize_movement("复合运动") == "复合运动"
