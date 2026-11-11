"""Read-only diagnostics and export; no model, credential, or database writes."""
from datetime import datetime
import json

VERSION = "style-diagnostics.v1"
LABELS = {"observing": "基础视觉观察", "acquiring": "本地补证据", "verifying": "补充核验",
          "retrieving": "候选召回", "interpreting": "专业转译", "rendering": "校验与渲染"}


def build_diagnostics(result, *, stages=(), validation_seconds=0, retrieval_seconds=0, render_seconds=0):
    calls = result.get("raw_responses", [])
    audit = result.get("audit", [])
    budget = result.get("budget", {})
    profile = result.get("profile", {})
    return {"schema_version": VERSION, "status": "available", "stages": list(stages),
        "candidate_snapshot": result.get("candidate_snapshot", []),
        "decisions": list(audit),
        "timing": {"total_seconds": budget.get("elapsed_seconds"),
                   "model_seconds": round(sum(c.get("elapsed_seconds", 0) for c in calls), 4),
                   "validation_seconds": round(validation_seconds, 4),
                   "retrieval_seconds": round(retrieval_seconds, 4),
                   "render_seconds": round(render_seconds, 4)},
        "calls": {key: budget.get(key) for key in ("model_requests", "max_model_requests", "correction_used", "supplement_rounds", "crop_count")},
        "requests": [{key: c.get(key) for key in ("phase", "schema", "images", "status", "prompt_characters", "prompt_sha256", "elapsed_seconds", "validation_status", "validation_error")}
                     for c in calls],
        "quality": {"state": result.get('quality_state'), "evidence_claims": len(result.get("evidence", {}).get("claims", [])),
                    "approved_descriptions": len(profile.get("descriptions", [])),
                    "selected_anchor_id": (profile.get("anchor") or {}).get("id"),
                    "selected_reference_id": (profile.get("reference") or {}).get("id"),
                    "rejected_or_repaired_fields": len(audit),
                    "human_accuracy": None, "semantic_validation":
                        "quoted-reference-comparison+explicit-controls" if profile.get('reference') else
                        "model-grounded-quotes+explicit-controls" if result.get('quality_state', {}).get('anchor_validation') == 'model-grounded-quotes'
                        else "explicit-controls-only",
                    "note": "引用、范围和已知矛盾检查不等于完整语义或真实视觉准确率"},
        "provenance": dict(profile.get("provenance", {})),
        "usage": {"prompt_tokens": None, "completion_tokens": None,
                  "estimated_list_price_cny": None, "complete": False}}


def attach_usage(result, usage):
    """Missing/failed usage is unknown, not free. Keep known subtotals separate."""
    diagnostic = result.get("diagnostics")
    if not diagnostic:
        return result
    records = list(usage or [])
    expected = result.get("budget", {}).get("model_requests", 0)
    tokens_complete = bool(records) and len(records) == expected and all(u.get("usage_available") for u in records)
    cost_complete = bool(records) and len(records) == expected and all(u.get("estimated_list_price_cny") is not None for u in records)
    known_costs = [u["estimated_list_price_cny"] for u in records if u.get("estimated_list_price_cny") is not None]
    diagnostic["usage"] = {
        "prompt_tokens": sum(u.get("prompt_tokens") or 0 for u in records) if tokens_complete else None,
        "completion_tokens": sum(u.get("completion_tokens") or 0 for u in records) if tokens_complete else None,
        "estimated_list_price_cny": sum(known_costs) if cost_complete else None,
        "known_cost_subtotal_cny": sum(known_costs) if known_costs else None,
        "record_count": len(records), "token_usage_complete": tokens_complete,
        "cost_estimate_complete": cost_complete, "complete": tokens_complete and cost_complete,
        "failed_requests": sum(u.get("status") == "failed" for u in records),
    }
    return result


def legacy_diagnostics():
    return {"schema_version": VERSION, "status": "legacy-unavailable", "stages": [],
            "candidate_snapshot": [], "decisions": [], "timing": {}, "calls": {}, "usage": {},
            "quality": {"human_accuracy": None},
            "note": "历史任务未记录该诊断，不重算、不补造候选或阶段耗时"}


def _safe(value):
    if value is None:
        return "未记录"
    return str(value).replace("\n", " ").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("`", "'")


def markdown_report(result):
    """Accept a full analysis result or its exported diagnostics."""
    data = result.get("diagnostics", result) if result else legacy_diagnostics()
    timing, calls, usage = data.get("timing", {}), data.get("calls", {}), data.get("usage", {})
    lines = ["# 画风分析诊断", "", "结构/引用通过不代表真实语义正确；真实质量仍需人工评审。", "",
             "状态：" + _safe(data.get("status", "unknown")), "",
             "总耗时：" + _safe(timing.get("total_seconds", "未记录")) + " 秒；模型调用：" + _safe(calls.get("model_requests", "未记录")), "",
             "输入 Token：" + _safe(usage.get("prompt_tokens")) + "；输出 Token：" + _safe(usage.get("completion_tokens")), "",
             "人民币估算：" + _safe(usage.get("estimated_list_price_cny")) + "；用量完整：" + _safe(usage.get("complete", False)), "",
             "## 阶段", "", "| 阶段 | 秒 | 请求 | 状态 |", "| --- | ---: | ---: | --- |"]
    for stage in data.get("stages", []):
        lines.append("| " + " | ".join(_safe(stage.get(k)) for k in ("label", "elapsed_seconds", "model_requests", "status")) + " |")
    state = data.get('quality', {}).get('state')
    if state:
        lines += ['', '## 结果状态', '']
        for key, label in (('structure', '结构校验'), ('observation', '视觉观察'),
                           ('interpretation', '专业转译'), ('style', '画风判断'), ('anchor', '具体风格锚点'),
                           ('reference', '生成视觉参考（非来源鉴定）')):
            lines.append('- ' + label + '：' + _safe(state.get(key)))
    lines += ["", "## 候选与拒绝", ""]
    for candidate in data.get("candidate_snapshot", []):
        lines.append("- " + _safe(candidate.get("label")) + "（" + _safe(candidate.get("id")) + "）：完整组合=" +
                     _safe(candidate.get("selection_eligible")) + "；缺失组=" + _safe(candidate.get("missing_group_ids", [])))
    for decision in data.get("decisions", []):
        lines.append("- " + _safe(decision.get("field", "")) + "：" + _safe(decision.get("reason") or decision.get("repair") or decision.get("verified_feature_groups")))
    lines += ["", "## 来源版本", "", "~~~~json", json.dumps(data.get("provenance", {}), ensure_ascii=False, indent=2), "~~~~", ""]
    return "\n".join(lines)


def job_diagnostics(service, job_id):
    """End-to-end latency from persisted events, including queue wait; read only."""
    job = service.status(job_id)
    if not job:
        raise ValueError("任务不存在")
    events = service.db.events_since(job_id)
    first = next((e for e in events if e["type"] == "prompt.ready"), None)
    def seconds_between(start, end):
        if not start or not end:
            return None
        try:
            return round(max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()), 4)
        except (ValueError, TypeError):
            return None
    state = json.loads(job.get("style_direct_json") or "{}")
    analysis_id = state.get("pending_analysis_id") or state.get("analysis_id")
    style = legacy_diagnostics()
    if analysis_id:
        row = service.db.fetchone("SELECT result_json FROM style_analyses WHERE id=?", (analysis_id,))
        if row:
            style = json.loads(row["result_json"] or "{}").get("diagnostics") or legacy_diagnostics()
    # Only supplied usage is reported; no draining provider or mutating old rows.
    usage = json.loads(job.get("model_usage_json") or "[]")
    per_shot = {}
    for item in usage:
        key = item.get("shot_id")
        if key:
            value = per_shot.setdefault(key, {"shot_id": key, "requests": 0, "model_seconds": 0, "phases": []})
            value["requests"] += 1
            value["model_seconds"] = round(value["model_seconds"] + (item.get("elapsed_seconds") or 0), 4)
            value["phases"].append({k: item.get(k) for k in ("phase", "status", "elapsed_seconds", "usage_available", "prompt_tokens", "completion_tokens", "estimated_list_price_cny")})
    return {"schema_version": "job-analysis-diagnostics.v1", "job_id": job_id,
            "provider": job.get("provider"), "model": job.get("model"), "status": job.get("status"),
            "first_prompt_seconds_including_queue": seconds_between(job.get("created_at"), first.get("created_at") if first else None),
            "first_prompt_shot_id": first.get("payload", {}).get("shot_id") if first else None,
            "style_analysis_id": analysis_id, "style": style, "shots": list(per_shot.values()),
            "note": "无历史事件/用量时不补造延迟；首镜时间包含排队与预处理"}
