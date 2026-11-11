"""Offline evidence and explicit creative-selection regressions, without paid calls."""
import copy
import json
from pathlib import Path

import pytest

from app.db import utc_now
from app.style_direct.anchors import candidates
from app.style_direct.controls import visual_controls
from app.style_direct.library import runtime_library
from app.style_direct.models import EvidenceClaim, StyleReference, safe_generation_text
from app.style_direct.recommendations import historical_recommendations
from app.style_direct.references import verify_reference
from app.style_direct.service import apply_style, get_analysis, prepare_image, update_profile
from test_style_direct import client, image_bytes, pipeline
from test_prompt_profiles import observation


def fixture():
    from style_protocol_samples import reference_pair
    a, b = reference_pair()
    return dict(stage_a=a, stage_b=b)


def latest_pipeline(tmp_path, stage_b=None):
    saved = fixture()
    assets = [prepare_image(image_bytes(), tmp_path / alias, alias) for alias in ('I1', 'I2', 'I3')]
    return pipeline(tmp_path, [saved['stage_a'], stage_b or saved['stage_b']], scope='project', assets=assets)


def latest_result(tmp_path):
    p, provider = latest_pipeline(tmp_path)
    return p.run(), provider


def recommendations(stage_b, ids):
    recipes = {r.id: r for r in runtime_library().recipes}
    rows = []
    for index, identifier in enumerate(ids):
        decision = copy.deepcopy(stage_b['reference'])
        decision.update(id=identifier, fit_level='high' if index == 0 else 'medium')
        decision['alternative_assessments'] = [dict(id=recipes[identifier].confusable_with[0],
            verdict='indistinguishable', support_claim_ids=['E2', 'E4'],
            difference='两种参考共享当前明确可见的比例造型和明暗组织，因此具有非排他的视觉适配重叠。')]
        rows.append(decision)
    stage_b['recommendations'] = rows
    stage_b['reference'] = None
    return stage_b


def insert_analysis(service, result, identifier='style-offline'):
    now = utc_now()
    service.db.execute('INSERT INTO style_analyses VALUES(?,?,?,?,?,?,?,?,?)', (
        identifier, 'partial', 'partial', identifier, json.dumps({'assets': result['assets']}),
        json.dumps(result, ensure_ascii=False), None, now, now))
    service.db.execute('INSERT INTO style_analysis_revisions VALUES(?,?,?,?)', (
        identifier, 1, json.dumps(result['profile'], ensure_ascii=False), now))
    return identifier


def test_authored_response_counts_controls_and_keeps_local_redness_out(tmp_path):
    result, provider = latest_result(tmp_path)
    assert len(provider.calls) == 2
    assert result['profile']['reference']['id'] == 'pixar-character-3d'
    assert result['profile']['reference']['confidence'] == .9
    assert result['profile']['generation_constraints'] == []
    assert result['recommendations'][0]['library_prompt'] == next(
        r.compact_prompt for r in runtime_library().recipes if r.id == 'pixar-character-3d')
    prompt = result['profile']['rendered_prompt']
    assert '块面化' in prompt and '阴影边缘' in prompt
    assert not any(word in prompt for word in ('发热', '泛红', '湿润', '卷曲'))
    local = next(c for c in result['evidence']['claims'] if '泛红' in c['observation'])
    assert local['evidence_scope'] == 'uncertain'
    assert '泛红' not in next(c for c in result['evidence']['claims'] if c['id'] == 'E3')['observation']


def test_two_specific_claims_can_support_three_independent_controls(tmp_path):
    result, _ = latest_result(tmp_path)
    claims = {c['id']: EvidenceClaim.model_validate(c) for c in result['evidence']['claims']}
    raw = copy.deepcopy(fixture()['stage_b']['reference'])
    raw['matched_features'] = [f for f in raw['matched_features'] if f['claim_id'] != 'E1']
    assert verify_reference(StyleReference.model_validate(raw), claims, candidates(list(claims.values()))) == ['E2', 'E4']
    assert len(visual_controls(raw['matched_features'][0]['quote'], 'form_language')) == 2


def test_synonyms_do_not_manufacture_more_visual_controls(tmp_path):
    result, _ = latest_result(tmp_path)
    claims = {c['id']: EvidenceClaim.model_validate(c) for c in result['evidence']['claims']}
    raw = copy.deepcopy(fixture()['stage_b']['reference'])
    claims['E2'].observation = '夸张比例，比例夸张，大头与大眼，头部比例偏大'
    claims['E4'].observation = '阴影边缘柔和，柔软的阴影边界'
    raw['matched_features'] = [dict(claim_id=k, quote=claims[k].observation,
        relevance='所见造型或阴影组织形成可以复现的视觉适配依据。') for k in ('E2', 'E4')]
    assert len(visual_controls(claims['E2'].observation, 'form_language')) == 1
    with pytest.raises(ValueError, match='三条具体控制点'):
        verify_reference(StyleReference.model_validate(raw), claims, candidates(list(claims.values())))


def test_visual_fit_cannot_omit_every_curated_nearby_comparison(tmp_path):
    result, _ = latest_result(tmp_path)
    claims = {c['id']: EvidenceClaim.model_validate(c) for c in result['evidence']['claims']}
    raw = copy.deepcopy(fixture()['stage_b']['reference'])
    other = raw['alternative_assessments'][1]
    other['id'] = 'art.3d.matte_hierarchy'  # Admitted candidate, outside the curated near-neighbor set.
    raw['alternative_assessments'] = [other]
    with pytest.raises(ValueError, match='相关近邻'):
        verify_reference(StyleReference.model_validate(raw), claims, candidates(list(claims.values())))


def test_proportion_family_name_does_not_repeat_generic_control(tmp_path):
    b = fixture()['stage_b']; b['reference'] = None; b['recommendations'] = []
    p, _ = latest_pipeline(tmp_path, b)
    prompt = p.run()['profile']['rendered_prompt']
    assert prompt.count('夸张比例') == 1
    assert '眼部' in prompt and '块面化' in prompt


def test_stage_b_can_supply_three_reviewed_cards_without_duplicate_reference(tmp_path):
    b = recommendations(fixture()['stage_b'], ['pixar-character-3d', 'disney-modern-3d', 'art.3d.matte_hierarchy'])
    p, provider = latest_pipeline(tmp_path, b)
    result = p.run()
    assert len(provider.calls) == 2 and provider.calls[1]['max_tokens'] == 3200
    assert len(result['recommendations']) == 3
    assert result['profile']['reference']['id'] == 'pixar-character-3d'
    library = {r.id: r for r in runtime_library().recipes}
    for card in result['recommendations']:
        assert card['library_prompt'] == library[card['recipe_id']].compact_prompt
        assert card['detailed_description'] == library[card['recipe_id']].detailed_description
        assert card['overlap'] and not card['differences']
        assert card['library_source_hash'] == runtime_library().source_hash


def test_over_budget_or_invalid_choice_never_falls_back_to_retrieved_first_three(tmp_path):
    b = recommendations(fixture()['stage_b'], ['pixar-character-3d'])
    b['recommendations'] *= 4
    p, provider = latest_pipeline(tmp_path, b)
    result = p.run()
    assert len(provider.calls) == 2 and result['recommendations'] == []
    assert result['profile']['reference'] is None
    b = recommendations(fixture()['stage_b'], ['pixar-character-3d'])
    b['recommendations'][0]['matched_features'][0]['quote'] = '这段话完全没有出现在观察证据当中'
    p, provider = latest_pipeline(tmp_path / 'invalid', b)
    assert p.run()['recommendations'] == [] and len(provider.calls) == 2


def test_long_truncated_stage_b_uses_bounded_existing_structural_repair(tmp_path):
    b = recommendations(fixture()['stage_b'], ['pixar-character-3d', 'disney-modern-3d', 'art.3d.matte_hierarchy'])
    text = json.dumps(b, ensure_ascii=False)
    # Mimic the response ending during the final card; the existing correction
    # budget handles it once, without another mandatory recommendation request.
    saved = fixture()
    assets = [prepare_image(image_bytes(), tmp_path / alias, alias) for alias in ('I1', 'I2', 'I3')]
    p, provider = pipeline(tmp_path, [saved['stage_a'], text[:-200], b], scope='project', assets=assets)
    result = p.run()
    assert len(provider.calls) == 3 and result['budget']['correction_used']
    assert len(result['recommendations']) == 3


def test_old_rejected_decision_hydrates_read_only_and_unreviewed_records_do_not(client, tmp_path):
    service = client.app.state.job_service
    result, _ = latest_result(tmp_path)
    result.pop('recommendations')
    result['profile']['reference'] = None
    result['interpretation']['reference'] = None
    result['profile']['profile_hash'] = 'historical-hash'
    identifier = insert_analysis(service, result)
    before = service.db.fetchone('SELECT result_json FROM style_analyses WHERE id=?', (identifier,))['result_json']
    hydrated = get_analysis(service, identifier)
    assert hydrated['recommendations_derived'] is True
    assert hydrated['recommendations'][0]['recipe_id'] == 'pixar-character-3d'
    assert hydrated['result']['profile']['reference'] is None
    assert service.db.fetchone('SELECT result_json FROM style_analyses WHERE id=?', (identifier,))['result_json'] == before
    assert len(service.db.fetchall('SELECT * FROM style_analysis_revisions WHERE analysis_id=?', (identifier,))) == 1
    result['raw_responses'] = []; result['candidate_snapshot'] = [{'id': 'pixar-character-3d'}]
    result['interpretation']['recommendations'] = []
    assert historical_recommendations(result) == ([], True)


def test_selected_recipe_is_creative_override_and_apply_preserves_facts_and_history(client, tmp_path):
    service = client.app.state.job_service
    result, _ = latest_result(tmp_path)
    identifier = insert_analysis(service, result)
    card = result['recommendations'][0]
    source = dict(kind='library-recipe', recipe_id=card['recipe_id'], source_hash=card['library_source_hash'])
    before_evidence = copy.deepcopy(result['evidence'])
    before_descriptions = copy.deepcopy(result['profile']['descriptions'])
    before_reference = copy.deepcopy(result['profile']['reference'])
    response = client.put(f'/api/style/analyses/{identifier}/profile', json=dict(
        rendered_prompt='整体画面风格：' + card['library_prompt'], expected_version=1, source=source))
    assert response.status_code == 200, response.text
    saved = response.json()['result']
    assert saved['evidence'] == before_evidence
    assert saved['profile']['descriptions'] == before_descriptions
    assert saved['profile']['reference'] == before_reference
    assert saved['profile']['application_status'] == 'user-edited'
    provenance = saved['profile']['provenance']['creative_override']
    assert provenance['kind'] == 'user-selected-library' and provenance['observed_fact'] is False
    assert provenance['library_prompt'] == card['library_prompt']
    now = utc_now()
    service.db.insert_job(dict(id='recipe-target', status='completed', stage='completed', progress=100,
        input_type='script', config={}, created_at=now, updated_at=now))
    visual = observation()
    service.db.execute('INSERT INTO shots(id,job_id,number,start_time,end_time,duration,time,observation_json) VALUES(?,?,?,?,?,?,?,?)',
        ('recipe-shot', 'recipe-target', 1, 0, 4, 4, '00:00', visual.model_dump_json()))
    state = apply_style(service, 'recipe-target', identifier, 2)
    assert state['profile']['rendered_prompt'] == '整体画面风格：皮克斯式角色三维动画（创作参考），' + card['library_prompt']
    assert state['profile']['edited_prompt'] == '整体画面风格：' + card['library_prompt']
    prompts = service.db.fetchall('SELECT * FROM prompts WHERE job_id=?', ('recipe-target',))
    assert len(prompts) == 2 and all(card['library_prompt'] in row['positive_prompt'] for row in prompts)
    assert service.db.fetchone('SELECT observation_json FROM shots WHERE id=?', ('recipe-shot',))['observation_json'] == visual.model_dump_json()
    assert len(service.db.fetchall('SELECT * FROM style_analysis_revisions WHERE analysis_id=?', (identifier,))) == 2
    # Subsequent manual changes remain exact user text, without an injected name.
    manually_changed = '整体画面风格：细密图形与冷暖分离，阴影边缘柔和。'
    changed = update_profile(service, identifier, manually_changed, 2)
    assert changed['result']['profile']['rendered_prompt'] == manually_changed
    assert changed['result']['profile']['provenance']['creative_override']['edited_after_selection'] is True


@pytest.mark.parametrize('problem', ['membership', 'hash', 'version', 'text', 'technique'])
def test_recipe_selection_rejects_stale_or_untrusted_sources(client, tmp_path, problem):
    service = client.app.state.job_service
    result, _ = latest_result(tmp_path)
    identifier = insert_analysis(service, result)
    card = result['recommendations'][0]
    source = dict(kind='library-recipe', recipe_id=card['recipe_id'], source_hash=card['library_source_hash'])
    text, version = card['library_prompt'], 1
    if problem == 'membership': source['recipe_id'] = 'disney-modern-3d'
    if problem == 'hash': source['source_hash'] = 'stale'
    if problem == 'version': version = 2
    if problem == 'text': text = '仅选择自己修改的一部分造型描述'
    if problem == 'technique': text += '使用Unreal渲染'
    with pytest.raises(ValueError):
        update_profile(service, identifier, text, version, source)
    assert get_analysis(service, identifier)['result']['profile']['version'] == 1
    assert len(service.db.fetchall('SELECT * FROM style_analysis_revisions WHERE analysis_id=?', (identifier,))) == 1


def test_selected_library_allows_only_its_name_and_never_technical_guesses():
    recipe = next(r for r in runtime_library().recipes if r.id == 'pixar-character-3d')
    assert safe_generation_text(recipe.label + recipe.compact_prompt, recipe=recipe)
    assert not safe_generation_text(recipe.label + recipe.compact_prompt + '迪士尼', recipe=recipe)
    assert not safe_generation_text(recipe.compact_prompt + 'PBR与SSS渲染', recipe=recipe)


def test_recipe_membership_is_rechecked_in_write_transaction(client, tmp_path, monkeypatch):
    import app.style_direct.service as module
    service = client.app.state.job_service
    result, _ = latest_result(tmp_path)
    identifier = insert_analysis(service, result)
    card = result['recommendations'][0]
    source = dict(kind='library-recipe', recipe_id=card['recipe_id'], source_hash=card['library_source_hash'])
    original, checks = module.selected_recipe, []

    def concurrent_change(current, selected):
        validated = original(current, selected)
        checks.append(True)
        if len(checks) == 1:
            changed = copy.deepcopy(result); changed['recommendations'] = []
            service.db.execute('UPDATE style_analyses SET result_json=? WHERE id=?',
                (json.dumps(changed, ensure_ascii=False), identifier))
        return validated

    monkeypatch.setattr(module, 'selected_recipe', concurrent_change)
    with pytest.raises(ValueError, match='推荐配方'):
        update_profile(service, identifier, card['library_prompt'], 1, source)
    assert get_analysis(service, identifier)['result']['profile']['version'] == 1
    assert len(service.db.fetchall('SELECT * FROM style_analysis_revisions WHERE analysis_id=?', (identifier,))) == 1
