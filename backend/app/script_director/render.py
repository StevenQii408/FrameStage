"""Script-specific rendering: dialogue is an audio track, not a visual action."""
from __future__ import annotations

from app.prompt_profiles import profile
from app.schemas import PromptBundle, ShotContract
from app.services.prompt_delivery import attach_prompt_lint, display_timing, is_known
from app.services.generation_timing import VERSION as TIMING_VERSION, allocate_phase_durations, ceil_half_seconds


DEFAULT_STYLE = "整体画面风格：写实电影影像，色彩克制，明暗层次清晰，人物与环境材质自然，避免过度磨皮与泛光。"
NEGATIVE = "模糊，低分辨率，水印，字幕，文字覆盖，logo，畸形面部，多余手指，手部变形，主体漂移"


def _sentence(text):
    return str(text).rstrip("。；;.!！") + "。"


def _style(direction, direct_style=None, visual_bible=None, style_profile=None):
    if direct_style:
        return direct_style.rendered_prompt, f"style-profile.v3:{direct_style.version}", direct_style.profile_hash
    if visual_bible and visual_bible.status == "confirmed":
        return visual_bible.rendered_global_prompt, f"{visual_bible.schema_version}:{visual_bible.version}", visual_bible.bible_hash
    if style_profile and style_profile.status == "confirmed":
        return style_profile.rendered_style_prompt, f"{style_profile.schema_version}:{style_profile.version}", style_profile.profile_hash
    return direction.get("global_style_prompt") or DEFAULT_STYLE, "script-creative-style.v1", ""


def build_script_prompts_v1(shot_id, observation, *, aspect_ratio="待设定", direct_style=None,
                         visual_bible=None, style_profile=None, generation_duration_seconds=None, **kwargs):
    direction = observation.script_direction
    details = observation.action_details
    duration = generation_duration_seconds or direction["duration_seconds"]
    ratio = direction.get("aspect_ratio", "9:16") if aspect_ratio == "待设定" else aspect_ratio
    style, style_version, style_hash = _style(direction, direct_style, visual_bible, style_profile)
    camera = "，".join(filter(None, [observation.shot_size, f"{observation.lens}视觉方案", direction.get("camera_angle"), observation.camera_movement]))
    still_camera = "，".join(filter(None, [observation.shot_size, f"{observation.lens}视觉方案", direction.get("camera_angle")]))
    character_lines = [f"{item['name']}：{item['description']}" for item in direction.get("character_profiles", []) if item.get("description")]
    characters = "；".join(character_lines)
    subject = details.primary_subject
    scene = observation.visual_facts.scene
    dialogue = direction.get("dialogue", [])
    voices = [f"{item.get('speaker_name') or '说话人未明确'}{('（' + item['parenthetical'] + '）') if item.get('parenthetical') else ''}：“{item['text']}”" for item in dialogue]
    sounds = direction.get("sound_cues", [])
    audio = "原对白／旁白（单独声音层，不生成字幕）：" + "；".join(voices) if voices else "本镜没有原文对白；不自行补写台词。"
    audio += "\n拟音／画外声音：" + ("；".join(sounds) if sounds else "按场景需要保留自然环境声，不凭空增加剧情声音。")
    guard = "不生成文字、字幕、招牌文字、Logo、水印或可读符号；手部与道具接触自然，角色身份与场景连续。"
    first = "\n".join(filter(None, ["【首帧图片提示词】", _sentence(f"主体：{subject}"), _sentence(f"人物设定：{characters}") if characters else "",
        _sentence(f"场景：{scene}"), _sentence(f"0秒定格：{details.initial_state}"), _sentence(f"摄影：{still_camera}；构图：{observation.composition}；光线：{observation.lighting_detail or observation.lighting_vibe}"),
        style, f"画幅：{ratio}；分辨率：1080p。", "冻结在动作起点，不提前展示交接、逃离或后续结果。" + guard]))
    stages = direction.get("action_timeline", [])
    if len(stages) > 1:
        action = [f"{stage['start_seconds']:g}–{stage['end_seconds']:g}秒：{stage['description']}" for stage in stages]
    else:
        action = ["动作与表演：" + "；".join(details.steps)]
    video = "\n".join(filter(None, ["【Seedance 剧本视频提示词】", _sentence(f"主体：{subject}"), _sentence(f"人物设定：{characters}") if characters else "",
        _sentence(f"场景：{scene}"), _sentence(f"以首帧为起点：{details.initial_state}"), *action, audio,
        _sentence(f"摄影：{camera}；构图：{observation.composition}；光线：{observation.lighting_detail or observation.lighting_vibe}"),
        style, _sentence(f"结束状态：{details.final_state}"), f"建议生成时长：{duration:g}秒；分辨率：1080p；画幅：{ratio}。", guard]))
    contract = ShotContract(timing_origin="script-performance", purpose=direction.get("shot_function", "剧本事件呈现"),
        first_frame_state=details.initial_state, primary_action="；".join(details.steps), end_state=details.final_state,
        continuity_anchors=direction.get("continuity_notes", []) + character_lines, shot_size=observation.shot_size,
        lens=observation.lens, camera_movement=observation.camera_movement, lighting=observation.lighting_detail or observation.lighting_vibe,
        composition=observation.composition, generation_duration_seconds=duration, aspect_ratio=ratio,
        observed_style=style, style_profile_version=style_version, style_profile_hash=style_hash,
        editorial_note="剧本创作计划；秒数为表演估计，不是原视频测量事实",
        generation_suggestions={"sound": "保留原对白/旁白与声音层，音频尚未生成", "focal_length": "焦段为导演建议，不是原片实测"})
    contract.delivery = {"schema_version": "prompt-delivery.script-v3", "script_direction": direction,
        "first_frame_prompt": first, "seedance_video_prompt": video,
        "source_documents": ["Prompts/script_director_v3.md", "Prompts/short_drama_storyboard_skill.md"],
        "audio_policy": "preserve-source-dialogue", "audio_generated": False,
        "style_layers": {"global": style, "scene": "", "shot": "", "local_effect": ""},
        "settings": [f"时长{duration:g}秒；1080p；{ratio}", "对白是声音层，不生成字幕；本轮未生成音频或图片"]}
    warnings = list(observation.analysis_warnings)
    return [PromptBundle(id=f"{shot_id}-{suffix}", shot_id=shot_id, prompt_type=metadata.prompt_type,
        platform=metadata.platform, version=metadata.version, positive_prompt=text,
        negative_prompt=NEGATIVE if metadata.prompt_type == "first_frame" else "",
        status="warning" if warnings else "ready", template_id=metadata.id, template_version=metadata.version,
        warnings=warnings, contract=contract)
        for template, suffix, text in [("script-first-frame-v1", "first-frame", first), ("seedance-script-v1", "seedance", video)]
        for metadata in [profile(template)]]


def _camera_text(observation, direction, *, still=False):
    """Omit unavailable optional photography without supplying a creative guess."""
    parts = [observation.shot_size, observation.lens, direction.get("camera_angle")]
    if not still:
        parts.append(observation.camera_movement)
    parts.extend([
        "构图：" + observation.composition if is_known(observation.composition) else "",
        "光线：" + (observation.lighting_detail or observation.lighting_vibe)
        if is_known(observation.lighting_detail or observation.lighting_vibe) else "",
    ])
    return "；".join(value for value in parts if is_known(value))


def build_script_prompts(shot_id, observation, *, aspect_ratio="待设定", direct_style=None,
                         visual_bible=None, style_profile=None, generation_duration_seconds=None,
                         render_version=None, template_id=None, **kwargs):
    # Historical style refreshes keep their original delivery grammar.
    if render_version in {"v1", "script-v1", "prompt-delivery.script-v3"} or template_id in {
        "script-first-frame-v1", "seedance-script-v1",
    }:
        return build_script_prompts_v1(shot_id, observation, aspect_ratio=aspect_ratio,
            direct_style=direct_style, visual_bible=visual_bible, style_profile=style_profile,
            generation_duration_seconds=generation_duration_seconds, **kwargs)
    prompts = build_script_prompts_v1(shot_id, observation, aspect_ratio=aspect_ratio,
        direct_style=direct_style, visual_bible=visual_bible, style_profile=style_profile,
        generation_duration_seconds=generation_duration_seconds, **kwargs)
    direction, details = observation.script_direction, observation.action_details
    contract = prompts[0].contract
    contract.generation_duration_seconds = ceil_half_seconds(contract.generation_duration_seconds)
    resolution = direction.get("resolution") or "1080p"
    ratio = contract.aspect_ratio
    characters = "；".join(f"{item['name']}：{item['description']}"
        for item in direction.get("character_profiles", []) if item.get("description"))
    common = [
        _sentence("主体：" + details.primary_subject) if is_known(details.primary_subject) else "",
        _sentence("人物设定：" + characters) if characters else "",
        _sentence("场景：" + observation.visual_facts.scene) if is_known(observation.visual_facts.scene) else "",
    ]
    initial = _sentence("起始画面：" + details.initial_state) if is_known(details.initial_state) else ""
    guard = "不生成文字、字幕、Logo或水印；角色身份与道具接触关系保持连续。"
    style = contract.observed_style
    first_camera = _camera_text(observation, direction, still=True)
    first = "\n".join(filter(None, ["【首帧图片提示词】", *common, initial,
        _sentence("摄影：" + first_camera) if first_camera else "", style,
        f"图像规格：分辨率 {resolution}；画幅 {ratio}。",
        "冻结在动作起点，不提前展示动作结果。" + guard]))
    stages = [dict(stage) for stage in direction.get("action_timeline", [])]
    if stages:
        try:
            durations = allocate_phase_durations([stage['end_seconds'] - stage['start_seconds'] for stage in stages], contract.generation_duration_seconds)
            cursor = 0.0
            for stage, seconds in zip(stages, durations):
                stage['start_seconds'], stage['end_seconds'] = cursor, cursor + seconds
                cursor += seconds
        except ValueError:
            stages = []  # Ordered action text remains; never fabricate zero-time stages.
    timing = display_timing(contract.generation_duration_seconds,
        [(stage["start_seconds"], stage["end_seconds"]) for stage in stages])
    if len(stages) > 1:
        action = [f"{interval['display_start_seconds']}–{interval['display_end_seconds']} 秒：{stage['description']}"
                  for stage, interval in zip(stages, timing["intervals"])]
    else:
        steps = [step for step in details.steps if is_known(step)]
        action = [_sentence("动作：" + "；".join(steps))] if steps else []
    voices = [f"{item.get('speaker_name') or '说话人未明确'}"
        f"{('（' + item['parenthetical'] + '）') if item.get('parenthetical') else ''}：“{item['text']}”"
        for item in direction.get("dialogue", [])]
    sounds = direction.get("sound_cues", [])
    if isinstance(sounds, str):
        sounds = [sounds]
    camera = _camera_text(observation, direction)
    video = "\n".join(filter(None, ["【Seedance 剧本视频提示词】", *common, initial,
        _sentence("摄影：" + camera) if camera else "", *action,
        "对白／旁白：" + "；".join(voices) if voices else "",
        "声音：" + "；".join(sounds) if sounds else "",
        _sentence("结束状态：" + details.final_state) if is_known(details.final_state) else "",
        style, f"视频规格：时长 {timing['display_duration_seconds']} 秒；分辨率 {resolution}；画幅 {ratio}。", guard]))
    missing = [name for name, value in (("主体", details.primary_subject),
        ("场景", observation.visual_facts.scene), ("起始状态", details.initial_state),
        ("动作", "；".join(details.steps)), ("结束状态", details.final_state)) if not is_known(value)]
    contract.delivery.update(schema_version="prompt-delivery.script-v4", render_version="script-prompt.v2",
        first_frame_prompt=first, seedance_video_prompt=video,
        creative_assumptions=direction.get("creative_assumptions", []),
        provenance={"timing_origin": "script-performance", "photography_origin": "director-creative-plan",
                    "note": "秒数、运镜与光线为导演创作建议，不是原视频测量事实"},
        video_spec={"duration_seconds": contract.generation_duration_seconds, "aspect_ratio": ratio,
                    "resolution": resolution},
        source_timeline=direction.get('action_timeline', []), display_timing=timing,
        generation_timing_version=TIMING_VERSION,
        render_timeline=[{"start_seconds": stage["start_seconds"], "end_seconds": stage["end_seconds"],
                         "display_start": interval["display_start_seconds"],
                         "display_end": interval["display_end_seconds"]} for stage, interval in zip(stages, timing["intervals"])])
    for prompt, template, text in zip(prompts, ("script-first-frame-v2", "seedance-script-v2"), (first, video)):
        metadata = profile(template)
        prompt.template_id, prompt.template_version, prompt.version = metadata.id, metadata.version, metadata.version
        prompt.positive_prompt = text
        prompt.warnings.extend(f"剧本{name}未知，需复核" for name in missing)
        if prompt.warnings:
            prompt.status = "warning"
        attach_prompt_lint(prompt)
    return prompts
