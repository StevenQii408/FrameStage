"""Pure projection and temporary-database tests; no paid provider or media I/O."""
import copy
import json

import pytest
from pydantic import ValidationError

from app.db import Database
from app.event_agent.projection import VERSION, project_event_agent_result
from app.event_agent.models import EventAgentConfig, EventAgentResult, NarrativeEvent, SourceOwnership
from app.event_agent.rendering import VERSION as RENDER_VERSION, render_event_clips
from app.schemas import GenerationSequence, JobConfig, PromptBundle, ShotContract
from app.services.event_sequences import assert_sequence_prompts_unedited, store_sequence_payload
from test_event_agent import Provider, event, final, frame, run, source


def fixture_result(count=3, *, mode="continuous", stages=None, dispositions=None, duration=8.0):
    shots = [source(index, (index - 1) * 1.0, 1.0) for index in range(1, count + 1)]
    for index, shot in enumerate(shots):
        shot.source_raw_shot_ids = [f"source-raw-{index:03d}"]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames, mode=mode, stages=stages, duration=duration, dispositions=dispositions)
    provider = Provider([final(proposal)])
    result = run(shots, frames, provider, style_text="来源已确认的水彩风格")
    assert result.status == "ready"
    return result, shots, frames, provider


def test_projection_keeps_original_event_clip_prompt_ids_and_text_without_legacy_classification(monkeypatch):
    result, shots, frames, provider = fixture_result()
    before = copy.deepcopy(result.model_dump(mode="json"))
    source_before = [shot.model_dump() for shot in shots]
    def forbidden(*args, **kwargs):
        pytest.fail("projection must not rerun legacy grouping or prompt rendering")
    monkeypatch.setattr("app.services.event_sequences.derive_sequences", forbidden)
    monkeypatch.setattr("app.services.event_sequences._render", forbidden)
    payload = project_event_agent_result(result, shots, frames)
    event_model, clip = result.events[0], result.generation_clips[0]
    projected = payload["generation_sequences"][0]
    assert payload["narrative_events"][0]["id"] == event_model.id
    assert payload["narrative_events"][0]["goal"] == event_model.goal
    assert projected["id"] == clip.id and projected["event_id"] == clip.event_id
    assert projected["build_hash"] == projected["version_hash"] == clip.version_hash
    for key in ("first_frame_prompt", "seedance_prompt"):
        original = getattr(clip, key).model_dump(mode="json")
        assert {k: projected[key][k] for k in original} == original
        assert projected[key]["contract"]["delivery"]["agent_event"] == event_model.model_dump(mode="json")
        assert projected[key]["contract"]["delivery"]["agent_clip"] == clip.model_dump(mode="json")
        assert projected[key]["contract"]["delivery"]["render_version"] == RENDER_VERSION
    assert len(provider.calls) == 1
    assert payload["sequence_metadata"]["extra_model_calls"] == 0
    assert payload["sequence_metadata"]["version"] == VERSION
    assert payload["sequence_metadata"]["render_version"] == "event-agent-render.v5"
    assert payload["sequence_metadata"]["agent_run_id"] == result.run_id
    assert result.model_dump(mode="json") == before
    assert [shot.model_dump() for shot in shots] == source_before


def test_multi_source_stage_remains_one_stage_and_physical_source_cuts_do_not_become_output_cuts():
    result, shots, frames, _ = fixture_result(6)
    sequence = project_event_agent_result(result, shots, frames)["generation_sequences"][0]
    assert sequence["source_shot_ids"] == [shot.id for shot in shots]
    assert sequence["source_raw_shot_ids"] == [rid for shot in shots for rid in shot.source_raw_shot_ids]
    assert len(sequence["timeline"]) == 1
    stage = sequence["timeline"][0]
    original = result.generation_clips[0].timeline[0]
    assert stage["id"] == original.id
    assert stage["source_shot_id"] == shots[0].id
    assert stage["source_shot_ids"] == [shot.id for shot in shots]
    assert stage["fact_ids"] == original.fact_ids
    assert (stage["generation_start_seconds"], stage["generation_end_seconds"]) == (0.0, 8.0)
    assert stage["transition"] == "initial" and stage["generation_edit_times"] == []
    assert stage["source_edit_times"] == [float(index) for index in range(6)]
    assert stage["photography"] == original.photography.model_dump(mode="json")
    assert stage["shot_size"] == original.photography.shot_size
    assert "切换画面" not in sequence["seedance_prompt"]["positive_prompt"]
    assert GenerationSequence.model_validate(sequence).generation_duration_seconds == 8.0


def test_stage_camera_and_explicit_cut_are_retained_with_their_agent_timing():
    stages = [{"id": f"stage-{index}", "source_shot_ids": [f"shot-{index}"], "fact_ids": [f"fact-{index}"],
        "duration_seconds": duration, "transition": "initial" if index == 1 else "cut" if index == 2 else "continuation",
        "photography": {"shot_size": "特写" if index == 2 else "中景", "lens": "长焦" if index == 2 else "标准",
            "camera_movement": "固定", "composition": f"第{index}段可见杯子", "intent": "生成摄影编排"}}
        for index, duration in enumerate((1.5, 2.5, 4.0), 1)]
    result, shots, frames, _ = fixture_result(mode="internal_cuts", stages=stages)
    sequence = project_event_agent_result(result, shots, frames)["generation_sequences"][0]
    assert sequence["generation_mode"] == "internal_cuts"
    assert [stage["transition"] for stage in sequence["timeline"]] == ["initial", "cut", "continuation"]
    assert [(stage["generation_start_seconds"], stage["generation_end_seconds"]) for stage in sequence["timeline"]] == [(0.0, 1.5), (1.5, 4.0), (4.0, 8.0)]
    assert sequence["timeline"][1]["shot_size"] == "特写" and sequence["timeline"][1]["lens"] == "长焦"
    assert sequence["timeline"][1]["generation_edit_times"] == [1.5]
    assert sequence["timeline"][1]["source_start_time"] == 1.0
    assert sequence["seedance_prompt"]["positive_prompt"] == result.generation_clips[0].seedance_prompt.positive_prompt
    assert sequence["causal_facts"] == [fact.model_dump(mode="json") for fact in result.events[0].causal_facts]
    assert sequence["presentation"] == [decision.model_dump(mode="json") for decision in result.generation_clips[0].presentation]


def test_omitted_incidental_source_stays_owned_without_inventing_a_generation_stage():
    shots = [source(index, index - 1, 1.0) for index in range(1, 4)]
    frames = [frame(shot) for shot in shots]
    stages = [{"id": "begin", "source_shot_ids": [shots[0].id], "fact_ids": ["fact-1"], "duration_seconds": 3.0, "transition": "initial"},
        {"id": "finish", "source_shot_ids": [shots[2].id], "fact_ids": ["fact-3"], "duration_seconds": 4.0, "transition": "continuation"}]
    proposal = event(shots, frames, stages=stages, dispositions={shots[1].id: "omit"})
    proposal["causal_facts"][1]["critical"] = False
    result = run(shots, frames, Provider([final(proposal)]))
    assert result.status == "ready"
    sequence = project_event_agent_result(result, shots, frames)["generation_sequences"][0]
    assert sequence["source_shot_ids"] == [shot.id for shot in shots]
    assert [sid for stage in sequence["timeline"] for sid in stage["source_shot_ids"]] == [shots[0].id, shots[2].id]
    assert sequence["presentation"][1]["disposition"] == "omit"
    assert sequence["source_duration_seconds"] == 3.0
    assert shots[1].action not in sequence["seedance_prompt"]["positive_prompt"]
    assert len(sequence["seedance_prompt"]["contract"]["delivery"]["source_trace"]) == 3


def test_projection_supports_twenty_agent_stages_and_independent_twelve_second_contract():
    stages = [{"id": f"stage-{index}", "source_shot_ids": [f"shot-{index}"], "fact_ids": [f"fact-{index}"],
        "duration_seconds": 0.5, "transition": "initial" if index == 1 else "continuation"} for index in range(1, 21)]
    shots = [source(index, (index - 1) * 0.2, 0.2) for index in range(1, 21)]
    frames = [frame(shot, offset=0.05) for shot in shots]
    result = run(shots, frames, Provider([final(event(shots, frames, stages=stages))]),
        config=EventAgentConfig(timing_policy="authored", max_initial_frames_per_window=24))
    assert result.status == "ready"
    sequence = project_event_agent_result(result, shots, frames)["generation_sequences"][0]
    assert len(sequence["timeline"]) == 20
    assert sequence["generation_duration_seconds"] == 10.0
    assert len(sequence["first_frame_prompt"]["contract"]["source_shot_ids"]) == 20
    assert ShotContract(generation_duration_seconds=10.0).generation_duration_seconds == 10.0


def test_projection_accepts_one_hundred_twenty_whole_sources_without_a_four_source_split():
    shots = [source(index, (index - 1) * 0.05, 0.05) for index in range(1, 121)]
    frames = [frame(shot, offset=0.01) for shot in shots]
    saved = NarrativeEvent(id="event-120", anchor_shot_id=shots[0].id,
        initial_state=shots[0].action_details.initial_state, final_state=shots[-1].action_details.final_state,
        window_id="event-window-120", version_hash="event-hash-120", **event(shots, frames, duration=12.0))
    config = EventAgentConfig()
    clips = render_event_clips(saved, shots, config, frames=frames)
    result = EventAgentResult(job_id="job", run_id="agent-run-120", status="ready", config=config,
        windows=[], events=[saved], generation_clips=clips, source_coverage=[
            SourceOwnership(source_shot_id=shot.id, event_id=saved.id) for shot in shots], version_hash="result-120")
    payload = project_event_agent_result(result, shots, frames)
    assert len(payload["generation_sequences"]) == 1
    sequence = payload["generation_sequences"][0]
    assert len(sequence["source_shot_ids"]) == len(sequence["timeline"][0]["source_shot_ids"]) == 120
    assert sequence["generation_duration_seconds"] == 12.0
    assert sequence["first_frame_prompt"]["contract"]["source_shot_ids"] == [shot.id for shot in shots]


def test_source_aspect_ratio_and_initial_cover_are_projected_without_rewriting_agent_text():
    shots = [source(index, index - 1, 1.0) for index in range(1, 3)]
    contract = ShotContract(aspect_ratio="4:3", observed_style="已确认水彩", style_profile_hash="style-version", style_profile_version="style-profile.v3:4")
    prompt = PromptBundle(id="source-first", shot_id=shots[0].id, prompt_type="first_frame", platform="image",
        positive_prompt="已有首帧", negative_prompt="", contract=contract)
    shots[0].prompts = [prompt]
    frames = [frame(shot) for shot in shots]
    frames[0] = frames[0].model_copy(update={"width": 1920, "height": 1080})
    result = run(shots, frames, Provider([final(event(shots, frames))]))
    sequence = project_event_agent_result(result, shots, frames, [prompt])["generation_sequences"][0]
    delivered = sequence["first_frame_prompt"]["contract"]
    assert delivered["aspect_ratio"] == "4:3" and delivered["orientation"] == "横屏"
    assert delivered["style_profile_hash"] == "style-version"
    assert sequence["cover_frame_id"] == frames[0].id
    assert sequence["first_frame_prompt"]["positive_prompt"] == result.generation_clips[0].first_frame_prompt.positive_prompt
    assert delivered["delivery"]["video_spec"]["aspect_source"] == "source_contract"


def test_later_external_source_bundle_cannot_change_saved_rendered_settings():
    result, shots, frames, _ = fixture_result()
    clip = result.generation_clips[0]
    before = clip.model_dump(mode="json")
    external = PromptBundle(id="later-settings", shot_id=shots[0].id, prompt_type="first_frame", platform="image",
        positive_prompt="来源更新", negative_prompt="", contract=ShotContract(aspect_ratio="16:9"))
    projected = project_event_agent_result(result, shots, frames, [external])["generation_sequences"][0]
    contract = projected["seedance_prompt"]["contract"]
    assert contract["aspect_ratio"] == clip.delivery["video_spec"]["aspect_ratio"] == "9:16"
    assert "竖屏 9:16" in projected["seedance_prompt"]["positive_prompt"]
    assert contract["delivery"]["display_timing"] == clip.delivery["display_timing"]
    assert contract["observed_style"] == "来源已确认的水彩风格"
    assert result.generation_clips[0].model_dump(mode="json") == before


def test_legacy_committed_clip_projection_keeps_text_and_renderer_version():
    result, shots, frames, _ = fixture_result()
    clip = result.generation_clips[0]
    clip.delivery = {}
    clip.first_frame_prompt.positive_prompt = "主体：女子\n整体画面风格：旧水彩\n声音：旧交付说明\n收束：旧状态"
    clip.seedance_prompt.positive_prompt = "旧视频正文，原样保留。"
    projected = project_event_agent_result(result, shots, frames)["generation_sequences"][0]
    assert projected["first_frame_prompt"]["positive_prompt"] == clip.first_frame_prompt.positive_prompt
    assert projected["seedance_prompt"]["positive_prompt"] == clip.seedance_prompt.positive_prompt
    assert projected["first_frame_prompt"]["contract"]["observed_style"] == "旧水彩"
    assert projected["first_frame_prompt"]["contract"]["delivery"]["render_version"] == "event-agent-render.v3"


@pytest.mark.parametrize("kind", ["missing-event-source", "duplicate-clip", "bad-ownership", "stage-gap", "wrong-event", "true-scene-boundary"])
def test_projection_rejects_lossy_or_inconsistent_ownership_without_replanning(kind):
    result, shots, frames, _ = fixture_result()
    damaged = result.model_copy(deep=True)
    if kind == "missing-event-source":
        damaged.events[0].source_shot_ids = damaged.events[0].source_shot_ids[:-1]
    elif kind == "duplicate-clip":
        damaged.generation_clips.append(damaged.generation_clips[0].model_copy(deep=True))
    elif kind == "bad-ownership":
        damaged.source_coverage[0].event_id = "foreign-event"
    elif kind == "stage-gap":
        damaged.generation_clips[0].timeline[0].generation_start_seconds = 0.5
    elif kind == "wrong-event":
        damaged.generation_clips[0].event_id = "foreign-event"
    else:
        shots[1].scene_group_id = "real-scene-2"
    with pytest.raises(ValueError):
        project_event_agent_result(damaged, shots, frames)


def test_projection_is_json_compatible_and_preserves_exact_positive_negative_edit_baselines(tmp_path):
    result, shots, frames, _ = fixture_result()
    result.generation_clips[0].first_frame_prompt.negative_prompt = "首帧手动保留负面"
    result.generation_clips[0].seedance_prompt.negative_prompt = "视频手动保留负面"
    payload = project_event_agent_result(result.model_dump(mode="json"), [s.model_dump() for s in shots], [f.model_dump() for f in frames])
    assert_sequence_prompts_unedited(payload)
    db = Database(tmp_path / "projection-only.sqlite3")
    db.init()
    db.insert_job({"id": "job", "status": "completed", "stage": "completed", "progress": 100,
        "config": {}, "created_at": "now", "updated_at": "now"})
    with db.connect() as conn:
        persisted = store_sequence_payload(conn, "job", payload, expected_version=0)
    stored = json.loads(db.fetchone("SELECT event_sequence_json FROM jobs WHERE id='job'")[0])
    assert stored == persisted
    assert stored["sequence_metadata"]["render_version"] == RENDER_VERSION
    assert stored["sequence_metadata"]["mode"] == "persisted"
    assert_sequence_prompts_unedited(stored)
    edited = copy.deepcopy(stored)
    edited["generation_sequences"][0]["first_frame_prompt"]["negative_prompt"] = "发生编辑"
    with pytest.raises(ValueError, match="负面"):
        assert_sequence_prompts_unedited(edited)


def test_policy_schema_defaults_old_jobs_to_legacy_and_accepts_explicit_agent_policy():
    assert JobConfig().event_grouping_policy == "legacy-pairs-v1"
    assert JobConfig(event_grouping_policy="event-agent-v2").event_grouping_policy == "event-agent-v2"
    with pytest.raises(ValidationError):
        JobConfig(event_grouping_policy="unknown")
