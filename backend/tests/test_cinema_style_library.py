"""Offline structure and evidence tests, not claims of real visual accuracy."""
import copy
import json
from collections import Counter

import pytest
from pydantic import ValidationError

from app.style_direct import anchors
from app.style_direct.library import (ARTIFACT, CinemaLibrary, StyleTerm, build, group_evidence,
                                     positive_mention, read_source, runtime_library, search)
from app.style_direct.models import EvidenceClaim, StyleAnchor, StyleInterpretationV3
from app.style_direct.models import safe_visual_text
from test_style_direct import evidence, interpretation, pipeline


def mini():
    def term(identifier, dimension, cues, negatives=None):
        return dict(id=identifier, label=identifier + '明确视觉特征', dimension=dimension,
            definition='通过同一观察中的具体视觉关系定义特征，不能由通用媒介词单独推断。', aliases=[],
            positive_cues=cues, negative_cues=negatives or [], generation_phrase='具有可验证的具体视觉关系',
            scope='global', source_ids=['internal-authored'])
    terms = [term('test.cool-warm', 'color_tone', [['青蓝', '冷蓝'], ['暗部', '阴影'], ['暖色', '琥珀'], ['高光', '亮部']]),
             term('test.low-key', 'lighting_shading', [['低调', '大面积暗部'], ['层次', '细节']], ['高调均匀亮白'])]
    recipe = dict(id='test.noir', label='新黑色电影式冷暖分离影调', family='photo', category='cinema',
        definition='以低调明暗组织和明确的冷暖色彩分离形成克制紧张的视觉配方。',
        signature_groups=[dict(id='tone', dimension='color_tone', term_ids=['test.cool-warm']),
                          dict(id='light', dimension='lighting_shading', term_ids=['test.low-key'])],
        supporting_term_ids=[], insufficient_evidence=['只有夜景', '只有蓝色画面'], confusable_with=[],
        distinction='蓝色画面或夜景并不意味着冷暖分离与低调层次同时存在，必须核验具体组合。',
        compact_prompt='新黑色电影式冷暖分离影调，青蓝暗部与少量暖色高光形成张力，整体低调，阴影边界清晰，黑位深沉但保留层次，肤色表现自然克制。',
        detailed_description='这是一种明确的视觉配方分支，而不是所有新黑色电影共有的唯一外观。识别需要冷暖组织与低调层次共同成立，单独的夜景、蓝色或暗曝光不能完成判断；没有可验证的组合时仍保留实际观察，不把参考配方补写成事实。',
        generation_constraints=['避免无依据的全画面霓虹泛光'], source_ids=['internal-authored'])
    source = dict(id='internal-authored', name='测试自编', url='', license='Project-authored',
                  policy='authored-reference', revision='test.v1', notes='无外部文本导入')
    card = dict(id='test.qa', title='夜景不等于新黑色电影',
        summary='夜景描述时间条件，不能单独定义视觉流派。需要综合色彩组织和明暗层次共同提供证据，不能依据一个夜景标签补造完整的电影画风，也不能把生成建议直接作为观察事实。',
        related_recipe_ids=['test.noir'], related_term_ids=['test.cool-warm'],
        questions=[dict(question='夜景是否足以确定这个风格？', answer='不足，需要冷暖分离和低调层次等实际证据共同成立。'),
                   dict(question='无法确定名称时如何输出？', answer='保留已观察到的具体视觉特征，不强行命名或补造配方。')],
        source_ids=['internal-authored'])
    return CinemaLibrary.model_validate(dict(version='cinema-style-library.v1', terms=terms,
        recipes=[recipe], knowledge=[card], sources=[source]))


def facts(text='青蓝暗部与少量暖色高光分离', light='整体低调，黑位保留细节与明暗层次'):
    return [EvidenceClaim(id='E1', dimension='medium_rendering', observation='真人摄影影像', images=['I1'], confidence=.9),
            EvidenceClaim(id='E2', dimension='color_tone', observation=text, images=['I1'], confidence=.9),
            EvidenceClaim(id='E3', dimension='lighting_shading', observation=light, images=['I1'], confidence=.9)]


@pytest.fixture
def small_library(monkeypatch):
    library = mini()
    monkeypatch.setattr(anchors, 'runtime_library', lambda: library)
    anchors.catalog.cache_clear()
    yield library
    anchors.catalog.cache_clear()


def supported_anchor():
    return StyleAnchor(id='test.noir', confidence=.92, support_claim_ids=['E2', 'E3'],
        distinguishing_claim_ids=['E2'], reason='冷暖分离与低调层次来自两个不同维度的实际观察',
        feature_support=[dict(group_id='tone', support_claim_ids=['E2']), dict(group_id='light', support_claim_ids=['E3'])])


def test_coverage_and_legacy_ids():
    library = runtime_library()
    assert len(library.recipes) >= 120 and len(library.terms) >= 250 and len(library.knowledge) >= 40
    counts = Counter(recipe.category for recipe in library.recipes)
    assert counts['cinema'] >= 40 and counts['documentary'] >= 20 and counts['commercial'] >= 10
    assert counts['animation2d'] >= 15 and counts['animation3d'] >= 10
    assert sum(counts[k] for k in ('handmade', 'painting', 'design')) >= 25
    legacy = json.loads(ARTIFACT.with_name('anchors.v1.json').read_text())
    assert {t['id'] for t in legacy['terms']} <= {r.id for r in library.recipes}
    assert read_source().source_hash == library.source_hash
    assert len({r.compact_prompt for r in library.recipes}) == len(library.recipes)
    assert all(safe_visual_text(r.compact_prompt) for r in library.recipes)


def test_authored_signatures_have_no_internal_countercue_conflict():
    # Synthetic sentences check catalogue consistency, not real recognition accuracy.
    library = runtime_library()
    terms = {t.id: t for t in library.terms}
    for recipe in library.recipes:
        claims = [EvidenceClaim(id='E' + str(i), dimension=g.dimension,
            observation='，'.join(cues[0] for tid in g.term_ids for cues in terms[tid].positive_cues),
            images=['I1'], confidence=.9) for i, g in enumerate(recipe.signature_groups)]
        assert len(group_evidence(recipe, claims, library)) == len(recipe.signature_groups), recipe.id
        assert not anchors._blocked(recipe, claims, library), recipe.id


def test_source_policies_are_explicit():
    sources = {s.id: s for s in runtime_library().sources}
    assert sources['mj-reference'].policy == sources['fooocus'].policy == 'research-only'
    assert sources['met-open-access'].policy == 'open-data'
    assert 'unverified' in sources['photographic-krea'].revision


def test_static_reference_signatures_do_not_require_invisible_motion():
    library = runtime_library()
    assert all(g.dimension != 'motion_signature' for r in library.recipes for g in r.signature_groups)
    assert all(len(r.compact_prompt) >= 50 and len(r.detailed_description) >= 80 for r in library.recipes)
    assert library.taxonomy is not None


def test_taxonomy_rejects_non_core_global_dimension():
    from app.style_direct.library import Taxonomy
    data = dict(version='cinema-style-taxonomy.v1', dimensions=['medium_rendering', 'photography'],
        non_global_dimensions=['photography', 'local_effects', 'technical_knowledge'],
        category_targets={'cinema': 40}, rules=[])
    with pytest.raises(ValidationError):
        Taxonomy.model_validate(data)


@pytest.mark.parametrize('change', ['extra', 'duplicate', 'bad-term', 'wrong-dimension', 'local-term', 'bad-source', 'bad-recipe'])
def test_schema_rejects_invalid_relationships(change):
    data = mini().model_dump()
    if change == 'extra': data['recipes'][0]['unsupported'] = '不能忽略'
    elif change == 'duplicate': data['terms'].append(copy.deepcopy(data['terms'][0]))
    elif change == 'bad-term': data['recipes'][0]['signature_groups'][0]['term_ids'] = ['missing']
    elif change == 'wrong-dimension': data['recipes'][0]['signature_groups'][0]['dimension'] = 'texture_material'
    elif change == 'local-term': data['terms'][0]['scope'] = 'shot'
    elif change == 'bad-source': data['knowledge'][0]['source_ids'] = ['missing-source']
    else: data['recipes'][0]['confusable_with'] = ['missing-recipe']
    with pytest.raises(ValidationError): CinemaLibrary.model_validate(data)


def test_duplicate_conditions_rejected():
    data = mini().terms[0].model_dump()
    data['positive_cues'] = [['青蓝'], ['青蓝']]
    with pytest.raises(ValidationError): StyleTerm.model_validate(data)


@pytest.mark.parametrize('text,cue', [('没有网点阴影', '网点'), ('并非青蓝暗部', '青蓝'),
    ('不是三维动画', '三维'), ('not warm tones', '暖调'), ('without hard light', '硬光')])
def test_negated_mentions_are_not_positive(text, cue):
    assert not positive_mention(text, cue)


def test_positive_stylistic_absence():
    assert positive_mention('采用无轮廓线的平面色块', '无轮廓线')
    assert positive_mention('不是暖色影调；实际为青蓝暗部', '青蓝')


def test_cross_dimension_complete_signature(small_library):
    selected = anchors.candidates(facts())
    assert selected[0]['selection_eligible'] and selected[0]['matched_group_ids'] == ['tone', 'light']
    assert len(selected) <= 6
    assert all('compact_prompt' not in row and 'detailed_description' not in row for row in selected)
    assert anchors.verify_signature_support(supported_anchor(), {c.id: c for c in facts()})
    weak = anchors.candidates(facts('整体蓝色画面', '整体低调'))
    assert weak and not weak[0]['selection_eligible']
    assert weak[0]['retrieval_basis'] == 'medium-only-review'


def test_relationship_not_synthesized_from_separate_facts():
    library = mini()
    split = facts('画面可见青蓝暗部')
    split.append(EvidenceClaim(id='E4', dimension='color_tone', observation='有暖色高光', images=['I1'], confidence=.9))
    assert 'tone' not in group_evidence(library.recipes[0], split, library)


@pytest.mark.parametrize('text', ['三维动画，几何比例，材质表现', '红色衣服与蓝色饮料',
                                '没有青蓝暗部与暖色高光', '温暖柔和光影'])
def test_generic_content_and_negation_do_not_identify_style(small_library, text):
    assert all(not row['selection_eligible'] for row in anchors.candidates(facts(text, '低调')))


def test_feature_support_wrong_dimension_and_missing_relationship(small_library):
    data = supported_anchor().model_dump()
    data['feature_support'][0]['support_claim_ids'] = ['E3']
    with pytest.raises(ValueError, match='维度'):
        anchors.verify_signature_support(StyleAnchor.model_validate(data), {c.id: c for c in facts()})
    with pytest.raises(ValueError, match='完整特征'):
        anchors.verify_signature_support(supported_anchor(), {c.id: c for c in facts('蓝色画面')})


def test_missing_or_duplicate_group(small_library):
    anchor = supported_anchor()
    anchor.feature_support = anchor.feature_support[:1] * 2
    with pytest.raises(ValueError, match='逐项特征组'):
        anchors.verify_signature_support(anchor, {c.id: c for c in facts()})


def test_equal_confusable_candidate_rejected(monkeypatch):
    data = mini().model_dump()
    other = copy.deepcopy(data['recipes'][0]); other['id'] = 'test.similar'
    data['recipes'][0]['confusable_with'] = [other['id']]; data['recipes'].append(other)
    monkeypatch.setattr(anchors, 'runtime_library', lambda: CinemaLibrary.model_validate(data))
    with pytest.raises(ValueError, match='相近配方同时'):
        anchors.verify_signature_support(supported_anchor(), {c.id: c for c in facts()})


def test_bad_name_preserves_descriptions_without_retry(tmp_path):
    b = interpretation()
    b['anchor'] = dict(id='pixar-character-3d', confidence=.99, support_claim_ids=['C1', 'C2'],
        distinguishing_claim_ids=['C2'], reason='仅通用三维和比例不能支持具体风格名称', feature_support=[])
    p, provider = pipeline(tmp_path, [evidence(), b]); result = p.run()
    assert result['profile']['anchor'] is None and result['profile']['descriptions']
    assert len(provider.calls) == 2 and not result['budget']['correction_used']
    assert '皮克斯' not in result['profile']['rendered_prompt']
    from app.style_direct.library import MATCHER_VERSION
    assert result['profile']['provenance']['anchor_matcher_version'] == MATCHER_VERSION


def test_history_readable_and_json_schema_strict():
    legacy = StyleAnchor(id='pixar-character-3d', confidence=.9, support_claim_ids=['E1', 'E2'],
        distinguishing_claim_ids=['E2'], reason='历史结果保留读取，不静默重新执行')
    assert legacy.feature_support == []
    schema = StyleInterpretationV3.model_json_schema()
    assert 'feature_support' in schema['$defs']['StyleAnchor']['properties']
    assert schema['$defs']['AnchorFeatureSupport']['additionalProperties'] is False


def test_search_offline_citable(monkeypatch):
    monkeypatch.setattr('httpx.Client.request', lambda *a, **k: pytest.fail('network forbidden'))
    results = search('剪纸', section='recipes')
    assert results and all(row['sources'] for row in results)
    assert search('胶片', section='knowledge') and search('', library=mini()) == []
    with pytest.raises(ValueError): search('电影', section='undefined', library=mini())


def test_failed_build_keeps_artifact(tmp_path):
    artifact = tmp_path / 'compiled.json'; artifact.write_text('previous')
    (tmp_path / 'sources.v1.json').write_text('{"version":"wrong","sources":[]}')
    with pytest.raises(ValueError): build(tmp_path, artifact)
    assert artifact.read_text() == 'previous'
