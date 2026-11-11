"""Offline event-first checks. This command never calls a model or edits a job."""
from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path


def event_agent_metrics(result: dict, gold: dict | None = None) -> dict:
    if result.get("schema_version") != "event-agent.v1":
        raise ValueError("Expected an event-agent.v1 result")
    events = result.get("events", [])
    sources = [sid for event in events for sid in event["source_shot_ids"]]
    ownership = [item["source_shot_id"] for item in result.get("source_coverage", [])]
    clips = result.get("generation_clips", [])
    clip_sources = [sid for clip in clips for sid in clip["source_shot_ids"]]
    windows = result.get("audit", [])
    metrics = {
        "source_count": len(sources), "event_count": len(events), "clip_count": len(clips),
        "source_ownership_unique": len(sources) == len(set(sources)) and sources == ownership,
        "clip_ownership_complete": sources == clip_sources,
        "model_attempts": sum(w.get("usage", {}).get("model_calls", 0) for w in windows),
        "tool_attempts": sum(w.get("usage", {}).get("tool_calls", 0) for w in windows),
        "extra_frames": sum(w.get("usage", {}).get("extra_frames", 0) for w in windows),
        "window_budget_violations": sum(
            w.get("usage", {}).get("model_calls", 0) > 2 or
            w.get("usage", {}).get("tool_calls", 0) > 2 or
            w.get("usage", {}).get("extra_frames", 0) > 6 for w in windows),
        "fallback_windows": sum(w.get("status") == "fallback" for w in windows),
        "supplemental_window_ratio": sum(w.get("usage", {}).get("model_calls", 0) > 1 for w in windows) / len(windows) if windows else None,
        "generation_duration_valid": all(0 < c["generation_duration_seconds"] <= 12 for c in clips),
        "human_review_required": ["因果事实准确性", "幻觉", "一镜到底可执行性", "状态与动作顺序", "事件边界合理性"],
        "note": "来源切点F1不是创作改编指标；镜头数量减少不等于质量提高。",
    }
    if gold is not None:
        expected = gold["source_shot_ids"]
        groups = gold["event_groups"]
        flattened = [sid for group in groups for sid in group]
        if flattened != expected or len(flattened) != len(set(flattened)):
            raise ValueError("Manual gold groups must cover each source exactly once in order")
        metrics["expected_source_coverage"] = sources == expected
        predicted_pairs = {tuple(pair) for event in events for pair in combinations(event["source_shot_ids"], 2)}
        gold_pairs = {tuple(pair) for group in groups for pair in combinations(group, 2)}
        matched = len(predicted_pairs & gold_pairs)
        precision = matched / len(predicted_pairs) if predicted_pairs else (1.0 if not gold_pairs else 0.0)
        recall = matched / len(gold_pairs) if gold_pairs else (1.0 if not predicted_pairs else 0.0)
        metrics.update(event_pairwise_precision=precision, event_pairwise_recall=recall,
                       event_pairwise_f1=2 * precision * recall / (precision + recall) if precision + recall else 0.0)
        presented = "\n".join(stage["action"] for clip in clips for stage in clip["timeline"])
        phrases = gold.get("required_fact_phrases", [])
        metrics["required_phrase_coverage_proxy"] = sum(phrase in presented for phrase in phrases) / len(phrases) if phrases else None
        metrics["missing_required_phrases"] = [phrase for phrase in phrases if phrase not in presented]
        metrics["phrase_metric_note"] = "只作人工标注短语覆盖的辅助检查，不等同于语义事实准确率。"
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="Saved experimental result JSON")
    parser.add_argument("--gold", type=Path, help="Human-authored source_shot_ids/event_groups/required_fact_phrases")
    args = parser.parse_args()
    result = json.loads(args.result.read_text(encoding="utf-8"))
    gold = json.loads(args.gold.read_text(encoding="utf-8")) if args.gold else None
    print(json.dumps(event_agent_metrics(result, gold), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
