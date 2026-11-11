from app.schemas import ActionDetails, SpatialEvidence, VisionObservation
from app.prompt_profiles import profiles
from app.services.prompt_service import build_prompts


def observation(**overrides):
    payload = dict(
        subject_action="女孩从门边走到桌前并放下书",
        shot_size="中景",
        lens="标准",
        camera_movement="跟拍：从左向右平移",
        lighting_vibe="自然柔光",
        composition="主体位于右侧，门框形成框景",
        lighting_type="自然柔光",
        lighting_detail="左侧窗户提供自然柔光",
        spatial_evidence=SpatialEvidence(foreground="门框", midground="女孩与书", background="木桌和窗户", relationships="女孩朝向右侧桌面"),
        action_details=ActionDetails(primary_subject="穿蓝衣的女孩", initial_state="女孩站在左侧门边并手持书", steps=["女孩向右走近桌子", "女孩把书放在桌面"], interaction_target="书和桌面", final_state="女孩停在桌边，书位于桌面", emotion_change="平静", evidence=["Frame 1：位于门边", "Frame 4：书已在桌面"]),
        confidence=.88,
    )
    payload.update(overrides)
    return VisionObservation(**payload)


def test_prompt_profile_registry_is_schema_valid_and_versioned():
    import json
    from pathlib import Path
    registry = profiles()
    raw = json.loads((Path(__file__).resolve().parents[1] / "app/prompt_profiles/profiles.json").read_text())["profiles"]
    assert len({item["id"] for item in raw}) == len(raw)
    assert {"first-frame-v1", "kling-video-v1", "first-frame-v7", "seedance-video-v8", "script-first-frame-v1", "script-first-frame-v2", "seedance-script-v1", "seedance-script-v2"} <= set(registry)
    assert all(item.version >= 1 and item.required_contract_fields for item in registry.values())
    assert all(item.id.endswith(f"-v{item.version}") for item in registry.values())


def test_versioned_profiles_render_one_contract_for_each_platform():
    prompts = build_prompts("shot-1", observation())
    assert [prompt.template_id for prompt in prompts] == ["first-frame-v7", "seedance-video-v8"]
    assert all(prompt.contract == prompts[0].contract for prompt in prompts)
    assert "女孩停在桌边" not in prompts[0].positive_prompt
    assert "跟拍" not in prompts[0].positive_prompt
    assert "女孩站在左侧门边" in prompts[1].positive_prompt
    assert all("女孩停在桌边" in prompt.positive_prompt for prompt in prompts if prompt.prompt_type == "video")


def test_unknown_observation_is_not_replaced_by_invented_cinematography():
    prompts = build_prompts("shot-2", observation(lens="未知", camera_movement="未知", lighting_vibe="未知", lighting_detail=""))
    assert "35mm" not in prompts[0].positive_prompt
    assert "推近" not in prompts[1].positive_prompt
    assert "体积光" not in prompts[1].positive_prompt


def test_chinese_delivery_order_style_and_creative_suggestions():
    from app.schemas import VisualStyleEvidence
    visual = observation(visual_style_evidence=VisualStyleEvidence(medium="3D动画", character_design="大眼睛、夸张头身比例", rendering="柔光渲染", texture="光滑角色与织物纹理", color_palette="蓝白与暖米色"))
    prompts = build_prompts("shot-3", visual, generation_duration_seconds=4.8, aspect_ratio="9:16")
    first, video = prompts
    for prompt in prompts:
        assert prompt.positive_prompt.index("主体：") < prompt.positive_prompt.index("前景：") < prompt.positive_prompt.index("整体画面风格：")
        assert "3D动画" in prompt.positive_prompt
        assert "大眼睛" in prompt.positive_prompt
        assert "标准焦段" in prompt.positive_prompt
        assert "50mm" not in prompt.positive_prompt
        assert prompt.contract.generation_suggestions["focal_length"] == "50mm（生成建议，不是原片实测）"
        assert "塑料皮肤" not in prompt.negative_prompt
        assert prompt.contract.lens == "标准"
        assert "English" not in str(prompt.contract.delivery)
        assert "英文" not in str(prompt.contract.delivery)
    assert "0–2.4 秒" not in video.positive_prompt and "2.4–4.8 秒" not in video.positive_prompt
    assert "动作：女孩向右走近桌子；女孩把书放在桌面。" in video.positive_prompt
    assert video.contract.delivery["action_presentation"]["timing_mode"] == "none"
    assert "声音：" in video.positive_prompt
    assert any("未分析原片音轨" in item for item in video.contract.delivery["settings"])
    assert video.negative_prompt == ""
    assert "女孩停在桌边" not in first.positive_prompt
    assert prompts[0].contract.delivery["order"] == ["镜头合同", "首帧意图说明", "首帧图片提示词", "Seedance 中文图生视频提示词", "Seedance 素材与设置建议", "连续性检查"]


def test_missing_initial_state_is_not_replaced_by_middle_frame():
    visual = observation(action_details=ActionDetails(primary_subject="男孩", initial_state="未知", final_state="已举起杯子"))
    first = build_prompts("shot-4", visual)[0]
    assert "已举起杯子" not in first.positive_prompt
    assert "待人工设定" not in first.positive_prompt
    assert "待人工设定" in first.contract.first_frame_state
    assert first.warnings


def test_style_rules_are_reviewed_versioned_and_extendable():
    from app.prompt_profiles import style_rules
    rules = style_rules()
    assert rules["schema_version"] == "style-rules.v1" and rules["version"] == 1
    assert len(rules["sources"]) == 2
    assert all(len(source["sha256"]) == 64 for source in rules["sources"])
    assert "不猜作者" in rules["observation_guidance"]


def test_action_rules_have_reviewed_source_and_do_not_change_style_rules():
    import hashlib
    from pathlib import Path
    from app.prompt_profiles import action_rules, style_rules
    rules = action_rules()
    assert rules["schema_version"] == "action-delivery.v1"
    assert "不要强制" in rules["observation_guidance"] and "四段" in rules["observation_guidance"]
    root = Path(__file__).resolve().parents[2]
    for source in rules["sources"]:
        assert hashlib.sha256((root / source["path"]).read_bytes()).hexdigest() == source["sha256"]
    assert style_rules()["schema_version"] == "style-rules.v1"


def test_unknown_main_action_remains_unknown_and_warned():
    visual = observation(subject_action="未知", action_details=ActionDetails(initial_state="主体静止", final_state="主体静止"))
    first, video = build_prompts("unknown", visual)
    assert video.contract.primary_action == "未知"
    assert video.contract.delivery["action_presentation"]["source"] == "unknown"
    assert any("主体动作未知" in warning for warning in video.warnings)
    assert "动作：未知" not in video.positive_prompt
    assert "动作时间安排" not in video.positive_prompt


def test_legacy_contract_remains_readable_without_recompute():
    from app.schemas import PromptBundle
    legacy = dict(id="old", shot_id="old-shot", prompt_type="video", platform="kling", version=1,
                  positive_prompt="历史提示词", negative_prompt="", template_id="kling-video-v1", template_version=1,
                  contract={"first_frame_state": "旧起点"})
    result = PromptBundle.model_validate(legacy)
    assert result.positive_prompt == "历史提示词" and result.contract.delivery == {}


def test_first_frame_has_only_static_controls_and_no_audio_or_delivery_audit():
    first, video = build_prompts("compact", observation(), source_edit_times=[1.2],
                                 generation_duration_seconds=4.8, aspect_ratio="9:16")
    assert "起始画面：女孩站在左侧门边并手持书" in first.positive_prompt
    assert all(word not in first.positive_prompt for word in (
        "声音", "台词", "旁白", "配乐", "时长", "结束画面", "收束", "源片", "生成建议", "原片实测"))
    assert "\n\n" not in first.positive_prompt + video.positive_prompt
    assert video.positive_prompt.count("无台词") == 1
    assert video.positive_prompt.count("不生成文字") == 1
    assert all(word not in video.positive_prompt for word in ("源片包含", "原片实测", "未分析原片音轨"))
    assert first.contract.source_edit_times == [1.2]
    assert first.contract.editorial_note
    assert first.contract.delivery["continuity_checks"]
    assert first.contract.delivery["first_frame_prompt"] == first.positive_prompt
    assert video.contract.delivery["seedance_video_prompt"] == video.positive_prompt


def test_unknown_optional_fields_are_omitted_without_losing_core_review_state():
    visual = observation(lens="未知", camera_movement="未知", shot_size="未知", composition="未知",
        lighting_vibe="未知", lighting_detail="未知", spatial_evidence=SpatialEvidence(),
        action_details=ActionDetails(primary_subject="女子", initial_state="未知", final_state="未知"))
    for prompt in build_prompts("missing", visual):
        assert all(text not in prompt.positive_prompt for text in (
            "前景：未知", "中景：未知", "背景：未知", "空间关系：未知", "镜头：未知",
            "构图：未知", "光线：未知", "画幅：待设定", "地点待确认", "毫米焦段待设定"))
        assert prompt.contract.first_frame_state.startswith("首帧状态待人工")
        assert any("首帧或结束状态" in warning for warning in prompt.warnings)


def test_direct_style_global_paragraph_is_reused_verbatim():
    from types import SimpleNamespace
    style = "整体画面风格：新黑色电影式冷暖分离影调，青蓝暗部与克制暖光，黑位深沉但保留层次。"
    direct = SimpleNamespace(rendered_prompt=style, version=3, profile_hash="stable-style",
                             scene_prompts={}, anchor=None, reference=None,
                             application_status="applied", provenance={})
    for strategy in ("legacy-v1", "qwen-direct-v3"):
        for prompt in build_prompts("style", observation(), direct_style=direct, style_strategy=strategy):
            assert prompt.positive_prompt.count(style) == 1
            assert prompt.contract.delivery["style_layers"]["global"] == style
            assert prompt.contract.style_profile_hash == "stable-style"


def test_compact_video_preserves_repeated_meaningful_actions():
    visual = observation(subject_action="女子饮用后再次饮用杯中液体",
        action_details=ActionDetails(primary_subject="女子", initial_state="女子握着杯子",
            steps=["女子饮用杯中液体", "女子再次饮用杯中液体"], final_state="女子放下杯子"))
    video = build_prompts("repeat", visual)[1]
    assert "女子饮用后再次饮用杯中液体" in video.positive_prompt
    assert video.contract.delivery["action_presentation"]["main_action"] == visual.subject_action


def test_unresolved_style_is_review_metadata_instead_of_generator_placeholder():
    for strategy in ("legacy-v1", "vector-v2", "qwen-direct-v3"):
        for prompt in build_prompts("pending-style", observation(), style_strategy=strategy):
            assert all(value not in prompt.positive_prompt for value in (
                "项目画风待确认", "画风待人工确认", "画风分析未完成", "不套用默认"))
            assert prompt.contract.observed_style
            assert any("整体画面风格待复核" in warning for warning in prompt.warnings)


def test_partial_style_keeps_known_local_traits_without_claiming_global_resolution():
    from types import SimpleNamespace
    local = "本镜基础视觉观察（非完整画风）：三维动画，暖色调。"
    style = "画风分析未完成，未确定具体风格。\n" + local
    direct = SimpleNamespace(rendered_prompt=style, version=3, profile_hash="partial-style",
                             scene_prompts={}, anchor=None, reference=None,
                             application_status="partial", provenance={})
    for prompt in build_prompts("partial-style", observation(), direct_style=direct):
        assert local in prompt.positive_prompt
        assert "画风分析未完成" not in prompt.positive_prompt
        assert "整体画面风格：" not in prompt.positive_prompt
        assert prompt.contract.observed_style == style
        assert any("整体画面风格待复核" in warning for warning in prompt.warnings)
