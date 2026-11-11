"""Synthetic protocol replay; these are not human style-accuracy labels."""
from style_protocol_samples import stage_failure
import copy
import json
import sqlite3
from pathlib import Path
import time

import pytest

from app.style_direct.inheritance import resolve_shot_style
from app.style_direct.models import InterpretationClaim, StyleProfileV3, render_profile
from app.style_direct.reliability import feature_quality, resolution, usable_baseline
from app.style_direct.service import draft_project_style, prepare_image
from app.services.prompt_service import build_prompts
from app.services.video_processor import DetectedShot, ExtractedFrame, VideoProcessResult
from evals.replay_style_pipeline import replay_saved
from test_style_direct import evidence, interpretation, pipeline, image_bytes, client
from test_style_delivery import local_observation
from test_prompt_profiles import observation


def saved_failure():
    return stage_failure()


def representatives(tmp_path):
    return [prepare_image(image_bytes(color=(50 + i * 30, 80, 110)), tmp_path / str(i), f'I{i+1}',
                          {'scene_id': 'scene-a', 'timestamp': i * 3.0}) for i in range(3)]


def test_actual_empty_evidence_correction_empty_translation_retains_project_facts(tmp_path):
    p, provider = pipeline(tmp_path, saved_failure()['responses'], scope='project', assets=representatives(tmp_path))
    result = p.run()
    assert len(provider.calls) == 3  # No new retry or cloud request was added to the old failure path.
    assert len(result['evidence']['claims']) == len(result['profile']['descriptions']) == 5
    assert all(d['scope'] == 'global' for d in result['profile']['descriptions'])
    assert result['profile']['application_status'] == 'partial'
    assert result['quality_state']['style'] == 'partial'
    assert result['quality_state']['observation'] == 'available'
    assert result['quality_state']['interpretation'] == 'partial'
    assert result['profile']['anchor'] is None
    assert '头部偏大' in result['profile']['rendered_prompt']
    assert '服装' not in result['profile']['rendered_prompt'] and '衣物' not in result['profile']['rendered_prompt']
    assert result['raw_responses'][-1]['validation_status'] == 'failed'
    assert next(s for s in result['diagnostics']['stages'] if s['name'] == 'interpreting')['status'] == 'failed'
    assert result['diagnostics']['quality']['state'] == result['quality_state']


def test_project_fallback_does_not_elevate_local_fact(tmp_path):
    a = evidence()
    a['claims'][0]['images'] = ['I1', 'I2', 'I3']
    p, _ = pipeline(tmp_path, [a, '[]', '[]'], scope='project', assets=representatives(tmp_path))
    result = p.run()
    assert len(result['evidence']['claims']) == 3
    assert [d['dimension'] for d in result['profile']['descriptions']] == ['medium_rendering']
    assert result['quality_state']['style'] == 'partial'


def test_broad_words_remain_known_but_do_not_claim_resolved_style(tmp_path):
    a = {'claims': [
        {'id': 'C1', 'dimension': 'medium_rendering', 'observation': '3D动画', 'images': ['I1'], 'confidence': .8},
        {'id': 'C2', 'dimension': 'form_language', 'observation': '风格化写实', 'images': ['I1'], 'confidence': .8},
        {'id': 'C3', 'dimension': 'color_tone', 'observation': '暖色调', 'images': ['I1'], 'confidence': .8}], 'gaps': []}
    b = {'descriptions': [dict(dimension=c['dimension'], core_concept=c['dimension'],
         professional_description=c['observation'], support_claim_ids=[c['id']]) for c in a['claims']]}
    p, provider = pipeline(tmp_path, [a, b])
    result = p.run()
    assert len(provider.calls) == 2
    assert result['quality_state']['interpretation'] == 'completed'
    assert result['quality_state']['style'] == 'partial'
    assert not result['quality_state']['specificity']['sufficient']
    assert '三维动画' in result['profile']['rendered_prompt']


def test_renderer_prioritizes_existing_discriminating_clauses():
    profile = render_profile(StyleProfileV3(descriptions=[InterpretationClaim(dimension='form_language',
        core_concept='造型', professional_description='精美造型，风格化写实，大头大眼比例，圆润流畅的角色轮廓',
        support_claim_ids=['C1'])]))
    assert '大头大眼比例' in profile.rendered_prompt and '圆润流畅' in profile.rendered_prompt
    assert '风格化写实' not in profile.rendered_prompt
    assert '风格化写实' in profile.detailed_prompt


def test_partial_baseline_warns_without_mutating_baseline_or_losing_prompts(tmp_path):
    p, _ = pipeline(tmp_path, saved_failure()['responses'], scope='project', assets=representatives(tmp_path))
    baseline = StyleProfileV3.model_validate(p.run()['profile'])
    before = baseline.model_dump()
    obs = local_observation('三维动画')
    effective = resolve_shot_style(baseline, obs, 's1')
    assert effective.provenance['resolution'] == 'inherited-baseline'
    assert effective.provenance['resolution_status'] == 'partial'
    assert baseline.model_dump() == before and obs.subject_action != '未知'
    assert any('画风仅部分确定' in w for w in obs.analysis_warnings)
    prompts = build_prompts('s1', obs, direct_style=effective, style_strategy='qwen-direct-v3')
    assert len(prompts) == 2
    assert all(p.contract.delivery['style_provenance']['resolution_status'] == 'partial' for p in prompts)
    assert all('头部偏大' in p.positive_prompt for p in prompts)


def test_local_fallback_is_explicitly_local_and_partial():
    obs = local_observation('三维动画')
    effective = resolve_shot_style(None, obs, 's1')
    assert effective.rendered_prompt.startswith('画风分析未完成')
    assert '本镜基础视觉观察（非完整画风）' in effective.rendered_prompt
    assert effective.provenance['delivery_status'] == 'basic-observation-only'
    assert effective.provenance['resolution_status'] == 'partial' and obs.analysis_warnings


def test_empty_output_reobserves_once_with_short_prompt_and_same_images(tmp_path):
    p, provider = pipeline(tmp_path, ['[]', evidence(), interpretation()])
    result = p.run()
    assert len(provider.calls) == 3
    first, recovery = provider.calls[:2]
    assert len(recovery['prompt']) < len(first['prompt'])
    assert recovery['frames'] == first['frames']
    assert recovery['phase'] == 'style.direct.correction'
    assert '实际图片目录' in recovery['prompt']
    assert result['raw_responses'][1]['correction_strategy'] == 'empty-evidence-reobserve'
    assert result['evidence']['claims'] and result['profile']['descriptions']


def test_repeated_empty_output_stops_and_does_not_reach_reference_selection(tmp_path):
    p, provider = pipeline(tmp_path, ['[]', '[]'])
    result = p.run()
    assert len(provider.calls) == 2 and result['budget']['correction_used']
    assert result['quality_state']['structure'] == 'failed'
    assert not result['profile']['descriptions'] and result['profile']['reference'] is None
    assert not result['tools'] and not result['candidate_snapshot']
    obs = local_observation('三维动画')
    partial = resolve_shot_style(StyleProfileV3.model_validate(result['profile']), obs, 's1')
    prompts = build_prompts('s1', obs, direct_style=partial, style_strategy='qwen-direct-v3')
    assert len(prompts) == 2
    assert all('画风分析未完成' not in prompt.positive_prompt for prompt in prompts)
    assert all('画风分析未完成' in prompt.contract.observed_style for prompt in prompts)
    assert all(prompt.warnings for prompt in prompts)
    assert all('本镜可见画风：' not in prompt.positive_prompt for prompt in prompts)
    assert obs.subject_action != '未知'


def test_unknown_style_is_explicit_in_prompt_contract():
    prompts = build_prompts('s1', observation(), style_strategy='qwen-direct-v3')
    assert len(prompts) == 2
    assert all(p.contract.delivery['style_provenance']['resolution_status'] == 'unknown' for p in prompts)


@pytest.mark.parametrize('responses,expected', [
    (saved_failure()['responses'], 'partial'),
    ([{'claims': [], 'uncertainties': ['遮挡导致不可观察']}], 'unknown'),
])
def test_failed_baseline_not_frozen_no_paid_retry_and_streamed_prompts_survive(client, tmp_path, monkeypatch, responses, expected):
    service = client.app.state.job_service
    path = service.settings.data_dir / 'frame.png'; path.write_bytes(image_bytes())
    frames = [ExtractedFrame(f'frame-{i}-1', f'shot-{i:03d}', float((i - 1) * 2 + 1), str(path), str(path), 160, 240) for i in (1, 2, 3)]
    monkeypatch.setattr('app.services.job_service.process_video', lambda *args: VideoProcessResult(
        {'duration': 6}, [DetectedShot(i, (i-1)*2, i*2) for i in (1, 2, 3)], frames, []))
    pending, calls = iter(copy.deepcopy(responses)), []
    def style(*args, **kwargs):
        calls.append(kwargs['phase'])
        value = next(pending)
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    monkeypatch.setattr(service.provider, 'analyze_style', style)
    monkeypatch.setattr(service.provider, 'analyze_shot', lambda frames, context: local_observation('三维动画', frames=frames))
    job = client.post('/api/jobs', files={'video': ('v.mp4', b'mock', 'video/mp4')}).json()['job_id']
    for _ in range(200):
        state = client.get(f'/api/jobs/{job}').json()
        if state['status'] in ('completed', 'failed'):
            break
        time.sleep(.01)
    assert state['status'] == 'completed', state
    result = client.get(f'/api/jobs/{job}/result').json()
    saved = json.loads(service.status(job)['style_direct_json'])
    assert not saved['frozen'] and saved['resolution_status'] == expected
    assert saved['baseline_usable'] == (expected == 'partial')
    assert len(result['prompts']) == 6
    assert all(s['status'] == 'warning' and s['action'] != '未知' for s in result['shots'])
    artifacts = {frame['id']: frame for frame in result['frames']}
    for shot in result['shots']:
        record = json.loads(service.db.fetchone('SELECT perception_json FROM shots WHERE id=?', (shot['id'],))['perception_json'])
        for field in (record['observation']['facts']['primary_subject'], record['observation']['dynamics']['subject_action']):
            assert field['observable'] and field['evidence']
            for ref in field['evidence']:
                frame = artifacts[ref['frame_id']]
                assert frame['shot_id'] == shot['id']
                assert shot['start_time'] <= ref['timestamp'] == frame['timestamp'] < shot['end_time']
    before = list(calls)
    resumed = draft_project_style(service, job)
    assert resumed['analysis_id'] == saved['analysis_id'] and calls == before
    assert len(calls) == len(responses)


def test_empty_or_scene_only_profile_never_usable_global():
    assert not usable_baseline(StyleProfileV3())
    p = StyleProfileV3(descriptions=[InterpretationClaim(dimension='color_tone', core_concept='局部色彩',
        professional_description='冷灰低饱和', support_claim_ids=['C1'], scope='scene', scene_id='scene-a')])
    assert not usable_baseline(p) and resolution(p) == 'unknown'


def test_saved_database_replay_is_read_only_and_never_calls_cloud(tmp_path, monkeypatch):
    p, _ = pipeline(tmp_path, saved_failure()['responses'], scope='project', assets=representatives(tmp_path))
    saved = p.run()
    saved['profile']['descriptions'] = []  # Recreate the original failed stored result.
    database = tmp_path / 'saved.sqlite3'
    source = {'assets': p.assets, 'scope': 'project'}
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE style_analyses (id TEXT,status TEXT,source_json TEXT,result_json TEXT)')
        conn.execute('INSERT INTO style_analyses VALUES(?,?,?,?)',
                     ('saved-analysis', 'failed', json.dumps(source), json.dumps(saved)))
    before = database.read_bytes()
    monkeypatch.setattr('app.providers.bailian.BailianVisionProvider.__init__',
                        lambda *args: pytest.fail('offline replay must not construct a cloud provider'))
    result = replay_saved(database, 'saved-analysis', tmp_path / 'replay')
    assert database.read_bytes() == before
    assert result['replay']['new_paid_requests'] == 0
    assert result['replay']['old_description_count'] == 0
    assert len(result['profile']['descriptions']) == 5
    assert all('path' not in a and 'original_path' not in a for a in result['assets'])


def test_normal_valid_output_reports_all_three_states_without_forcing_anchor(tmp_path):
    p, provider = pipeline(tmp_path, [evidence(), interpretation()])
    result = p.run()
    assert len(provider.calls) == 2
    assert result['quality_state']['structure'] == 'valid'
    assert result['quality_state']['observation'] == 'available'
    assert result['quality_state']['interpretation'] == 'completed'
    assert result['quality_state']['style'] == 'resolved'
    assert result['quality_state']['anchor'] == 'unknown'


def test_unknown_observation_does_not_mean_invalid_json(tmp_path):
    p, provider = pipeline(tmp_path, [{'claims': [], 'uncertainties': ['画面完全遮挡']}])
    result = p.run()
    assert len(provider.calls) == 1
    assert result['quality_state']['structure'] == 'valid'
    assert result['quality_state']['observation'] == result['quality_state']['style'] == 'unknown'


def test_all_invalid_responses_report_failed_structure_without_fake_baseline(tmp_path):
    p, provider = pipeline(tmp_path, ['[]', '[]'])
    result = p.run()
    assert len(provider.calls) == 2
    assert result['quality_state']['structure'] == 'failed'
    assert result['quality_state']['style'] == 'unknown'
    assert not result['profile']['descriptions']


def test_specificity_does_not_require_known_keywords_or_a_named_recipe():
    descriptions = [InterpretationClaim(dimension=dim, core_concept=dim,
        professional_description=text, support_claim_ids=['C1']) for dim, text in (
        ('medium_rendering', '真人摄影'),
        ('form_language', '形体遵循可辨识的人体比例并经过轻度抽象'),
        ('color_tone', '暮色中的琥珀亮区对照灰绿色暗部'))]
    assert feature_quality(descriptions)['sufficient']
