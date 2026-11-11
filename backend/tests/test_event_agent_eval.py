import pytest

from evals.event_agent import event_agent_metrics


def result():
    return {"schema_version": "event-agent.v1", "events": [{"source_shot_ids": ["a", "b", "c"]}],
            "source_coverage": [{"source_shot_id": sid, "event_id": "e"} for sid in ["a", "b", "c"]],
            "generation_clips": [{"source_shot_ids": ["a", "b", "c"], "generation_duration_seconds": 8,
                                  "timeline": [{"action": "男孩递杯，女子饮用"}]}],
            "audit": [{"status": "ready", "usage": {"model_calls": 2, "tool_calls": 1, "extra_frames": 2}}]}


def test_omitted_insert_is_not_penalized_as_missing_physical_cut():
    metrics = event_agent_metrics(result(), {"source_shot_ids": ["a", "b", "c"],
                                           "event_groups": [["a", "b", "c"]],
                                           "required_fact_phrases": ["男孩递杯", "女子饮用"]})
    assert metrics["event_pairwise_f1"] == 1
    assert metrics["source_ownership_unique"] and metrics["clip_ownership_complete"]
    assert metrics["required_phrase_coverage_proxy"] == 1
    assert "physical_cut_f1" not in metrics


def test_incorrect_merge_and_budget_violation_reported():
    data = result()
    data["audit"][0]["usage"]["model_calls"] = 3
    metrics = event_agent_metrics(data, {"source_shot_ids": ["a", "b", "c"], "event_groups": [["a", "b"], ["c"]]})
    assert metrics["event_pairwise_f1"] < 1
    assert metrics["window_budget_violations"] == 1


def test_gold_cannot_be_generated_with_duplicate_source_ownership():
    with pytest.raises(ValueError, match="Manual gold"):
        event_agent_metrics(result(), {"source_shot_ids": ["a", "b", "c"], "event_groups": [["a", "b"], ["b", "c"]]})
