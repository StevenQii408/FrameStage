"""Signed human review, bound to an immutable report hash; never model scoring."""
import argparse
import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool

RATINGS = ("facts_accurate", "representative", "content_leakage", "unsupported_expansion", "prompt_usable")


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    facts_accurate: Optional[StrictBool] = None
    representative: Optional[StrictBool] = None
    content_leakage: Optional[StrictBool] = None
    unsupported_expansion: Optional[StrictBool] = None
    prompt_usable: Optional[StrictBool] = None
    comments: str = ""


class HumanReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["style-direct-human-review.v1"]
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: Literal["human"]
    reviewer: str
    reviewed_at: str
    annotation_signature: str
    samples: list[ReviewItem]


def review_draft(report, digest):
    return {"schema_version": "style-direct-human-review.v1", "report_sha256": digest,
            "source": "human", "reviewer": "", "reviewed_at": "", "annotation_signature": "",
            "samples": [{"id": item["id"], **{key: None for key in RATINGS}, "comments": ""}
                        for item in report["samples"]]}


def technical_summary(items):
    """Accounting only. A name mismatch is not a human perceptual accuracy score."""
    wrong_names, missed_names, model_seconds, total_seconds = [], [], [], []
    for item in items:
        prediction, gold = item["prediction"], item["gold"]
        profile = prediction.get('profile', {})
        anchor = (profile.get('reference') or profile.get('anchor') or {}).get('id')
        expected = gold.get("expected_recipe_ids", [])
        naming = gold.get("naming_expectation", "unreviewed")
        if naming != "unreviewed" and anchor and anchor not in expected:
            wrong_names.append({"id": item["id"], "selected": anchor, "accepted": expected})
        if naming == "named" and not anchor:
            missed_names.append(item["id"])
        timing = prediction.get("diagnostics", {}).get("timing", {})
        if timing.get("model_seconds") is not None:
            model_seconds.append(timing["model_seconds"])
        if prediction.get("budget", {}).get("elapsed_seconds") is not None:
            total_seconds.append(prediction["budget"]["elapsed_seconds"])
    return {"name_disagreements": wrong_names, "missing_names": missed_names,
            "known_model_seconds": sum(model_seconds) if model_seconds else None,
            "known_total_seconds": sum(total_seconds) if total_seconds else None,
            "human_accuracy": None, "note": "技术统计/名称对照不等于视觉准确率；是否该命名仍依赖人工真值。"}


def summarize(report_path, review_path):
    from evals.style_direct_gold import file_hash
    report_path = Path(report_path)
    report = json.loads(report_path.read_text(encoding="utf8"))
    review = HumanReview.model_validate_json(Path(review_path).read_text(encoding="utf8"))
    if file_hash(report_path) != review.report_sha256:
        raise ValueError("报告已变化，人工评分SHA-256不匹配")
    expected = {item["id"] for item in report["samples"]}
    actual = [item.id for item in review.samples]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("人工评分必须完整且每个样本只出现一次")
    signed = all(value.strip() for value in (review.reviewer, review.reviewed_at, review.annotation_signature))
    complete = signed and all(getattr(item, key) is not None for item in review.samples for key in RATINGS)
    # A mock result must never become a real acceptance score via a signed form.
    original_live = all(item["prediction"].get("probe_mode") == "live" or
                        (item["prediction"].get("replay_source") == "database" and
                         item["prediction"].get("profile", {}).get("provenance", {}).get("model") == "qwen3.8-flash")
                        for item in report["samples"])
    eligible = complete and report.get("mode") in ("live", "replay") and original_live
    metrics = None
    if eligible:
        metrics = {key + "_rate": sum(bool(getattr(item, key)) for item in review.samples) / len(review.samples)
                   for key in RATINGS}
    return {"schema_version": "style-direct-review-summary.v1", "mode": report.get("mode"),
            "sample_count": len(review.samples), "human_review_complete": bool(complete),
            "quality_scores": metrics, "eligible_for_real_quality": bool(eligible),
            "independent_acceptance": bool(eligible and report.get("split") == "acceptance" and
                                           report.get("independence", {}).get("verified")),
            "reviewer": review.reviewer, "reviewed_at": review.reviewed_at,
            "report_sha256": review.report_sha256, "technical": technical_summary(report["samples"]),
            "failures": [{"id": item.id, "issues": [key for key in RATINGS if
                          getattr(item, key) is (True if key in ("content_leakage", "unsupported_expansion") else False)],
                          "comments": item.comments} for item in review.samples if any(
                          getattr(item, key) is (True if key in ("content_leakage", "unsupported_expansion") else False)
                          for key in RATINGS)],
            "note": "空白或Mock不输出真实质量分数。独立验收需与开发集分离；签名是人工确认记录，不是密码学认证。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="汇总带报告哈希的人工评分，不调用模型")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = summarize(args.report, args.review)
    if args.output:
        if args.output.exists():
            parser.error("汇总已存在，请使用新路径")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
