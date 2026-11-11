from app.schemas import ActionDetails, VisionObservation, VisualFacts
from app.script_director.render import build_script_prompts


def observation():
    return VisionObservation(subject_action="甲敲门", shot_size="中景", lens="标准", camera_movement="固定",
        lighting_vibe="未知", composition="未知", visual_facts=VisualFacts(scene="走廊"),
        action_details=ActionDetails(primary_subject="甲", initial_state="甲站在门前", steps=["甲敲门", "甲敲门"], final_state="甲等待回应"),
        script_direction={"duration_seconds": 4, "aspect_ratio": "16:9", "resolution": "2160p",
            "global_style_prompt": "整体画面风格：多层剪纸拼贴动画，纸纤维清晰。",
            "dialogue": [{"speaker_name": "甲", "text": "无台词\n别删这句。"}],
            "creative_assumptions": ["摄影是导演创作建议"]})


def test_script_v2_compact_prompt_preserves_dialogue_actions_specs_and_audit():
    item = observation()
    first, video = build_script_prompts("script-shot", item)
    assert (first.template_id, video.template_id) == ("script-first-frame-v2", "seedance-script-v2")
    assert first.template_version == video.template_version == 2
    assert "2160p" in first.positive_prompt and "16:9" in first.positive_prompt
    assert "摄影：中景；标准。" in first.positive_prompt
    assert not any(text in first.positive_prompt for text in ("未知", "0秒", "结束状态：", "对白／旁白：", "建议生成时长"))
    assert "无台词\n别删这句。" in video.positive_prompt
    assert video.positive_prompt.count("甲敲门") == 2
    assert "0–4 秒" not in video.positive_prompt and "环境声" not in video.positive_prompt
    assert "创作建议" not in video.positive_prompt
    assert video.contract.delivery["creative_assumptions"] == ["摄影是导演创作建议"]
    assert video.contract.delivery["video_spec"]["resolution"] == "2160p"
    assert "prompt_lint" in video.contract.delivery
    assert "整体画面风格：多层剪纸拼贴动画，纸纤维清晰。" in first.positive_prompt


def test_script_v2_multi_phase_keeps_precise_contract_and_one_timeline():
    item = observation()
    item.script_direction["action_timeline"] = [
        {"start_seconds": 0, "end_seconds": 1.2345, "description": "甲拿起杯子"},
        {"start_seconds": 1.2345, "end_seconds": 4, "description": "甲喝下液体后放回杯子"},
    ]
    _, video = build_script_prompts("script-shot", item)
    assert "0–1 秒：甲拿起杯子" in video.positive_prompt
    assert "1–4 秒：甲喝下液体后放回杯子" in video.positive_prompt
    assert "甲敲门" not in video.positive_prompt
    assert video.contract.delivery["source_timeline"] == item.script_direction["action_timeline"]
    assert video.contract.delivery["render_timeline"][0]["end_seconds"] == 1
    assert item.script_direction['action_timeline'][0]['end_seconds'] == 1.2345


def test_script_v1_explicit_refresh_does_not_silently_upgrade_template():
    first, video = build_script_prompts("script-shot", observation(), template_id="seedance-script-v1")
    assert first.template_id == "script-first-frame-v1" and video.template_id == "seedance-script-v1"
    assert "0秒定格" in first.positive_prompt and "拟音／画外声音" in video.positive_prompt
    assert video.contract.delivery["schema_version"] == "prompt-delivery.script-v3"


def test_script_display_preserves_very_short_stage_without_changing_contract():
    item = observation()
    item.script_direction["action_timeline"] = [
        {"start_seconds": 0, "end_seconds": .001, "description": "甲眼神一动"},
        {"start_seconds": .001, "end_seconds": 4, "description": "甲举起手"},
    ]
    _, video = build_script_prompts("script-shot", item)
    assert "0–0.5 秒" in video.positive_prompt and "0–0 秒" not in video.positive_prompt
    assert video.contract.delivery["source_timeline"][0]["end_seconds"] == .001
    assert video.contract.delivery["render_timeline"][0]["end_seconds"] == .5
