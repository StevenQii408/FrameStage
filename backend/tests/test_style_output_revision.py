"""Saved-text replay and deterministic checks, not visual accuracy measurements."""
import copy
import json
from pathlib import Path

import pytest

from app.style_direct.anchors import candidates
from app.style_direct.models import (EvidenceClaim, GenerationConstraint, InterpretationClaim,
    StyleProfileV3, StyleReference, render_profile, safe_visual_text)
from app.style_direct.prompts import INTERPRET_PROMPT
from app.style_direct.references import safe_comparison_text, verify_reference
from app.services.prompt_service import build_prompts
from test_prompt_profiles import observation
from test_style_direct import pipeline
from test_style_reference_review import reference_case
from test_style_reliability import representatives


def saved_responses():
    from style_protocol_samples import reference_pair
    return list(reference_pair(contrastive=True))


def test_negative_hair_comparison_does_not_bypass_content_or_tech_checks():
    allowed = {'pixar-character-3d', 'disney-modern-3d'}
    assert safe_comparison_text('符合该造型概括的视觉方向，区别于写实毛发模拟。', allowed)
    assert not safe_visual_text('写实毛发模拟')  # Exception is not global.
    assert not safe_comparison_text('区别于写实毛发模拟，身穿白色裙子。', allowed)
    assert not safe_comparison_text('区别于写实毛发模拟，使用SSS处理。', allowed)
    assert not safe_comparison_text('具体表现为棕色卷发与白色衣服。', allowed)


def test_explicit_visual_fit_accepts_overlap_without_claiming_unique_source():
    _, claims, ref = reference_case()
    ref['selection_basis'] = 'visual_fit'
    ref['alternative_assessments'][0]['verdict'] = 'indistinguishable'
    value = StyleReference.model_validate(ref)
    assert verify_reference(value, claims, candidates(list(claims.values())))
    # An old/contrastive decision still must justify a distinction.
    ref.pop('selection_basis')
    with pytest.raises(ValueError, match='不可区分'):
        verify_reference(StyleReference.model_validate(ref), claims, candidates(list(claims.values())))


@pytest.mark.parametrize('failure', ['insufficient', 'wrong-quote', 'local', 'weak', 'wrong-family', 'missing-comparison'])
def test_visual_fit_cannot_bypass_evidence_gates(failure):
    _, claims, ref = reference_case()
    ref['selection_basis'] = 'visual_fit'
    ref['alternative_assessments'][0]['verdict'] = 'indistinguishable'
    if failure == 'insufficient':
        ref['alternative_assessments'][0]['verdict'] = 'insufficient_evidence'
    elif failure == 'wrong-quote':
        ref['matched_features'][0]['quote'] = '并未观察到的全新造型'
    elif failure == 'local':
        claims['F'].evidence_scope = 'local'
    elif failure == 'weak':
        claims['T'].observation = ref['matched_features'][-1]['quote'] = '整体暖色调'
    elif failure == 'wrong-family':
        ref['id'] = 'shinkai-anime'
    elif failure == 'missing-comparison':
        # Nonexclusive visual-fit choices need a related reviewed neighbour;
        # exhaustive nearest-alternative coverage remains the distinctive gate.
        ref['alternative_assessments'] = []
    with pytest.raises(ValueError):
        verify_reference(StyleReference.model_validate(ref), claims, candidates(list(claims.values())))


def test_saved_response_replays_details_without_new_model_claims(tmp_path):
    responses = saved_responses()
    p, provider = pipeline(tmp_path, responses, scope='project', assets=representatives(tmp_path))
    result = p.run()
    text = result['profile']['rendered_prompt']
    assert '块面化几何体' in text and '眼部' in text
    assert '阴影边缘柔和过渡' in text
    assert '以强化表情' not in text
    assert len(text.removeprefix('整体画面风格：')) <= 120
    assert '湿润' not in text and '汗水' not in text
    # Original response is contrastive and admitted Disney overlap: no silent promotion.
    assert result['profile']['reference'] is None
    assert len(provider.calls) == 2
    bundles = build_prompts('s', observation(), direct_style=StyleProfileV3.model_validate(result['profile']))
    assert all('生成画风约束' not in b.positive_prompt for b in bundles)
    assert all(text in b.positive_prompt for b in bundles)
    assert result['profile']['generation_constraints']  # History remains auditable.


def test_same_evidence_can_back_an_explicit_nonexclusive_recommendation(tmp_path):
    responses = copy.deepcopy(saved_responses())
    # This tests the NEW decision mode; it is not presented as a fresh Flash response.
    responses[1]['reference']['selection_basis'] = 'visual_fit'
    p, provider = pipeline(tmp_path, responses, scope='project', assets=representatives(tmp_path))
    result = p.run()
    assert result['profile']['reference'], result['audit']
    assert result['profile']['reference']['selection_basis'] == 'visual_fit'
    assert result['profile']['rendered_prompt'].startswith('整体画面风格：皮克斯式角色三维动画（视觉参考）')
    assert '块面化几何体' in result['profile']['rendered_prompt']
    assert len(provider.calls) == 2 and not result['tools']
    assert any(a.get('selection_basis') == 'visual_fit' for a in result['audit'])


def test_no_style_constraints_in_either_delivery_even_for_historical_profiles():
    profile = StyleProfileV3(rendered_prompt='整体画面风格：剪纸拼贴，手工裁切边缘。',
        generation_constraints=[GenerationConstraint(text='保持纸纤维与裁切边缘。', support_claim_ids=['E1'])])
    bundles = build_prompts('s', observation(), direct_style=profile)
    assert all('生成画风约束' not in b.positive_prompt and '保持纸纤维' not in b.positive_prompt for b in bundles)
    assert '首帧' in bundles[0].positive_prompt and '动作：' in bundles[1].positive_prompt
    assert profile.generation_constraints[0].text.endswith('。')


def test_all_dimension_descriptions_can_contribute_without_content_leakage():
    entries = [('medium_rendering', '三维动画'), ('form_language', '风格化夸张比例'),
        ('form_language', '块面化发束'), ('lighting_shading', '阴影边缘宽缓'),
        ('lighting_shading', '面部明暗层级清晰'), ('color_tone', '冷灰低饱和'),
        ('texture_material', '纸纤维颗粒分布清晰')]
    profile = StyleProfileV3(descriptive_name='风格化三维角色动画', descriptions=[
        InterpretationClaim(dimension=d, core_concept=t, professional_description=t, support_claim_ids=['E1'])
        for d, t in entries])
    render_profile(profile)
    for _, text in entries[1:]:
        assert text in profile.rendered_prompt
    assert len(profile.rendered_prompt.removeprefix('整体画面风格：')) <= 120


def test_prompt_explicitly_requires_single_style_and_evidence_backed_fit():
    assert 'generation_constraints固定=[]' in INTERPRET_PROMPT
    assert 'selection_basis=visual_fit' in INTERPRET_PROMPT
    assert 'insufficient_evidence不是已证明的重叠' in INTERPRET_PROMPT
