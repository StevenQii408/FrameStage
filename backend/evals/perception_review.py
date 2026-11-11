"""Evidence statistics and explicit human review; never infer human accuracy from a model."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app.perception_models import VisualObservationV2, iter_observed_fields


RATINGS = ("action_correct", "objects_scene_correct", "spatial_correct", "artistic_effects_correct")


def shot_rows(report):
    return [(clip["id"], shot) for clip in report["clips"] for shot in clip["model_metrics"]]


def ratio(values):
    return sum(values) / len(values) if values else None


def technical_summary(report):
    records = [shot["perception"] for _, shot in shot_rows(report) if shot.get("perception")]
    stages = [raw for record in records for raw in record["raw_responses"]
              if raw["phase"] in ("shot.static", "shot.motion")]
    all_observable = all_evidenced = 0
    for record in records:
        for field in iter_observed_fields(VisualObservationV2.model_validate(record["observation"])):
            all_observable += int(field.observable)
            all_evidenced += int(field.observable and bool(field.evidence))
    usage = report.get("model_usage", [])
    priced = [row["estimated_list_price_cny"] for row in usage
              if row.get("estimated_list_price_cny") is not None]
    return {
        "v2_shots": len(records),
        "first_response_parse_rate": ratio([raw.get("validation") == "passed" for raw in stages]),
        "locally_repaired_stage_rate": ratio([bool(raw.get("field_repairs")) for raw in stages]),
        "partial_stage_rate": ratio([raw.get("validation") == "partial" for raw in stages]),
        "field_repair_count": sum(len(raw.get("field_repairs", [])) for raw in stages),
        "post_repair_stage_rate": ratio([stage in record["normalized_stages"]
                                        for record in records for stage in ("static", "motion")]),
        "fact_evidence_reference_coverage": all_evidenced / all_observable if all_observable else None,
        "without_supplement_rate": ratio([not record["supplemental_call_used"] for record in records]),
        "budget_compliance_rate": ratio([
            record["budgets"]["used_steps"] <= 6 and record["budgets"]["used_extra_frames"] <= 6
            and record["budgets"]["used_crops"] <= 4
            and sum(raw["phase"] == "shot.supplement" for raw in record["raw_responses"]) <= 1
            and sum(raw["phase"].endswith(".correction") for raw in record["raw_responses"]) <= 1
            for record in records]),
        "requests": len(usage),
        "prompt_tokens": sum(row.get("prompt_tokens", 0) for row in usage),
        "completion_tokens": sum(row.get("completion_tokens", 0) for row in usage),
        "estimated_list_price_cny": round(sum(priced), 6) if priced else None,
        "cost_estimate_complete": bool(usage) and len(priced) == len(usage),
        "human_quality_status": "pending",
        "note": "证据引用覆盖不代表证据真实支持结论；准确率与幻觉率须人工核验。缺失 usage 不是免费。",
    }


def review_draft(report, digest):
    return {
        "schema_version": "perception-human-review.v1", "report_sha256": digest,
        "reviewer": "", "reviewed_at": "",
        "shots": [
            {"clip_id": clip_id, "number": shot["number"], "ratings": {name: None for name in RATINGS},
             "generic_action": None, "unsupported_facts": None, "evaluated_facts": None,
             "notes": ""}
            for clip_id, shot in shot_rows(report)
        ],
    }


def score_review(report, review):
    expected = {(clip_id, shot["number"]) for clip_id, shot in shot_rows(report)}
    actual = [(shot["clip_id"], shot["number"]) for shot in review["shots"]]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("人工评审镜头必须与报告完全对应，不能重复、遗漏或增加")
    metrics = {name: [] for name in RATINGS}
    generic = []
    unsupported = evaluated = 0
    pending = False
    for shot in review["shots"]:
        if set(shot["ratings"]) != set(RATINGS):
            raise ValueError("人工评分字段不完整")
        for key, value in shot["ratings"].items():
            if value is not None and type(value) is not bool:
                raise ValueError("人工正确性须为布尔值或 null（不可判断）")
            if value is not None:
                metrics[key].append(value)
        if shot["generic_action"] is None:
            pending = True
        elif type(shot["generic_action"]) is not bool:
            raise ValueError("泛化空话须为布尔值")
        else:
            generic.append(shot["generic_action"])
        count, total = shot["unsupported_facts"], shot["evaluated_facts"]
        if count is None or total is None:
            pending = True
        elif type(count) is not int or type(total) is not int or not 0 <= count <= total:
            raise ValueError("无证据事实数必须在已检查事实数范围内")
        else:
            unsupported += count
            evaluated += total
    values = {name: ratio(items) for name, items in metrics.items()}
    values.update(generic_action_rate=ratio(generic),
                  unsupported_fact_rate=unsupported / evaluated if evaluated else None)
    checks = {
        "action_accuracy": values["action_correct"] is not None and values["action_correct"] >= 0.85,
        "objects_scene_accuracy": values["objects_scene_correct"] is not None and values["objects_scene_correct"] >= 0.90,
        "spatial_accuracy": values["spatial_correct"] is not None and values["spatial_correct"] >= 0.85,
        "hallucination_rate": values["unsupported_fact_rate"] is not None and values["unsupported_fact_rate"] <= 0.05,
        "generic_action_rate": values["generic_action_rate"] is not None and values["generic_action_rate"] <= 0.10,
    }
    complete = not pending and bool(review.get("reviewer", "").strip()) and bool(review.get("reviewed_at", "").strip())
    return {"status": "reviewed" if complete else "pending", "metrics": values,
            "sample_counts": {key: len(items) for key, items in metrics.items()},
            "evaluated_facts": evaluated, "checks": checks, "passed": complete and all(checks.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成空白人工评审表或核验已填评分；不会自动生成真值")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    raw = args.report.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    report = json.loads(raw)
    if args.review:
        review = json.loads(args.review.read_text(encoding="utf-8"))
        if review.get("report_sha256") != digest:
            parser.error("评审表不属于当前报告，禁止混用旧预测")
        output = score_review(report, review)
        output["technical_passed"] = report.get("technical_acceptance", report["acceptance"])["passed"]
        output["combined_passed"] = output["passed"] and output["technical_passed"]
    else:
        output = review_draft(report, digest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
