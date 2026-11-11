import copy
import json
import sqlite3

import pytest

from app.schemas import (
    ActionDetails, GenerationSequence, NarrativeEvent, PromptBundle, Shot,
    ShotContract, VisualFacts,
)
from app.script_director.sequences import build_script_sequences, rerender_script_sequences
from app.services.event_sequences import store_sequence_payload


def script_shot(number, duration=3, *, start=None, event="event-1", dialogue="", source_text=None):
    start = (number - 1) * duration if start is None else start
    return Shot(id=f"script-job-shot-{number:03d}", number=number,
        timing_origin="script-performance", start_time=start, end_time=start + duration,
        duration=duration, generation_duration_seconds=duration, time=f"{start}–{start + duration}s",
        source_text=source_text if source_text is not None else f"动作原文 {number}\n{dialogue}",
        action=f"甲完成动作{number}", scene_group_id="scene-001", status="ready",
        shot_size="中景", lens="标准", camera_movement="固定", composition="甲在左侧", lighting_vibe="室内暖光",
        visual_facts=VisualFacts(scene="客厅"),
        action_details=ActionDetails(primary_subject="甲", initial_state=f"起点{number}",
            final_state=f"终点{number}", steps=[f"甲完成动作{number}"]),
        script_direction={"event_id": event, "event_title": "甲交出文件", "event_goal": "完成文件交接",
            "source_span_ids": [f"span-{number}"], "camera_angle": "低角度",
            "character_profiles": [{"id": "character-a", "name": "甲", "description": "红色外套"}],
            "dialogue": [{"id": f"dialogue-{number}", "speaker_id": "character-a", "speaker_name": "甲",
                "text": dialogue, "parenthetical": "压低声音"}] if dialogue else [],
            "sound_cues": [f"脚步声{number}"], "creative_assumptions": ["暖光为导演创作建议"]})


def source_prompts(shots, style="纸层剪影", style_hash="style-1", version=1):
    prompts = []
    for shot in shots:
        contract = ShotContract(timing_origin="script-performance", first_frame_state=shot.action_details.initial_state,
            end_state=shot.action_details.final_state, generation_duration_seconds=shot.generation_duration_seconds,
            observed_style=style, style_profile_hash=style_hash, style_profile_version=f"script-style:{version}",
            delivery={"script_direction": copy.deepcopy(shot.script_direction), "style_layers": {"global": style}})
        for kind, template in (("first_frame", "script-first-frame-v1"), ("video", "seedance-script-v1")):
            prompts.append(PromptBundle(id=f"{shot.id}-{kind}", shot_id=shot.id, prompt_type=kind,
                platform="Seedance" if kind == "video" else "image-generation", version=version,
                positive_prompt="来源剧本 Prompt", negative_prompt="", template_id=template,
                template_version=1, contract=contract.model_copy(deep=True)))
    return prompts


def test_authored_event_uses_four_shots_and_internal_cuts_with_typed_source_contracts():
    shots = [script_shot(index) for index in range(1, 5)]
    original = [shot.model_dump() for shot in shots]
    payload = build_script_sequences(shots, source_prompts(shots))
    assert [shot.model_dump() for shot in shots] == original
    assert len(payload["narrative_events"]) == len(payload["generation_sequences"]) == 1
    event = NarrativeEvent.model_validate(payload["narrative_events"][0])
    sequence = GenerationSequence.model_validate(payload["generation_sequences"][0])
    assert event.source_shot_ids == sequence.source_shot_ids == [shot.id for shot in shots]
    assert sequence.generation_mode == "internal_cuts" and sequence.generation_duration_seconds == 12
    assert [(stage.generation_start_seconds, stage.generation_end_seconds) for stage in sequence.timeline] == [(0, 3), (3, 6), (6, 9), (9, 12)]
    assert [stage.transition for stage in sequence.timeline] == ["initial", "cut", "cut", "cut"]
    assert all(stage.photography["camera_angle"] == "低角度" for stage in sequence.timeline)
    assert sequence.first_frame_prompt.contract.source_edit_times == []
    assert payload["sequence_metadata"]["event_grouping_policy"] == "script-director-v3"
    assert payload["sequence_metadata"]["render_version"] == "script-sequence.v2"
    assert payload["sequence_metadata"]["extra_model_calls"] == 0
    assert payload == build_script_sequences(shots, source_prompts(shots))


def test_seconds_and_shot_budget_partition_without_two_shot_rule_or_lost_coverage():
    durations = [4, 3, 4, 2, 2, 2, 2, 2]
    shots, cursor = [], 0
    for number, duration in enumerate(durations, 1):
        shots.append(script_shot(number, duration, start=cursor))
        cursor += duration
    payload = build_script_sequences(shots, source_prompts(shots))
    sequences = payload["generation_sequences"]
    assert len(payload["narrative_events"]) == 1
    assert [len(sequence["source_shot_ids"]) for sequence in sequences] == [3, 4, 1]
    assert [sequence["generation_duration_seconds"] for sequence in sequences] == [11, 8, 2]
    assert [shot_id for sequence in sequences for shot_id in sequence["source_shot_ids"]] == [shot.id for shot in shots]
    assert sum(sequence["source_duration_seconds"] for sequence in sequences) == sum(durations)
    assert all(sequence["timeline"][0]["generation_start_seconds"] == 0 for sequence in sequences)


def test_only_adjacent_same_event_ids_merge_and_missing_ownership_is_singleton():
    shots = [script_shot(1), script_shot(2, event="event-2"), script_shot(3),
             script_shot(4, event=""), script_shot(5, event="")]
    payload = build_script_sequences(shots, source_prompts(shots))
    assert len(payload["narrative_events"]) == 5
    assert len({event["id"] for event in payload["narrative_events"]}) == 5
    assert [sequence["source_shot_ids"] for sequence in payload["generation_sequences"]] == [[shot.id] for shot in shots]


def test_long_source_dialogue_preserves_every_character_sound_and_source_text():
    dialogue = "请记住原文。" * 180 + "\n别替我删改标点：真的！"
    source_text = "甲从桌上拿起文件。\n甲（压低声音）：" + dialogue
    shot = script_shot(1, 12, dialogue=dialogue, source_text=source_text)
    sequence = build_script_sequences([shot], source_prompts([shot]))["generation_sequences"][0]
    video = sequence["seedance_prompt"]["positive_prompt"]
    first_frame = sequence["first_frame_prompt"]["positive_prompt"]
    delivery = sequence["seedance_prompt"]["contract"]["delivery"]
    assert dialogue in video and video.count(dialogue) == 1
    assert dialogue not in first_frame
    assert "甲（压低声音）" in video and "脚步声1" in video
    assert not any(guard in video for guard in ("无台词", "无对白", "无人声", "无人物说话", "仅提供无对白"))
    assert not any(text in video for text in ("未生成音频", "不是原视频测量事实", "非原文拍摄事实", "暖光为导演创作建议"))
    assert "不是原视频测量事实" in delivery["provenance"]["note"]
    assert delivery["creative_assumptions"] == ["暖光为导演创作建议"]
    assert delivery["dialogue"][0]["text"] == dialogue
    assert delivery["source_texts"][shot.id] == source_text
    assert delivery["source_span_ids"] == ["span-1"]
    assert delivery["audio_generated"] is False
    assert video.index("主体：") < video.index("动作：") < video.index("结束状态：") < video.index("整体画面风格") < video.index("视频规格")
    assert "导演摄影建议" not in video and "0–12 秒" not in video
    assert "1080p" in video and "9:16" in video
    assert "红色外套" in video and "纸层剪影" in video


def test_script_performance_durations_are_not_silently_clamped_to_video_limits():
    shot = script_shot(1, 8, dialogue="请把文件交给我。")
    payload = build_script_sequences([shot], source_prompts([shot]))
    assert payload["generation_sequences"][0]["generation_duration_seconds"] == 8
    too_long = shot.model_copy(update={"generation_duration_seconds": 12.1, "duration": 12.1, "end_time": 12.1})
    with pytest.raises(ValueError, match="不得截断"):
        build_script_sequences([too_long], [])
    video_source = shot.model_copy(update={"timing_origin": "source-video"})
    with pytest.raises(ValueError, match="不解释原视频"):
        build_script_sequences([video_source], [])
    with pytest.raises(ValueError, match="ID 重复"):
        build_script_sequences([shot, shot], [])


def test_style_rerender_preserves_saved_choreography_ownership_and_exact_dialogue():
    shots = [script_shot(index, dialogue=f"原对白{index}，不能改。") for index in range(1, 7)]
    payload = build_script_sequences(shots, source_prompts(shots))
    original = copy.deepcopy(payload)
    # Even a changed input direction cannot rewrite the saved director plan.
    changed_shots = [shot.model_copy(deep=True) for shot in shots]
    for shot in changed_shots:
        shot.script_direction.update(event_id="new-event", dialogue=[{"text": "不得替换为新对白"}])
        shot.action_details.steps = ["不得替换为新动作"]
        shot.action_details.initial_state = "不得替换起点"
    changed_prompts = source_prompts(changed_shots, style="水彩纸纹", style_hash="style-2", version=2)
    for prompt in changed_prompts:
        prompt.contract.continuity_anchors = ["不得替换连续性锚点"]
    refreshed = rerender_script_sequences(payload, changed_shots, changed_prompts)
    assert payload == original
    assert refreshed["narrative_events"] == payload["narrative_events"]
    assert refreshed["sequence_metadata"]["source_snapshot"]["shots"] == payload["sequence_metadata"]["source_snapshot"]["shots"]
    assert refreshed["sequence_metadata"]["event_ownership"] == payload["sequence_metadata"]["event_ownership"]
    assert refreshed["sequence_metadata"]["source_hash"] == payload["sequence_metadata"]["source_hash"]
    assert refreshed["sequence_metadata"]["build_hash"] != payload["sequence_metadata"]["build_hash"]
    assert refreshed["sequence_metadata"]["version_number"] == 2
    for old, new in zip(payload["generation_sequences"], refreshed["generation_sequences"]):
        for key in old:
            if key not in {"first_frame_prompt", "seedance_prompt", "build_hash", "version_hash", "version"}:
                assert new[key] == old[key]
        assert new["version"] == new["seedance_prompt"]["version"] == 2
        assert new["seedance_prompt"]["contract"]["style_profile_hash"] == "style-2"
        assert new["seedance_prompt"]["contract"]["continuity_anchors"] == old["seedance_prompt"]["contract"]["continuity_anchors"]
        assert "水彩纸纹" in new["seedance_prompt"]["positive_prompt"]
        assert "纸层剪影" not in new["seedance_prompt"]["positive_prompt"]
        assert "不得替换" not in new["seedance_prompt"]["positive_prompt"]
        for shot_id in new["source_shot_ids"]:
            shot = next(shot for shot in shots if shot.id == shot_id)
            assert shot.script_direction["dialogue"][0]["text"] in new["seedance_prompt"]["positive_prompt"]
        GenerationSequence.model_validate(new)


def test_rerender_keeps_manual_prompt_edits_and_rejects_missing_sources():
    shots = [script_shot(1)]
    prompts = source_prompts(shots)
    payload = build_script_sequences(shots, prompts)
    edited = copy.deepcopy(payload)
    edited["generation_sequences"][0]["seedance_prompt"]["positive_prompt"] += "用户自定义补充"
    with pytest.raises(ValueError, match="手动修改"):
        rerender_script_sequences(edited, shots, prompts)
    with pytest.raises(ValueError, match="缺少来源镜头"):
        rerender_script_sequences(payload, [], prompts)


def test_existing_sequence_store_archives_script_payload_and_preserves_versions():
    shots = [script_shot(1, 6, dialogue="不要丢失这个声音层。"), script_shot(2, 6)]
    prompts = source_prompts(shots)
    payload = build_script_sequences(shots, prompts)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,event_sequence_json TEXT,updated_at TEXT)")
    conn.execute("CREATE TABLE event_sequence_revisions(job_id TEXT,version INTEGER,payload_json TEXT,created_at TEXT)")
    conn.execute("INSERT INTO jobs(id,event_sequence_json) VALUES('script-job','{}')")
    conn.commit()
    try:
        with conn:
            saved = store_sequence_payload(conn, "script-job", payload, expected_version=0)
        assert saved["sequence_metadata"]["version_number"] == 1
        assert saved["sequence_metadata"]["source_snapshot"] == payload["sequence_metadata"]["source_snapshot"]
        refreshed = rerender_script_sequences(saved, shots,
            source_prompts(shots, style="低饱和电影影像", style_hash="style-2", version=2))
        with conn:
            updated = store_sequence_payload(conn, "script-job", refreshed, expected_version=1)
        assert updated["sequence_metadata"]["version_number"] == 2
        assert updated["generation_sequences"][0]["first_frame_prompt"]["version"] == 2
        archived = conn.execute("SELECT payload_json FROM event_sequence_revisions WHERE version=1").fetchone()
        assert json.loads(archived["payload_json"]) == saved
        assert "不要丢失这个声音层。" in updated["generation_sequences"][0]["seedance_prompt"]["positive_prompt"]
    finally:
        conn.close()


def test_first_frame_excludes_later_characters_and_inherits_landscape_delivery():
    shots = [script_shot(1), script_shot(2)]
    shots[1].script_direction["character_profiles"] = [{"id": "character-b", "name": "乙", "description": "蓝色雨衣"}]
    prompts = source_prompts(shots)
    for prompt in prompts:
        prompt.contract.aspect_ratio = "16:9"
        prompt.contract.delivery["video_spec"] = {"resolution": "2160p", "aspect_ratio": "16:9"}
    sequence = build_script_sequences(shots, prompts)["generation_sequences"][0]
    first, video = sequence["first_frame_prompt"]["positive_prompt"], sequence["seedance_prompt"]["positive_prompt"]
    assert "红色外套" in first and "蓝色雨衣" not in first
    assert "蓝色雨衣" in video
    assert "横屏 16:9" in first and "2160p" in first and "9:16" not in first
    assert not any(text in first for text in ("声音：", "对白／旁白", "0 秒", "结束状态：", "切至下一镜头"))
    delivery = sequence["seedance_prompt"]["contract"]["delivery"]
    assert len(delivery["character_profiles"]) == 2
    assert delivery["first_frame_character_profiles"] == shots[0].script_direction["character_profiles"]
    assert delivery["video_spec"]["aspect_source"] == "first-source-setting"


@pytest.mark.parametrize("field,values", [("aspect_ratio", ["16:9", "9:16"]), ("resolution", ["1080p", "2160p"])])
def test_conflicting_explicit_generation_settings_reject_merge(field, values):
    shots = [script_shot(1), script_shot(2)]
    for shot, value in zip(shots, values):
        shot.script_direction[field] = value
    with pytest.raises(ValueError, match="显式设置冲突"):
        build_script_sequences(shots, source_prompts(shots))


def test_action_camera_share_timeline_and_repeated_source_actions_dialogue_remain():
    shots = [script_shot(1, dialogue="无台词"), script_shot(2, dialogue="无台词")]
    for shot in shots:
        shot.action_details.steps = ["甲敲门", "甲敲门"]
    sequence = build_script_sequences(shots, source_prompts(shots))["generation_sequences"][0]
    video = sequence["seedance_prompt"]["positive_prompt"]
    assert video.count("甲敲门") == 4 and video.count("“无台词”") == 2
    assert video.count("0–3 秒") == video.count("3–6 秒") == 1
    assert "3–6 秒：切至下一镜头；中景" in video
    assert "导演摄影建议" not in video
    assert sequence["seedance_prompt"]["contract"]["delivery"]["source_timeline"] == sequence["timeline"]


def test_optional_unknown_camera_is_omitted_core_unknown_warns_without_invention():
    shot = script_shot(1)
    shot.shot_size = shot.lens = shot.camera_movement = shot.composition = shot.lighting_vibe = "未知"
    shot.script_direction["camera_angle"] = "未知"
    shot.visual_facts.scene = "未知"
    sequence = build_script_sequences([shot], source_prompts([shot]))["generation_sequences"][0]
    video = sequence["seedance_prompt"]["positive_prompt"]
    assert "未知" not in video and "摄影方案待确认" not in video
    assert "场景：" not in video
    assert sequence["status"] == "warning" and "剧本事件场景未知，需复核" in sequence["warnings"]
    assert sequence["timeline"][0]["photography"]["shot_size"] == "未知"


def test_authored_subphases_survive_sequence_projection_without_extra_cuts_or_dialogue():
    shot = script_shot(1, 4, dialogue="请喝水。")
    shot.script_direction["action_timeline"] = [
        {"start_seconds": 0, "end_seconds": 1.2345, "description": "甲拿起杯子"},
        {"start_seconds": 1.2345, "end_seconds": 4, "description": "甲喝下液体后放回杯子"},
    ]
    sequence = build_script_sequences([shot], source_prompts([shot]))["generation_sequences"][0]
    video = sequence["seedance_prompt"]["positive_prompt"]
    assert "0–1.23 秒：中景" in video and "1.23–4 秒：甲喝下液体后放回杯子" in video
    assert "甲完成动作1" not in video and video.count("请喝水。") == 1
    assert "切至下一镜头" not in video and video.count("低角度") == 1
    assert len(sequence["timeline"]) == 1 and sequence["timeline"][0]["action_steps"] == ["甲完成动作1"]
    assert sequence["seedance_prompt"]["contract"]["delivery"]["render_timeline"][0]["end_seconds"] == 1.2345


def test_legacy_style_rerender_keeps_v1_grammar_and_render_version():
    from app.script_director.sequences import _bundles, _render_v1
    shots = [script_shot(1)]
    prompts = source_prompts(shots)
    payload = build_script_sequences(shots, prompts)
    legacy = _render_v1(GenerationSequence.model_validate(payload["generation_sequences"][0]), shots, _bundles(shots, prompts))
    payload["generation_sequences"] = [legacy.model_dump(mode="json")]
    payload["sequence_metadata"].update(version="script-sequence.v1", render_version="script-sequence.v1")
    refreshed = rerender_script_sequences(payload, shots, source_prompts(shots, style="水彩纸纹", version=2))
    sequence = refreshed["generation_sequences"][0]
    assert refreshed["sequence_metadata"]["render_version"] == "script-sequence.v1"
    assert sequence["seedance_prompt"]["contract"]["delivery"]["render_version"] == "script-sequence.v1"
    assert "本轮未生成音频" in sequence["seedance_prompt"]["positive_prompt"]
    assert "导演摄影建议（创作方案）" in sequence["seedance_prompt"]["positive_prompt"]
    assert "水彩纸纹" in sequence["seedance_prompt"]["positive_prompt"]
