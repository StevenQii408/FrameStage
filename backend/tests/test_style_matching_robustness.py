"""Keyless text-matching regressions; these do not assert visual accuracy."""
from types import SimpleNamespace

import pytest

from app.style_direct import anchors
from app.style_direct.inheritance import medium_families, medium_family
from app.style_direct.library import (
    MATCHER_VERSION, StyleRecipe, StyleTerm, group_evidence, positive_mention,
    runtime_library, term_matches,
)
from app.style_direct.lexicon import normalized_cues
from app.style_direct.models import EvidenceClaim, StyleAnchor


@pytest.mark.parametrize('text,cue', [
    ('不存在硬光', '硬光'), ('没有明显的轮廓线', '轮廓'),
    ('看不到网点阴影', '网点'), ('不具备硬边阴影', '硬边阴影'),
    ('无輪廓不作猜测', '輪廓'), ('无轮廓线', '轮廓'),
    ('硬光并不存在', '硬光'), ('赛璐璐分色不成立', '赛璐璐分色'),
    ('暖调不明显', '暖调'), ('网点未被观察到', '网点'),
    ('暖调与硬光均不存在', '暖调'),
    ('not warm tones', '暖调'), ('without hard light', '硬光'),
    ('hard light is not present', '硬光'), ('warm tones are absent', '暖调'),
    ('hard light does not exist', '硬光'),
])
def test_prefix_and_suffix_denial_are_not_positive(text, cue):
    assert not positive_mention(text, cue)


@pytest.mark.parametrize('text,negative,positive', [
    ('不是暖调而是冷调', '暖调', '冷调'),
    ('未见硬光但可见柔光', '硬光', '柔光'),
    ('不是剪纸，实际为三维动画', '剪纸', '三维'),
    ('暖调不明显，不过冷调清楚', '暖调', '冷调'),
    ('not warm tones but cool tones', '暖调', '冷调'),
    ('暖调而非冷调', '冷调', '暖调'),
])
def test_contrast_resets_negation_scope(text, negative, positive):
    assert not positive_mention(text, negative)
    assert positive_mention(text, positive)


@pytest.mark.parametrize('text,cue', [
    ('采用无轮廓线的平面色块', '无轮廓线'),
    ('不规则的轮廓清晰可见', '轮廓'),
    ('不仅有暖调，也有冷调', '暖调'),
    ('not only warm tones but also cool tones', '暖调'),
    ('并非没有硬光', '硬光'),
    ('暖调不明显；另一张图暖调清晰', '暖调'),
])
def test_affirmed_absence_and_repeated_positive_mentions(text, cue):
    assert positive_mention(text, cue)


@pytest.mark.parametrize('text,family', [
    ('三维角色，背景不是剪纸', '3d'),
    ('3D角色，剪纸并不存在', '3d'),
    ('不是三维而是手绘动画', '2d'),
    ('not clay but cg animation', '3d'),
    ('without paper cut, live action', 'photo'),
    ('剪纸动画', 'paper'),
])
def test_only_affirmed_medium_can_hard_filter(text, family):
    assert medium_family(text) == family
    assert medium_families(text) == frozenset([family])


@pytest.mark.parametrize('text,families', [
    ('三维角色与剪纸背景', {'3d', 'paper'}),
    ('三维角色，二维背景', {'3d', '2d'}),
    ('live action with watercolor', {'photo', 'painting'}),
    ('不是三维，也不是剪纸', set()),
])
def test_mixed_or_unknown_medium_does_not_force_single_family(text, families):
    assert medium_families(text) == frozenset(families)
    assert medium_family(text) == ''


def test_alias_normalization_never_appends_positive_evidence():
    assert normalized_cues('not warm tones') == 'not 暖调'
    assert not positive_mention(normalized_cues('not warm tones'), '暖调')


@pytest.mark.parametrize('text', [
    '轮廓线清楚，阴影呈现边缘锐利的色块',
    '描边清楚，阴影边界锐利',
    'Clear outlines with sharply bounded shadows',
])
def test_authored_feature_paraphrases_recall_without_name_shortcut(text):
    term = next(term for term in runtime_library().terms if term.id == 'art.cel_layers')
    assert term_matches(term, text)


@pytest.mark.parametrize('text', [
    '赛璐璐分色', '轮廓分色层', '轮廓清楚',
    '硬边阴影明显', '轮廓清楚，硬边阴影并不存在',
    '轮廓清楚，阴影边界锐利不明显',
])
def test_labels_and_partial_or_denied_features_cannot_replace_combination(text):
    term = next(term for term in runtime_library().terms if term.id == 'art.cel_layers')
    assert not term_matches(term, text)


def claim(identifier, dimension, text, confidence=.9):
    return EvidenceClaim(id=identifier, dimension=dimension, observation=text,
                         images=['I1'], confidence=confidence)


@pytest.fixture
def matching_library(monkeypatch):
    def term(identifier, dimension, cues, counter=None):
        return StyleTerm(id=identifier, label=identifier + '具体现象', dimension=dimension,
            definition='只通过观察中的明确可见特征组合匹配，不从风格名称推断完整外观。',
            positive_cues=cues, negative_cues=counter or [], generation_phrase='保留具体可见特征的组合',
            scope='global', source_ids=['authored'])
    terms = [term('test.surface', 'medium_rendering', [['轮廓'], ['硬边阴影']]),
             term('test.form', 'form_language', [['曲线'], ['简化']], ['尖锐几何'])]
    recipe = StyleRecipe(id='test.surface-2d', label='轮廓硬边曲线简化视觉', family='2d', category='animation2d',
        definition='通过轮廓与硬边阴影以及曲线简化造型的跨维度组合定义视觉配方。',
        signature_groups=[dict(id='surface', dimension='medium_rendering', term_ids=['test.surface']),
                          dict(id='form', dimension='form_language', term_ids=['test.form'])],
        insufficient_evidence=['只出现术语名称', '仅有轮廓描述'],
        distinction='必须同时存在媒介表面的完整特征组合及另一维度造型证据，单独风格名不成立。',
        compact_prompt='具有可见轮廓和硬边阴影的画面，以曲线主导的简化造型组织主体，保留区别性视觉关系，不从名称或高置信度补齐缺失的色彩与表面特征。',
        detailed_description='该测试配方只验证结构和文字证据门槛。轮廓与硬边阴影必须在同条媒介观察中成立，曲线与简化造型必须由造型维度提供证据；两个维度缺一不可，反向线索明确成立时拒绝。这个测试不代表实际图片已通过视觉语义核验。',
        source_ids=['authored'])
    other = recipe.model_copy(update={'id': 'test.surface-3d', 'family': '3d', 'category': 'animation3d'})
    library = SimpleNamespace(terms=terms, recipes=[recipe, other])
    monkeypatch.setattr(anchors, 'runtime_library', lambda: library)
    return library


def complete_claims():
    return [claim('E1', 'medium_rendering', '轮廓清楚，硬边阴影可见'),
            claim('E2', 'form_language', '曲线轮廓主导，造型细节简化')]


def supported_anchor():
    return StyleAnchor(id='test.surface-2d', confidence=.99, support_claim_ids=['E1', 'E2'],
        distinguishing_claim_ids=['E1'], reason='完整表面与造型组合分别来自两个维度的观察',
        feature_support=[dict(group_id='surface', support_claim_ids=['E1']),
                         dict(group_id='form', support_claim_ids=['E2'])])


def test_mixed_media_candidates_are_not_pruned_to_first_family(matching_library):
    claims = complete_claims() + [claim('E3', 'medium_rendering', '三维角色与二维背景')]
    rows = anchors.candidates(claims)
    assert {row['family'] for row in rows} == {'2d', '3d'}
    assert all(row['selection_eligible'] for row in rows)


def test_denied_medium_does_not_exclude_actual_family(matching_library):
    claims = complete_claims() + [claim('E3', 'medium_rendering', '三维角色，背景不是剪纸')]
    assert [row['family'] for row in anchors.candidates(claims)] == ['3d']


def test_negation_scope_never_leaks_between_independent_claims(matching_library):
    claims = complete_claims() + [claim('E3', 'medium_rendering', '背景不是剪纸'),
                                 claim('E4', 'medium_rendering', '三维角色动画')]
    assert [row['family'] for row in anchors.candidates(claims)] == ['3d']


def test_complete_cross_dimension_combination_remains_required(matching_library):
    recipe = matching_library.recipes[0]
    claims = complete_claims()
    assert set(group_evidence(recipe, claims, matching_library)) == {'surface', 'form'}
    assert anchors.verify_signature_support(supported_anchor(), {c.id: c for c in claims})
    claims[1] = claim('E2', 'color_tone', '曲线轮廓主导，造型细节简化', 1)
    with pytest.raises(ValueError, match='维度'):
        anchors.verify_signature_support(supported_anchor(), {c.id: c for c in claims})


def test_separate_claims_cannot_synthesize_one_feature_relationship(matching_library):
    claims = [claim('E1', 'medium_rendering', '轮廓清楚'),
              claim('E3', 'medium_rendering', '硬边阴影可见'), complete_claims()[1]]
    assert 'surface' not in group_evidence(matching_library.recipes[0], claims, matching_library)


def test_counterevidence_blocks_even_high_confidence(matching_library):
    claims = complete_claims() + [claim('E3', 'form_language', '尖锐几何轮廓明确', 1)]
    assert not anchors.candidates(claims)
    with pytest.raises(ValueError, match='反向线索'):
        anchors.verify_signature_support(supported_anchor(), {c.id: c for c in claims})


def test_negated_countercue_does_not_block_valid_evidence(matching_library):
    claims = complete_claims()
    claims[1] = claim('E2', 'form_language', '不是尖锐几何，而是曲线主导且细节简化')
    assert anchors.candidates(claims)
    assert anchors.verify_signature_support(supported_anchor(), {c.id: c for c in claims})


def test_name_and_confidence_cannot_bypass_feature_evidence(matching_library):
    claims = complete_claims()
    claims[0] = claim('E1', 'medium_rendering', '赛璐璐分色', 1)
    with pytest.raises(ValueError, match='完整特征'):
        anchors.verify_signature_support(supported_anchor(), {c.id: c for c in claims})


def test_new_matcher_is_versioned_and_legacy_anchor_remains_readable():
    assert MATCHER_VERSION == 'style-signatures.v4'
    legacy = StyleAnchor(id='pixar-character-3d', confidence=.9,
        support_claim_ids=['E1', 'E2'], distinguishing_claim_ids=['E1'], reason='旧版保存结果保持可读，不自动重新识别')
    assert legacy.feature_support == []
