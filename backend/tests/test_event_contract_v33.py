"""Protocol, interval ownership and half-second delivery regressions."""
import copy
import json

import pytest

from app.event_agent import EventAgentConfig, EventProposal, build_event_windows, validate_event_proposals
from app.event_agent.models import EventAgentResult
from app.event_agent.observations import saved_observations
from app.event_agent.projection import project_event_agent_result
from app.event_agent.prompts import source_observation
from app.event_agent.engine import _normalize_protocol
from app.perception_models import EvidenceRef, MotionObservationV2, StaticObservationV2, empty_stage, merge_stages
from test_event_agent import Provider, event, final, frame, ref, run, source


def observed(shot, artifact):
    static = empty_stage(StaticObservationV2, "unknown")
    motion = empty_stage(MotionObservationV2, "unknown")
    def set_field(group, name, value):
        old = getattr(group, name)
        setattr(group, name, type(old).model_validate({"value": value, "observable": True,
            "confidence": 0.9, "evidence": [ref(artifact, "已保存证据")], "uncertainties": []}))
    set_field(static.facts, "primary_subject", "女子")
    set_field(static.facts, "scene", "厨房")
    set_field(static.facts, "relationships", "人物与咖啡机进行交互")
    set_field(motion.dynamics, "subject_action", shot.action)
    shot.visual_observation_v2 = merge_stages(static, motion)
    return shot


def test_shared_catalog_accepts_saved_relationship_and_exposes_stable_ids():
    shot = source(1, 0, 5.483333, action="女子操作咖啡机")
    artifact = frame(shot)
    observed(shot, artifact)
    proposal = event([shot], [artifact], duration=shot.duration)
    proposal["causal_facts"].append({"id": "relationship", "text": "人物与咖啡机进行交互",
        "source_shot_ids": [shot.id], "kind": "context", "critical": False,
        "evidence": [ref(artifact, "关系") ]})
    provider = Provider([final(proposal)])
    result = run([shot], [artifact], provider)
    assert len(provider.calls) == 1
    assert not result.audit[0].validation_errors
    assert len(result.events) == len(result.generation_clips) == 1
    assert result.generation_clips[0].generation_duration_seconds == 5.5
    catalogue = source_observation(shot, {artifact.id})["saved_literal_observations"]
    row = next(row for row in catalogue if row["field_path"] == "facts.relationships")
    assert row["observation_type"] == "static" and row["observation_id"].startswith("obs-")
    assert row == next(row for row in saved_observations(shot) if row["observation_id"] == row["observation_id"] and row["field_path"] == "facts.relationships")


def test_first_source_scene_metadata_has_no_review_warning():
    shot = source(1, 0)
    artifact = frame(shot)
    proposal = event([shot], [artifact])
    proposal["scene_continuity"] = "same_scene"
    proposal["scene_evidence"] = [ref(artifact, "同场景")]
    result = run([shot], [artifact], Provider([final(proposal)]))
    assert result.status == "ready"
    assert result.events[0].scene_continuity == "unknown"
    assert not result.audit[0].warnings


def test_long_raw_source_uses_bounded_spans_without_new_source_cards():
    shot = source(1, 0, 30.0)
    frames = [frame(shot, str(index), float(index) + 0.1) for index in range(30)]
    before = copy.deepcopy(shot.model_dump())
    provider = Provider([])  # no interval-verified action => honest static fallback
    result = run([shot], frames, provider)
    assert shot.model_dump() == before
    assert len(result.windows) == 3
    assert all(window.end_time - window.start_time <= 14 for window in result.windows)
    assert [owner.source_shot_id for owner in result.source_coverage] == [shot.id]
    assert [event.source_shot_ids for event in result.events] == [[shot.id]]
    assert [clip.generation_duration_seconds for clip in result.generation_clips] == [12.0, 12.0, 6.0]
    assert [(span["start_time"], span["end_time"]) for clip in result.generation_clips
            for span in clip.delivery["source_spans"]] == [(0.0, 12.0), (12.0, 24.0), (24.0, 30.0)]
    payload = project_event_agent_result(EventAgentResult.model_validate(result.model_dump()), [shot], frames)
    assert len(payload["narrative_events"]) == 1
    assert [row["source_shot_ids"] for row in payload["generation_sequences"]] == [[shot.id]] * 3
    assert [row["source_start_time"] for row in payload["generation_sequences"]] == [0, 12, 24]
    broken = result.model_copy(deep=True)
    broken.generation_clips[1].delivery["source_spans"][0]["start_time"] = 11.0
    with pytest.raises(ValueError, match="来源区间"):
        project_event_agent_result(broken, [shot], frames)


def test_three_semantic_phases_round_total_once():
    shots = [source(index, (index - 1) * 1.21, 1.21) for index in range(1, 4)]
    frames = [frame(shot) for shot in shots]
    stages = [{"id": str(index), "source_shot_ids": [shot.id], "fact_ids": [f"fact-{index}"],
               "duration_seconds": 1.21, "transition": "initial" if index == 1 else "continuation"}
              for index, shot in enumerate(shots, 1)]
    result = run(shots, frames, Provider([final(event(shots, frames, stages=stages))]))
    clip = result.generation_clips[0]
    assert clip.generation_duration_seconds == 4.0
    assert all(stage.generation_start_seconds * 2 == int(stage.generation_start_seconds * 2)
               and stage.generation_end_seconds * 2 == int(stage.generation_end_seconds * 2) for stage in clip.timeline)
    assert shots[-1].end_time == pytest.approx(3.63)


def test_unpresented_optional_bad_context_does_not_destroy_valid_action():
    shot = source(1, 0)
    artifact = frame(shot)
    proposal = event([shot], [artifact])
    proposal['causal_facts'].append({'id': 'optional-context', 'text': '背景是火星基地',
        'source_shot_ids': [shot.id], 'kind': 'context', 'critical': False,
        'evidence': [ref(artifact, '未经保存的猜测')]})
    provider = Provider([final(proposal)])
    result = run([shot], [artifact], provider)
    assert len(provider.calls) == 1 and result.status == 'ready'
    assert all(fact.id != 'optional-context' for fact in result.events[0].causal_facts)
    assert any(repair['field'] == 'unused_context_fact' for repair in result.raw_responses[0]['local_repairs'])


def test_catalog_does_not_offer_unshown_atom_for_copying():
    shot = source(1, 0)
    saved, shown = frame(shot), frame(shot, 'different', 1.0)
    observed(shot, saved)
    assert source_observation(shot, {shown.id})['saved_literal_observations'] == []
    assert saved_observations(shot)  # persisted observations remain untouched


def test_new_default_preserves_source_time_instead_of_models_eight_second_budget():
    shot = source(1, 0, 3.23)
    artifact = frame(shot)
    result = run([shot], [artifact], Provider([final(event([shot], [artifact], duration=8.0))]),
                 config=EventAgentConfig())
    assert result.events[0].timing_policy == "source-faithful"
    assert result.generation_clips[0].generation_duration_seconds == 3.5
    assert "时长 3.5 秒" in result.generation_clips[0].seedance_prompt.positive_prompt
    assert shot.duration == 3.23


def test_long_raw_source_consumes_independent_span_observations_and_keeps_pending_blank():
    from app.event_agent.spans import analysis_views
    shot = source(1, 0, 30.0)
    frames = [frame(shot, f'interval-{index}', timestamp) for index, timestamp in enumerate((1.0, 13.0, 25.0))]
    records = []
    for index, (start, end) in enumerate(((0.0, 12.0), (12.0, 24.0), (24.0, 30.0))):
        temporary = shot.model_copy(deep=True, update={'action': f'女子完成第{index + 1}阶段动作'})
        observed(temporary, frames[index])
        records.append({'source_shot_id': shot.id, 'start_time': start, 'end_time': end,
                        'status': 'ready' if index < 2 else 'pending_review',
                        'observation': temporary.visual_observation_v2.model_dump() if index < 2 else None,
                        'frame_ids': [frames[index].id]})
    shot = shot.model_copy(update={'analysis_span_observations': records})
    views, mapped, _ = analysis_views([shot], frames, EventAgentConfig())
    assert [view.action for view in views] == ['女子完成第1阶段动作', '女子完成第2阶段动作', '未知']
    assert not views[-1].visual_observation_v2.dynamics.subject_action.observable
    assert [view.frame_ids for view in views] == [[artifact.id] for artifact in frames]
    assert all(artifact.shot_id == views[index].id for index, artifact in enumerate(mapped))
