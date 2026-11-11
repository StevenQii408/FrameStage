"""Scoped source-evidence revisions; no database or cloud side effects here."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from PIL import Image

from app.perception_models import PerceptionRecord, VisualObservationV2, iter_observed_fields, observation_review, project_v1, validate_evidence
from app.services.perception_wire import decode_stage


ACTION_FIELDS = frozenset(f"dynamics.{name}" for name in ("subject_action", "initial_state", "action_steps", "final_state"))
FACT_FIELDS = frozenset(f"facts.{name}" for name in ("primary_subject", "objects", "people", "scene", "foreground", "midground", "background", "relationships"))
DYNAMIC_FIELDS = ACTION_FIELDS | frozenset(f"dynamics.{name}" for name in ("interaction_target", "emotion_change", "accompanying_states"))
REVIEW_GROUPS = {
    "facts": FACT_FIELDS,
    "subject": frozenset(("facts.primary_subject", "facts.objects", "facts.people")),
    "scene": frozenset(("facts.scene",)),
    "spatial": frozenset(f"facts.{name}" for name in ("foreground", "midground", "background", "relationships")),
    "action": ACTION_FIELDS,
    "dynamics": DYNAMIC_FIELDS,
    "interaction": frozenset(("dynamics.interaction_target",)),
    "expression": frozenset(("dynamics.emotion_change",)),
    "accompanying": frozenset(("dynamics.accompanying_states",)),
}
_UNSUPPORTED_CLAIM = re.compile(
    r"因为|所以|导致|为了|目的是|意图|打算|试图|报复|意识到|想要|觉得|感到|享受|品尝|吹凉|"
    r"正在说话|交谈|台词|全程|始终|一直|\b(?:because|therefore|in order to|intends?|wants?|realizes?|enjoys?|throughout)\b", re.I)


def review_group_fields(field_group):
    if field_group in REVIEW_GROUPS:
        return REVIEW_GROUPS[field_group]
    if field_group in ACTION_FIELDS:
        return ACTION_FIELDS
    if field_group in FACT_FIELDS | DYNAMIC_FIELDS:
        return frozenset((field_group,))
    raise ValueError("复核仅限指定的可见事实或动作字段；摄影与画风不在修订范围")


def frame_snapshot(frame):
    data = {"frame_id": frame.id, "timestamp": frame.timestamp, "artifact_kind": frame.artifact_kind,
            "parent_frame_id": frame.parent_frame_id, "width": frame.width, "height": frame.height}
    try:
        data["content_sha256"] = hashlib.sha256(Path(frame.path).read_bytes()).hexdigest()
        with Image.open(frame.path) as image:
            rgb = image.convert("RGB")
            data.update(width=rgb.width, height=rgb.height)
            data["visual_sha256"] = hashlib.sha256(f"{rgb.size}".encode() + rgb.tobytes()).hexdigest()
    except (OSError, ValueError):
        # Missing source images cannot become extra evidence later.
        data["content_sha256"] = data["visual_sha256"] = None
    return data


def _old_frames(record):
    rows = [*record.input_frames]
    rows.extend(frame for call in record.raw_responses for frame in call.get("input_frames", []))
    for stage in record.stages.values():
        rows.extend(stage.input_frames)
    for tool in record.tools:
        rows.extend({"frame_id": row.get("id"), **row} for row in (tool.get("output") or {}).get("frames", []))
    for field in iter_observed_fields(record.observation):
        rows.extend({"frame_id": ref.frame_id, "timestamp": ref.timestamp} for ref in field.evidence)
    catalogue = {}
    for row in rows:
        frame_id = row.get("frame_id")
        if frame_id:
            catalogue[frame_id] = {**catalogue.get(frame_id, {}), **{key: value for key, value in row.items() if value is not None}}
    return catalogue


def fresh_evidence_ids(record, frames, context):
    old = _old_frames(record)
    current = {frame.id: frame_snapshot(frame) for frame in frames}
    # Some legacy records contain only final observations. Every supplied
    # canonical keyframe still belongs to their original source catalogue,
    # even if no surviving field happened to cite it.
    for frame in frames:
        if frame.artifact_kind == "keyframe":
            old.setdefault(frame.id, current[frame.id])
    for frame_id, source in old.items():
        if frame_id not in current:
            continue
        actual = current[frame_id]
        if (isinstance(source.get("timestamp"), (int, float)) and abs(source["timestamp"] - actual["timestamp"]) > .002) or (
            source.get("visual_sha256") and source["visual_sha256"] != actual.get("visual_sha256")):
            return set()
    # Legacy telemetry did not store image hashes. Recover them from the exact
    # registered original IDs, without treating a changed ID as provenance.
    for frame_id in old:
        if frame_id in current:
            old[frame_id] = {**current[frame_id], **old[frame_id]}
    old_hashes = {row.get("visual_sha256") for row in old.values()} - {None}
    old_times = [row["timestamp"] for row in old.values() if isinstance(row.get("timestamp"), (float, int))]
    fresh = set()
    for frame in frames:
        row = current[frame.id]
        if frame.id in old or not row.get("visual_sha256") or row["visual_sha256"] in old_hashes:
            continue
        if not context.start_time <= frame.timestamp < context.end_time:
            continue
        # Only locally published source extractions qualify. Renaming a base
        # keyframe or citing a neighboring shot cannot authorize a revision.
        temporal_gain = frame.artifact_kind in ("motion", "review") and old_times and all(abs(frame.timestamp - timestamp) > .002 for timestamp in old_times)
        parent = old.get(frame.parent_frame_id)
        resolution_gain = (frame.artifact_kind == "highres" and parent and
            abs(frame.timestamp - parent.get("timestamp", -1)) <= .002 and
            row["width"] >= parent.get("width", row["width"]) and row["height"] >= parent.get("height", row["height"]) and
            (row["width"] > parent.get("width", row["width"]) or row["height"] > parent.get("height", row["height"])))
        if temporal_gain or resolution_gain:
            fresh.add(frame.id)
    return fresh


def _field(model, path):
    group, name = path.split(".")
    return getattr(getattr(model, group), name)


def _unsupported_claim(field):
    value = " ".join(field.value) if isinstance(field.value, list) else field.value
    if _UNSUPPORTED_CLAIM.search(value):
        return True
    for ref in field.evidence:
        description = ref.description
        if re.search(r"(?:最后|最初)[几两一二三四五六\d]*帧|这[几两三四五六\d]+帧|(?:采样|附图|所示)帧|F\d+(?:\s*[-–~至]\s*F?\d+)?(?:这些)?采样帧", description):
            # Persistence explicitly limited to attached samples is not a
            # blanket assertion about the entire unsampled source interval.
            # "全程" remains unsupported even if a frame ID or sampled
            # interval appears elsewhere in the same evidence description.
            description = re.sub(r"始终|一直", "", description)
        if _UNSUPPORTED_CLAIM.search(description):
            return True
    return False


def _project(record):
    record.budgets["review"] = observation_review(record.observation, record)
    projection = project_v1(record.observation, raw_response=json.dumps(record.raw_responses, ensure_ascii=False),
        partial_failure=any(stage.state in ("failed", "deferred") for stage in record.stages.values()))
    projection.perception_record = record
    projection.analysis_review = record.budgets["review"]
    return projection


def revision_audit(record, field_group, reason, source_revision=None):
    return {"revision_id": uuid.uuid4().hex, "field_group": field_group, "reason": reason,
            "source_revision": source_revision if source_revision is not None else record.source_revision,
            "status": "rejected", "patches": [], "rationale": "", "error": None}


def reject_revision(record, audit, message):
    result = record.model_copy(deep=True)
    audit = copy.deepcopy(audit)
    audit["error"] = message
    result.revisions.append(audit)
    return _project(result)


def apply_revision(record, proposal, frames, context, field_group, reason, *, source_revision=None):
    """Apply a fully validated atomic proposal or retain the original with audit."""
    record = PerceptionRecord.model_validate(record) if isinstance(record, dict) else record
    audit = revision_audit(record, field_group, reason, source_revision)
    try:
        allowed = review_group_fields(field_group)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("复核必须记录用户指出的问题")
        if source_revision is not None and record.source_revision is not None and source_revision != record.source_revision:
            raise ValueError("复核来源版本已变更")
        fresh = fresh_evidence_ids(record, frames, context)
        if not fresh:
            raise ValueError("没有来自本镜源片的新增时序或分辨率证据；重复帧不能修订已有事实")
        if not isinstance(proposal, dict) or set(proposal) - {"fields", "rationale"}:
            raise ValueError("修订提议必须只包含 fields 和 rationale")
        fields = proposal.get("fields")
        rationale = proposal.get("rationale")
        if not isinstance(fields, dict) or not fields or not set(fields) <= allowed:
            raise ValueError("提议包含未授权字段或没有字段")
        if not isinstance(rationale, str) or not 4 <= len(rationale.strip()) <= 1200:
            raise ValueError("修订必须说明旧值与新增可见证据的差异")
        audit["rationale"] = rationale
        candidate, repairs, warnings = decode_stage(json.dumps({"fields": fields}, ensure_ascii=False), VisualObservationV2,
            frames, context.start_time, context.end_time)
        if warnings or any(row.get("repair") == "field_only_unknown" for row in repairs):
            raise ValueError("修订字段结构或帧引用无效")
        if set(fields) & ACTION_FIELDS:
            # A patch may omit unchanged anchors. Copy their exact saved IR,
            # then validate the complete group atomically; never synthesize
            # or paraphrase an initial/final state that was unknown before.
            for path in ACTION_FIELDS - set(fields):
                setattr(candidate.dynamics, path.split(".")[1], copy.deepcopy(_field(record.observation, path)))
            audit["validated_field_group"] = sorted(ACTION_FIELDS)
        merged = record.observation.model_dump()
        changed = []
        for path in fields:
            old, new = _field(record.observation, path), _field(candidate, path)
            if _unsupported_claim(new):
                raise ValueError("修订引入了源片无法直接证明的因果、意图、音轨或全程持续断言")
            if old.value == new.value and old.observable == new.observable:
                continue
            if not new.observable or not any(ref.frame_id in fresh for ref in new.evidence):
                raise ValueError("每个改变的事实必须有新增可见证据，不能仅靠理由或原证据改写")
            group, name = path.split(".")
            merged[group][name] = new.model_dump()
            changed.append({"field": path, "old": old.model_dump(), "new": new.model_dump(), "rationale": rationale})
        if not changed:
            audit.update(status="unchanged", error="新增证据未支持改变旧值")
            result = record.model_copy(deep=True)
            result.revisions.append(audit)
            return _project(result)
        if set(fields) & ACTION_FIELDS:
            dynamics = candidate.dynamics
            initial_times = [ref.timestamp for ref in dynamics.initial_state.evidence]
            final_times = [ref.timestamp for ref in dynamics.final_state.evidence]
            step_times = [ref.timestamp for ref in dynamics.action_steps.evidence]
            if not all(_field(candidate, path).observable for path in ACTION_FIELDS) or not dynamics.action_steps.value:
                raise ValueError("动作修订需要有证据的完整状态与步骤")
            validate_evidence(dynamics, frames)
            if not initial_times or not final_times or min(initial_times) > max(final_times) or len(set(step_times)) < 2:
                raise ValueError("动作修订缺少起始、变化与结果的有序时序证据")
            if max(final_times) < max(step_times) or min(initial_times) > min(step_times):
                raise ValueError("动作起始和最终状态不能与步骤证据顺序冲突")
            source_times = [frame.timestamp for frame in frames if context.start_time <= frame.timestamp < context.end_time]
            if not any(abs(timestamp - min(source_times)) <= .002 for timestamp in initial_times):
                raise ValueError("起始状态必须引用本镜最早源帧")
            if not any(abs(timestamp - max(source_times)) <= .002 for timestamp in final_times):
                raise ValueError("最终状态必须引用本镜最新源帧，不能把中间状态作为结果")
            if dynamics.step_evidence:
                if len(dynamics.step_evidence) != len(dynamics.action_steps.value) or any(not refs for refs in dynamics.step_evidence):
                    raise ValueError("动作步骤必须逐项对应有效证据")
                starts = [min(ref.timestamp for ref in refs) for refs in dynamics.step_evidence]
                if starts != sorted(starts):
                    raise ValueError("动作步骤证据不能按逆时序排列")
            merged["dynamics"]["action_phases"] = []
            merged["dynamics"]["step_evidence"] = [
                [ref.model_dump() for ref in refs] for refs in dynamics.step_evidence]
        observation = VisualObservationV2.model_validate(merged)
        validate_evidence(observation, frames)
        result = record.model_copy(deep=True)
        result.observation = observation
        result.input_frames = [frame_snapshot(frame) for frame in frames]
        audit.update(status="applied", patches=changed, new_evidence_frame_ids=sorted(fresh))
        result.revisions.append(audit)
        return _project(result)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return reject_revision(record, audit, str(exc).splitlines()[0][:300])


def review_observation(provider, context, frames, record, field_group, reason, *, source_revision=None, cancelled=None):
    """One explicit evidence-bearing cloud review, with no correction retries."""
    record = PerceptionRecord.model_validate(record) if isinstance(record, dict) else record
    audit = revision_audit(record, field_group, reason, source_revision)
    try:
        allowed = review_group_fields(field_group)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("复核必须记录用户指出的问题")
        if source_revision is not None and record.source_revision is not None and source_revision != record.source_revision:
            raise ValueError("复核来源版本已变更")
        fresh = fresh_evidence_ids(record, frames, context)
        if not fresh:
            raise ValueError("没有来自本镜源片的新增时序或分辨率证据；不会请求模型重复猜测")
    except (ValueError, TypeError) as exc:
        projection = reject_revision(record, audit, str(exc))
        projection.perception_record.budgets = {**record.budgets, "strategy": "explicit-evidence-review-v1",
            "used_model_calls": 0, "max_model_calls": 1, "total_record_model_calls": len(record.raw_responses)}
        return projection
    frames = sorted(frames, key=lambda frame: frame.timestamp)
    prompt = (
        "针对用户指出的可见事实错误做一次证据复核，用户理由只是待核查问题，不能当事实。"
        "只输出 JSON 对象 {fields:{字段路径:{value:值,frames:[F编号],note:可见证据描述,confidence:0到1}},rationale:说明旧值与新增证据的差异}。"
        "仅在新增源片证据直接支持时修订指定字段；不能修改摄影或画风，不能引入因果、意图、用途、对白或未见剧情。"
        "动作按 subject_action、initial_state、action_steps、final_state 的完整组校验。原来已明确且本次保持原值的状态可省略，服务端保留原字段；原来未知或需改变的状态必须给出可见证据。步骤引用变化前后帧；初始和最终状态按时序校验。"
        "未知或未获支持时保留旧值，不以用户猜测替换。F编号按附图时间排序，不能编造时间或帧ID。\n"
        + json.dumps({"allowed_fields": sorted(allowed), "user_reason": reason,
            "previous_fields": {path: _field(record.observation, path).model_dump() for path in allowed},
            "frames": [{"frame": f"F{index+1}", "timestamp": frame.timestamp, "additional_evidence": frame.id in fresh}
                       for index, frame in enumerate(frames)]}, ensure_ascii=False)
    )
    working = record.model_copy(deep=True)
    call = {"phase": "shot.review", "attempt": 1, "input_frames": [frame_snapshot(frame) for frame in frames],
            "response": None, "error": None, "schema_enforcement": "local-scoped-revision", "validation": "failed"}
    if cancelled and cancelled():
        projection = reject_revision(record, audit, "感知操作已取消，未发送复核请求")
        projection.perception_record.budgets = {**record.budgets, "strategy": "explicit-evidence-review-v1", "cancelled": True,
            "used_model_calls": 0, "max_model_calls": 1, "total_record_model_calls": len(record.raw_responses)}
        return projection
    working.raw_responses.append(call)
    started = time.monotonic()
    usage_start = len(getattr(provider, "usage", []))
    try:
        raw = provider._call(prompt, frames, "shot.review", max_tokens=1536,
            timeout_seconds=min(provider.settings.vlm_timeout_seconds, 45), max_attempts=1, shot_id=context.shot_id)
        call["response"] = raw
        text = raw.strip()
        if text.startswith("```") and text.endswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)[:-3].strip()
        projection = apply_revision(record, json.loads(text), frames, context, field_group, reason, source_revision=source_revision)
        call["validation"] = "passed" if projection.perception_record.revisions[-1]["status"] != "rejected" else "failed"
    except Exception as exc:
        from app.services.perception import safe_stage_error
        call["error"] = safe_stage_error(exc, schema=isinstance(exc, (ValueError, TypeError)))["message"]
        projection = reject_revision(working, audit, call["error"])
    finally:
        call["usage"] = copy.deepcopy(getattr(provider, "usage", [])[usage_start:])
    output = projection.perception_record
    output.raw_responses = [*copy.deepcopy(record.raw_responses), call]
    output.input_frames = [frame_snapshot(frame) for frame in frames]
    output.supplemental_call_used = True
    output.elapsed_seconds = round(time.monotonic() - started, 3)
    output.budgets = {**output.budgets, "strategy": "explicit-evidence-review-v1", "used_model_calls": 1,
        "max_model_calls": 1, "total_record_model_calls": len(output.raw_responses)}
    output.revisions[-1]["usage"] = call["usage"]
    return projection
