"""Compact model output -> strict internal evidence IR, salvaging independent fields.

Model wire IDs are F1..Fn. Only the server supplies artifact IDs/timestamps.
An optional localization failure never invalidates an otherwise supported fact.
"""
from __future__ import annotations

import copy
import json
import re

from app.perception_models import (
    ActionPhase, ObservedField, VisualObservationV2,
    empty_stage, unknown_field, validate_evidence,
)


def field_paths(model):
    if model is VisualObservationV2:
        from app.perception_models import merge_stages
        payload = merge_stages(None, None).model_dump()
    else:
        payload = empty_stage(model, "未提供字段").model_dump()
    paths = {}
    for group, section in payload.items():
        if isinstance(section, dict) and "value" in section:
            paths[group] = ("", group, section)
        elif isinstance(section, dict):
            for name, value in section.items():
                if isinstance(value, dict) and "value" in value:
                    paths[f"{group}.{name}"] = (group, name, value)
    return paths


def compact_guide(model, frames=None):
    paths = field_paths(model)
    fields = {key: ("value类型=list[string]" if isinstance(value["value"], list) else "value类型=string")
              for key, (_, _, value) in paths.items()}
    example_key = "dynamics.subject_action" if model.__name__ == "MotionObservationV2" else "facts.primary_subject"
    example_value = "男孩搅拌杯中液体" if example_key.startswith("dynamics") else "卷发男孩"
    last_frame = f"F{len(frames)}" if frames else "F4"
    dynamic = example_key.startswith("dynamics")
    evidence_frames = ["F1", last_frame] if dynamic and last_frame != "F1" else ["F1"]
    example = {"fields": {example_key: {"value": example_value, "frames": evidence_frames, "note": "前后帧可见主体与杯子的相对位置发生变化" if dynamic else "F1可见主体及其状态", "confidence": 0.9}}, "uncertainties": []}
    list_key = "dynamics.action_steps" if example_key.startswith("dynamics") else "facts.objects"
    example["fields"][list_key] = {"value": ["握勺搅拌", "举起勺子"] if example_key.startswith("dynamics") else ["玻璃杯"],
                                 "frames": evidence_frames,
                                 "note": "前后帧分别可见搅拌与举起勺子的状态" if dynamic else "F1台面上可见玻璃杯", "confidence": 0.9}
    return (
        "返回观察实例，不要返回 Schema/$defs，不要返回数组。唯一 JSON 对象格式："
        + json.dumps(example, ensure_ascii=False, separators=(",", ":")) + "。"
        "每个字段只输出 value、frames、note、confidence，可选 uncertainty；"
        "列表字段也必须包装成对象，例如 action_steps={value:[动作1,动作2],frames:[F1,F4],note:证据,confidence:0.9}，不能直接返回数组。"
        "frames 只填附图的 F 编号。note 简短描述可见证据，不重复时间戳、坐标或长 ID。"
        "默认不输出 bbox；未知字段 value=未知（列表字段=[]），frames=[]、confidence=0。"
        "不写 observable，服务端依据证据计算。只填本次任务对应字段；不能把字段值直接写成字符串或数组。"
        "主体、场景与主要动作优先；摄影细节、准确按钮名称和不明确的动作阶段可以未知，不为填满字段猜测。"
        "以下字段需填写（accompanying_states 可省略，最多两项）："
        + json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        + '。景别仅大远景/远景/全景/中景/近景/特写/大特写/未知；'
        '焦段仅超广角/广角/标准/长焦/未知；光线仅自然柔光/硬质方向光/高调/低调/逆光/实景光源/体积光/混合/未知；'
        '运镜仅固定/摇镜/俯仰/推近/拉远/跟拍/手持/升降/环绕/变焦/航拍/复合运动/未知。'
        + ("可选 fields.dynamics.action_phases 是2–3个对象的数组，仅在存在不同语义阶段时提供，否则省略或[]。"
           "每个对象使用 description、source_step_indices（action_steps的1起编号）、frames、note、confidence；"
           "阶段按序覆盖全部步骤，每条有可见帧证据，不把保持姿态或伴随状态单独编成后续阶段。"
           "有必要且可合理安排时可附 generation_start_seconds、generation_end_seconds，二者均为生成建议而非源片测量，"
           "不能按步骤个数均分；时间证据不足时省略秒数。"
           if "dynamics.subject_action" in paths else "")
    )


def _phase_claims(source, compact, frames, start_time, end_time, steps, audit, warnings):
    from app.services.action_presentation import validate_phase_sequence
    item = source.get("dynamics.action_phases") if compact else source.get("dynamics", {}).get("action_phases")
    if item is None or item == []:
        return []
    if not isinstance(item, list) or not 2 <= len(item) <= 3:
        raise ValueError("语义阶段必须为2–3项数组，单动作不分阶段")
    aliases = {f"F{n+1}": frame for n, frame in enumerate(sorted(frames, key=lambda f: f.timestamp))}
    phases = []
    for entry in item:
        if not isinstance(entry, dict):
            raise ValueError("阶段必须为对象")
        data = copy.deepcopy(entry)
        if compact:
            names = data.pop("frames", None)
            note = data.pop("note", "")
            if not isinstance(names, list) or not names or any(not isinstance(alias, str) or alias not in aliases for alias in names):
                raise ValueError("阶段必须引用有效F编号")
            data["evidence"] = [dict(frame_id=aliases[alias].id, timestamp=aliases[alias].timestamp, description=note) for alias in names]
        try:
            phase = ActionPhase.model_validate(data)
        except ValueError:
            if not any(key in data for key in ("generation_start_seconds", "generation_end_seconds")):
                raise
            data.pop("generation_start_seconds", None)
            data.pop("generation_end_seconds", None)
            # Only malformed optional timing may be discarded. All descriptive
            # claims, references and source ownership must still validate.
            phase = ActionPhase.model_validate(data)
            audit.append({"field": "dynamics.action_phases", "repair": "discard_optional_generation_timing"})
            warnings.append("阶段秒数无效，保留有证据的阶段顺序，不重新均分")
        validate_evidence(phase, frames)
        if any(not start_time <= ref.timestamp < end_time for ref in phase.evidence):
            raise ValueError("阶段不能引用本镜范围外的帧")
        phases.append(phase)
    validate_phase_sequence(phases, len(steps))
    return [phase.model_dump() for phase in phases]


def is_unknown(value):
    return value is None or (isinstance(value, str) and (
        not value.strip() or bool(re.match(r"^(未知(?:$|[（(:：\s])|unknown\b|n/?a$|待判断|无法判断)", value.strip(), re.I))
    ))


def declared_fields(raw, model):
    """Known fields actually supplied, including explicit unknown values."""
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)[:-3].strip()
    payload = json.loads(text)
    source = payload.get("fields", payload)
    result = []
    for path, (group, name, _) in field_paths(model).items():
        if path in source or (group and isinstance(source.get(group), dict) and name in source[group]):
            result.append(path)
    return result


def decode_stage(raw, model, frames, start_time, end_time):
    """Return valid stage, localized audit and warnings; never invent frame evidence."""
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)[:-3].strip()
    payload = json.loads(text)
    if not isinstance(payload, dict) or "$defs" in payload or "properties" in payload:
        raise ValueError("模型未返回观察对象（空数组、Schema 回声或错误根类型），不做无图纠错")
    compact = "fields" in payload
    source = payload.get("fields") if compact else payload
    if not isinstance(source, dict):
        raise ValueError("fields 必须为对象")
    paths = field_paths(model)
    root_repairs = []
    if compact:
        # Flatten a known grouping without changing claims or inventing references.
        source = copy.deepcopy(source)
        for key, (group, name, _) in paths.items():
            if group and key not in source and isinstance(source.get(group), dict) and name in source[group]:
                source[key] = source[group][name]
                root_repairs.append({"field": key, "repair": "flatten_known_group", "level": "info"})
    if not any((key in source if compact else (isinstance(source.get(group), dict) and name in source[group] if group else name in source))
               for key, (group, name, _) in paths.items()):
        raise ValueError("没有可解析的观察字段")
    blank = empty_stage(model, "模型未提供字段").model_dump() if model is not VisualObservationV2 else None
    if blank is None:
        from app.perception_models import merge_stages
        blank = merge_stages(None, None).model_dump()
    if "artistic_effects" in blank:
        blank["artistic_effects"]["uncertain_techniques"] = []
        legacy_style = source.get("artistic_effects", {}) if not compact else {}
        if isinstance(legacy_style, dict) and isinstance(legacy_style.get("uncertain_techniques"), list):
            blank["artistic_effects"]["uncertain_techniques"] = [value[:300] for value in legacy_style["uncertain_techniques"] if isinstance(value, str)][:8]
    catalogue = {frame.id: frame for frame in frames}
    aliases = {f"F{n+1}": frame for n, frame in enumerate(sorted(frames, key=lambda f: f.timestamp))}
    audit, warnings = root_repairs, []
    for key, (group, name, default) in paths.items():
        section = source.get(group, {}) if group else source
        item = source.get(key) if compact else (section.get(name) if isinstance(section, dict) else None)
        if item is None:
            continue
        try:
            item_claims = None
            if compact and isinstance(item, list) and item and isinstance(default["value"], list):
                # Some models emit one evidenced claim per action instead of one list claim.
                if not all(isinstance(entry, dict) and isinstance(entry.get("value"), str)
                           and isinstance(entry.get("frames"), list) and entry["frames"]
                           and isinstance(entry.get("note"), str) and entry["note"].strip() for entry in item):
                    raise ValueError("列表动作必须各自带有有效帧证据，不能为裸字符串补造证据")
                item_claims = copy.deepcopy(item)
                item = dict(value=[entry["value"] for entry in item],
                            frames=list(dict.fromkeys(alias for entry in item for alias in entry["frames"])),
                            note="；".join(entry["note"] for entry in item),
                            confidence=min(entry.get("confidence", 0.0) for entry in item))
                audit.append({"field": key, "repair": "combine_evidenced_list_claims"})
            if not isinstance(item, dict):
                raise ValueError("字段不是对象")
            value = item.get("value")
            if key == "artistic_effects.rendering" and isinstance(value, str) and re.search(
                r"次表面散射|全局光照|路径追踪|光线追踪|\bSSS\b|\bPBR\b|ray.?tracing", value, re.I
            ):
                blank["artistic_effects"]["uncertain_techniques"].append(value[:300])
                audit.append({"field": key, "repair": "technique_to_uncertainty", "level": "info"})
                technical = re.compile(r"次表面散射|全局光照|路径追踪|光线追踪|\bSSS\b|\bPBR\b|ray.?tracing", re.I)
                visible = [part.strip() for part in re.split(r"[，；。]", value) if part.strip() and not technical.search(part)]
                # The note is already model-provided visual evidence, not a backend invention.
                note = item.get("note", "") if compact else "；".join(
                    ref.get("description", "") for ref in item.get("evidence", []) if isinstance(ref, dict))
                visible += [part.strip() for part in re.split(r"[，；。]", note) if part.strip()
                            and not technical.search(part) and not re.search(r"若有|通常|可能|模拟了|应当", part)
                            and re.search(r"皮肤|光影|阴影|高光|曲面|光泽|色彩|明暗|纹理|轮廓|质感|透光|边缘|渐层", part)]
                if not visible:
                    continue
                item = copy.deepcopy(item)
                value = item["value"] = "；".join(dict.fromkeys(visible))
                audit.append({"field": key, "repair": "preserve_visible_rendering", "level": "info"})
            list_field = isinstance(default["value"], list)
            if list_field and isinstance(value, list):
                value = [entry.strip() for entry in value if isinstance(entry, str) and not is_unknown(entry)]
                limit = 4 if key == "dynamics.action_steps" else 2 if key == "dynamics.accompanying_states" else None
                if limit and len(value) > limit:
                    raise ValueError(f"{key} 不得超过{limit}项")
                if key == "dynamics.accompanying_states" and any(re.search(r"全程|始终|一直", entry) for entry in value):
                    raise ValueError("采样帧不能证明伴随状态全程持续")
            unknown = is_unknown(value) or (list_field and value == [] and item.get("observable") is not True and not item.get("frames"))
            if unknown:
                normalized = unknown_field([] if list_field else "未知", "模型标记不可判断")
                reasons = item.get("uncertainties", []) if not compact else [item.get("uncertainty", "")]
                if isinstance(reasons, list):
                    normalized["uncertainties"] = [reason[:300] for reason in reasons if isinstance(reason, str) and reason.strip()][:4] or normalized["uncertainties"]
                if value != normalized["value"]:
                    audit.append({"field": key, "repair": "unknown_alias"})
            else:
                if compact:
                    refs = []
                    names = item.get("frames", [])
                    if not isinstance(names, list):
                        raise ValueError("frames 必须为数组")
                    for alias in names:
                        if not isinstance(alias, str) or alias not in aliases:
                            raise ValueError("引用不存在的 F 编号")
                        frame = aliases[alias]
                        refs.append(dict(frame_id=frame.id, timestamp=frame.timestamp, bbox=None,
                                         crop_artifact_id=None, description=item.get("note", "")))
                    normalized = dict(value=value, observable=True, confidence=item.get("confidence", 0.0),
                                      evidence=refs, uncertainties=[item["uncertainty"]] if item.get("uncertainty") else [])
                else:
                    normalized = copy.deepcopy(item)
                    # A real value explicitly marked unobservable is not promoted to fact.
                    if normalized.get("observable") is not True:
                        raise ValueError("有具体值但标记不可观察")
                    for ref in normalized.get("evidence", []):
                        bbox = ref.get("bbox")
                        valid_box = isinstance(bbox, (list, tuple)) and len(bbox) == 4 and all(
                            isinstance(x, (float, int)) and not isinstance(x, bool) for x in bbox)
                        valid_box = valid_box and 0 <= bbox[0] < bbox[2] <= 1 and 0 <= bbox[1] < bbox[3] <= 1
                        if bbox is not None and not valid_box:
                            # Cannot infer whether 1000 means normalized units or pixels.
                            ref["bbox"] = None
                            audit.append({"field": key, "repair": "discard_optional_bbox"})
                        if ref.get("crop_artifact_id") and ref["crop_artifact_id"] not in catalogue:
                            ref["crop_artifact_id"] = None
                            audit.append({"field": key, "repair": "discard_unresolved_optional_crop"})
                annotation = model.model_fields[group].annotation.model_fields[name].annotation if group else model.model_fields[name].annotation
                checked = annotation.model_validate(normalized)
                validate_evidence(checked, frames)
                if any(not start_time <= ref.timestamp < end_time for ref in checked.evidence):
                    raise ValueError("本镜事实不能引用镜头范围外的相邻帧")
                in_shot = [frame.timestamp for frame in frames if start_time <= frame.timestamp < end_time]
                if key == "dynamics.initial_state" and in_shot and not any(
                    abs(ref.timestamp - min(in_shot)) < 0.002 for ref in checked.evidence
                ):
                    raise ValueError("起始状态必须引用本镜最早帧，不能引用后续结果")
                normalized = checked.model_dump()
                if item_claims and key == "dynamics.action_steps":
                    per_step = []
                    for entry in item_claims:
                        refs = [dict(frame_id=aliases[alias].id, timestamp=aliases[alias].timestamp,
                                     description=entry["note"]) for alias in entry["frames"]]
                        claim = ObservedField[str].model_validate(dict(value=entry["value"], observable=True,
                            confidence=entry.get("confidence", 0.0), evidence=refs, uncertainties=[]))
                        validate_evidence(claim, frames)
                        per_step.append([ref.model_dump() for ref in claim.evidence])
                    blank["dynamics"]["step_evidence"] = per_step
            if group:
                blank[group][name] = normalized
            else:
                blank[name] = normalized
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            reason = str(exc).splitlines()[0][:160]
            audit.append({"field": key, "repair": "field_only_unknown", "reason": reason,
                          "code": "invalid_field_structure_or_evidence", "severity": "review"})
            warnings.append(f"{key} 未通过校验，仅该字段待复核")
            replacement = unknown_field([] if isinstance(default["value"], list) else "未知", "字段结构或引用无效，非视觉缺口")
            if group:
                blank[group][name] = replacement
            else:
                blank[name] = replacement
    if "dynamics" in blank:
        try:
            blank["dynamics"]["action_phases"] = _phase_claims(source, compact, frames, start_time, end_time,
                blank["dynamics"]["action_steps"]["value"], audit, warnings)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            audit.append({"field": "dynamics.action_phases", "repair": "field_only_unknown", "reason": str(exc).splitlines()[0][:160]})
            warnings.append("dynamics.action_phases 未通过校验，保留主体动作和详细步骤，不补造时段")
    if isinstance(payload.get("uncertainties"), list):
        blank["uncertainties"] = [item[:300] for item in payload["uncertainties"] if isinstance(item, str)][:8]
    # Interpretations are intentionally not accepted as shot facts here.
    return model.model_validate(blank), audit, list(dict.fromkeys(warnings))
