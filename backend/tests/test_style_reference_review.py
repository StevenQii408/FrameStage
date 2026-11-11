"""Keyless evidence/selection regressions, not measured visual accuracy."""
import pytest

from app.style_direct.anchors import candidates
from app.style_direct.evidence import grounded_name
from app.style_direct.models import EvidenceClaim, InterpretationClaim, StyleReference, safe_generation_text
from app.style_direct.references import verify_reference, review_candidates
from test_style_direct import claim, evidence, interpretation, pipeline
from test_style_density_repair import investigation


def reference_case():
    facts = [claim('M', text='三维动画采用连续体积受光'),
        claim('F', 'form_language', '主辅体量具有大中小梯级，面部结构简化，形体以圆润轮廓组织'),
        claim('L', 'lighting_shading', '阴影边缘宽缓，明暗过渡连续且高光克制'),
        claim('T', 'texture_material', '反射宽度按区域分区，有稳定区域边界')]
    model_claims = {c['id']: EvidenceClaim.model_validate(c) for c in facts}
    ref = dict(id='pixar-character-3d', confidence=.93,
        matched_features=[dict(claim_id=c['id'], quote=c['observation'],
            relevance='所见的形体、受光或表面组织形成具体的视觉参考适配依据。') for c in facts[1:]],
        distinguishing_claim_ids=['F', 'T'], reason='主辅体量梯级与分区表面组织共同支持这个生成视觉方向，不认定制作来源。',
        alternative_assessments=[dict(id=k, verdict='distinguishable', support_claim_ids=['F', 'T'],
            difference='相比另一参考方向，当前明确可见的体量梯级与表面分区更适合所选方向，不以未提到的特征当反例。')
            for k in ('disney-modern-3d', 'photoreal-cgi')])
    # Keep the contrast free of the forbidden "未提到 == absent" heuristic.
    for item in ref['alternative_assessments']:
        item['difference'] = '相比另一参考方向，当前可见的体量梯级与区域表面组织更适合所选方向，这是视觉适配差异。'
    return facts, model_claims, ref


def test_reference_is_distinct_from_full_signature_and_renders_named_head(tmp_path):
    facts, claims, ref = reference_case()
    rows = candidates(list(claims.values()))
    assert verify_reference(StyleReference.model_validate(ref), claims, rows)
    b = dict(descriptive_name='风格化三维角色动画', descriptions=[dict(dimension=c['dimension'],
        core_concept=c['dimension'], professional_description=c['observation'], support_claim_ids=[c['id']])
        for c in facts], reference=ref)
    p, provider = pipeline(tmp_path, [dict(claims=facts), b])
    result = p.run()
    assert len(provider.calls) == 2
    assert result['profile']['anchor'] is None
    assert result['quality_state']['reference'] == 'supported'
    assert result['profile']['rendered_prompt'].startswith('整体画面风格：皮克斯式角色三维动画（视觉参考）')
    assert result['diagnostics']['quality']['selected_reference_id'] == ref['id']
    profile_ref = StyleReference.model_validate(ref)
    assert safe_generation_text(result['profile']['rendered_prompt'], profile_ref)
    assert not safe_generation_text(result['profile']['rendered_prompt'] + '使用Unreal渲染', profile_ref)
    from app.services.prompt_service import build_prompts
    from app.style_direct.models import StyleProfileV3
    from test_prompt_profiles import observation
    bundles = build_prompts('s', observation(), direct_style=StyleProfileV3.model_validate(result['profile']))
    assert len(bundles) == 2
    assert all(result['profile']['rendered_prompt'] in p.positive_prompt for p in bundles)
    assert all(p.contract.delivery['style_provenance']['reference']['id'] == ref['id'] for p in bundles)


@pytest.mark.parametrize('bad', ['quote', 'missing-alternative', 'indistinguishable', 'local', 'unknown', 'different-medium', 'generic', 'unmentioned', 'missing-distinction'])
def test_reference_rejects_unsupported_or_ambiguous_names(bad):
    _, claims, ref = reference_case()
    rows = candidates(list(claims.values()))
    if bad == 'quote': ref['matched_features'][0]['quote'] = '画面根本没有提供的任意新视觉细节'
    if bad == 'missing-alternative': ref['alternative_assessments'].pop()
    if bad == 'indistinguishable': ref['alternative_assessments'][0]['verdict'] = 'indistinguishable'
    if bad == 'local': claims['F'].evidence_scope = 'local'
    if bad == 'unknown': ref['id'] = 'invented-style'
    if bad == 'different-medium': claims['M'].observation = '真人摄影实拍画面'
    if bad == 'generic':
        for f in ref['matched_features']:
            claims[f['claim_id']].observation = f['quote'] = '整体画面非常柔和且表面细腻平滑'
    if bad == 'unmentioned': ref['alternative_assessments'][0]['difference'] = '没有提及另一候选的特征，因此认为另一候选完全不成立'
    if bad == 'missing-distinction': ref['distinguishing_claim_ids'] = ['M']
    with pytest.raises(ValueError):
        verify_reference(StyleReference.model_validate(ref), claims, rows)


def test_rejected_reference_does_not_select_another_candidate(tmp_path):
    facts, _, ref = reference_case()
    ref['alternative_assessments'][0]['verdict'] = 'insufficient_evidence'
    b = interpretation(); b['reference'] = ref
    p, _ = pipeline(tmp_path, [dict(claims=facts), b])
    result = p.run()
    assert result['profile']['reference'] is None and result['profile']['anchor'] is None
    assert '皮克斯' not in result['profile']['rendered_prompt']
    assert any(a['field'] == 'reference' for a in result['audit'])


def test_large_eyes_and_soft_shadows_do_not_force_a_studio_reference():
    _, claims, ref = reference_case()
    claims.pop('T')
    claims['F'].observation = '大眼与圆润轮廓，头部比例夸张'
    ref['matched_features'] = [f for f in ref['matched_features'] if f['claim_id'] != 'T']
    ref['matched_features'][0]['quote'] = claims['F'].observation
    ref['distinguishing_claim_ids'] = ['F', 'L']
    for a in ref['alternative_assessments']:
        a['support_claim_ids'] = ['F', 'L']
    with pytest.raises(ValueError, match='通用轮廓和柔光'):
        verify_reference(StyleReference.model_validate(ref), claims, candidates(list(claims.values())))


def test_semantic_family_name_survives_latest_user_wording(tmp_path):
    a = dict(claims=[claim('M', text='画面呈现三维数字渲染特征，光影过渡自然'),
        claim('F', 'form_language', '头部与眼睛尺寸明显大于写实人体标准，面部轮廓圆润')])
    b = dict(descriptive_name='风格化三维角色动画', descriptions=[dict(dimension=c['dimension'],
        core_concept=c['dimension'], professional_description=c['observation'], support_claim_ids=[c['id']])
        for c in a['claims']])
    p, _ = pipeline(tmp_path, [a, b])
    result = p.run()
    assert result['profile']['descriptive_name'] == b['descriptive_name']
    assert result['profile']['rendered_prompt'].startswith('整体画面风格：风格化三维角色动画')
    assert result['profile']['reference'] is None


def test_family_name_cannot_invent_a_different_medium_or_palette():
    items = [InterpretationClaim(dimension='medium_rendering', core_concept='三维',
        professional_description='三维动画', support_claim_ids=['M'])]
    assert grounded_name(items, '水彩绘画风格') == '三维动画'
    assert grounded_name(items, '低饱和三维动画') == '三维动画'


def test_expanded_shape_synonyms_do_not_turn_negation_into_cartoon_proportions():
    from app.style_direct.evidence import representative_name
    items = [InterpretationClaim(dimension=dim, core_concept=dim,
        professional_description=value, support_claim_ids=['M']) for dim, value in [
        ('medium_rendering', '三维动画'), ('form_language', '头部与眼睛尺寸并非明显大于写实人体标准')]]
    assert representative_name(items) == '三维动画'


def test_comparison_may_name_a_candidate_without_guessing_a_renderer():
    _, claims, ref = reference_case()
    ref['alternative_assessments'][0]['difference'] = '相比迪士尼式现代三维动画参考，当前可见体量梯级与分区表面组织更适合所选方向。'
    assert verify_reference(StyleReference.model_validate(ref), claims, candidates(list(claims.values())))
    ref['alternative_assessments'][0]['difference'] += '使用Unreal渲染。'
    with pytest.raises(ValueError, match='技术猜测'):
        verify_reference(StyleReference.model_validate(ref), claims, candidates(list(claims.values())))


def test_punctuation_only_normalization_keeps_valid_professional_phrase(tmp_path):
    a = evidence()
    a['claims'][2]['observation'] += '。'
    b = interpretation()
    b['descriptions'][2]['professional_description'] = '阴影边缘宽缓，明暗过渡连续，高光克制'
    b['descriptions'][2]['concise_phrase'] = '阴影边缘宽缓，高光克制'
    p, _ = pipeline(tmp_path, [a, b])
    result = p.run()
    assert result['profile']['descriptions'][2]['concise_phrase']
    assert 'C3' not in p.repaired_claim_ids


def test_correction_can_still_acquire_and_review_in_one_remaining_call(tmp_path):
    b = interpretation(); b['investigation_gaps'] = [investigation()]
    extra = dict(claims=[claim('V1', 'form_language', '轮廓圆弧在转折处顺滑衔接', ['I2'])])
    final = interpretation()
    p, provider = pipeline(tmp_path, ['[]', evidence(), b,
        dict(new_evidence=extra, interpretation=final)])
    result = p.run()
    assert [c['phase'] for c in provider.calls] == ['style.direct.evidence', 'style.direct.correction',
        'style.direct.interpretation', 'style.direct.verification']
    assert result['budget']['model_requests'] == 4 and result['budget']['crop_count'] == 1
    assert any(c['id'] == 'V1' for c in result['evidence']['claims'])


def test_joint_failure_preserves_previous_interpretation_and_no_fifth_call(tmp_path):
    b = interpretation(); b['investigation_gaps'] = [investigation()]
    p, provider = pipeline(tmp_path, ['[]', evidence(), b, dict(new_evidence={}, interpretation={})])
    result = p.run()
    assert len(provider.calls) == 4
    assert result['profile']['descriptions']
    assert any('保留第一次有效画风' in n for n in result['profile']['uncertainties'])


def test_joint_review_cannot_overwrite_previous_fact_ids(tmp_path):
    b = interpretation(); b['investigation_gaps'] = [investigation()]
    p, provider = pipeline(tmp_path, ['[]', evidence(), b,
        dict(new_evidence=dict(claims=[claim('C2', 'form_language', '尖锐几何轮廓', ['I2'])]),
             interpretation=interpretation())])
    result = p.run()
    assert len(provider.calls) == 4
    assert next(c for c in result['evidence']['claims'] if c['id'] == 'C2')['observation'].startswith('曲线轮廓')


def test_review_keeps_crop_parents_and_at_most_four_images(tmp_path):
    p, _ = pipeline(tmp_path, [])
    for alias in ('I2', 'I3', 'I4'):
        p.images[alias] = dict(p.images['I1'], alias=alias)
    for alias, parent in [('I5', 'I3'), ('I6', 'I4')]:
        p.images[alias] = dict(p.images[parent], alias=alias, parent_alias=parent)
    assert p.review_aliases(['I1', 'I2', 'I3', 'I4']) == ['I3', 'I4', 'I5', 'I6']


def test_reference_cards_are_retrieval_guidance_not_production_recipes():
    _, claims, _ = reference_case()
    cards = review_candidates(candidates(list(claims.values())))
    assert any(c['id'] == 'pixar-character-3d' and '并非皮克斯独有' in c['caution'] for c in cards)
    assert all('compact_prompt' not in c and 'signature_groups' not in c for c in cards)


def test_human_review_accounting_includes_new_references_not_just_old_anchors():
    from evals.style_direct_review import technical_summary
    summary = technical_summary([dict(id='s', prediction=dict(profile=dict(reference={'id': 'pixar-character-3d'})),
        gold=dict(naming_expectation='named', expected_recipe_ids=['pixar-character-3d']))])
    assert summary['missing_names'] == [] and summary['name_disagreements'] == []
    assert summary['human_accuracy'] is None
