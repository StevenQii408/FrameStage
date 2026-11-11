"""Finite perception state machine: base analysis, one evidence pass, final projection."""
from __future__ import annotations

import json
import copy
import re
import time

from app.perception_models import (
    MotionObservationV2, PerceptionRecord, PerceptionStageRecord, StaticObservationV2, VisualObservationV2,
    iter_observed_fields, key_fields, merge_stages, observation_review, project_v1, validate_evidence,
)
from app.services.visual_tools import LocalToolRegistry, PerceptionBudget
from app.services.perception_wire import compact_guide, decode_stage, declared_fields
from app.prompt_profiles import action_rules, style_rules
from app.providers.cloud_errors import CloudTransportError


def structured_schema(model):
    """Make optional properties explicit for provider strict-output implementations."""
    schema = model.model_json_schema()
    def walk(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(schema)
    return schema


def observed_paths(model, prefix=""):
    from app.perception_models import ObservedField
    from pydantic import BaseModel
    if isinstance(model, ObservedField):
        yield prefix, model
    elif isinstance(model, BaseModel):
        for name in type(model).model_fields:
            yield from observed_paths(getattr(model, name), f"{prefix}.{name}".strip("."))


def safe_stage_error(exc, *, schema=False):
    if isinstance(exc, PerceptionCancelled):
        return {"kind": "cancelled", "category": "operation_cancelled", "message": "感知操作已取消，未发送后续请求", "retryable": False}
    if isinstance(exc, CloudTransportError):
        return {"kind": "transport", "category": exc.category, "message": str(exc), "retryable": True}
    status = re.search(r"HTTP (\d{3})", str(exc))
    if status:
        authorization = status.group(1) in ("401", "403")
        return {"kind": "authorization" if authorization else "provider", "category": f"http_{status.group(1)}",
                "message": f"云端请求失败，HTTP {status.group(1)}", "retryable": not authorization}
    # Arbitrary provider exceptions can contain credentials and proxy URLs.
    return {"kind": "schema" if schema else "provider", "category": "invalid_response" if schema else type(exc).__name__,
            "message": "模型返回结构或证据引用无效" if schema else f"感知阶段未完成（{type(exc).__name__}）", "retryable": True}


def legacy_stage_records(record):
    """Adapt persisted pre-health records without assigning failure to unknowns."""
    if record is None:
        return {}
    result = copy.deepcopy(record.stages)
    for name in ("static", "motion"):
        if name in result:
            continue
        calls = [row for row in record.raw_responses if row.get("phase") == f"shot.{name}"]
        last = calls[-1] if calls else {}
        valid = name in record.normalized_stages or last.get("validation") in ("passed", "locally_repaired", "partial")
        if not calls and not record.normalized_stages:
            # Observation-only legacy records are valid evidence records; lack
            # of provider telemetry cannot establish a request failure.
            valid = True
        state = ("partial" if last.get("validation") == "partial" else "succeeded") if valid else "failed" if calls else "deferred"
        error = None
        if not valid:
            category = record.budgets.get("transport_error_category") if record.budgets.get("network_failure") else None
            error = {"kind": "transport" if category and calls else "dependency" if not calls else "schema",
                     "category": category if category and calls else "legacy_stage_unavailable",
                     "message": "旧记录该阶段没有有效响应，需阶段恢复", "retryable": True}
        result[name] = PerceptionStageRecord(state=state, error=error,
            attempts=len(calls), input_frames=last.get("input_frames", []),
            schema_status="partial" if state == "partial" else "passed" if valid else "failed" if calls else "not_run",
            usage=[row for call in calls for row in call.get("usage", [])])
    return result


def recoverable_stages(record):
    record = PerceptionRecord.model_validate(record) if isinstance(record, dict) else record
    if record is None:
        return []
    if record.analysis_span_observations:
        pending = set()
        for span in record.analysis_span_observations:
            if span.get("status") == "ready":
                continue
            saved = span.get("perception_record")
            pending.update(recoverable_stages(saved) if saved else ("static", "motion"))
        return [name for name in ("static", "motion") if name in pending]
    result = []
    for name, stage in legacy_stage_records(record).items():
        core = ([record.observation.facts.primary_subject, record.observation.facts.scene] if name == "static"
                else [record.observation.dynamics.subject_action])
        if stage.state in ("failed", "deferred") or (stage.state == "partial" and any(not field.observable for field in core)):
            result.append(name)
    return result


def infer_stage_records(saved_record):
    record = PerceptionRecord.model_validate(saved_record) if isinstance(saved_record, dict) else saved_record
    return legacy_stage_records(record)


class PerceptionCancelled(RuntimeError):
    pass


class PerceptionOrchestrator:
    def __init__(self, provider, context, frames, budget=None, *, seed_record=None, selected_stages=None, cancelled=None):
        self.provider = provider
        self.cancelled = cancelled
        self.cancelled_requested = False
        self.context = context
        self.all_frames = sorted(frames, key=lambda frame: frame.timestamp)
        self.frames = [frame for frame in self.all_frames if frame.artifact_kind == "keyframe"][:4] or self.all_frames[:4]
        self.budget = budget or PerceptionBudget()
        self.started = time.monotonic()
        self.tools = LocalToolRegistry(context, self.frames, self.budget,
                                       deadline=self.started + provider.settings.vlm_timeout_seconds)
        self.tools.frames.update({frame.id: frame for frame in self.all_frames})
        self.seed = (PerceptionRecord.model_validate(seed_record) if isinstance(seed_record, dict) else seed_record)
        self.recovery = self.seed is not None or selected_stages is not None
        if self.recovery and self.seed is None:
            raise ValueError("阶段恢复必须提供已有感知记录")
        if selected_stages is not None and (not selected_stages or any(stage not in ("static", "motion") for stage in selected_stages)):
            raise ValueError("恢复阶段只能选择 static 或 motion")
        self.selected_stages = list(dict.fromkeys(selected_stages)) if selected_stages is not None else None
        self.raw = copy.deepcopy(self.seed.raw_responses) if self.seed else []
        self.previous_calls = len(self.raw)
        self.operation_calls = 0
        self.max_model_calls = 2 if self.recovery else 3
        self.trace = []
        self.warnings = []
        self.normalized = copy.deepcopy(self.seed.normalized_stages) if self.seed else {}
        self.stages = legacy_stage_records(self.seed) if self.seed else {}
        self.correction_used = self.seed.correction_used if self.seed else False
        self.supplement_used = self.seed.supplemental_call_used if self.seed else False
        self.local_repairs = []
        self.stage_requests = {}
        self.followup_transport_error = None

    def _checkpoint(self):
        callback = getattr(self.context, "checkpoint_perception", None)
        if not callable(callback):
            return
        static = StaticObservationV2.model_validate(self.normalized["static"]) if self.normalized.get("static") else None
        motion = MotionObservationV2.model_validate(self.normalized["motion"]) if self.normalized.get("motion") else None
        stages = dict(self.stages)
        for name in ("static", "motion"):
            stages.setdefault(name, PerceptionStageRecord(state="deferred"))
        callback(PerceptionRecord(observation=merge_stages(static, motion), stages=stages,
            raw_responses=copy.deepcopy(self.raw), normalized_stages=copy.deepcopy(self.normalized),
            input_frames=self._catalogue(self.tools.catalogue()), tools=copy.deepcopy(self.tools.audit),
            trace=[*self.trace, {"state": "stage_checkpoint"}], elapsed_seconds=time.monotonic() - self.started,
            budgets={"used_model_calls": self.operation_calls, "checkpoint": True,
                    "in_flight_cost_status": "unknown_if_interrupted"},
            revisions=copy.deepcopy(self.seed.revisions) if self.seed else [],
            source_revision=self.seed.source_revision if self.seed else None))

    def _remaining(self):
        remaining = self.provider.settings.vlm_timeout_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError("单镜总时间预算耗尽")
        return remaining

    def _cancel_requested(self):
        self.cancelled_requested = self.cancelled_requested or bool(self.cancelled and self.cancelled())
        return self.cancelled_requested

    def _catalogue(self, frames):
        from app.services.perception_revision import frame_snapshot
        return [frame_snapshot(frame) for frame in frames]

    def _call(self, prompt, frames, phase, model, max_tokens):
        if self.operation_calls >= self.max_model_calls:
            raise RuntimeError("本次感知模型请求预算已耗尽")
        entry = {"phase": phase, "response": None, "error": None,
                 "attempt": 1,
                 "schema_enforcement": "local-compact-fields",
                 "input_frames": self._catalogue(sorted(frames, key=lambda f: f.timestamp))}
        if self._cancel_requested():
            raise PerceptionCancelled("感知操作已取消")
        compiled_prompt = prompt + "\n" + compact_guide(model, frames)
        remaining_timeout = min(self._remaining(), 45 if phase == "shot.supplement" else 75)
        self.operation_calls += 1
        self.raw.append(entry)
        progress = getattr(self.context, 'report_perception_progress', None)
        if progress:
            progress(phase, 'running')
        usage_start = len(getattr(self.provider, "usage", []))
        try:
            result = self.provider._call(
                compiled_prompt, frames, phase, max_tokens=max_tokens,
                timeout_seconds=remaining_timeout, max_attempts=1,
                shot_id=self.context.shot_id,
            )
            entry["response"] = result
            return result
        except Exception as exc:
            entry["error"] = safe_stage_error(exc)["message"]
            raise
        finally:
            entry["usage"] = copy.deepcopy(getattr(self.provider, "usage", [])[usage_start:])
            if progress:
                progress(phase, 'failed' if entry.get('error') else 'completed')

    def _parse(self, raw, model, frames):
        value = model.model_validate(json.loads(raw))
        validate_evidence(value, frames)
        # Neighbors may support interpretations, never claims about this shot.
        for field in iter_observed_fields(value):
            if any(not self.context.start_time <= ref.timestamp < self.context.end_time for ref in field.evidence):
                raise ValueError("本镜事实不能引用镜头范围外的相邻帧")
        return value

    def _structured(self, prompt, frames, phase, model, max_tokens):
        raw = self._call(prompt, frames, phase, model, max_tokens)
        try:
            parsed, repairs, warnings = decode_stage(raw, model, frames, self.context.start_time, self.context.end_time)
            self.raw[-1]["validation"] = "partial" if warnings else "locally_repaired" if repairs else "passed"
            self.raw[-1]["field_repairs"] = repairs
            self.raw[-1]["field_warnings"] = warnings
            self.raw[-1]["provided_fields"] = declared_fields(raw, model)
            self.raw[-1]["wire_format"] = "perception-compact.v2"
            self.local_repairs.extend(repairs)
            # Safe local repair is audit information, not a visual review failure.
            return parsed
        except Exception as exc:
            self.raw[-1]["validation"] = "failed"
            self.raw[-1]["validation_error"] = str(exc).splitlines()[0][:300]
            raise

    def _execute_stage(self, name, frames, prompt, model, max_tokens):
        self.stage_requests[name] = (list(frames), prompt, model, max_tokens)
        previous = self.stages.get(name, PerceptionStageRecord())
        call_start = len(self.raw)
        try:
            result = self._structured(prompt, frames, f"shot.{name}", model, max_tokens)
            entry = self.raw[-1]
            partial = entry.get("validation") == "partial"
            self.stages[name] = PerceptionStageRecord(
                state="partial" if partial else "succeeded", error=None,
                input_frames=entry["input_frames"], attempts=previous.attempts + 1,
                schema_status="partial" if partial else "passed",
                usage=[*previous.usage, *entry.get("usage", [])],
                unknown_fields=[path for path, field in observed_paths(result) if not field.observable],
            )
            self.normalized[name] = result.model_dump()
            self._checkpoint()
            return result
        except Exception as exc:
            if isinstance(exc, PerceptionCancelled):
                raise
            entry = self.raw[-1] if len(self.raw) > call_start else {}
            self.stages[name] = PerceptionStageRecord(
                state="failed", error=safe_stage_error(exc, schema=entry.get("validation") == "failed"),
                input_frames=entry.get("input_frames", self._catalogue(frames)),
                attempts=previous.attempts + int(len(self.raw) > call_start),
                schema_status="failed" if entry.get("validation") == "failed" else "not_run",
                usage=[*previous.usage, *entry.get("usage", [])],
            )
            self._checkpoint()
            raise

    def _defer_motion(self, message, *, cancelled=False):
        previous = self.stages.get("motion", PerceptionStageRecord())
        self.stages["motion"] = previous.model_copy(update={
            "state": "deferred", "schema_status": "not_run",
            "error": {"kind": "cancelled" if cancelled else "dependency", "category": "operation_cancelled" if cancelled else "static_unavailable",
                      "message": message, "retryable": not cancelled},
        })

    def _seed_stage(self, name, model):
        if not self.seed or self.stages[name].state not in ("succeeded", "partial"):
            return None
        if name in (self.selected_stages or recoverable_stages(self.seed)) and name in recoverable_stages(self.seed):
            return None
        observation = self.seed.observation
        if name == "static":
            return StaticObservationV2(facts=observation.facts,
                photography={key: getattr(observation.photography, key) for key in StaticObservationV2.model_fields["photography"].annotation.model_fields},
                artistic_effects=observation.artistic_effects, uncertainties=observation.uncertainties)
        return MotionObservationV2(dynamics=observation.dynamics,
            motion_signature=observation.artistic_effects.motion_signature,
            camera_movement_type=observation.photography.camera_movement_type,
            camera_movement_detail=observation.photography.camera_movement_detail, uncertainties=observation.uncertainties)

    def _prompt(self, stage, frames):
        shared = (
            "只记录画面可以直接证明的内容；不要输出思维链。"
            "身份、因果、用途、情绪和摄影技法无法证实时写未知。"
            "每个可观察字段（包括确认没有物体）都要给出 F 编号及简短可见描述。"
            "例如前景没有遮挡也必须写 value=无明显前景遮挡、frames=[F1]、note=该帧未见前景遮挡，不能用空frames。"
            "字段不可判断时 value=未知（数组用[]）、confidence=0，"
            "写入 uncertainty；无人物时仍描述主要可见主体与变化。"
            "F 编号按附图时间顺序对应，禁止自己输出帧 ID、时间戳或坐标。"
            "同一字段包含多个对象时必须在 evidence 中分别说明；局部裁剪不能推断全画面构图。"
            f"\n镜头范围：[{self.context.start_time:.6f},{self.context.end_time:.6f})秒。"
            f"\n附图目录：{json.dumps([{'frame': f'F{i+1}', 'time': round(f.timestamp, 3)} for i, f in enumerate(sorted(frames, key=lambda f: f.timestamp))], ensure_ascii=False)}"
        )
        if stage == "static":
            try:
                overview_style = json.loads(self.context.global_style).get("visual_style", "")
            except (ValueError, AttributeError):
                overview_style = ""
            return (
                "分析代表帧中的静态摄影信息。按前景遮挡物与位置、中景主体姿态及手部/道具、"
                "背景环境入口光源、主体距离朝向视线遮挡关系观察。"
                "景别与焦段分开；只描述可见色彩质感特效。画风字段只写媒介、抽象造型、整体色彩、光影组织和稳定表面纹理；发型、服装、道具、地点、构图与一次性特效不得写进画风字段。代表帧不一定是起始帧，不能用于假定0秒动作状态。"
                + style_rules()["observation_guidance"] +
                (f"全片媒介线索仅作对照：{str(overview_style)[:240]}，本帧不同则以本帧证据为准。" if overview_style else "") +
                "输出 JSON。\n" + shared
            )
        return (
            "比较按时间排序的帧，描述主体、初始状态、1–4个有意义的具体动作、"
            "互动对象、结束状态。区分主体移动与背景视差、主体尺度和画面覆盖范围的变化。"
            + action_rules()["observation_guidance"] +
            "动作事实只写已看见的身体与物体变化，不在事实中写多个猜测用途："
            "例如只能看见闭眼、撅嘴、再微笑，就写这些变化；吹凉、品尝、享受等解释移入 uncertainties，"
            "不得写成确定动作或用'吹气或品尝'替代可见事实。未分析音轨，嘴部张合不等于交谈或说出台词；"
            "只描述嘴部变化，不推断正在说话。动态事实的 frames 应同时引用变化之前和之后的有效帧，"
            "不能仅用最早静止帧证明稍后接住、饮用或放下；短镜头证据确实不足时保留不确定性。"
            f"本镜建议生成时长为{self._generation_duration():g}秒。"
            "仅在多个语义阶段需要时序约束且可以合理安排时提供生成秒数，连续覆盖这段建议时长；"
            "不要按动作数量均分，不能把采样帧时间当动作精确起止时间。依据不足只保留先后顺序。"
            "不能只因人物变大就断言镜头推近，先区分人物走近与摄像机运动；无法区分时写未知。"
            "切镜、突然切换景别不是变焦或复合运镜；有剪辑时不要把帧间构图跳变说成连续运镜。"
            f"已检测的源片内部剪辑时刻：{self.context.source_edit_times}秒。"
            "动作必须有对象、方向或结果；静止画面如实描述保持状态，不臆造运动。动态画风只描述跨帧可证实的逐帧抖动、有限动画、形变与惯性等表现，不把普通人物动作当画风。输出 JSON。\n" + shared
        )

    def _generation_duration(self):
        from app.services.generation_timing import ceil_half_seconds
        return ceil_half_seconds(self.context.end_time - self.context.start_time)

    def _correct_protocol(self, static, motion):
        """One image-backed field patch; shares the third request with visual tools.

        Preserve successful values and the *original* request's F aliases. A
        format defect must not be treated as a reason to fetch different images.
        """
        if self.recovery or self.budget.max_corrections == 0 or self.correction_used or self.supplement_used:
            return static, motion
        if self.operation_calls >= self.max_model_calls or self._cancel_requested():
            return static, motion
        if len(self.trace) + 3 > self.budget.max_steps:
            return static, motion
        for name, current in (("motion", motion), ("static", static)):
            if name not in self.stage_requests:
                continue
            entries = [entry for entry in self.raw[self.previous_calls:] if entry.get("phase") == f"shot.{name}"]
            if not entries:
                continue
            original = entries[-1]
            failures = [item for item in original.get("field_repairs", []) if item.get("repair") == "field_only_unknown"]
            failed_paths = {item["field"] for item in failures}
            core_paths = {"dynamics.subject_action"} if name == "motion" else {"facts.primary_subject", "facts.scene"}
            # Optional field defects stay local. Explicit unknowns are valid
            # observations and never cause repeated structural requests.
            target = bool(failed_paths & core_paths) or (name == "motion" and len(failed_paths) >= 3)
            if name == "motion" and current is not None and not current.dynamics.subject_action.observable and "dynamics.subject_action" not in original.get("provided_fields", []):
                target = True
                failed_paths.add("dynamics.subject_action")
                failures.append({"field": "dynamics.subject_action", "reason": "缺少主要动作字段；明确不可判断时仍需返回未知对象"})
            if current is None and original.get("validation") == "failed" and static is not None and name == "motion":
                target = True
                failures = [{"field": "$root", "reason": original.get("validation_error", "无效 JSON 对象")}]
            if not target:
                continue
            frames, base_prompt, model, max_tokens = self.stage_requests[name]
            valid = {path: field.value for path, field in observed_paths(current) if field.observable} if current else {}
            patch_prompt = (
                "仅纠正上次响应的字段结构和证据引用。这是同一次观察的修复，附图及F编号与原请求完全相同。"
                "已通过字段由服务端锁定，不要重写；只返回错误字段及必要的核心字段。"
                "每个字段必须是{value,frames,note,confidence}对象；动作列表也必须包装，不能返回裸字符串或裸数组。"
                "重新对照附图填写真实证据；看不清则明确未知，不为原描述编造证据。"
                "\n具体错误：" + json.dumps(failures, ensure_ascii=False)
                + "\n已通过字段：" + json.dumps(valid, ensure_ascii=False)
                + "\n原观察任务：" + base_prompt
            )
            self.correction_used = True
            self.trace.append({"state": "protocol_correction", "stage": name,
                               "fields": [item["field"] for item in failures]})
            call_start = len(self.raw)
            try:
                repaired = self._structured(patch_prompt, frames, f"shot.{name}.correction", model, max_tokens)
                last = self.raw[-1]
                correction_failures = {item["field"] for item in last.get("field_repairs", []) if item.get("repair") == "field_only_unknown"}
                corrected_paths = set(last.get("provided_fields", [])) - correction_failures
                if current is None:
                    merged = repaired
                    failed_paths = correction_failures | (core_paths - corrected_paths)
                else:
                    merged_data = current.model_dump()
                    for path, field in observed_paths(repaired):
                        if path not in failed_paths or path not in corrected_paths:
                            continue
                        parts = path.split(".")
                        container = merged_data
                        for part in parts[:-1]:
                            container = container[part]
                        container[parts[-1]] = field.model_dump()
                    merged = model.model_validate(merged_data)
                resolved = corrected_paths
                unresolved = failed_paths - resolved
                last.update(correction_for=f"shot.{name}", unresolved_fields=sorted(unresolved))
                # Keep the first response and its defects in the audit log,
                # but active warning state describes the final retained fields.
                original["resolved_fields"] = sorted(failed_paths & resolved)
                previous = self.stages[name]
                self.stages[name] = PerceptionStageRecord(
                    state="partial" if unresolved else "succeeded", error=None,
                    input_frames=last["input_frames"], attempts=previous.attempts + 1,
                    schema_status="partial" if unresolved else "passed",
                    usage=[*previous.usage, *last.get("usage", [])],
                    unknown_fields=[path for path, field in observed_paths(merged) if not field.observable])
                self.normalized[name] = merged.model_dump()
                if name == "motion":
                    motion = merged
                else:
                    static = merged
                self.warnings = [warning for warning in self.warnings if not warning.startswith("动态分析未返回有效结构") or name != "motion"]
                self._checkpoint()
            except Exception as exc:
                if isinstance(exc, CloudTransportError):
                    self.followup_transport_error = exc
                if len(self.raw) > call_start:
                    last = self.raw[-1]
                    previous = self.stages[name]
                    self.stages[name] = previous.model_copy(update={
                        "attempts": previous.attempts + 1,
                        "usage": [*previous.usage, *last.get("usage", [])]})
                self.warnings.append(f"{name}结构纠正未完成，保留已有有效字段：{safe_stage_error(exc, schema=True)['message']}")
            break
        return static, motion

    def _generic(self, observation):
        action = observation.dynamics.subject_action
        return action.observable and action.value.strip("。 ") in {
            "人物移动", "主体移动", "表情变化", "画面变化", "人物表情变化",
        }

    def _gap(self, observation):
        facts, dynamics, photo = observation.facts, observation.dynamics, observation.photography
        uncertainties = " ".join(observation.uncertainties + [
            item for field in iter_observed_fields(observation) for item in field.uncertainties
        ])
        # Schema defects have their own bounded correction path. Only genuine
        # missing core observations justify gathering extra visual evidence.
        structure_error = re.compile("字段结构|引用无效|模型未提供|阶段失败")
        action_uncertainties = " ".join(dynamics.subject_action.uncertainties)
        if not facts.primary_subject.observable and re.search("小物体|细小|辨认|看不清", " ".join(facts.primary_subject.uncertainties)):
            return "detail"
        if self._generic(observation) or (not dynamics.subject_action.observable and
                not structure_error.search(action_uncertainties) and
                re.search("动作.*不清|遮挡|帧.*不足|时间.*稀疏|运动.*模糊", action_uncertainties)):
            return "action"
        if not photo.camera_movement_type.observable and re.search("运镜.*不清|视差.*不清|运动.*模糊", uncertainties):
            return "camera"
        if not facts.scene.observable and self.context.neighbor_frames and re.search("相邻.*不清|场景.*变化|地点.*不清", uncertainties):
            return "neighbors"
        return None

    def _tool(self, name, payload):
        self._remaining()
        if len(self.trace) + 2 >= self.budget.max_steps:  # reserve supplement + finalize
            raise RuntimeError("执行步骤不足，停止补证据")
        try:
            return self.tools.call(name, payload, step_allowance=2)
        finally:
            self.trace.append({"state": "tool", "name": name})

    def _supplement(self, observation, gap):
        if self.budget.max_supplemental_calls == 0 or self.correction_used or self.operation_calls >= self.max_model_calls:
            self.warnings.append("补充推理预算为零，需人工复核")
            return observation
        comparison = None
        if gap == "detail":
            cited = next((ref for field in iter_observed_fields(observation)
                          for ref in field.evidence if ref.bbox and ref.frame_id in self.tools.base_ids), None)
            source = self.tools.frames[cited.frame_id] if cited else self.frames[0]
            highres = self._tool("get_high_res_frame", {"frame_id": source.id})
            box = cited.bbox if cited else None
            if box:
                self._tool("crop_frame", {"frame_id": highres.frames[0].id, "bbox": box})
            elif source.width and source.height and highres.frames[0].width <= source.width and highres.frames[0].height <= source.height:
                raise RuntimeError("原图分辨率没有增加，停止重复请求，保留已有字段待复核")
        elif gap == "action":
            self._tool("sample_motion_peak_frames", {"count": min(2, self.budget.max_extra_frames)})
        elif gap == "camera":
            result = self._tool("compare_frames", {
                "first_frame_id": self.frames[0].id, "last_frame_id": self.frames[-1].id,
            })
            comparison = result.comparison.model_dump()
            if self.budget.max_extra_frames:
                self._tool("sample_motion_peak_frames", {"count": min(2, self.budget.max_extra_frames)})
        else:
            self._tool("get_neighbor_shots", {})
        if len(self.trace) + 1 >= self.budget.max_steps:
            raise RuntimeError("执行步骤不足，不能追加云端请求")
        frames = self.tools.catalogue()
        self.trace.append({"state": "supplement", "reason": gap})
        self.supplement_used = True
        prompt = (
            "下面初次分析有证据缺口，请根据附加证据校验并补充；不要凭空新增物体或剧情。"
            "相邻镜头只作叙事解释背景，不能把它们的物体当作本镜事实。"
            "裁剪帧只支持局部细节；光流指标只是线索，不能单独证明运镜。"
            "只补证据不足的字段；已有有证据的字段不会被覆盖。输出紧凑 fields JSON。"
            "\n" + self._prompt("motion", frames) +
            "\n已识别字段：" + json.dumps({name: field.value for name, field in [
                ("主体", observation.facts.primary_subject), ("场景", observation.facts.scene),
                ("动作", observation.dynamics.subject_action)] if field.observable}, ensure_ascii=False) +
            "\n辅助指标：" + json.dumps(comparison, ensure_ascii=False)
        )
        result = self._structured(prompt, frames, "shot.supplement", VisualObservationV2, 3072)
        self.normalized["supplement"] = result.model_dump()
        merged = observation.model_dump()
        for group in ("facts", "dynamics", "photography", "artistic_effects"):
            for name, field in getattr(result, group):
                current = getattr(getattr(observation, group), name)
                replace_generic = group == "dynamics" and name == "subject_action" and self._generic(observation)
                if hasattr(field, "observable") and field.observable and (not current.observable or replace_generic):
                    merged[group][name] = field.model_dump()
        return VisualObservationV2.model_validate(merged)

    def run(self):
        if not self.frames:
            raise ValueError("当前镜头没有可用关键帧")
        self.trace.append({"state": "stage_recovery" if self.recovery else "base_analysis"})
        static = self._seed_stage("static", StaticObservationV2) if self.seed else None
        motion = self._seed_stage("motion", MotionObservationV2) if self.seed else None
        selected = self.selected_stages if self.selected_stages is not None else recoverable_stages(self.seed) if self.seed else ["static", "motion"]
        authorization_failed = False
        transport_failure = None
        representative = self.frames[0]
        if "static" in selected and static is None:
            try:
                path = self.provider.helper._sharpest_image([frame.path for frame in self.frames])
                representative = next(frame for frame in self.frames if frame.path == path)
                static = self._execute_stage("static", [representative], self._prompt("static", [representative]),
                                             StaticObservationV2, 2304)
            except Exception as exc:
                if "static" not in self.stages:
                    self.stages["static"] = PerceptionStageRecord(state="deferred" if isinstance(exc, PerceptionCancelled) else "failed",
                                                                  error=safe_stage_error(exc), schema_status="not_run")
                self.warnings.append(f"静态分析未返回有效结构，保留其他阶段：{safe_stage_error(exc)['message']}")
                authorization_failed = bool(re.search(r"HTTP (401|403)", str(exc)))
                if isinstance(exc, CloudTransportError):
                    transport_failure = exc
        if "motion" in selected and motion is None:
            cancelled = self._cancel_requested()
            if cancelled or transport_failure is not None or authorization_failed or (self.recovery and static is None):
                message = "感知操作已取消，未发送动态请求" if cancelled else "云端鉴权失败，停止本镜后续请求" if authorization_failed else "静态阶段未完成，动态阶段等待依赖恢复"
                self._defer_motion(message, cancelled=cancelled)
                self.warnings.append(message)
            else:
                try:
                    anchors = {key: getattr(static.facts, key).value for key in ("primary_subject", "objects", "scene")
                               if static and getattr(static.facts, key).observable}
                    motion = self._execute_stage("motion", self.frames, self._prompt("motion", self.frames) +
                        "\n静态主体清单仅用于统一称呼，不替代时序证据：" + json.dumps(anchors, ensure_ascii=False),
                        MotionObservationV2, 1536)
                except Exception as exc:
                    self.warnings.append(f"动态分析未返回有效结构，保留其他阶段：{safe_stage_error(exc)['message']}")
                    if isinstance(exc, CloudTransportError):
                        transport_failure = exc
        if transport_failure is None and not authorization_failed:
            static, motion = self._correct_protocol(static, motion)
        observation = merge_stages(static, motion)
        if self.seed:
            observation.interpretations = copy.deepcopy(self.seed.observation.interpretations)
        self.trace.append({"state": "evidence_check"})
        gap = self._gap(observation)
        # Transport/JSON failures and invalid optional coordinates are not visual gaps.
        if gap and not self.recovery and not self.correction_used and not self._cancel_requested() and transport_failure is None and (static is not None or motion is not None):
            try:
                observation = self._supplement(observation, gap)
            except Exception as exc:
                if isinstance(exc, CloudTransportError):
                    self.followup_transport_error = exc
                self.warnings.append(f"补证据未完成，保留已有分析并需复核：{safe_stage_error(exc)['message']}")
        if self._gap(observation) in ("detail", "action", "neighbors"):
            self.warnings.append("核心视觉信息仍不足，其他已识别字段已保留")
        if self._generic(observation):
            self.warnings.append("动作描述过于笼统，需复核")
        review = observation_review(observation)
        if review["static"]["status"] != "ready":
            self.warnings.append("静态主体或场景证据不足，其他有效字段已保留")
        if review["dynamic"]["status"] != "ready":
            self.warnings.append("动态分析待复核，已保留有效静态场景描述")
        final_fields = dict(observed_paths(observation))
        for entry in self.raw[self.previous_calls:]:
            for item in entry.get("field_repairs", []):
                path = item.get("field", "")
                if path in entry.get("resolved_fields", []):
                    continue
                # Wire camera fields live under photography in the merged IR.
                merged_path = "photography." + path if path.startswith("camera_movement_") else (
                    "artistic_effects.motion_signature" if path == "motion_signature" else path)
                field = final_fields.get(merged_path)
                if item.get("repair") == "field_only_unknown" and not (field and field.observable):
                    self.warnings.append(f"{path} 未通过校验，仅该字段待复核")
            self.warnings.extend(warning for warning in entry.get("field_warnings", [])
                                 if "未通过校验" not in warning)
        observation.warnings = list(dict.fromkeys([*observation.warnings, *self.warnings]))
        validate_evidence(observation, self.tools.catalogue())
        self.trace.append({"state": "finalize"})
        record = PerceptionRecord(
            observation=observation, raw_responses=self.raw,
            normalized_stages=self.normalized, trace=self.trace, tools=[*(self.seed.tools if self.seed else []), *self.tools.audit],
            budgets={**self.budget.model_dump(), "used_steps": len(self.trace),
                     "used_extra_frames": self.tools.extra_frames, "used_crops": self.tools.crops,
                     "used_model_calls": self.operation_calls, "total_record_model_calls": len(self.raw),
                     "strategy": "stage-recovery-v1" if self.recovery else "two-stage-compact-v2",
                     "local_repairs": len(self.local_repairs), "max_model_calls": self.max_model_calls,
                     "network_failure": transport_failure is not None or self.followup_transport_error is not None,
                     "cancelled": self.cancelled_requested,
                     "transport_error_category": (transport_failure or self.followup_transport_error).category if (transport_failure or self.followup_transport_error) else None},
            supplemental_call_used=self.supplement_used, correction_used=self.correction_used,
            elapsed_seconds=round(time.monotonic() - self.started, 3),
            stages=self.stages, input_frames=self._catalogue(self.tools.catalogue()),
            revisions=copy.deepcopy(self.seed.revisions) if self.seed else [],
            source_revision=self.seed.source_revision if self.seed else None,
        )
        record.budgets["review"] = observation_review(observation, record)
        projection = project_v1(
            observation, raw_response=json.dumps(self.raw, ensure_ascii=False),
            partial_failure=static is None or motion is None,
        )
        projection.perception_record = record
        if "analysis_review" in type(projection).model_fields:
            projection.analysis_review = record.budgets["review"]
        return projection
