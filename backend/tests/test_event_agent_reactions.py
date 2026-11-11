"""Complete event feedback and trusted boundaries, using offline provider replies."""
import copy
import json

import pytest

from app.event_agent import EventAgentConfig
from test_event_agent import Provider, event, final, frame, ref, run, source


def delivery_sources():
    actions = ["男孩向女子递出水杯", "女子接稳水杯", "男孩微笑", "女子举杯",
               "男孩露出惊讶表情", "女子喝水", "男孩等待", "女子放下水杯"]
    shots = [source(index, (index - 1) * 1.5, 1.5, action=action)
             for index, action in enumerate(actions, 1)]
    return shots, [frame(shot) for shot in shots]


def with_reactions(shots, frames, disposition="integrate"):
    proposal = event(shots, frames, duration=8.0)
    for index in (2, 4, 6):
        proposal["causal_facts"][index]["kind"] = "reaction"
        proposal["presentation"][index]["disposition"] = disposition
    return proposal


@pytest.mark.parametrize("disposition", ["integrate", "preserve"])
def test_eight_source_delivery_includes_feedback_in_one_event_without_forced_split(disposition):
    shots, frames = delivery_sources()
    before = copy.deepcopy([shot.model_dump() for shot in shots])
    proposal = with_reactions(shots, frames, disposition)
    protected = {shots[index].id for index in (2, 4, 6)} if disposition == "preserve" else set()
    provider = Provider([final(proposal)])
    result = run(shots, frames, provider, protected_source_shot_ids=protected)
    assert result.status == "ready"
    assert len(result.events) == len(result.generation_clips) == 1
    saved = result.events[0]
    assert saved.source_shot_ids == [shot.id for shot in shots]
    assert [item.source_shot_id for item in result.source_coverage] == [shot.id for shot in shots]
    assert len(provider.calls) == 1
    assert [shot.model_dump() for shot in shots] == before
    text = result.generation_clips[0].seedance_prompt.positive_prompt
    assert all(shot.action in text for shot in shots)
    presented = {fact_id for stage in saved.stages for fact_id in stage.fact_ids}
    for index in (2, 4, 6):
        fact = saved.causal_facts[index]
        assert fact.kind == "reaction" and fact.critical and fact.id in presented
        assert saved.presentation[index].disposition == disposition
        assert fact.evidence[0].frame_id == frames[index].id
        assert fact.evidence[0].timestamp == frames[index].timestamp
    rules, payload = provider.calls[0][0].split("\n输入：\n", 1)
    assert "递水→接杯→喝水" in rules
    assert "preserve 只是保留画面内容" in rules
    assert "可 integrate，不自动建立独立 event" in rules
    assert all(item["status"] == "ready" for item in json.loads(payload)["source_observations"])


@pytest.mark.parametrize("mutation", ["noncritical", "omit", "not-presented"])
def test_reaction_feedback_cannot_be_downgraded_omitted_or_removed_from_stages(mutation):
    shots, frames = delivery_sources()
    proposal = with_reactions(shots, frames)
    if mutation == "noncritical":
        proposal["causal_facts"][2]["critical"] = False
    elif mutation == "omit":
        proposal["presentation"][2]["disposition"] = "omit"
    else:
        proposal["stages"][0]["fact_ids"].remove("fact-3")
    provider = Provider([final(proposal), final(proposal)])
    result = run(shots, frames, provider)
    if mutation == "noncritical":
        assert result.status == "ready" and len(provider.calls) == 1
        assert result.events[0].causal_facts[2].critical
        assert result.events[0].stages[0].fact_ids == proposal["stages"][0]["fact_ids"]
        assert result.raw_responses[0]["local_repairs"][0]["field"] == "critical"
        return
    assert result.status == "degraded"
    errors = " ".join(result.audit[0].validation_errors)
    assert "Observed reactions" in errors or "Every critical causal fact" in errors
    assert "男孩微笑" in "\n".join(clip.seedance_prompt.positive_prompt for clip in result.generation_clips)
    assert [item.source_shot_id for item in result.source_coverage] == [shot.id for shot in shots]
    assert len(provider.calls) == 2


@pytest.mark.parametrize("disposition", ["integrate", "preserve"])
def test_cross_window_reaction_continues_same_event_without_automatic_cut(disposition):
    shots = [source(1, 0, action="女子端起水杯"), source(2, 2, action="杯中露出虫子"),
             source(3, 4, action="男孩惊讶"), source(4, 6, action="女子放下杯子")]
    frames = [frame(shot, offset=1.1) for shot in shots]
    initial_stages = [
        {"id": "lift", "source_shot_ids": [shots[0].id], "fact_ids": ["fact-1"], "duration_seconds": 2.0, "transition": "initial"},
        {"id": "reveal", "source_shot_ids": [shots[1].id], "fact_ids": ["fact-2"], "duration_seconds": 2.0, "transition": "cut"},
    ]
    first = event(shots[:2], frames[:2], mode="internal_cuts", stages=initial_stages,
                  dispositions={shots[1].id: "preserve"})
    first["causal_facts"][1]["kind"] = "reveal"
    second = event(shots[2:], frames[2:], duration=4.0, dispositions={shots[2].id: disposition})
    second["causal_facts"][0]["kind"] = "reaction"
    second["continuation_of_anchor_shot_id"] = shots[0].id
    second["continuation_evidence"] = [ref(frames[1], shots[1].action), ref(frames[2], shots[2].action)]
    protected = {shots[2].id} if disposition == "preserve" else set()
    provider = Provider([final(first), final(second)])
    result = run(shots, frames, provider, config=EventAgentConfig(window_seconds=4.0, overlap_seconds=1.0),
                 protected_source_shot_ids=protected)
    assert result.status == "ready" and len(result.events) == 1
    saved = result.events[0]
    assert saved.generation_mode == "internal_cuts"
    assert [stage.transition for stage in saved.stages] == ["initial", "cut", "continuation"]
    assert saved.presentation[2].disposition == disposition
    assert "男孩惊讶" in result.generation_clips[0].seedance_prompt.positive_prompt
    assert len(saved.causal_facts) == 4 and len(result.source_coverage) == 4


def test_actual_scene_ids_block_a_merged_event_even_when_scene_descriptions_match():
    shots = [source(1, 0), source(2, 2)]
    shots[1].scene_group_id = "scene-2"
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames)
    result = run(shots, frames, Provider([final(proposal), final(proposal)]))
    assert result.status == "degraded"
    assert "actual scene-group boundaries" in " ".join(result.audit[0].validation_errors)
    assert [item.source_shot_ids for item in result.events] == [[shot.id] for shot in shots]


@pytest.mark.parametrize("scene_ids,reason", [
    (["scene-1", "scene-1", "scene-1"], ""),
    (["legacy-1", "legacy-2", "legacy-3"], ""),
    (["presentation-1", "presentation-2", "presentation-3"], ""),
    (["scene-1", "scene-2", "scene-3"], "legacy-presentation"),
])
def test_view_descriptions_and_legacy_presentation_groups_do_not_force_event_boundaries(scene_ids, reason):
    shots = [source(index, (index - 1) * 2) for index in range(1, 4)]
    for shot, scene_id, description in zip(shots, scene_ids, ["家里客厅", "开放厨房", "手部特写未知"]):
        shot.scene_group_id, shot.scene_reason, shot.visual_facts.scene = scene_id, reason, description
    frames = [frame(shot) for shot in shots]
    result = run(shots, frames, Provider([final(event(shots, frames))]))
    assert result.status == "ready" and len(result.events) == 1


def test_new_independent_goal_stays_separate_without_keyword_based_forced_merging():
    shots = [source(1, 0, action="男孩向女子递出杯子"), source(2, 2, action="女子接住杯子"),
             source(3, 4, action="女子起身打开窗户")]
    frames = [frame(shot) for shot in shots]
    first = event(shots[:2], frames[:2], duration=4.0)
    first["goal"] = "完成递杯"
    second = event(shots[2:], frames[2:], duration=4.0)
    second["goal"] = "打开窗户"
    result = run(shots, frames, Provider([final(first, second)]))
    assert result.status == "ready" and len(result.events) == 2
    assert [saved.goal for saved in result.events] == ["完成递杯", "打开窗户"]
    assert [saved.source_shot_ids for saved in result.events] == [[shots[0].id, shots[1].id], [shots[2].id]]


def test_trusted_time_or_scene_boundary_is_visible_to_model_and_blocks_same_window_merge():
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames)
    provider = Provider([final(proposal), final(proposal)])
    result = run(shots, frames, provider, protected_boundaries={(shots[0].id, shots[1].id)})
    assert result.status == "degraded"
    assert "trusted scene-change or time-elision boundary" in " ".join(result.audit[0].validation_errors)
    payload = json.loads(provider.calls[0][0].split("\n输入：\n", 1)[1])
    assert payload["trusted_protected_boundaries"] == [{"left_shot_id": shots[0].id, "right_shot_id": shots[1].id}]
    assert len(result.source_coverage) == 2


def test_valid_events_on_either_side_of_a_trusted_boundary_are_preserved():
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    reply = final(event(shots[:1], frames[:1], duration=4.0), event(shots[1:], frames[1:], duration=4.0))
    result = run(shots, frames, Provider([reply]), protected_boundaries={(shots[0].id, shots[1].id)})
    assert result.status == "ready" and len(result.events) == 2


def test_trusted_boundary_blocks_cross_window_continuation_without_discarding_valid_new_event():
    shots = [source(index, (index - 1) * 2) for index in range(1, 5)]
    frames = [frame(shot) for shot in shots]
    first, second = event(shots[:2], frames[:2], duration=4.0), event(shots[2:], frames[2:], duration=4.0)
    second["continuation_of_anchor_shot_id"] = shots[0].id
    second["continuation_evidence"] = [ref(frames[1], shots[1].action), ref(frames[2], shots[2].action)]
    provider = Provider([final(first), final(second)])
    result = run(shots, frames, provider, config=EventAgentConfig(window_seconds=4.0, overlap_seconds=1.0),
                 protected_boundaries={(shots[1].id, shots[2].id)})
    assert len(result.events) == 2 and all(saved.status == "ready" for saved in result.events)
    assert "已核实场景变化或时间省略" in " ".join(result.warnings)
    assert len(provider.calls) == 2 and len(result.source_coverage) == 4


def test_foreign_reversed_and_nonadjacent_boundary_pairs_cannot_split_the_event():
    shots = [source(index, (index - 1) * 2) for index in range(1, 4)]
    frames = [frame(shot) for shot in shots]
    provider = Provider([final(event(shots, frames))])
    result = run(shots, frames, provider, protected_boundaries={
        ("foreign", shots[0].id), (shots[1].id, shots[0].id), (shots[0].id, shots[2].id)})
    assert result.status == "ready" and len(result.events) == 1
    payload = json.loads(provider.calls[0][0].split("\n输入：\n", 1)[1])
    assert payload["trusted_protected_boundaries"] == []


def test_failed_source_cannot_be_hidden_inside_a_successful_merged_event():
    shots = [source(index, (index - 1) * 2) for index in range(1, 4)]
    shots[1].status = "failed"
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames)
    provider = Provider([final(proposal), final(proposal)])
    result = run(shots, frames, provider)
    assert result.status == "degraded"
    assert "context is read-only" in " ".join(result.audit[0].validation_errors)
    assert [saved.source_shot_ids for saved in result.events] == [[shot.id] for shot in shots]
    payload = json.loads(provider.calls[0][0].split("\n输入：\n", 1)[1])
    assert payload["source_observations"][1]["status"] == "failed"
    assert payload["owned_source_shot_ids"] == [shots[0].id, shots[2].id]
    assert payload["system_fallback_source_shot_ids"] == [shots[1].id]


def test_failed_single_source_is_fallback_and_does_not_downgrade_other_valid_events():
    shots = [source(index, (index - 1) * 2) for index in range(1, 4)]
    shots[1].status = "failed"
    frames = [frame(shot) for shot in shots]
    responses = [event([shot], [artifact], duration=2.0) for shot, artifact in zip(shots, frames) if shot.status != "failed"]
    result = run(shots, frames, Provider([final(*responses)]))
    assert result.status == "degraded" and result.audit[0].status == "fallback"
    assert [saved.status for saved in result.events] == ["ready", "fallback", "ready"]
    assert result.events[1].source_shot_ids == [shots[1].id]
    assert result.events[1].causal_facts[0].evidence[0].frame_id == frames[1].id
    assert [item.source_shot_id for item in result.source_coverage] == [shot.id for shot in shots]
    assert "分析失败" in " ".join(result.warnings)
