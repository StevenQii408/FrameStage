import copy

from app.perception_models import (
    MotionObservationV2, StaticObservationV2, empty_stage, merge_stages,
)
from app.services.delivery_quality import delivery_quality


def observed(value, frame="f1", timestamp=0.0):
    return dict(value=value, observable=True, confidence=0.9, uncertainties=[],
                evidence=[dict(frame_id=frame, timestamp=timestamp, description="对应帧中的可见状态")])


def source(*, motion=False, endpoints=True, static=True):
    still = empty_stage(StaticObservationV2, "未提供")
    moving = empty_stage(MotionObservationV2, "动态阶段结构校验失败")
    if static:
        data = still.model_dump()
        data["facts"]["primary_subject"] = observed("女子")
        data["facts"]["scene"] = observed("厨房台面与咖啡机")
        still = StaticObservationV2.model_validate(data)
    if motion:
        data = moving.model_dump()
        data["dynamics"]["subject_action"] = observed("女子保持站立，双手停在咖啡机旁")
        data["dynamics"]["action_steps"] = observed(["保持站立姿态"])
        if endpoints:
            data["dynamics"]["initial_state"] = observed("双手位于咖啡机旁")
            data["dynamics"]["final_state"] = observed("双手仍位于咖啡机旁", "f4", 5.0)
        moving = MotionObservationV2.model_validate(data)
    return dict(id="s1", status="warning" if not motion else "ready", action="未知",
                visual_observation_v2=merge_stages(still, moving).model_dump())


def evaluate(shot, *, job_status="completed", prompts=None):
    return delivery_quality(dict(status=job_status, input_type="video"), [shot], prompts or [],
                            {"generation_sequences": [{"status": "ready", "source_shot_ids": ["s1"]}]},
                            {"profile": {"application_status": "ready", "rendered_prompt": "日常纪实影像"}})


def test_static_success_survives_dynamic_structure_failure_without_claiming_video_ready():
    shot = source()
    before = copy.deepcopy(shot)
    result = evaluate(shot, prompts=[
        dict(shot_id="s1", prompt_type="first_frame", status="ready"),
        dict(shot_id="s1", prompt_type="video", status="ready"),
    ])
    assert result["overall"] == "partial"
    assert result["visual"]["usable"] == 1
    assert result["visual"]["unknown"] == 0
    assert result["visual"]["static_usable"] == 1
    assert result["visual"]["dynamic_usable"] == result["visual"]["video_usable"] == 0
    assert result["visual"]["static_only"] == 1
    assert result["prompts"]["ready"] == result["prompts"]["review"] == 1
    assert any(i["code"] == "dynamic_review" for i in result["issues"])
    assert not any(i["code"] == "visual_unavailable" for i in result["issues"])
    assert shot == before  # A quality read never invents or repairs dynamic facts.


def test_valid_static_ir_prevents_whole_job_quality_being_reported_as_failed():
    shot = source()
    shot["status"] = "failed"  # Legacy aggregate execution flag does not erase saved evidence.
    result = evaluate(shot, job_status="failed")
    assert result["execution_state"] == "failed"
    assert result["overall"] == "partial"
    assert result["visual"]["static_usable"] == result["visual"]["usable"] == 1


def test_action_understood_but_endpoints_missing_remains_a_video_draft():
    result = evaluate(source(motion=True, endpoints=False))
    assert result["visual"]["dynamic_usable"] == 1
    assert result["visual"]["video_usable"] == 0
    assert result["visual"]["static_only"] == 0
    assert any(i["code"] == "video_context_review" for i in result["issues"])
    assert not any(i["code"] == "dynamic_review" for i in result["issues"])


def test_a_supported_held_state_is_a_valid_action():
    result = evaluate(source(motion=True))
    assert result["overall"] == "ready"
    assert result["visual"]["dynamic_usable"] == result["visual"]["video_usable"] == 1
    assert result["visual"]["video_review"] == 0


def test_all_unknown_remains_unavailable_and_failed_when_no_result_survives():
    result = evaluate(source(static=False), job_status="failed")
    assert result["overall"] == "failed"
    assert result["visual"]["usable"] == 0
    assert result["visual"]["unknown"] == 1


def test_legacy_static_scene_description_is_retained_without_motion():
    result = evaluate(dict(id="s1", status="warning", action="未知",
                           visual_facts={"scene": "厨房咖啡机旁"}))
    assert result["visual"]["static_only"] == result["visual"]["usable"] == 1
    assert result["visual"]["dynamic_usable"] == 0


def test_malformed_v2_cannot_be_promoted_by_legacy_action_text():
    result = evaluate(dict(id="s1", status="warning", action="女子操作咖啡机",
                           visual_observation_v2={"facts": {"primary_subject": "女子"}}))
    assert result["visual"]["usable"] == 0
    assert result["visual"]["video_usable"] == 0


def test_style_only_warnings_do_not_poison_event_or_motion_readiness():
    warning = "画风仅部分确定，已保留有证据的特征，具体风格名称可能未知；动作与摄影结果不受影响"
    prompts = [dict(shot_id="s1", prompt_type=kind, status="warning",
                    warnings=[warning, "Prompt校验：合同必需字段待复核：observed_style"],
                    contract={"delivery": {"prompt_lint": {
                        "video" if kind == "video" else "first_frame": {
                            "passed": False, "issues": ["合同必需字段待复核：observed_style"]}}}})
               for kind in ("first_frame", "video")]
    result = delivery_quality(dict(status="completed", input_type="video"), [source(motion=True)], prompts,
        {"generation_sequences": [dict(id="seq1", status="ready", warnings=[warning], source_shot_ids=["s1"])]},
        {"profile": {"application_status": "partial", "rendered_prompt": "真人摄影"}})
    assert result["aggregation"]["state"] == "ready"
    assert result["aggregation"]["ready"] == 1 and result["aggregation"]["fallback"] == 0
    assert result["prompts"]["ready"] == 2 and result["prompts"]["review"] == 0
    assert result["prompts"]["style_review"] == 2
    assert result["style"]["state"] == "partial" and result["overall"] == "partial"
    assert [item["code"] for item in result["issues"]] == ["style_partial"]


def test_unknown_warnings_and_nonstyle_lint_remain_blocking():
    prompts = [dict(shot_id="s1", prompt_type="video", status="ready", warnings=[],
                    contract={"delivery": {"prompt_lint": {"video": {
                        "passed": False, "issues": ["时间线存在重叠"]}}}})]
    result = evaluate(source(motion=True), prompts=prompts)
    assert result["prompts"]["ready"] == 0 and result["prompts"]["review"] == 1
    result = delivery_quality(dict(status="completed", input_type="video"), [source(motion=True)], [],
        {"generation_sequences": [dict(id="seq1", status="ready", warnings=["事实归属有冲突"], source_shot_ids=["s1"])]})
    assert result["aggregation"]["state"] == "partial" and result["aggregation"]["fallback"] == 1


def test_safe_action_summary_fallback_is_not_a_rejected_action():
    result = evaluate(source(motion=True), prompts=[dict(shot_id="s1", prompt_type="video", status="warning",
        warnings=["动作摘要可能省略独立事件或来源方向，已保留来源动作；未自动拆镜"])])
    assert result["overall"] == "ready"
    assert result["prompts"]["ready"] == 1


def test_interval_coverage_accepts_disjoint_clips_from_one_original_source():
    shot = source(motion=True)
    shot.update(start_time=0.0, end_time=30.0)
    sequences = [dict(id=f"clip-{index}", status="ready", source_shot_ids=["s1"], seedance_prompt={"contract": {
        "delivery": {"source_spans": [dict(source_shot_id="s1", start_time=start, end_time=end)]}}})
        for index, (start, end) in enumerate(((0.0, 12.0), (12.0, 24.0), (24.0, 30.0)))]
    result = delivery_quality(dict(status="completed", input_type="video"), [shot], [],
        {"generation_sequences": sequences},
        {"profile": {"application_status": "ready", "rendered_prompt": "纪实摄影"}})
    assert result["aggregation"]["state"] == "ready" and result["overall"] == "ready"
    sequences[1]["seedance_prompt"]["contract"]["delivery"]["source_spans"][0]["start_time"] = 11.5
    result = delivery_quality(dict(status="completed", input_type="video"), [shot], [], {"generation_sequences": sequences})
    assert result["aggregation"]["state"] == "partial"
    assert any(item["code"] == "aggregation_coverage" for item in result["issues"])
