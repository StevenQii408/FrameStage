"""Read-only delivery health: processing completion is not a quality verdict."""
from __future__ import annotations

import math


def _dict(value):
    return value.model_dump() if hasattr(value, "model_dump") else value or {}


def _source_availability(shot, input_type):
    """Retained observations and readiness to generate motion are separate.

    This is a read-only projection; it never repairs facts or supplies missing
    dynamic evidence. Use the same normalized review contract as perception.
    """
    if input_type == "script":
        ready = bool(shot.get("action_details", {}).get("steps"))
        return dict(usable=ready, static=ready, dynamic=ready, video=ready)
    observation = shot.get("visual_observation_v2")
    if observation:
        from app.perception_models import VisualObservationV2, iter_observed_fields, observation_review

        try:
            model = VisualObservationV2.model_validate(observation)
        except (ValueError, TypeError):
            # Malformed or unverified IR cannot be promoted through a looser
            # legacy text projection. The saved response remains untouched.
            return dict(usable=False, static=False, dynamic=False, video=False)
        review = observation_review(model)
        static = any(field.observable and field.evidence for field in iter_observed_fields(model.facts))
        dynamic = review["dynamic"]["status"] == "ready"
        retained_dynamic = any(field.observable and field.evidence for field in iter_observed_fields(model.dynamics))
        return dict(usable=bool(static or retained_dynamic), static=bool(static),
                    dynamic=dynamic, video=review["video_ready"])
    # Old results have no evidence IR. Preserve their previous compatibility
    # behavior, while allowing saved static scene descriptions to survive a
    # missing action string.
    def known(value):
        return isinstance(value, str) and value.strip() not in ("", "未知", "无", "待设定", "待判断", "待确认")
    facts, details = shot.get("visual_facts", {}), shot.get("action_details", {})
    static = any(known(value) for value in (
        facts.get("scene"), details.get("primary_subject"),
        *facts.get("objects", []), *facts.get("people", [])))
    dynamic = known(shot.get("action"))
    return dict(usable=static or dynamic, static=static, dynamic=dynamic, video=dynamic)


def _warning_scope(warning):
    """Only recognized non-blocking diagnostics may leave core readiness intact."""
    if isinstance(warning, dict):
        if warning.get("scope") == "style":
            return "style"
        return "info" if warning.get("severity") == "info" else "review"
    message = str(warning).strip()
    if message.startswith((
        "画风分析未完成；本镜仅保留基础视觉观察",
        "画风仅部分确定，已保留有证据的特征",
        "画风字段部分待复核，当前视觉参考已通过证据校验",
        "本镜画风转换未完成，动作与摄影结果已保留",
        "整体画面风格待复核，未将未知画风占位写入正文",
    )) or message == "Prompt校验：合同必需字段待复核：observed_style":
        return "style"
    if message == "动作摘要可能省略独立事件或来源方向，已保留来源动作；未自动拆镜":
        return "info"  # The renderer already kept the full source action.
    return "review"


def _item_readiness(item):
    warnings = item.get("warnings") or []
    scopes = [_warning_scope(warning) for warning in warnings]
    # Lint structure errors remain blocking even if a caller failed to copy
    # their text into warnings. A missing style alone is scoped separately.
    delivery = _dict(item.get("contract")).get("delivery", {})
    lint_key = "video" if item.get("prompt_type") == "video" else "first_frame"
    for warning in delivery.get("prompt_lint", {}).get(lint_key, {}).get("issues", []):
        scopes.append(_warning_scope("Prompt校验：" + warning))
    status = item.get("status", "queued")
    blocking = (status not in ("ready", "warning") or "review" in scopes
                or status == "warning" and not scopes)
    return not blocking, "style" in scopes


def _sequence_coverage(sequences, shots):
    """One long source may legitimately occur in several disjoint clips."""
    source_ids = [shot.get("id") for shot in shots]
    span_lists = [_dict(_dict(seq.get("seedance_prompt")).get("contract")).get("delivery", {}).get("source_spans", [])
                  for seq in sequences]
    if not any(span_lists):
        return [sid for seq in sequences for sid in seq.get("source_shot_ids", [])] == source_ids
    if not all(span_lists) or len(set(source_ids)) != len(source_ids):
        return False
    if any(not isinstance(spans, list) or any(not isinstance(span, dict) for span in spans) for spans in span_lists):
        return False
    by_id = {shot["id"]: shot for shot in shots}
    cursor = {sid: by_id[sid].get("start_time") for sid in source_ids}
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in cursor.values()):
        return False
    order = {sid: index for index, sid in enumerate(source_ids)}
    previous = -1
    for seq, spans in zip(sequences, span_lists):
        if list(dict.fromkeys(span.get("source_shot_id") for span in spans)) != seq.get("source_shot_ids", []):
            return False
        for span in spans:
            sid, start, end = span.get("source_shot_id"), span.get("start_time"), span.get("end_time")
            source_end = by_id.get(sid, {}).get("end_time")
            if (sid not in cursor or any(not isinstance(value, (int, float)) or not math.isfinite(value)
                                        for value in (start, end, source_end))
                    or order[sid] < previous or abs(start - cursor[sid]) > 0.0001
                    or end <= start or end > source_end + 0.0001):
                return False
            cursor[sid], previous = end, order[sid]
    return all(abs(cursor[sid] - by_id[sid]["end_time"]) <= 0.0001 for sid in source_ids)


def review_requests_view(metadata, operations, run_created_at):
    """Resolve displayed suggestions only after an explicit matching review.

    A read does not update archived Agent decisions or trigger inference.
    """
    import json
    requests = []
    for request in metadata.get("fact_review_requests", []):
        item = dict(request)
        matches = [op for op in operations if op["shot_id"] == item.get("source_shot_id")
                   and op["mode"] == "review" and op["status"] in ("applied", "unchanged", "rejected")
                   and op["created_at"] >= run_created_at
                   and json.loads(op["request_json"]).get("field_group") == item.get("field_group")]
        if matches:
            latest = max(matches, key=lambda op: op["created_at"])
            item.update(status="reviewed", operation_id=latest["id"], review_outcome=latest["status"])
        requests.append(item)
    return requests


def delivery_quality(job, shots, prompts, derived=None, direct_style=None, script_plan=None):
    derived, direct_style, plan = derived or {}, direct_style or {}, script_plan or {}
    shots, prompts = [_dict(s) for s in shots], [_dict(p) for p in prompts]
    execution = job["status"]
    issues = []
    visual = dict(total=len(shots), usable=0, ready=0, warning=0, failed=0, unknown=0,
                  static_usable=0, dynamic_usable=0, video_usable=0, static_only=0,
                  dynamic_review=0, video_review=0)
    availability = {}
    for shot in shots:
        status = shot.get("status", "queued")
        if status in ("ready", "warning", "failed"):
            visual[status] += 1
        state = _source_availability(shot, job["input_type"])
        availability[shot.get("id")] = state
        finished = status in ("ready", "warning") or (status == "failed" and bool(shot.get("visual_observation_v2")))
        usable = finished and state["usable"]
        visual["usable"] += int(usable)
        visual["unknown"] += int(not usable and status not in ("queued", "processing"))
        visual["static_usable"] += int(finished and state["static"])
        visual["dynamic_usable"] += int(finished and state["dynamic"])
        visual["video_usable"] += int(finished and state["video"])
        visual["static_only"] += int(finished and state["static"] and not state["dynamic"])
        visual["dynamic_review"] += int(usable and not state["dynamic"])
        visual["video_review"] += int(usable and not state["video"])
    seqs = derived.get("generation_sequences", [])
    metadata = derived.get("sequence_metadata", {})
    stale = bool(metadata.get("source_refresh_needed"))
    ownership = [sid for seq in seqs for sid in seq.get("source_shot_ids", [])]
    covered = set(ownership)
    changed_ids = {sid for revision in metadata.get("perception_revisions", []) for sid in revision.get("sequence_ids", [])}
    stale_ids = changed_ids if stale and changed_ids else {s.get("id") for s in seqs} if stale else set()
    sequence_ready = [_item_readiness(seq)[0] for seq in seqs]
    fallback = sum(not ready and seq.get("status") not in ("queued", "processing")
                   for seq, ready in zip(seqs, sequence_ready))
    coverage_complete = _sequence_coverage(seqs, shots)
    aggregation = dict(state="pending", total=len(seqs), ready=sum(ready and seq.get("id") not in stale_ids
                       for seq, ready in zip(seqs, sequence_ready)),
                       fallback=fallback, stale=len(stale_ids), covered_sources=len(covered))
    if stale:
        aggregation["state"] = "stale"
    elif seqs:
        aggregation["state"] = "partial" if fallback or not coverage_complete else "ready"
    elif execution in ("completed", "failed"):
        aggregation["state"] = "unavailable"
    profile = direct_style.get("profile") or {}
    style_state = "ready" if job["input_type"] == "script" else "unknown"
    if profile:
        style_state = "ready" if profile.get("application_status") == "ready" and profile.get("rendered_prompt") else "partial" if profile.get("rendered_prompt") else "unknown"
    style = {"state": style_state, "origin": "creative" if job["input_type"] == "script" else "observed"}
    prompt_readiness = [_item_readiness(prompt) for prompt in prompts]
    source_review = sum(not readiness[0]
                        or (p.get("prompt_type") == "video" and p.get("shot_id") in availability
                            and not availability[p["shot_id"]]["video"])
                        for p, readiness in zip(prompts, prompt_readiness))
    output = dict(total=len(prompts), ready=len(prompts) - source_review, review=source_review,
                  style_review=sum(style_pending for _, style_pending in prompt_readiness),
                  stale=sum(bool(s.get(k)) for s in seqs if s.get("id") in stale_ids for k in ("first_frame_prompt", "seedance_prompt")))
    def issue(code, message, count=1):
        if count:
            issues.append(dict(code=code, message=message, count=count))
    issue("visual_unavailable", "部分来源镜头尚无可用的静态或动态观察", visual["unknown"])
    issue("dynamic_review", "部分镜头动态待复核，已识别的静态场景与关键帧已保留", visual["dynamic_review"])
    issue("video_context_review", "部分镜头动作已识别，视频起止状态或静态上下文仍待完善",
          visual["video_review"] - visual["dynamic_review"])
    issue("source_review", "部分来源镜头需要复核", visual["warning"])
    issue("aggregation_fallback", "部分事件片段采用保守规划或需要复核", fallback)
    issue("aggregation_coverage", "生成片段的来源区间尚未完整连续覆盖原片", int(bool(seqs) and not coverage_complete))
    issue("source_refresh_needed", "视觉事实已修订，事件Prompt待显式重新编排", int(stale))
    issue("style_partial", "项目画风尚未完整确定", int(style_state != "ready" and bool(shots)))
    issue("prompt_style_review", "部分Prompt的局部画风待复核，动作与场景内容仍可用",
          output["style_review"] if style_state == "ready" else 0)
    issue("script_fallback", "部分剧本窗口使用规则兜底", len(plan.get("fallback_windows", [])))
    pending_reviews = [r for r in metadata.get("fact_review_requests", []) if r.get("status", "pending") == "pending"]
    issue("agent_fact_review", "事件Agent发现上游事实疑点，等待证据复核", len(pending_reviews))
    if execution == "interrupted":
        issue("interrupted", "服务重启，任务已中断；已完成结果保留，请显式恢复")
    overall = "pending" if execution not in ("completed", "failed", "cancelled", "interrupted") else (
        "failed" if execution == "failed" and not visual["usable"] else "partial" if issues or execution != "completed" else "ready")
    if execution == "completed" and (aggregation["state"] != "ready" or output["review"]):
        overall = "partial"
    return {"version": "delivery-quality.v2", "execution_state": execution, "overall": overall,
            "visual": visual, "aggregation": aggregation, "style": style, "prompts": output,
            "issues": issues}
