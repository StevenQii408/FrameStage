"""Saved-response and synthetic engineering tests; no paid visual-accuracy claims."""
import json
from pathlib import Path

import pytest

from app.style_direct import anchors
from app.style_direct.consistency import check_consistency
from app.style_direct.models import EvidenceClaim, InterpretationClaim, StyleAnchor, safe_visual_text, visible_clauses, safe_generation_text
from app.style_direct.evidence import representative_name
from app.style_direct.reliability import specificity_score
from test_cinema_style_library import mini
from test_style_direct import claim, evidence, interpretation, pipeline
from test_style_reliability import representatives


def density_failure():
    from style_protocol_samples import density_pair
    return density_pair()


def test_partial_response_preserves_shape_and_separates_local_effects(tmp_path):
    p, provider = pipeline(tmp_path, density_failure()['responses'], scope='project', assets=representatives(tmp_path))
    result = p.run()
    prompt = result['profile']['rendered_prompt']
    assert len(provider.calls) == 2 and not result['budget']['correction_used']
    assert '柔和漫射三维角色' in prompt  # Preserve the grounded model family, not overwrite it.
    assert '夸张头部比例' in prompt and '眼睛占据面部较大面积' in prompt
    assert '阴影边缘模糊' in prompt
    for forbidden in ('衣物', '玻璃杯', '湿润', '纸纤维', '哑光', '红橙', '情绪'):
        assert forbidden not in prompt
    assert len(prompt.removeprefix('整体画面风格：')) <= 120
    facts = result['evidence']['claims']
    wet = next(c for c in facts if '汗水' in c['observation'])
    assert wet['images'] == ['I3'] and wet['evidence_scope'] == 'local'
    assert result['profile']['anchor'] is None
    assert {'pixar-character-3d', 'disney-modern-3d'} <= {r['id'] for r in result['candidate_snapshot']}
    assert result['quality_state']['anchor'] == 'unknown'
    assert '情绪' not in str(result['profile']['generation_constraints'])
    assert any('情绪' in u for u in result['evidence']['uncertainties'])
    assert all('暗示' not in c['observation'] for c in result['evidence']['claims'])
    assert result['profile']['descriptive_name'] == '柔和漫射三维角色'
    assert any('大块的卷曲体块' in c['observation'] for c in facts)


def test_verified_anchor_short_name_can_be_applied_without_technical_filter_rejection():
    anchor = StyleAnchor(id='disney-modern-3d', confidence=.9, support_claim_ids=['A', 'B'],
        distinguishing_claim_ids=['A'], reason='已保存的参考锚点，只有通过完整校验的结果才允许应用')
    assert safe_generation_text('整体画面风格：迪士尼式现代三维，长弧轮廓与顺滑转折。', anchor)
    assert not safe_generation_text('整体画面风格：迪士尼式现代三维，使用Unreal渲染。', anchor)


@pytest.mark.parametrize('medium,form,expected', [
    ('不是三维而是手绘动画', '没有大眼造型', '二维动画'),
    ('三维动画', '没有大眼造型', '三维动画'),
    ('三维角色与剪纸背景', '圆润轮廓', '混合媒介视觉'),
    ('黏土手塑表面', '', '手塑黏土视觉'),
])
def test_family_descriptor_respects_negation_mixed_media_and_static_limits(medium, form, expected):
    items = [InterpretationClaim(dimension=dim, core_concept=dim,
        professional_description=value, support_claim_ids=['E1'])
        for dim, value in [('medium_rendering', medium), ('form_language', form)] if value]
    assert representative_name(items) == expected


def test_orphan_material_is_not_salvaged_from_clothing_sentence():
    assert visible_clauses('衣物表面无明显织物纹理，呈现哑光质感；皮肤表面光滑') == ['皮肤表面光滑']
    assert safe_visual_text('头发采用简化体块而非单根发丝')
    assert not safe_visual_text('头发呈棕色，身穿白色裙子')


@pytest.mark.parametrize('medium', ['三维数字渲染，非二维手绘或定格材质',
    '3D动画，不是二维，也不是写实摄影', '三维动画，非纸片拼贴也非手绘动画'])
def test_negated_other_media_never_form_a_mixed_family(medium):
    from app.style_direct.inheritance import medium_families
    assert medium_families(medium) == {'3d'}


def test_explicit_object_target_cannot_hide_content(tmp_path):
    a = evidence()
    a['claims'].append(dict(claim('P1', 'texture_material', '哑光低频纹理'), target='衣物'))
    p, _ = pipeline(tmp_path, [a, interpretation()])
    assert 'P1' not in {c['id'] for c in p.run()['evidence']['claims']}


def test_local_only_claim_is_preserved_but_never_global(tmp_path):
    a = evidence()
    a['claims'].append(dict(claim('L1', 'texture_material', '仅在I1中出现细小颗粒与湿润反光'), evidence_scope='local'))
    b = interpretation('global')
    b['descriptions'].append(dict(dimension='texture_material', core_concept='湿润颗粒',
        professional_description='局部湿润反光', concise_phrase='湿润反光', support_claim_ids=['L1'], scope='global'))
    for c in a['claims'][:3]:
        c['images'] = ['I1', 'I2', 'I3']
    p, _ = pipeline(tmp_path, [a, b], scope='project', assets=representatives(tmp_path))
    result = p.run()
    assert '湿润' not in result['profile']['rendered_prompt']
    assert '湿润' in result['profile']['local_prompts']['I1']
    assert result['profile']['descriptions'][-1]['scope'] == 'local'


def test_negated_and_long_generic_prose_do_not_become_distinctive():
    assert specificity_score('无可见笔触或纸纤维纹理', 'texture_material') == 0
    assert specificity_score('画面并没有可见笔触或纸纤维纹理', 'texture_material') == 0
    assert specificity_score('物体表面具有连续平滑的体积过渡', 'medium_rendering') == 0
    assert specificity_score('皮肤呈现通透感而且表面细腻平滑', 'texture_material') == 0
    assert check_consistency('暖调均匀', ['整体色调偏暖'], 'color_tone', reject_new_controls=True)['acceptable']


def semantic_case(monkeypatch):
    library = mini()
    monkeypatch.setattr(anchors, 'runtime_library', lambda: library)
    c1 = EvidenceClaim.model_validate(claim('E1', 'color_tone', '冷暖分离：阴影呈靛青，亮区带琥珀黄'))
    c2 = EvidenceClaim.model_validate(claim('E2', 'lighting_shading', '照明让暗面占主要面积，仍能辨认明暗层级'))
    anchor = StyleAnchor(id='test.noir', confidence=.92, support_claim_ids=['E1', 'E2'],
        distinguishing_claim_ids=['E1'], reason='相比只有蓝色的夜景，这里有冷暖分离且暗面保留明暗层级作为区别。',
        feature_support=[dict(group_id='tone', support_claim_ids=['E1'], semantic_support=[dict(
            term_id='test.cool-warm', claim_id='E1', quote=c1.observation,
            explanation='靛青阴影和琥珀亮区构成冷暖分離，对应配方的区域色彩关系。')]),
            dict(group_id='light', support_claim_ids=['E2'], semantic_support=[dict(
                term_id='test.low-key', claim_id='E2', quote=c2.observation,
                explanation='暗面占主要面积且层级可辨，对应低调组织与暗部层次共同成立。')])])
    return anchor, {c.id: c for c in (c1, c2)}


def test_semantic_proofs_accept_paraphrases_not_just_authored_spelling(monkeypatch):
    anchor, claims = semantic_case(monkeypatch)
    assert anchors.verify_signature_support(anchor, claims)


def test_semantic_selection_requires_nearest_alternative_decision(monkeypatch):
    anchor, claims = semantic_case(monkeypatch)
    library = mini(); other = library.recipes[0].model_copy(update={'id': 'test.other'})
    library.recipes[0].confusable_with = ['test.other']
    library.recipes.append(other)
    monkeypatch.setattr(anchors, 'runtime_library', lambda: library)
    with pytest.raises(ValueError, match='逐项裁决'):
        anchors.verify_signature_support(anchor, claims)
    anchor.alternative_assessments = []
    from app.style_direct.models import AnchorAlternativeAssessment
    anchor.alternative_assessments.append(AnchorAlternativeAssessment(id='test.other', verdict='indistinguishable',
        support_claim_ids=['E1'], difference='同一观察无法区分两个具有相同特征的参考配方'))
    with pytest.raises(ValueError, match='尚不可区分'):
        anchors.verify_signature_support(anchor, claims)


@pytest.mark.parametrize('bad', ['forged-quote', 'wrong-term', 'local', 'generic', 'wrong-ref'])
def test_semantic_proof_cannot_bypass_evidence_gate(monkeypatch, bad):
    anchor, claims = semantic_case(monkeypatch)
    proof = anchor.feature_support[0].semantic_support[0]
    if bad == 'forged-quote': proof.quote = '图片不存在的青蓝阴影与暖色高光'
    if bad == 'wrong-term': proof.term_id = 'invented'
    if bad == 'local': claims['E1'].evidence_scope = 'local'
    if bad == 'generic':
        claims['E1'].observation = proof.quote = '三维画面整体非常温暖柔和'
    if bad == 'wrong-ref': proof.claim_id = 'E2'
    with pytest.raises(ValueError):
        anchors.verify_signature_support(anchor, claims)


def investigation():
    return dict(dimension='form_language', question='面部轮廓转折是否由连续长弧连接？', image='I1',
                tool='crop_image', regions=[[0, 0, .5, .5]], expected_evidence='核对轮廓走向与转折关系')


def test_candidate_ambiguity_can_request_one_bounded_evidence_round(tmp_path):
    b = interpretation(); b['investigation_gaps'] = [investigation()]
    extra = dict(claims=[claim('S1', 'form_language', '轮廓圆弧在转折处顺滑衔接', ['I2'])], gaps=[])
    final = interpretation()
    p, provider = pipeline(tmp_path, [evidence(), b, extra, final])
    result = p.run()
    assert [c['phase'] for c in provider.calls] == ['style.direct.evidence', 'style.direct.interpretation',
        'style.direct.supplement', 'style.direct.refinement']
    assert result['budget']['model_requests'] == 4 and result['budget']['supplement_rounds'] == 1
    assert result['budget']['crop_count'] == 1
    assert any(c['id'] == 'S1' for c in result['evidence']['claims'])


def test_whole_image_repeat_does_not_add_paid_call(tmp_path):
    b = interpretation(); gap = investigation(); gap['regions'] = [[0, 0, 1, 1]]
    b['investigation_gaps'] = [gap]
    p, provider = pipeline(tmp_path, [evidence(), b])
    result = p.run()
    assert len(provider.calls) == 2 and result['budget']['crop_count'] == 0
    assert any('没有新增可观察证据' in r.get('reason', '') for r in result['audit'])


def test_no_second_supplement_when_stage_a_already_acquired(tmp_path):
    a = evidence([investigation()])
    extra = {'claims': [claim('S1', 'form_language', '圆弧轮廓相连', ['I2'])], 'gaps': []}
    b = interpretation(); b['investigation_gaps'] = [investigation()]
    p, provider = pipeline(tmp_path, [a, extra, b])
    result = p.run()
    assert len(provider.calls) == 3 and result['budget']['supplement_rounds'] == 1
