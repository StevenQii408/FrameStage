"""Per-shot conservative style resolution using already acquired visual evidence."""
from pydantic import ValidationError
from .models import InterpretationClaim, StyleProfileV3, render_profile, visible_clauses, RENDERER_VERSION
from .library import positive_mention
from .reliability import feature_quality, resolution, warn_partial, usable_baseline, VERSION as RELIABILITY_VERSION


def medium_families(text):
    """Return affirmed medium families; mixed media must not force a single family."""
    families = set()
    for family, words in (
        ('paper', ('剪纸', '纸片', '拼贴')), ('clay', ('黏土', '粘土', '手塑')),
        ('painting', ('水彩', '油画', '水墨')),
        ('3d', ('三维', '3D', '3d', 'CGI', 'CG动画')),
        ('2d', ('二维', '2D', '2d', '赛璐璐', '手绘动画', '扁平', '矢量', '像素')),
        ('photo', ('真人摄影', '实拍', '写实摄影', '纪实摄影', '摄影')),
    ):
        if any(positive_mention(text, word) for word in words):
            families.add(family)
    return frozenset(families)


def medium_family(text):
    """Compatibility API: an unambiguous family, or unknown for mixed/absent evidence."""
    families = medium_families(text)
    return next(iter(families)) if len(families) == 1 else ''


def form_family(text):
    families = [name for name, words in (
        ('angular', ('棱面', '低多边形', '三角面', '尖锐几何', '棱角化')),
        ('round', ('圆润', '圆弧', '流线', '曲线主导')))
        if any(positive_mention(text, word) for word in words)]
    return families[0] if len(families) == 1 else ''


def build_scene_style_overrides(shots, baseline):
    """Read-only scene review candidates from existing evidence, never new recipes.

    Only clear medium/form contradictions are classified. Color, clothing,
    location and isolated effects cannot become a scene recipe by counting words.
    The preflight profile and all already delivered prompts remain untouched.
    """
    result = {'version': 'scene-style-review.v1', 'baseline_hash': baseline.profile_hash if baseline else '',
              'scene_overrides': [], 'pending_shot_changes': [], 'extra_model_calls': 0}
    if not baseline:
        return result
    baseline_text = lambda dimension: '；'.join(d.professional_description for d in baseline.descriptions
                                               if d.dimension == dimension)
    scenes = {}
    for shot in shots:
        if not shot.scene_group_id or shot.visual_observation_v2 is None:
            continue
        scenes.setdefault(shot.scene_group_id, []).append(shot)
    for dimension, field_name, classify in (
            ('medium_rendering', 'medium', medium_family), ('form_language', 'character_design', form_family)):
        base = classify(baseline_text(dimension))
        if not base:
            continue
        for scene_id, members in scenes.items():
            observed = []
            for shot in members:
                field = getattr(shot.visual_observation_v2.artistic_effects, field_name)
                text = '，'.join(visible_clauses(field.value)) if field.observable else ''
                family = classify(text)
                refs = [ref for ref in field.evidence if shot.start_time <= ref.timestamp < shot.end_time]
                if field.observable and field.confidence >= .7 and family and refs:
                    observed.append((shot, field, family, refs))
            alternatives = sorted({row[2] for row in observed if row[2] != base})
            for family in alternatives:
                supporters = [row for row in observed if row[2] == family]
                refs = [dict(source_shot_id=shot.id, **ref.model_dump(mode='json'))
                        for shot, _, _, evidence in supporters for ref in evidence]
                item = {'scene_group_id': scene_id, 'dimension': dimension, 'status': 'review_required',
                        'baseline_family': base, 'observed_family': family,
                        'supporting_shot_ids': [row[0].id for row in supporters],
                        'observable_shot_ids': [row[0].id for row in observed], 'evidence': refs,
                        'description': f'本场次已保存的{len(supporters)}个镜头显示与基础画风不同的'
                                       + ('媒介' if dimension == 'medium_rendering' else '造型语言')
                                       + '；确认前不改写全局画风或已有提示词。'}
                if len(supporters) >= 2 and len(supporters) / len(observed) >= .7:
                    result['scene_overrides'].append(item)
                else:
                    result['pending_shot_changes'].append(item)
    return result


def resolve_shot_style(baseline, observation, shot_id):
    """No extra request for ordinary light/color changes. Known medium changes isolate the shot."""
    record = observation.perception_record
    if not usable_baseline(baseline):
        baseline = None
    local = []
    evidence = {}
    if record:
        art = record.observation.artistic_effects
        for field, dimension in [('medium', 'medium_rendering'), ('character_design', 'form_language'),
                                 ('color_palette', 'color_tone'), ('rendering', 'lighting_shading'),
                                 ('texture', 'texture_material')]:
            value = getattr(art, field)
            if value.observable and value.confidence >= .5 and value.evidence:
                key = shot_id + ':' + field
                visible = '，'.join(visible_clauses(value.value))
                if not visible:
                    continue
                try:
                    item = InterpretationClaim(dimension=dimension, core_concept=dimension,
                        professional_description=visible, support_claim_ids=[key])
                except ValidationError:
                    observation.analysis_warnings.append(f'本镜画风 {field} 字段未通过转换，其他视觉结果已保留')
                    continue
                evidence[key] = [ref.model_dump() for ref in value.evidence]
                local.append(item)
    base_medium = '; '.join(d.professional_description for d in baseline.descriptions
                           if d.dimension == 'medium_rendering') if baseline else ''
    local_medium = next((d.professional_description for d in local if d.dimension == 'medium_rendering'), '')
    conflict = bool(medium_family(base_medium) and medium_family(local_medium)
                    and medium_family(base_medium) != medium_family(local_medium))
    base_form = ' '.join(d.professional_description for d in baseline.descriptions
                        if d.dimension == 'form_language') if baseline else ''
    local_form = next((d.professional_description for d in local if d.dimension == 'form_language'), '')
    form_conflict = bool(form_family(base_form) and form_family(local_form)
        and form_family(base_form) != form_family(local_form)
        and medium_family(base_medium) == medium_family(local_medium) and medium_family(local_medium))
    if local and (conflict or form_conflict or not baseline or not baseline.descriptions):
        if form_conflict and baseline:
            # Only unobserved dimensions inherit; changed shape must never retain the named anchor.
            dimensions = {d.dimension for d in local}
            local = [d.model_copy(deep=True) for d in baseline.descriptions if d.dimension not in dimensions] + local
        profile = StyleProfileV3(descriptive_name='本镜可见画风', descriptions=local, application_status='partial',
            provenance={'strategy': 'qwen-direct-v3', 'renderer_version': RENDERER_VERSION,
                'resolution': 'local-medium-change' if conflict else 'local-form-change' if form_conflict else 'local-evidence-fallback',
                'baseline_hash': baseline.profile_hash if baseline else '', 'shot_id': shot_id,
                'evidence': evidence})
        render_profile(profile)
        profile.rendered_prompt = profile.rendered_prompt.replace('整体画面风格：', '本镜基础视觉观察（非完整画风）：', 1)
        profile.rendered_prompt = '画风分析未完成，未确定具体风格。\n' + profile.rendered_prompt
        profile.provenance['delivery_status'] = 'basic-observation-only'
        observation.analysis_warnings.append('画风分析未完成；本镜仅保留基础视觉观察，不代表已确定具体风格')
        profile.provenance.update(resolution_status='partial', reliability_version=RELIABILITY_VERSION,
            quality_state={'structure': 'valid', 'observation': 'available', 'interpretation': 'partial',
                           'style': 'partial', 'anchor': 'unknown', 'specificity': feature_quality(local)})
        profile.profile_hash = profile.digest()
        warn_partial(observation)
        if conflict or form_conflict:
            observation.analysis_warnings.append('本镜媒介或造型与项目基础画风不同，已采用本镜证据，未改写其他镜头')
        return profile
    if baseline and baseline.descriptions:
        # A copy keeps the frozen project state intact. Scene regrouping never mutates old prompts.
        profile = baseline.model_copy(deep=True)
        profile.provenance['resolution'] = 'inherited-baseline'
        profile.provenance['baseline_hash'] = baseline.profile_hash
        profile.provenance['resolution_status'] = resolution(profile)
        if resolution(profile) != 'resolved':
            warn_partial(observation, profile)
        profile.profile_hash = profile.digest()
        return profile
    observation.analysis_warnings.append('画风分析未完成，缺少可用画风证据；本镜具体风格未知，不套用默认风格')
    return None
