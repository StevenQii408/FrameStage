"""Local-only API contract regression: source shots, scope, and history stay safe."""
import copy
import json
from unittest.mock import Mock

import pytest

from app.db import utc_now
from app.schemas import ActionDetails, SpatialEvidence, VisualFacts
from app.services.event_sequences import RENDER_VERSION, sequence_payload_view
from app.services.prompt_service import build_prompts
from test_prompt_profiles import observation
from test_style_direct import client, finished, image_bytes  # Shared isolated FastAPI fixture.


def seed_sources(client, job='source-job', status='completed'):
    service = client.app.state.job_service
    now = utc_now()
    service.db.insert_job(dict(id=job, status=status, stage=status, progress=100,
        input_type='video', config={'style_strategy': 'qwen-direct-v3'}, created_at=now, updated_at=now))
    for index in range(2):
        identifier = f'{job}-shot-{index + 1:03d}'
        visual = observation(subject_action='女人举起杯子' if not index else '女人喝下杯中液体',
            action_details=ActionDetails(primary_subject='女人', interaction_target='杯子',
                initial_state='女人持杯站立', steps=['女人举起杯子'] if not index else ['女人喝下杯中液体'],
                final_state='女人持杯站立'),
            spatial_evidence=SpatialEvidence(background='厨房'),
            visual_facts=VisualFacts(scene='厨房'))
        service.db.execute(
            'INSERT INTO shots(id,job_id,number,start_time,end_time,duration,time,status,scene_group_id,observation_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (identifier, job, index + 1, index * 4.0, (index + 1) * 4.0, 4.0, '00:00', 'ready', 'scene-001', visual.model_dump_json()))
        for p in build_prompts(identifier, visual, generation_duration_seconds=4.0):
            service.db.execute('INSERT INTO prompts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (p.id, job, identifier, p.prompt_type, p.platform, p.version, p.positive_prompt,
                 p.negative_prompt, p.status, p.template_id, p.template_version, '[]', p.contract.model_dump_json()))
    return service


def test_preview_is_read_only_and_does_not_call_provider(client):
    service = seed_sources(client)
    service.provider.analyze_shot = Mock(side_effect=AssertionError('cloud call forbidden'))
    service.provider.analyze_montage = Mock(side_effect=AssertionError('cloud call forbidden'))
    before = [dict(r) for r in service.db.fetchall('SELECT * FROM shots WHERE job_id=?', ('source-job',))]
    response = client.get('/api/jobs/source-job/sequences')
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['generation_sequences'] and payload['narrative_events']
    assert [s for seq in payload['generation_sequences'] for s in seq['source_shot_ids']] == [s['id'] for s in before]
    assert all(seq['generation_duration_seconds'] <= 12 for seq in payload['generation_sequences'])
    assert service.status('source-job')['event_sequence_json'] is None
    assert [dict(r) for r in service.db.fetchall('SELECT * FROM shots WHERE job_id=?', ('source-job',))] == before
    service.provider.analyze_shot.assert_not_called()
    service.provider.analyze_montage.assert_not_called()


def test_explicit_rebuild_is_scoped_and_keeps_source_contracts(client):
    service = seed_sources(client)
    seed_sources(client, job='untouched-job')
    response = client.post('/api/jobs/source-job/sequences/rebuild')
    assert response.status_code == 200, response.text
    assert service.status('source-job')['event_sequence_json']
    assert service.status('untouched-job')['event_sequence_json'] is None
    source = client.get('/api/jobs/source-job/result').json()
    assert len(source['shots']) == 2 and len(source['prompts']) == 4
    assert all(s['duration'] < 5 for s in source['shots'])
    assert all(p['contract']['generation_duration_seconds'] < 5 for p in source['prompts'])
    assert 'event_sequence_json' not in client.get('/api/jobs/source-job').json()
    assert client.get('/api/jobs/source-job/sequences/history').status_code == 200


def test_rebuild_requires_completed_task_and_missing_tasks_are_404(client):
    service = seed_sources(client, status='processing')
    assert client.post('/api/jobs/source-job/sequences/rebuild').status_code == 409
    assert service.status('source-job')['event_sequence_json'] is None
    for path in ('/api/jobs/missing/sequences', '/api/jobs/missing/sequences/history'):
        assert client.get(path).status_code == 404
    assert client.post('/api/jobs/missing/sequences/rebuild').status_code == 404


def test_style_application_rerenders_sequences_and_archives_exact_previous(client):
    service = seed_sources(client)
    original = client.post('/api/jobs/source-job/sequences/rebuild').json()
    created = client.post('/api/style/analyses', files={'image': ('style.png', image_bytes(), 'image/png')}).json()
    analysis = finished(client, created['analysis_id'])
    response = client.post('/api/jobs/source-job/style/apply',
        json={'analysis_id': created['analysis_id'], 'expected_version': 1})
    assert response.status_code == 200, response.text
    result = client.get('/api/jobs/source-job/result').json()
    prompt = analysis['result']['profile']['rendered_prompt']
    for sequence in result['generation_sequences']:
        assert sequence['version'] == 2
        assert all(prompt in sequence[key]['positive_prompt'] for key in ('first_frame_prompt', 'seedance_prompt'))
        assert all(sequence[key]['contract']['style_profile_hash'] for key in ('first_frame_prompt', 'seedance_prompt'))
    history = client.get('/api/jobs/source-job/sequences/history').json()['revisions']
    assert len(history) == 1
    assert history[0]['payload'] == original
    assert [s['duration'] for s in result['shots']] == [4, 4]
    assert service.status('source-job')['style_direct_json']


def test_style_application_does_not_overwrite_manual_sequence_or_half_apply(client):
    service = seed_sources(client)
    payload = client.post('/api/jobs/source-job/sequences/rebuild').json()
    payload['generation_sequences'][0]['seedance_prompt']['positive_prompt'] = '用户手工编辑的片段 Prompt'
    service.db.execute('UPDATE jobs SET event_sequence_json=? WHERE id=?',
                       (json.dumps(payload, ensure_ascii=False), 'source-job'))
    before = [dict(row) for row in service.db.fetchall('SELECT * FROM prompts WHERE job_id=?', ('source-job',))]
    created = client.post('/api/style/analyses', files={'image': ('style.png', image_bytes(), 'image/png')}).json()
    finished(client, created['analysis_id'])
    response = client.post('/api/jobs/source-job/style/apply',
        json={'analysis_id': created['analysis_id'], 'expected_version': 1})
    assert response.status_code == 409, response.text
    assert [dict(row) for row in service.db.fetchall('SELECT * FROM prompts WHERE job_id=?', ('source-job',))] == before
    assert json.loads(service.status('source-job')['event_sequence_json']) == payload
    assert service.status('source-job')['style_direct_json'] is None
    assert not service.db.fetchall('SELECT * FROM prompt_revisions WHERE job_id=?', ('source-job',))


def legacy_saved_sequences(client, service):
    response = client.post('/api/jobs/source-job/sequences/rebuild')
    assert response.status_code == 200, response.text
    payload = response.json()
    payload['sequence_metadata']['render_version'] = 'sequence-delivery.v1'
    for sequence in payload['generation_sequences']:
        for key, baseline_key in (('first_frame_prompt', 'first_frame_prompt'),
                                  ('seedance_prompt', 'seedance_video_prompt')):
            prompt = sequence[key]
            prompt['positive_prompt'] = f"旧格式：叙事动作概述；{sequence['id']}；{key}"
            prompt['contract']['delivery'][baseline_key] = prompt['positive_prompt']
            prompt['contract']['delivery']['render_version'] = 'sequence-delivery.v1'
    save_sequence_payload(service, payload)
    return payload


def save_sequence_payload(service, payload):
    service.db.execute('UPDATE jobs SET event_sequence_json=? WHERE id=?',
                       (json.dumps(payload, ensure_ascii=False), 'source-job'))


def test_legacy_saved_reads_refresh_format_without_replanning_or_db_writes(client, monkeypatch):
    service = seed_sources(client)
    service.db.execute(
        'INSERT INTO frames(id,job_id,shot_id,timestamp,path,thumbnail_path,width,height) VALUES(?,?,?,?,?,?,?,?)',
        ('source-frame', 'source-job', 'source-job-shot-001', 0, '/unused.jpg', '/unused.jpg', 640, 360))
    old = legacy_saved_sequences(client, service)
    # Read views must retain noncanonical historical prompt identities too.
    old['generation_sequences'][0]['seedance_prompt']['id'] = 'saved-custom-seedance-id'
    save_sequence_payload(service, old)
    original = copy.deepcopy(old)
    before_job = dict(service.status('source-job'))
    before_source = [dict(row) for row in service.db.fetchall('SELECT * FROM prompts WHERE job_id=?', ('source-job',))]
    before_shots = [dict(row) for row in service.db.fetchall('SELECT * FROM shots WHERE job_id=?', ('source-job',))]
    monkeypatch.setattr(service.db, 'execute', Mock(side_effect=AssertionError('read must not write')))
    monkeypatch.setattr(service, '_emit', Mock(side_effect=AssertionError('read must not emit')))
    monkeypatch.setattr(service.provider, 'analyze_shot', Mock(side_effect=AssertionError('cloud call forbidden')))
    monkeypatch.setattr(service.provider, 'analyze_montage', Mock(side_effect=AssertionError('cloud call forbidden')), raising=False)
    response = client.get('/api/jobs/source-job/sequences')
    assert response.status_code == 200, response.text
    view = response.json()
    result_response = client.get('/api/jobs/source-job/result')
    assert result_response.status_code == 200, result_response.text
    result = result_response.json()
    assert result['generation_sequences'] == view['generation_sequences']
    assert result['sequence_metadata'] == view['sequence_metadata']
    assert view['narrative_events'] == original['narrative_events']
    assert view['sequence_metadata']['render_version'] == RENDER_VERSION
    assert view['sequence_metadata']['render_refresh']['status'] == 'refreshed'
    for key in ('build_hash', 'version_number', 'mode', 'strategy'):
        assert view['sequence_metadata'].get(key) == original['sequence_metadata'].get(key)
    for saved, refreshed in zip(original['generation_sequences'], view['generation_sequences']):
        assert {key: value for key, value in refreshed.items() if key not in ('first_frame_prompt', 'seedance_prompt')} == {
            key: value for key, value in saved.items() if key not in ('first_frame_prompt', 'seedance_prompt')}
        for key in ('first_frame_prompt', 'seedance_prompt'):
            for stable in ('id', 'version', 'negative_prompt', 'status', 'platform', 'prompt_type'):
                assert refreshed[key][stable] == saved[key][stable]
            assert refreshed[key]['contract']['delivery']['render_version'] == RENDER_VERSION
        video = refreshed['seedance_prompt']['positive_prompt']
        assert '旧格式' not in video and '叙事动作概述' not in video
        assert video.splitlines()[1].startswith('主体：')
        assert f"时长 {saved['generation_duration_seconds']:g} 秒" in video
        assert '分辨率 1080p' in video and '整体画面风格：' in video
    assert '横屏 16:9' in view['generation_sequences'][0]['seedance_prompt']['positive_prompt']
    assert client.get('/api/jobs/source-job/sequences').json() == view
    assert old == original
    assert dict(service.status('source-job')) == before_job
    assert [dict(row) for row in service.db.fetchall('SELECT * FROM prompts WHERE job_id=?', ('source-job',))] == before_source
    assert [dict(row) for row in service.db.fetchall('SELECT * FROM shots WHERE job_id=?', ('source-job',))] == before_shots
    assert not service.db.fetchall('SELECT * FROM event_sequence_revisions WHERE job_id=?', ('source-job',))
    service.db.execute.assert_not_called()
    service._emit.assert_not_called()
    service.provider.analyze_shot.assert_not_called()
    service.provider.analyze_montage.assert_not_called()


@pytest.mark.parametrize('key,change,reason', [
    ('seedance_prompt', 'positive', '手动修改'),
    ('first_frame_prompt', 'positive', '手动修改'),
    ('seedance_prompt', 'negative', '负面'),
    ('first_frame_prompt', 'negative', '负面'),
    ('seedance_prompt', 'missing_baseline', '缺少'),
    ('first_frame_prompt', 'missing_baseline', '缺少'),
])
def test_legacy_saved_reads_preserve_manual_edits_and_missing_baselines(client, key, change, reason):
    service = seed_sources(client)
    old = legacy_saved_sequences(client, service)
    prompt = old['generation_sequences'][0][key]
    if change == 'missing_baseline':
        del prompt['contract']['delivery']['first_frame_prompt' if key == 'first_frame_prompt' else 'seedance_video_prompt']
    else:
        prompt[f'{change}_prompt'] = '用户手动编辑，必须保持原样'
    save_sequence_payload(service, old)
    persisted = service.status('source-job')['event_sequence_json']
    for path in ('sequences', 'result'):
        response = client.get(f'/api/jobs/source-job/{path}')
        assert response.status_code == 200, response.text
        view = response.json()
        assert view['generation_sequences'] == old['generation_sequences']
        refresh = view['sequence_metadata']['render_refresh']
        assert refresh['status'] == 'skipped' and reason in refresh['reason']
        assert view['sequence_metadata']['render_version'] == 'sequence-delivery.v1'
    assert service.status('source-job')['event_sequence_json'] == persisted


def test_format_refresh_preserves_verified_negative_text_and_input_payload(client):
    service = seed_sources(client)
    old = legacy_saved_sequences(client, service)
    for sequence in old['generation_sequences']:
        for key, baseline in (('first_frame_prompt', 'first_frame_negative_prompt'),
                              ('seedance_prompt', 'seedance_negative_prompt')):
            sequence[key]['negative_prompt'] = '已保存的负面限制'
            sequence[key]['contract']['delivery'][baseline] = '已保存的负面限制'
    source = service.result('source-job')
    original = copy.deepcopy(old)
    view = sequence_payload_view(old, source.shots, source.prompts, source.frames)
    assert old == original
    assert view['sequence_metadata']['render_refresh']['status'] == 'refreshed'
    for sequence in view['generation_sequences']:
        for key in ('first_frame_prompt', 'seedance_prompt'):
            assert sequence[key]['negative_prompt'] == '已保存的负面限制'


def test_style_application_after_read_refresh_archives_original_legacy_payload(client):
    service = seed_sources(client)
    old = legacy_saved_sequences(client, service)
    original = copy.deepcopy(old)
    assert client.get('/api/jobs/source-job/sequences').json()['sequence_metadata']['render_refresh']['status'] == 'refreshed'
    created = client.post('/api/style/analyses', files={'image': ('style.png', image_bytes(), 'image/png')}).json()
    finished(client, created['analysis_id'])
    response = client.post('/api/jobs/source-job/style/apply',
        json={'analysis_id': created['analysis_id'], 'expected_version': 1})
    assert response.status_code == 200, response.text
    saved = json.loads(service.status('source-job')['event_sequence_json'])
    assert saved['sequence_metadata']['render_version'] == RENDER_VERSION
    assert saved['sequence_metadata']['version_number'] == 2
    assert 'render_refresh' not in saved['sequence_metadata']
    assert all(sequence['version'] == 2 for sequence in saved['generation_sequences'])
    history = client.get('/api/jobs/source-job/sequences/history').json()['revisions']
    assert len(history) == 1 and history[0]['payload'] == original


@pytest.mark.parametrize('change', ['negative', 'missing_baseline'])
def test_style_application_never_overwrites_unverified_sequence_text(client, change):
    service = seed_sources(client)
    old = legacy_saved_sequences(client, service)
    prompt = old['generation_sequences'][0]['seedance_prompt']
    if change == 'negative':
        prompt['negative_prompt'] = '用户负面限制，必须保持'
    else:
        del prompt['contract']['delivery']['seedance_video_prompt']
    save_sequence_payload(service, old)
    before = [dict(row) for row in service.db.fetchall('SELECT * FROM prompts WHERE job_id=?', ('source-job',))]
    created = client.post('/api/style/analyses', files={'image': ('style.png', image_bytes(), 'image/png')}).json()
    finished(client, created['analysis_id'])
    response = client.post('/api/jobs/source-job/style/apply',
        json={'analysis_id': created['analysis_id'], 'expected_version': 1})
    assert response.status_code == 409, response.text
    assert [dict(row) for row in service.db.fetchall('SELECT * FROM prompts WHERE job_id=?', ('source-job',))] == before
    assert json.loads(service.status('source-job')['event_sequence_json']) == old
    assert service.status('source-job')['style_direct_json'] is None
    assert not service.db.fetchall('SELECT * FROM prompt_revisions WHERE job_id=?', ('source-job',))
    assert not service.db.fetchall('SELECT * FROM event_sequence_revisions WHERE job_id=?', ('source-job',))
