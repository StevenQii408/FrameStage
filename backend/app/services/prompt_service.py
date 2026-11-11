from __future__ import annotations

from typing import List

from app.prompt_profiles import action_rules, profile, style_rules
from app.schemas import PromptBundle, ShotContract, VisionObservation
from app.services.style_service import StyleProfile
from app.style_ontology.catalog import terms
from app.services.action_presentation import build_action_presentation, render_action_lines
from app.services.prompt_delivery import attach_prompt_lint, clean_join, display_timing, is_known
from app.services.generation_timing import VERSION as TIMING_VERSION, ceil_half_seconds


NEGATIVE = "模糊，低分辨率，水印，文字覆盖，logo，畸形面部，多余手指，手部变形，几何形体融化，主体漂移"


def _known(value: str) -> bool:
    text = str(value or "").strip().strip("。； ")
    return is_known(text) and not text.startswith(("待人工", "无法确认"))


def _known_style(value: str) -> bool:
    text = str(value or "").strip()
    for prefix in ("整体画面风格：", "本镜可见画风："):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return _known(text) and not text.startswith(("项目画风待确认", "画风待人工确认", "画风分析未完成"))


def _contract(observation: VisionObservation) -> ShotContract:
    details = observation.action_details
    first_state = details.initial_state if _known(details.initial_state) else "首帧状态待人工设定，不能使用后续动作结果代替"
    end_state = details.final_state if _known(details.final_state) else "结束状态待人工设定"
    anchors = [
        value for value in (
            details.primary_subject,
            details.interaction_target if details.interaction_target != "无" else "",
            observation.lighting_vibe,
            observation.composition,
        ) if _known(value)
    ]
    return ShotContract(
        purpose="忠实复现当前镜头的主体、动作与空间关系",
        first_frame_state=first_state,
        primary_action=observation.subject_action,
        end_state=end_state,
        continuity_anchors=anchors,
        shot_size=observation.shot_size,
        lens=observation.lens,
        camera_movement=observation.camera_movement,
        lighting=observation.lighting_detail or observation.lighting_vibe,
        composition=observation.composition,
    )


def _camera_sentence(contract: ShotContract, include_movement: bool = True) -> str:
    parts = []
    if _known(contract.shot_size):
        parts.append(contract.shot_size)
    if _known(contract.lens):
        parts.append(f"{contract.lens}焦段")
    if include_movement and _known(contract.camera_movement):
        if "复合运动" in contract.camera_movement or "切换" in contract.camera_movement:
            # Keep the unresolved source camera in the contract and warning,
            # rather than copying an editing instruction into the prompt.
            pass
        else:
            parts.append(contract.camera_movement)
    return "，".join(parts)


def _scene_sentence(observation: VisionObservation, contract: ShotContract) -> str:
    spatial = observation.spatial_evidence
    fields = (
        ("场景", observation.visual_facts.scene),
        ("前景", spatial.foreground), ("中景", spatial.midground),
        ("背景", spatial.background), ("空间关系", spatial.relationships),
        ("光线", contract.lighting),
    )
    known_fields = [f"{label}：{value.rstrip('。； ')}" for label, value in fields if _known(value)]
    return "；".join(known_fields) + "。" if known_fields else ""


def _camera_line(contract: ShotContract, *, include_movement: bool) -> str:
    parts = []
    if _known(contract.aspect_ratio):
        parts.append(f"画幅：{contract.aspect_ratio}")
    camera = _camera_sentence(contract, include_movement=include_movement)
    if camera:
        parts.append(f"镜头：{camera}")
    if _known(contract.composition):
        parts.append(f"构图：{contract.composition}")
    return "；".join(parts) + "。" if parts else ""


def build_prompts(shot_id: str, observation: VisionObservation, script_text: str = "", *, source_edit_times: List[float] | None = None, generation_duration_seconds: float | None = None, aspect_ratio: str = "待设定", style_profile: StyleProfile | None = None, visual_bible=None, direct_style=None, scene_group_id: str = "", style_strategy: str = "legacy-v1", template_id: str | None = None) -> List[PromptBundle]:
    if observation.script_direction:
        from app.script_director.render import build_script_prompts
        return build_script_prompts(shot_id, observation, generation_duration_seconds=generation_duration_seconds,
            aspect_ratio=aspect_ratio, style_profile=style_profile, visual_bible=visual_bible, direct_style=direct_style,
            template_id=template_id)
    contract = _contract(observation)
    contract.source_edit_times = list(source_edit_times or [])
    contract.generation_duration_seconds = ceil_half_seconds(generation_duration_seconds if generation_duration_seconds is not None else 4.5)
    contract.aspect_ratio = aspect_ratio
    rules = style_rules()
    contract.style_profile_version = rules["schema_version"]
    style = observation.visual_style_evidence
    style_parts = [("媒介", style.medium), ("角色与表情", style.character_design),
                   ("渲染与光影", style.rendering), ("材质", style.texture), ("色彩", style.color_palette)]
    contract.observed_style = "；".join(f"{label}：{value}" for label, value in style_parts if _known(value)) or "画风待人工确认，不套用默认写实风格"
    if style.visible_effects:
        contract.observed_style += "；可见特效：" + "、".join(style.visible_effects)
    if style_strategy == "vector-v2" and visual_bible is None:
        contract.observed_style = "项目画风待确认"
        contract.style_profile_version = "style-vector.v2:draft"
    if style_strategy == "qwen-direct-v3":
        contract.observed_style = direct_style.rendered_prompt if direct_style else "画风分析未完成；具体风格未知，不套用默认风格"
        contract.style_profile_version = f"style-profile.v3:{direct_style.version}" if direct_style else "style-profile.v3:pending"
        contract.style_profile_hash = direct_style.profile_hash if direct_style else ""
    if visual_bible and visual_bible.status == "confirmed":
        contract.observed_style = visual_bible.rendered_global_prompt
        contract.style_profile_version = f"{visual_bible.schema_version}:{visual_bible.version}"
        contract.style_profile_hash = visual_bible.bible_hash
    if style_profile and style_profile.status == "confirmed":
        contract.observed_style = style_profile.rendered_style_prompt
        contract.style_profile_version = f"{style_profile.schema_version}:{style_profile.version}"
        contract.style_profile_hash = style_profile.profile_hash
    if direct_style:
        # Supplying an explicit style profile is authoritative regardless of a
        # caller's default strategy; body and contract must refer to one style.
        contract.observed_style = direct_style.rendered_prompt
        contract.style_profile_version = f"style-profile.v3:{direct_style.version}"
        contract.style_profile_hash = direct_style.profile_hash
    focal = rules["creative_focal_lengths_mm"].get(contract.lens)
    contract.generation_suggestions = {
        "focal_length": f"{focal}mm（生成建议，不是原片实测）" if focal else "毫米焦段待设定，原片无法实测",
        "sound": rules["sound_policy"],
        "quality": "保持所观察的媒介、角色造型、表情、光影与材质；不添加风格化特效",
    }
    if contract.source_edit_times:
        contract.editorial_note = "源片有普通角度剪辑切点；生成时将连续动作重构为一个统一视觉镜头，不宣称源片没有切镜"
    details = observation.action_details
    scene_line = _scene_sentence(observation, contract)
    focal_line = contract.generation_suggestions["focal_length"]
    scene_style = ""
    shot_style_line = ""
    if direct_style:
        style_line = direct_style.rendered_prompt
        scene_style = direct_style.scene_prompts.get(scene_group_id, "")
        if scene_style:
            style_line += "\n" + scene_style
        local_style_line = ""
        # Historical constraints remain in the saved profile for auditing, but
        # delivery contains one observed style paragraph, not duplicate imperatives.
        style_avoid_line = ""
        recipe_constraint_line = ""
    elif style_strategy == "qwen-direct-v3":
        style_line = contract.observed_style + "。"
        local_style_line = style_avoid_line = recipe_constraint_line = ""
    elif visual_bible and visual_bible.status == "confirmed":
        from app.style_vector.models import resolve_effective
        effective = resolve_effective(visual_bible, shot_id, scene_group_id)
        scene_style = effective["scene"]
        shot_style_line = effective["shot"]
        style_line = "\n".join(value for value in (effective["global"], effective["scene"], effective["shot"]) if value)
        local_style_line = effective["effect"]
        style_avoid_line = effective["avoid"]
        recipe_constraint_line = effective["recipe_constraint"]
    elif style_strategy == "vector-v2":
        style_line = "项目画风待确认。"
        local_style_line = ""
        style_avoid_line = ""
        recipe_constraint_line = ""
    else:
        style_line = contract.observed_style if style_profile and style_profile.status == "confirmed" else f"整体画面风格：{contract.observed_style}。"
        local_effects = style_profile.per_shot_effect_overrides.get(shot_id, []) if style_profile and style_profile.status == "confirmed" else []
        local_style_line = "局部视觉效果：" + "、".join(terms()[term_id].generation_phrase for term_id in local_effects) + "。" if local_effects else ""
        style_avoid_line = ""
        recipe_constraint_line = ""
    style_pending = not _known_style(contract.observed_style)
    if style_pending:
        # Pending style is a review state, not a visual generator instruction.
        # A partial profile may also contain separately scoped, valid low-level
        # observations. Keep those paragraphs and their scope qualification.
        style_line = clean_join(line for line in style_line.splitlines() if _known_style(line))
    image_guard = "不生成文字、字幕、Logo或水印。"
    first_frame = clean_join([
        "【首帧图片提示词】", f"主体：{details.primary_subject}。" if _known(details.primary_subject) else "", scene_line,
        _camera_line(contract, include_movement=False),
        f"起始画面：{details.initial_state}。" if _known(details.initial_state) else "",
        style_line, style_avoid_line, image_guard,
    ])
    duration = contract.generation_duration_seconds
    try:
        presentation = build_action_presentation(observation, duration)
    except Exception:
        # Delivery must never invalidate an otherwise successful observation.
        fallback = "；".join(step for step in details.steps if _known(step)) or contract.primary_action
        presentation = dict(schema_version="action-presentation.v1", main_action=fallback,
            accompanying_states=[], phases=[], timing_mode="none", source="source_steps",
            evidence_granularity="field" if details.evidence else "none",
            warnings=["动作聚合未完成，保留来源动作，不补造时段"])
    action_lines = render_action_lines(presentation, compact=True) if presentation["source"] != "unknown" else []
    contract.primary_action = presentation["main_action"]
    timing = display_timing(duration, [(phase["start_seconds"], phase["end_seconds"])
        for phase in presentation["phases"]] if presentation["timing_mode"] == "generation-suggestion" else [])
    sound_details = []
    if "搅拌" in contract.primary_action:
        sound_details += ["勺子轻触杯壁", "液体搅动声"]
    elif "杯" in details.primary_subject + details.interaction_target:
        sound_details += ["握持杯子的细微接触声"]
    if "走" in contract.primary_action:
        sound_details += ["脚步轻响", "衣料摩擦"]
    sound = "、".join(sound_details[:4])
    sound_line = (f"声音：{sound}；" if sound else "声音：") + "无台词、旁白或配乐。"
    seedance = clean_join([
        "【Seedance 中文图生视频提示词】", f"主体：{details.primary_subject}。" if _known(details.primary_subject) else "", scene_line,
        f"起始画面：{details.initial_state}。" if _known(details.initial_state) else "",
        *action_lines,
        f"互动对象：{details.interaction_target}。" if _known(details.interaction_target) and details.interaction_target != "无" else "",
        _camera_line(contract, include_movement=True),
        style_line, local_style_line, style_avoid_line, recipe_constraint_line,
        f"结束画面：{details.final_state}。" if _known(details.final_state) else "",
        f"时长：{timing['display_duration_seconds']}秒。", sound_line,
        "保持人物、服装、道具、空间关系与光线连续，手部与物体接触自然；不添加未观察到的人物、道具或光效。" + image_guard,
    ])
    warnings = list(dict.fromkeys([*observation.analysis_warnings, *presentation["warnings"]]))
    if style_pending:
        warnings.append("整体画面风格待复核，未将未知画风占位写入正文")
    if not _known(details.primary_subject):
        warnings.append("主体身份未知，Prompt 需要人工补充")
    if not _known(contract.primary_action):
        warnings.append("主体动作未知，视频 Prompt 需要人工补充")
    if "待人工" in contract.first_frame_state or "待人工" in contract.end_state:
        warnings.append("首帧或结束状态待人工确认，未用中间帧或剧情推测补齐")
    if "复合运动" in contract.camera_movement or contract.source_edit_times:
        warnings.append("源片包含复合运动或剪辑；生成前需确认单一运镜方案")
    intent = "冻结在本镜首帧可见的起始姿态，后续动作与完成结果只出现在视频提示词。" if _known(details.initial_state) else "起始状态证据不足，必须人工确认；没有用清晰代表帧代替首帧。"
    settings = [f"建议生成时长：{duration:g}秒；画幅：{aspect_ratio}。", "@图片1 仅在实际提供首帧后作为首帧并锁定角色、服装和场景；当前不假称已生成或上传图片。", focal_line, rules["sound_policy"], "声音为生成建议，未分析原片音轨。", "Seedance 使用正文正向护栏，不默认附加独立负面框。"]
    checks = ["起点引用同一镜头合同，首帧不描述后续动作结果。", "动作按已观察顺序展开，起点与结束状态待人工确认项不得跳过。", "角色、服装、道具与方向沿用连续性锚点；跨镜连续性尚需相邻镜头复核。", "焦段、声音与可选阶段秒数是生成建议，不是源片测量事实；未按步骤个数均分。"]
    contract.delivery = {"schema_version": "prompt-delivery.zh-v3", "order": rules["delivery_order"],
                         "delivery_renderer": "source-compact.v1", "display_timing": timing,
                         "action_presentation": presentation,
                         "action_rule_version": action_rules()["schema_version"],
                         "action_source_documents": action_rules()["sources"],
                         "first_frame_intent": intent, "settings": settings, "continuity_checks": checks,
                         "first_frame_prompt": first_frame, "seedance_video_prompt": seedance,
                         "source_documents": rules["sources"], "planning_context": script_text[:800] if script_text else ""}
    # Preserve scope for derived sequence rendering without parsing or flattening
    # the final prose. The global hash alone cannot identify scene/local changes.
    contract.delivery["style_layers"] = {"global": contract.observed_style,
        "scene": scene_style, "shot": shot_style_line, "local_effect": local_style_line,
        "avoid": style_avoid_line, "recipe_constraint": recipe_constraint_line}
    contract.delivery['generation_timing_version'] = TIMING_VERSION
    contract.delivery['analysis_review'] = observation.analysis_review
    if direct_style:
        contract.delivery['style_provenance'] = {'profile_hash': direct_style.profile_hash,
            'version': direct_style.version, 'anchor': direct_style.anchor.model_dump() if direct_style.anchor else None,
            'reference': direct_style.reference.model_dump() if direct_style.reference else None,
            'application_status': direct_style.application_status,
            **direct_style.provenance}
    elif style_strategy == 'qwen-direct-v3':
        contract.delivery['style_provenance'] = {'resolution_status': 'unknown', 'application_status': 'partial',
            'quality_state': {'observation': 'unknown', 'interpretation': 'failed', 'style': 'unknown', 'anchor': 'unknown'}}
    rendered = [
        ("first-frame-v7", "first-frame", first_frame),
        ("seedance-video-v8", "seedance", seedance),
    ]
    bundles = [
        attach_prompt_lint(PromptBundle(id=f"{shot_id}-{suffix}", shot_id=shot_id, prompt_type=metadata.prompt_type, platform=metadata.platform, version=metadata.version, positive_prompt=text, negative_prompt=NEGATIVE if metadata.prompt_type == "first_frame" else "", template_id=metadata.id, template_version=metadata.version, warnings=warnings, contract=contract))
        for profile_id, suffix, text in rendered
        for metadata in [profile(profile_id)]
    ]
    if observation.analysis_review and not observation.analysis_review.get('video_ready', True):
        for bundle in bundles:
            if bundle.prompt_type == 'video':
                bundle.status = 'draft'
                bundle.warnings = list(dict.fromkeys([*bundle.warnings, '动态待复核：已保留静态场景，视频方案尚未就绪']))
                bundle.contract.delivery['video_ready'] = False
    if observation.analysis_review:
        from app.services.delivery_quality import _item_readiness
        for bundle in bundles:
            if bundle.status == 'warning':
                core_ready, style_review = _item_readiness(bundle.model_dump())
                if core_ready:
                    bundle.status = 'ready'
                    bundle.contract.delivery['style_review_needed'] = style_review
    return bundles
