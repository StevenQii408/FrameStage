import json
from types import SimpleNamespace

import pytest

from app.style_direct.diagnostics import attach_usage, job_diagnostics, legacy_diagnostics, markdown_report
from test_style_direct import client, evidence, finished, image_bytes, interpretation, pipeline


def test_diagnostics_stages_and_requests_are_persistable_without_prompts_or_paths(tmp_path):
    p, _ = pipeline(tmp_path, [evidence(), interpretation()])
    result = p.run()
    data = result['diagnostics']
    assert [stage['name'] for stage in data['stages']] == ['observing', 'retrieving', 'interpreting', 'rendering']
    assert sum(stage['model_requests'] for stage in data['stages']) == 2
    assert all(stage['status'] == 'completed' and stage['elapsed_seconds'] >= 0 for stage in data['stages'])
    assert all(len(request['prompt_sha256']) == 64 for request in data['requests'])
    assert 'path' not in json.dumps(data) and '原始提示词' not in json.dumps(data)
    assert data['quality']['human_accuracy'] is None
    report = markdown_report(result)
    assert '不代表真实语义正确' in report and '人民币估算' in report


def test_incomplete_usage_is_unknown_not_zero_and_known_subtotal_separate(tmp_path):
    p, _ = pipeline(tmp_path, [evidence(), interpretation()]); result = p.run()
    attach_usage(result, [])
    assert result['diagnostics']['usage']['estimated_list_price_cny'] is None
    good = {'usage_available': True, 'prompt_tokens': 10, 'completion_tokens': 5, 'estimated_list_price_cny': .001, 'status': 'success'}
    attach_usage(result, [good, {'usage_available': False, 'status': 'failed'}])
    data = result['diagnostics']['usage']
    assert not data['complete'] and data['prompt_tokens'] is None
    assert data['known_cost_subtotal_cny'] == .001 and data['estimated_list_price_cny'] is None
    assert data['failed_requests'] == 1
    attach_usage(result, [good, good])
    assert result['diagnostics']['usage']['complete']
    assert result['diagnostics']['usage']['prompt_tokens'] == 20


def test_diagnostic_api_is_read_only_and_legacy_does_not_recompute(client):
    created = client.post('/api/style/analyses', files={'image': ('a.png', image_bytes(), 'image/png')}).json()
    identifier = created['analysis_id']; finished(client, identifier)
    database = client.app.state.db
    before = dict(database.fetchone('SELECT * FROM style_analyses WHERE id=?', (identifier,)))
    response = client.get(f'/api/style/analyses/{identifier}/diagnostics')
    assert response.status_code == 200 and response.json()['schema_version'] == 'style-diagnostics.v1'
    markdown = client.get(f'/api/style/analyses/{identifier}/diagnostics?format=markdown')
    assert markdown.status_code == 200 and 'text/markdown' in markdown.headers['content-type']
    assert '# 画风分析诊断' in markdown.text
    assert client.get(f'/api/style/analyses/{identifier}/diagnostics?format=csv').status_code == 422
    assert dict(database.fetchone('SELECT * FROM style_analyses WHERE id=?', (identifier,))) == before
    old = json.loads(before['result_json']); old.pop('diagnostics')
    database.execute('UPDATE style_analyses SET result_json=? WHERE id=?', (json.dumps(old), identifier))
    assert client.get(f'/api/style/analyses/{identifier}/diagnostics').json()['status'] == 'legacy-unavailable'
    assert client.get('/api/jobs/missing/analysis/diagnostics').status_code == 404


def test_first_prompt_latency_from_persisted_events_and_missing_history_unknown():
    service = SimpleNamespace(status=lambda _: {'provider': 'bailian', 'model': 'qwen3.8-flash',
        'status': 'completed', 'created_at': '2026-10-03T00:00:00+00:00'},
        db=SimpleNamespace(events_since=lambda _: [{'type': 'prompt.ready', 'created_at': '2026-10-03T00:00:12+00:00', 'payload': {'shot_id': 's1'}}]))
    result = job_diagnostics(service, 'job')
    assert result['first_prompt_seconds_including_queue'] == 12 and result['first_prompt_shot_id'] == 's1'
    service.db.events_since = lambda _: []
    assert job_diagnostics(service, 'job')['first_prompt_seconds_including_queue'] is None
