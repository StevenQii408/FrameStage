"""Synthetic wire-shape regressions; no private model output is required."""
from style_protocol_samples import montage_case, wire_case
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from app.config import load_settings
from app.providers.bailian import BailianVisionProvider
from app.providers.base import FrameInput
from app.schemas import Shot
from app.services.event_sequences import derive_sequences
from app.style_direct.models import InterpretationClaim, compact_features
from app.style_direct.service import prepare_image
from test_event_sequences import handoff
from test_style_direct import image_bytes, pipeline


def saved():
    return montage_case()


@pytest.mark.parametrize('sample', [montage_case, wire_case])
def test_style_keeps_negation_parenthetical_controls_and_named_reference(tmp_path, sample):
    fixture = sample()
    assets = [prepare_image(image_bytes(), tmp_path / 'inputs', f'I{number}') for number in range(1, 4)]
    flow, provider = pipeline(tmp_path, fixture['style_responses'], scope='project', assets=assets)
    result = flow.run()
    assert len(provider.calls) == 2
    prompt = result['profile']['rendered_prompt']
    assert prompt.startswith('整体画面风格：皮克斯式角色三维动画（视觉参考）')
    assert result['profile']['reference']['id'] == 'pixar-character-3d'
    assert result['recommendations'][0]['recipe_id'] == 'pixar-character-3d'
    assert not any(text in prompt for text in ('肤色', '泛红', '汗', '衣服', '玻璃杯', '湿润'))
    assert '眼' in prompt and '阴影' in prompt
    assert len(prompt.removeprefix('整体画面风格：')) <= 120


def test_compact_style_preserves_complete_highlight_clause_and_excludes_skin_palette():
    claims = [InterpretationClaim(dimension='form_language', core_concept='五官比例',
        professional_description='角色造型采用风格化比例，头部相对身体偏大，五官（特别是眼睛）尺寸夸张且轮廓圆润', support_claim_ids=['F']),
        InterpretationClaim(dimension='color_tone', core_concept='色调', professional_description='米黄暖调环境光与健康暖褐肤色', support_claim_ids=['C']),
        InterpretationClaim(dimension='lighting_shading', core_concept='柔光',
            professional_description='阴影边缘柔和模糊，高光区域（如头发、皮肤）分布均匀且不刺眼', support_claim_ids=['L'])]
    prompt = compact_features(claims, reference_id='pixar-character-3d')
    assert '肤色' not in prompt
    assert '高光区域分布均匀且不刺眼' in prompt
    assert '高光区域，' not in prompt and not prompt.endswith('高光区域。')
    assert '眼睛' in prompt


def test_synthetic_montage_merges_only_evidenced_pairs_and_preserves_every_source():
    fixture = saved()
    shots = [Shot.model_validate(item) for item in fixture['shots']]
    payload = derive_sequences(shots, montage=fixture['montage'])
    groups = [s['source_shot_ids'] for s in payload['generation_sequences']]
    assert [identifier for group in groups for identifier in group] == [s.id for s in shots]
    assert any(len(group) > 1 for group in groups)
    protected = {r['right_shot_id'] for r in fixture['montage']['relations']
                 if r['boundary_role'] in ('independent-reaction', 'reveal')}
    assert all(len(group) == 1 for group in groups if protected.intersection(group))
    assert all(s['generation_duration_seconds'] <= 12 for s in payload['generation_sequences'])
    assert payload['sequence_metadata']['missing_pair_count'] == 0


@pytest.mark.parametrize('correction_fails', [False, True])
def test_chain_alias_correction_is_bounded_and_retains_valid_pairs(tmp_path, monkeypatch, correction_fails):
    source, relations = handoff()
    # Pair facts/state linkage are valid, but two local goal phrasings differ.
    first = copy.deepcopy(relations)
    first[1]['event_goal'] = '女人接稳同一杯子'
    provider = BailianVisionProvider(replace(load_settings(), cloud_api_key='test-only'))
    image = tmp_path / 'frame.jpg'
    Image.new('RGB', (40, 40)).save(image)
    frames = {s.id: [FrameInput(s.id + '-f', s.start_time, image)] for s in source}
    rows = [{'id': s.id, 'number': s.number, 'start_time': s.start_time, 'end_time': s.end_time,
             'status': s.status, 'observation': {'subject_action': s.action,
                'action_details': s.action_details.model_dump(), 'visual_facts': s.visual_facts.model_dump()}} for s in source]
    monkeypatch.setattr(provider.helper, '_contact_sheets', lambda *_: [image])
    calls = []
    def call(prompt, inputs, phase, **options):
        calls.append((prompt, phase, options))
        if len(calls) == 2 and correction_fails:
            raise TimeoutError('simulated correction timeout')
        return json.dumps({'relations': first if len(calls) == 1 else relations}, ensure_ascii=False)
    monkeypatch.setattr(provider, '_call', call)
    try:
        output = provider.analyze_montage(rows, frames)
    finally:
        provider.unload()
    assert len(calls) == len(output['relations']) == 2
    assert calls[1][1] == 'montage.correction'
    assert all(c[2]['max_attempts'] == 1 for c in calls)
    assert len(output['raw_responses']) == (1 if correction_fails else 2)
    merged = derive_sequences(source, montage=output)
    assert len(merged['generation_sequences']) == (2 if correction_fails else 1)
    assert output['status'] == ('partial' if correction_fails else 'completed')
    assert bool(output['unresolved_chain_pairs']) == correction_fails


def test_complete_cloud_root_array_and_literal_context_repair_do_not_fabricate_aliases():
    from app.schemas import MontageRelation
    from app.services.event_sequences import _context
    from app.services.montage_wire import parse_montage_payload, repair_context_evidence
    fixture = wire_case()
    response = fixture['montage_responses'][0]
    parsed, repairs = parse_montage_payload(json.dumps(response))
    assert repairs == ['complete-root-array-to-relations']
    assert len(parsed['relations']) == 7
    with pytest.raises(ValueError):
        parse_montage_payload(json.dumps(response)[:-1])
    source = [Shot.model_validate(s) for s in saved()['shots']]
    pair = source[3:5]
    raw = next(r for r in fixture['montage_responses'][1]['relations'] if r['left_shot_id'] == pair[0].id)
    relation, repairs = repair_context_evidence(MontageRelation.model_validate(raw), pair)
    assert _context(*pair, relation) is not None
    assert any('literal-saved-value' in repair for repair in repairs)
    assert all(any(ref.shot_id == s.id for s in pair) for ref in relation.context_evidence)
    relation.shared_location = '原片没有的办公室'
    relation, _ = repair_context_evidence(relation, pair)
    assert _context(*pair, relation) is None


def test_verified_reference_cannot_lose_its_saved_global_lighting_dimension(tmp_path):
    responses = saved()['style_responses']
    responses[1]['descriptions'] = [d for d in responses[1]['descriptions'] if d['dimension'] != 'lighting_shading']
    assets = [prepare_image(image_bytes(), tmp_path / 'inputs', f'I{n}') for n in range(1, 4)]
    flow, provider = pipeline(tmp_path, responses, scope='project', assets=assets)
    result = flow.run()
    assert result['profile']['reference']['id'] == 'pixar-character-3d'
    assert any(d['dimension'] == 'lighting_shading' and d['scope'] == 'global' for d in result['profile']['descriptions'])
    assert '阴影' in result['profile']['rendered_prompt']
    assert len(provider.calls) == 2  # Restore saved facts, not another inference.
    assert any(a.get('repair') == 'missing-reference-dimension-restored-from-saved-evidence' for a in result['audit'])


def test_same_place_evidence_is_transitive_without_fuzzy_location_guessing():
    source, relations = handoff()
    source[1].visual_facts.scene = '厨房操作台'
    source[2].visual_facts.scene = '厨房靠窗区域'
    relations[1]['shared_location'] = '厨房操作台'
    relations[1]['context_evidence'] = [{'kind': 'location', 'value': '厨房操作台', 'shot_id': source[1].id,
        'source_field': 'scene', 'quote': '厨房操作台'}]
    result = derive_sequences(source, montage=relations)
    assert len(result['generation_sequences']) == 1
    assert result['generation_sequences'][0]['source_shot_ids'] == [s.id for s in source]
    # A real scene change blocks the inference even if an alias happens to overlap.
    source[1].scene_group_id, source[2].scene_group_id = 'scene-001', 'scene-002'
    assert len(derive_sequences(source, montage=relations)['generation_sequences']) == 2
