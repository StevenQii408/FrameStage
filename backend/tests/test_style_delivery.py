import json
import threading
import time
from types import SimpleNamespace

import pytest

from app.style_direct.anchors import catalog, candidates
from app.style_direct.inheritance import resolve_shot_style
from app.style_direct.models import InterpretationClaim, StyleAnchor, StyleProfileV3, render_profile, safe_visual_text
from app.style_direct.service import frame_quality
from app.services.video_processor import DetectedShot, ExtractedFrame, VideoProcessResult
from app.perception_models import EvidenceRef, StaticObservationV2, MotionObservationV2, empty_stage, merge_stages, PerceptionRecord, ObservedField
from test_style_direct import claim, evidence, interpretation, pipeline, image_bytes, client
from test_prompt_profiles import observation


def base():
    return render_profile(StyleProfileV3(descriptions=[InterpretationClaim(dimension='medium_rendering',
        core_concept='动画媒介', professional_description='三维角色动画，平滑体积与柔和光影', support_claim_ids=['E1'])]))


def test_anchor_catalog_covers_non_animation_and_has_contrast_cues():
    terms = catalog()['terms']
    assert len(terms) >= 20
    assert len([t for t in terms if t['family'] == 'photo']) >= 8
    assert all(t['signature_groups'] and t['insufficient_evidence'] and t['distinction'] for t in terms)
    assert len({t['id'] for t in terms}) == len(terms)


@pytest.mark.parametrize('bad', ['made-up-studio', 'disney-modern-3d'])
def test_unverified_anchors_never_enter_facts_or_generated_text(tmp_path, bad):
    b = interpretation()
    b['anchor'] = dict(id=bad, confidence=.7, support_claim_ids=['C1','C2'], distinguishing_claim_ids=['C2'], reason='只有大眼和三维，无法区分工作室')
    p, _ = pipeline(tmp_path, [evidence(), b])
    result = p.run()
    assert result['profile']['anchor'] is None
    assert '迪士尼' not in result['profile']['rendered_prompt']


def test_evidence_supported_anchor_is_separate_from_observations(tmp_path):
    # Synthesized provider data tests the structural gate, not real Disney recognition.
    from app.style_direct.library import runtime_library
    library = runtime_library()
    recipe = next(r for r in library.recipes if r.id == 'disney-modern-3d')
    term_map = {t.id: t for t in library.terms}
    actual = [claim('C' + str(i + 1), group.dimension,
              '，'.join(cues[0] for t in group.term_ids for cues in term_map[t].positive_cues))
              for i, group in enumerate(recipe.signature_groups)]
    if not any(c['dimension'] == 'medium_rendering' for c in actual):
        actual.append(claim('CM', 'medium_rendering', '三维角色动画，轮廓体积连续'))
    for c in actual:
        if c['dimension'] == 'medium_rendering': c['observation'] = '三维角色动画，' + c['observation']
    a = {'claims': actual, 'gaps': [], 'uncertainties': []}
    b = {'descriptive_name': '具体流线三维参考', 'descriptions': [
        dict(dimension=c['dimension'], core_concept=c['observation'], professional_description=c['observation'],
             support_claim_ids=[c['id']], scope='reference') for c in actual],
         'generation_constraints': [], 'uncertainties': []}
    ids = [c['id'] for c in actual]
    b['anchor'] = dict(id='disney-modern-3d', confidence=.9, support_claim_ids=ids,
        distinguishing_claim_ids=ids[:2], reason='具体跨维度特征关系共同支持该视觉参考分支',
        feature_support=[dict(group_id=g.id, support_claim_ids=[actual[i]['id']])
                         for i, g in enumerate(recipe.signature_groups)])
    p, _ = pipeline(tmp_path, [a,b])
    result = p.run()
    assert result['profile']['anchor']['id'] == 'disney-modern-3d'
    assert result['profile']['rendered_prompt'].startswith('整体画面风格：迪士尼式')
    assert '迪士尼' not in json.dumps(result['evidence'], ensure_ascii=False)


def test_compact_output_keeps_detail_elsewhere_without_photography_or_midword_cut():
    p = base()
    p.descriptions.append(InterpretationClaim(dimension='form_language', core_concept='圆润造型',
        professional_description='圆润流畅的角色轮廓，大头大眼比例，强调表情传达而非解剖精确',
        concise_phrase='圆润轮廓与大头大眼比例', support_claim_ids=['E2']))
    p.descriptions.append(InterpretationClaim(dimension='lighting_shading', core_concept='光影',
        professional_description='柔和明暗过渡，背景景深虚化，焦点集中在人物面部',
        concise_phrase='柔和明暗过渡', support_claim_ids=['E3']))
    render_profile(p)
    assert len(p.rendered_prompt.removeprefix('整体画面风格：')) <= 100
    assert '焦点' not in p.rendered_prompt and '景深' not in p.rendered_prompt
    assert '强调表情' in p.detailed_prompt
    assert '圆润流畅的角色轮廓' in p.rendered_prompt and '大头大眼比例' in p.rendered_prompt


def test_short_features_never_erase_medium_or_turn_into_bare_keywords():
    p = base()
    p.descriptions[0].concise_phrase = '平滑表面，柔和光影'
    p.descriptions.append(InterpretationClaim(dimension='form_language', core_concept='造型',
        professional_description='大眼与简化五官', concise_phrase='大眼比例与简化五官', support_claim_ids=['E2']))
    render_profile(p)
    assert '三维角色动画' in p.rendered_prompt
    assert '平滑表面' not in p.rendered_prompt  # Generic medium prose must not displace form controls.
    assert '大眼比例与简化五官' in p.rendered_prompt


def test_photo_and_animation_anchor_candidates_cannot_cross_clear_media():
    selected = candidates([SimpleNamespace(dimension='medium_rendering', observation='真人摄影'),
        SimpleNamespace(dimension='color_tone', observation='自然光、宽景深、柔和明暗与日常数字影像')])
    assert selected and all(t['family'] == 'photo' for t in selected)


def test_english_observations_recall_chinese_anchors_without_changing_evidence():
    facts = [SimpleNamespace(dimension='medium_rendering', observation='3D computer-generated animation, smooth volumetric surfaces'),
        SimpleNamespace(dimension='form_language', observation='Stylized with exaggerated features, large eyes and expressive faces'),
        SimpleNamespace(dimension='texture_material', observation='Material rendering with controlled highlights')]
    original = [c.observation for c in facts]
    selected = candidates(facts)
    assert selected and all(not t['selection_eligible'] for t in selected)
    assert all(t['family'] == '3d' for t in selected)
    assert [c.observation for c in facts] == original


def test_sparse_same_medium_review_does_not_force_anchor_or_cross_media():
    selected = candidates([SimpleNamespace(dimension='medium_rendering', observation='3D animation')])
    assert selected and all(t['family'] == '3d' for t in selected)
    assert all(t['retrieval_basis'] == 'medium-only-review' for t in selected)
    p = render_profile(StyleProfileV3(descriptions=[InterpretationClaim(dimension='medium_rendering',
        core_concept='三维', professional_description='三维动画', support_claim_ids=['E1'])]))
    assert p.anchor is None and '迪士尼' not in p.rendered_prompt


def test_english_photography_and_counter_cues_are_respected():
    selected = candidates([SimpleNamespace(dimension='medium_rendering', observation='Live-action real photography'),
        SimpleNamespace(dimension='lighting_shading', observation='Natural light, gentle shadows, deep focus')])
    assert selected and all(t['family'] == 'photo' for t in selected)
    selected = candidates([SimpleNamespace(dimension='medium_rendering', observation='3D animation'),
        SimpleNamespace(dimension='form_language', observation='Faceted polygonal geometric forms, smooth curved surfaces')])
    # Mixed curved/faceted words may be reviewed, but cannot establish a signature.
    assert not next(t for t in selected if t['id'] == 'low-poly-3d')['selection_eligible']
    selected = candidates([SimpleNamespace(dimension='medium_rendering',
        observation='3D animation，曲面亮度完全平滑连续')])
    assert 'low-poly-3d' not in {t['id'] for t in selected}


def local_observation(medium, *, frames=()):
    """A synthetic successful shot observation, not merely a style-only record.

    Pipeline callers supply their actual source frames. The v2 observation and
    its v1 compatibility projection must describe the same visible content.
    """
    frames = frames or [SimpleNamespace(id='frame-1', timestamp=1.0)]
    first, last = frames[0], frames[-1]
    def observed(value, frame, description):
        return dict(value=value, observable=True, confidence=.9,
            evidence=[dict(frame_id=frame.id, timestamp=float(frame.timestamp), description=description)], uncertainties=[])
    obs = observation()
    static = empty_stage(StaticObservationV2, '其他维度未知')
    static.artistic_effects.medium = ObservedField[str](**observed(medium, first, '媒介可见'))
    static.facts.primary_subject = ObservedField[str](**observed(obs.action_details.primary_subject, first, '女孩与手持书本可见'))
    static.facts.people = ObservedField[list[str]](**observed([obs.action_details.primary_subject], first, '来源画面中的女孩'))
    static.facts.objects = ObservedField[list[str]](**observed(['书', '木桌'], first, '来源画面中的书与木桌'))
    motion = empty_stage(MotionObservationV2, '其他动态维度未知')
    for name, value, frame in (
        ('subject_action', obs.subject_action, last),
        ('initial_state', obs.action_details.initial_state, first),
        ('final_state', obs.action_details.final_state, last),
        ('interaction_target', obs.action_details.interaction_target, last),
    ):
        setattr(motion.dynamics, name, ObservedField[str](**observed(value, frame, value)))
    motion.dynamics.action_steps = ObservedField[list[str]](**observed(obs.action_details.steps, last, obs.subject_action))
    motion.dynamics.step_evidence = [[EvidenceRef(**ref) for ref in observed(step, frame, step)['evidence']]
        for step, frame in zip(obs.action_details.steps, (first, last))]
    obs.perception_record = PerceptionRecord(observation=merge_stages(static, motion))
    return obs


def test_local_medium_change_isolated_and_no_request_needed():
    baseline = base()
    original = baseline.model_dump()
    resolved = resolve_shot_style(baseline, local_observation('真人摄影'), 'shot-2')
    assert resolved.provenance['resolution'] == 'local-medium-change'
    assert '真人摄影' in resolved.rendered_prompt and '三维' not in resolved.rendered_prompt
    assert baseline.model_dump() == original and resolved.anchor is None
    inherited = resolve_shot_style(baseline, local_observation('三维动画'), 'shot-3')
    assert inherited.rendered_prompt == baseline.rendered_prompt
    assert baseline.model_dump() == original


def test_unknown_baseline_uses_known_shot_facts_not_default_style():
    result = resolve_shot_style(None, local_observation('手工剪纸动画'), 'shot-1')
    assert '剪纸' in result.rendered_prompt
    assert result.provenance['resolution'] == 'local-evidence-fallback'


@pytest.mark.parametrize('short', ['暖色调', '柔光', '3D'])
def test_short_legal_style_fields_do_not_fail_successful_shots(short):
    obs = local_observation('3D动画')
    obs.perception_record.observation.artistic_effects.color_palette = ObservedField[str](
        value=short, observable=True, confidence=.9,
        evidence=[dict(frame_id='frame-1', timestamp=1.0, description='色彩可见')], uncertainties=[])
    resolved = resolve_shot_style(base(), obs, 'shot-1')
    assert resolved and resolved.provenance['resolution'] == 'inherited-baseline'
    assert InterpretationClaim(dimension='color_tone', core_concept='色彩',
        professional_description=short, support_claim_ids=['E1']).professional_description == short


def test_invalid_local_style_field_is_isolated_and_other_fields_survive():
    obs = local_observation('真人摄影')
    obs.perception_record.observation.artistic_effects.color_palette = ObservedField[str](
        value='冷' * 301, observable=True, confidence=.9,
        evidence=[dict(frame_id='frame-1', timestamp=1.0, description='色彩可见')], uncertainties=[])
    resolved = resolve_shot_style(base(), obs, 'shot-1')
    assert '真人摄影' in resolved.rendered_prompt
    assert any('color_palette' in w for w in obs.analysis_warnings)


@pytest.mark.parametrize('text', ['fabric weaves', 'detailed hair strands', 'glass of juice',
    '木桌上的果汁', '发丝与织物纹理', 'subsurface scattering', 'Pixar animation'])
def test_bilingual_content_and_techniques_are_not_style_facts(text):
    assert not safe_visual_text(text)


def test_english_partial_repair_keeps_independent_visible_clauses(tmp_path):
    a = evidence()
    a['claims'].append(claim('E4', 'texture_material',
        'Directional paper fibers, fabric weaves, subsurface scattering. Visible cut edges.'))
    p, provider = pipeline(tmp_path, [a, interpretation()])
    result = p.run()
    repaired = next(c['observation'] for c in result['evidence']['claims'] if c['id'] == 'E4')
    assert 'Directional paper fibers' in repaired and 'Visible cut edges' in repaired
    assert safe_visual_text(repaired) and len(provider.calls) == 2


def test_generic_texture_words_never_replace_concrete_controls():
    p = base()
    p.descriptions.append(InterpretationClaim(dimension='texture_material', core_concept='纹理',
        professional_description='表面纹理频率较低，保留不规则细颗粒',
        concise_phrase='高保真纹理，细节丰富', support_claim_ids=['E2']))
    render_profile(p)
    assert '高保真' not in p.rendered_prompt and '细节丰富' not in p.rendered_prompt
    assert '不规则细颗粒' in p.detailed_prompt


def test_local_shape_change_drops_anchor_without_mutating_baseline():
    baseline = base()
    baseline.descriptions.append(InterpretationClaim(dimension='form_language', core_concept='圆润造型',
        professional_description='圆润流畅的轮廓', support_claim_ids=['E2']))
    baseline.anchor = StyleAnchor(id='disney-modern-3d', confidence=.9,
        support_claim_ids=['E1', 'E2'], distinguishing_claim_ids=['E2'],
        reason='测试已验证的三维流线造型生成参考')
    render_profile(baseline)
    original = baseline.model_dump()
    obs = local_observation('三维动画')
    obs.perception_record.observation.artistic_effects.character_design = ObservedField[str](
        value='低多边形棱面与尖锐几何轮廓', observable=True, confidence=.9,
        evidence=[dict(frame_id='frame-1', timestamp=1.0, description='棱面可见')], uncertainties=[])
    result = resolve_shot_style(baseline, obs, 'shot-2')
    assert result.provenance['resolution'] == 'local-form-change'
    assert '低多边形' in result.rendered_prompt and '圆润' not in result.rendered_prompt
    assert result.anchor is None and '迪士尼' not in result.rendered_prompt and obs.analysis_warnings
    assert baseline.model_dump() == original


def test_frame_quality_is_computed_on_actual_pixels(tmp_path):
    from PIL import Image, ImageDraw
    plain = tmp_path / 'plain.png'; plain.write_bytes(image_bytes())
    patterned = Image.new('RGB', (160,240), 'white')
    draw = ImageDraw.Draw(patterned)
    for i in range(0,160,8):
        draw.rectangle((i,0,i+3,239), fill='black')
    pattern = tmp_path / 'pattern.png'; patterned.save(pattern)
    assert frame_quality(pattern)[0] > frame_quality(plain)[0]


def test_first_prompt_ready_while_second_shot_blocked_and_never_rewritten(client, tmp_path, monkeypatch):
    service = client.app.state.job_service
    path = service.settings.data_dir / 'frame.png'; path.write_bytes(image_bytes())
    shots = [DetectedShot(1,0,2), DetectedShot(2,2,4)]
    frames = [ExtractedFrame(f'frame-{i}-1', f'shot-{i:03d}', float(i), str(path), str(path), 160,240) for i in (1,2)]
    monkeypatch.setattr('app.services.job_service.process_video', lambda *args: VideoProcessResult({'duration':4}, shots, frames, []))
    entered, release = threading.Event(), threading.Event()
    calls = []
    original_style, original_shot = service.provider.analyze_style, service.provider.analyze_shot
    def style(*args, **kwargs):
        calls.append('style'); return original_style(*args, **kwargs)
    def shot(frames, context):
        calls.append('shot'+str(context.number))
        if context.number == 2:
            entered.set()
            assert release.wait(5)
        return original_shot(frames, context)
    monkeypatch.setattr(service.provider, 'analyze_style', style)
    monkeypatch.setattr(service.provider, 'analyze_shot', shot)
    monkeypatch.setattr(service.provider, 'summarize_video', lambda *args: pytest.fail('redundant overview request'))
    job = client.post('/api/jobs', files={'video':('v.mp4',b'local-mock','video/mp4')}).json()['job_id']
    try:
        assert entered.wait(5)
        result = client.get(f'/api/jobs/{job}/result').json()
        assert result['job']['status'] != 'completed'
        assert len(result['prompts']) == 2
        first = {p['id']: p['positive_prompt'] for p in result['prompts']}
        assert all('参考画风尚未分析' not in text and '整体画面风格：' in text for text in first.values())
        assert all(p['contract']['delivery']['style_provenance']['resolution'] == 'inherited-baseline' for p in result['prompts'])
        assert calls == ['style','style','shot1','shot2']
    finally:
        release.set()
    for _ in range(100):
        state = client.get(f'/api/jobs/{job}').json()
        if state['status'] in ('completed','failed'):
            break
        time.sleep(.01)
    assert state['status'] == 'completed', state
    final = client.get(f'/api/jobs/{job}/result').json()
    assert all(p['positive_prompt'] == first[p['id']] for p in final['prompts'] if p['id'] in first)
    assert len(final['prompts']) == 4 and calls.count('style') == 2
    assert all(f['blur_score'] is not None for f in final['frames'])


@pytest.mark.parametrize('mode', ['unexpected-error', 'short-palette'])
def test_style_boundary_preserves_perception_and_two_prompts(client, tmp_path, monkeypatch, mode):
    service = client.app.state.job_service
    path = service.settings.data_dir / 'frame.png'; path.write_bytes(image_bytes())
    frames = [ExtractedFrame('frame-1-1', 'shot-001', 1.0, str(path), str(path), 160,240)]
    monkeypatch.setattr('app.services.job_service.process_video', lambda *args: VideoProcessResult(
        {'duration':2}, [DetectedShot(1,0,2)], frames, []))
    def observed(provider_frames, context):
        obs = local_observation('三维动画', frames=provider_frames)
        obs.perception_record.observation.artistic_effects.color_palette = ObservedField[str](
            value='暖色调', observable=True, confidence=.9,
            evidence=[dict(frame_id=provider_frames[0].id, timestamp=float(provider_frames[0].timestamp), description='整体暖调可见')], uncertainties=[])
        return obs
    monkeypatch.setattr(service.provider, 'analyze_shot', observed)
    def broken_style(*args):
        raise ValueError('optional style conversion failed')
    if mode == 'unexpected-error':
        monkeypatch.setattr('app.services.job_service.resolve_shot_style', broken_style)
    job = client.post('/api/jobs', files={'video':('v.mp4',b'local-mock','video/mp4')}).json()['job_id']
    for _ in range(150):
        state = client.get(f'/api/jobs/{job}').json()
        if state['status'] in ('completed','failed'):
            break
        time.sleep(.01)
    assert state['status'] == 'completed', state
    result = client.get(f'/api/jobs/{job}/result').json()
    assert result['shots'][0]['status'] == 'warning'
    assert result['shots'][0]['action'] != '未知'
    assert len(result['prompts']) == 2
    saved = service.db.fetchone('SELECT observation_json,perception_json FROM shots WHERE job_id=?', (job,))
    assert saved['observation_json'] and saved['perception_json']
    perception = json.loads(saved['perception_json'])['observation']
    assert perception['facts']['primary_subject']['observable']
    assert perception['dynamics']['subject_action']['observable']
    assert perception['dynamics']['subject_action']['value'] == result['shots'][0]['action']
    for field in (perception['facts']['primary_subject'], perception['dynamics']['subject_action']):
        assert all(ref['frame_id'] == result['frames'][0]['id'] and ref['timestamp'] == 1.0 for ref in field['evidence'])
    if mode == 'unexpected-error':
        assert any('画风转换' in w for w in result['shots'][0]['analysis_warnings'])
    else:
        assert all('画风转换' not in w for w in result['shots'][0]['analysis_warnings'])
        assert all('暖色调' in p['positive_prompt'] for p in result['prompts'])
