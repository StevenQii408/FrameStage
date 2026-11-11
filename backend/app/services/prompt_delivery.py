"""Local delivery checks, deliberately independent of model/segmentation logic.

These helpers never paraphrase facts, dialogue or style, retry inference, or
modify a saved timeline. Diagnostics belong in the delivery contract rather
than in the copyable generator instructions.
"""
from __future__ import annotations

import math
import re


VERSION = "prompt-delivery.v2"
_UNKNOWN = {"", "未知", "无", "待设定", "待判断", "待确认", "无法判断", "unknown", "Unknown", "画风待确认", "项目画风待确认"}


def is_known(value):
    if not isinstance(value, str):
        return False
    value = re.sub(r"^整体画面风格[：:]\s*", "", value.strip(), count=1).strip("。； ")
    return (value not in _UNKNOWN
            and not value.startswith(("未知；", "未知，", "未知：", "未知。", "待人工", "画风待人工",
                                      "画风分析未完成", "首帧状态待人工", "结束状态待人工")))


def clean_join(parts):
    """Remove structural empty lines; do not rewrite any source sentences."""
    return "\n".join(str(part).strip() for part in parts if part is not None and str(part).strip())


def format_seconds(value, precision=2):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("时间必须为有限数值")
    result = f"{number:.{precision}f}".rstrip("0").rstrip(".") if precision else f"{number:.0f}"
    return "0" if result in {"", "-0"} else result


def display_timing(duration, intervals):
    """A reversible display map, not a new performance/measurement timeline.

    Normally two decimals suffice. Preserve unusually short stages by increasing
    display precision until no interval collapses; never drop or stretch a stage.
    Shared endpoints are formatted once, keeping the displayed total coherent.
    """
    duration = float(duration)
    intervals = [(float(start), float(end)) for start, end in intervals]
    if not math.isfinite(duration) or duration <= 0 or any(
        not math.isfinite(start) or not math.isfinite(end) or end <= start
        for start, end in intervals
    ):
        raise ValueError("展示时间区间必须为正且有限")
    precision = 2
    while precision < 8 and any(round(end, precision) <= round(start, precision) for start, end in intervals):
        precision += 1
    return {"version": VERSION, "precision": precision, "exact_duration_seconds": duration,
            "display_duration_seconds": format_seconds(duration, precision),
            "intervals": [{"exact_start_seconds": start, "exact_end_seconds": end,
                           "display_start_seconds": format_seconds(start, precision),
                           "display_end_seconds": format_seconds(end, precision)} for start, end in intervals]}


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def lint_prompt(text, *, prompt_type, contract=None, profile=None):
    """Check generated block structure without mistaking dialogue for guards.

    Reports are warnings, not a reason to invent missing fields or run a model.
    Required contract fields are checked against the profile that rendered this
    new revision; legacy saved bodies are never passed here automatically.
    """
    issues = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ["正文为空"]
    if "\n\n\n" in text:
        issues.append("正文含多余空段")
    for name in ("主体", "场景", "整体画面风格", "声音", "声音与约束", "收束", "结束状态"):
        if sum(bool(re.match(r"^" + name + r"[：:]", line)) for line in lines) > 1:
            issues.append(name + "重复渲染")
    if _get(profile, "forbid_invented_unknowns", True):
        for line in lines:
            if re.search(r"(?:^|[：；;])\s*(?:景别|焦段|运镜|构图|光线|前景|中景|背景|空间关系)[：:]\s*(?:未知|待判断|待设定)(?=[；;。\n]|$)", line):
                issues.append("可选摄影或空间字段含未知占位")
                break
    if prompt_type == "first_frame" or _get(profile, "first_frame_only", False):
        if any(re.match(r"^(?:声音(?:与约束)?|对白|动作(?:时间线|阶段)?|收束|结束状态|视频规格)(?:[（(][^）)]*[）)])?[：:]", line) for line in lines):
            issues.append("首帧正文混入声音、动作时间线或结束段")
    guard_lines = [line for line in lines if re.match(r"^(?:声音(?:与约束)?|约束|画面约束|负面约束|连续性与约束)[：:]", line)]
    if sum(len(re.findall(r"无(?:台词|对白)", line)) for line in guard_lines) > 1:
        issues.append("无对白约束重复")
    if contract is not None:
        for name in _get(profile, "required_contract_fields", []):
            value = _get(contract, name)
            if value is None or (isinstance(value, str) and not is_known(value)) or value == []:
                issues.append("合同必需字段待复核：" + name)
        expected_style = _get(contract, "observed_style", "")
        if is_known(expected_style):
            expected_style = re.sub(r"^整体画面风格[：:]\s*", "", expected_style.strip(), count=1)
            if expected_style not in text:
                issues.append("正文未完整保留合同画风")
        delivery = _get(contract, "delivery", {})
        timing = delivery.get("display_timing", {}) if isinstance(delivery, dict) else {}
        duration = _get(contract, "generation_duration_seconds")
        match = re.search(r"^(?:(?:视频规格|生成设置)[^\n]*?)?(?:建议生成)?时长[：:]?\s*(\d+(?:\.\d+)?)\s*秒", text, re.M)
        if match and duration:
            expected = timing.get("display_duration_seconds", format_seconds(duration))
            if float(match[1]) != float(expected):
                issues.append("正文时长与合同展示时间不一致")
        ranges = re.findall(r"^(\d+(?:\.\d+)?)[–—-](\d+(?:\.\d+)?)\s*秒[：:]", text, re.M)
        if ranges and timing.get("intervals"):
            expected_ranges = [(part["display_start_seconds"], part["display_end_seconds"]) for part in timing["intervals"]]
            if [(float(a), float(b)) for a, b in ranges] != [(float(a), float(b)) for a, b in expected_ranges]:
                issues.append("正文阶段时间与合同展示映射不一致")
        aspect = str(_get(contract, "aspect_ratio", ""))
        expected = re.search(r"(\d+(?:\.\d+)?)\s*[:：/]\s*(\d+(?:\.\d+)?)", aspect)
        spec_lines = [line for line in lines if re.match(r"^(?:视频规格|图像规格|画幅|生成设置)", line)]
        if expected:
            ratio = float(expected[1]) / float(expected[2])
            for line in spec_lines:
                actual = re.search(r"(\d+(?:\.\d+)?)\s*[:：/]\s*(\d+(?:\.\d+)?)", line)
                if actual and abs(float(actual[1]) / float(actual[2]) - ratio) > 0.00001:
                    issues.append("正文画幅与合同不一致")
                    break
    return list(dict.fromkeys(issues))


def attach_prompt_lint(prompt):
    """Validate a newly built PromptBundle using its actual profile policy."""
    from app.prompt_profiles import profile

    selected = profile(prompt.template_id)
    issues = lint_prompt(prompt.positive_prompt, prompt_type=prompt.prompt_type,
                         contract=prompt.contract, profile=selected)
    if prompt.contract is not None:
        reports = prompt.contract.delivery.setdefault("prompt_lint", {})
        reports[prompt.prompt_type] = {"version": VERSION, "profile_id": selected.id,
                                       "passed": not issues, "issues": issues}
    prompt.warnings = list(dict.fromkeys([*prompt.warnings, *("Prompt校验：" + item for item in issues)]))
    if issues and prompt.status == "ready":
        prompt.status = "warning"
    return prompt
