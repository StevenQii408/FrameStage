"""Checks for delivery regressions that previously escaped profile metadata."""
import copy

import pytest

from app.schemas import PromptBundle, ShotContract
from app.services.prompt_delivery import (
    attach_prompt_lint, clean_join, display_timing, format_seconds, is_known, lint_prompt,
)


def test_compaction_does_not_paraphrase_dialogue_repetitions_or_style():
    dialogue = '对白：女子说：“再喝一次。。不要停！”'
    style = "整体画面风格：剪纸拼贴，保留纸纤维。"
    assert clean_join(["", dialogue, None, "动作：她喝水，再次喝水。", style]) == (
        dialogue + "\n动作：她喝水，再次喝水。\n" + style)


def test_unknown_identity_is_valid_visible_content_not_an_empty_field():
    from app.services.prompt_service import build_prompts
    from test_prompt_profiles import observation
    observed = observation()
    observed.action_details.primary_subject = "未知身份的男子"
    observed.action_details.initial_state = "未知身份的男子站在门口"
    first, _ = build_prompts("unknown-identity", observed)
    assert is_known("未知身份的男子") and is_known("未知型号的汽车")
    assert "未知身份的男子站在门口" in first.positive_prompt
    assert not is_known("未知；证据不足")


def test_display_map_rounds_consistently_without_retiming_or_losing_small_stages():
    intervals = [(0.0, 1.533333), (1.533333, 4.777777), (4.777777, 8.123456)]
    before = copy.deepcopy(intervals)
    shown = display_timing(8.123456, intervals)
    assert shown["display_duration_seconds"] == "8.12"
    assert [(part["display_start_seconds"], part["display_end_seconds"]) for part in shown["intervals"]] == [
        ("0", "1.53"), ("1.53", "4.78"), ("4.78", "8.12")]
    assert intervals == before
    assert shown["exact_duration_seconds"] == 8.123456
    tiny = display_timing(1.0, [(0.0, 0.001), (0.001, 1.0)])
    assert tiny["precision"] == 3 and tiny["intervals"][0]["display_end_seconds"] == "0.001"
    assert format_seconds(4.0) == "4" and format_seconds(1.533333) == "1.53"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf")])
def test_nonfinite_display_times_are_rejected(invalid):
    with pytest.raises(ValueError):
        display_timing(invalid, [(0, 1)])


def test_profile_policy_is_enforced_when_a_new_prompt_is_delivered():
    contract = ShotContract(first_frame_state="未知", observed_style="水彩纸纹")
    prompt = PromptBundle(id="first", shot_id="shot", prompt_type="first_frame", platform="generic-image",
        template_id="first-frame-v7", positive_prompt="主体：女子\n摄影：景别：未知；焦段：待判断\n声音：无对白\n结束状态：杯子落下",
        negative_prompt="", contract=contract)
    before_text = prompt.positive_prompt
    attach_prompt_lint(prompt)
    issues = prompt.contract.delivery["prompt_lint"]["first_frame"]["issues"]
    assert "合同必需字段待复核：first_frame_state" in issues
    assert "可选摄影或空间字段含未知占位" in issues
    assert "首帧正文混入声音、动作时间线或结束段" in issues
    assert "正文未完整保留合同画风" in issues
    assert prompt.status == "warning" and prompt.positive_prompt == before_text


def test_lint_checks_setting_consistency_without_flagging_quoted_guard_words():
    contract = {"observed_style": "剪纸拼贴", "generation_duration_seconds": 4.777777, "aspect_ratio": "16:9",
                "delivery": {"display_timing": display_timing(4.777777, [(0, 4.777777)])}}
    text = ('主体：女子\n视频规格：时长 4.78 秒；横屏 16:9。\n'
            '动作：再次举杯。\n对白：女子说：“无台词，这句话重复两次；无台词。”\n'
            '整体画面风格：剪纸拼贴\n声音与约束：无对白、旁白；不生成文字。')
    assert not lint_prompt(text, prompt_type="video", contract=contract)
    damaged = text.replace("4.78 秒", "4 秒").replace("16:9", "9:16")
    assert set(lint_prompt(damaged, prompt_type="video", contract=contract)) == {
        "正文时长与合同展示时间不一致", "正文画幅与合同不一致"}


def test_unknown_core_is_reported_without_requesting_invented_optional_camera_fields():
    issues = lint_prompt("主体：女子\n首帧状态：女子坐着。", prompt_type="first_frame",
        contract={"first_frame_state": "女子坐着", "observed_style": "未知", "lens": "未知"},
        profile={"required_contract_fields": ["first_frame_state", "observed_style"], "first_frame_only": True})
    assert issues == ["合同必需字段待复核：observed_style"]


def test_standalone_source_duration_and_stage_display_map_are_checked():
    contract = {"generation_duration_seconds": 4.8, "delivery": {"display_timing": display_timing(4.8, [(0, 2.3), (2.3, 4.8)])}}
    text = "动作时间线：\n0–2 秒：举杯\n2–4.8 秒：放下\n时长：4秒。"
    assert set(lint_prompt(text, prompt_type="video", contract=contract)) == {
        "正文时长与合同展示时间不一致", "正文阶段时间与合同展示映射不一致"}
    prefixed = {"observed_style": "整体画面风格：未知"}
    assert lint_prompt("主体：女子", prompt_type="first_frame", contract=prefixed,
        profile={"required_contract_fields": ["observed_style"]}) == ["合同必需字段待复核：observed_style"]
