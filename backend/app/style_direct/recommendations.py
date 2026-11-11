"""Evidence-reviewed recipe choices, with library prose attached only after review."""
import json
import math
import re

from .anchors import candidates
from .evidence import normalize_claim
from .library import runtime_library
from .models import EvidenceClaim, RecipeRecommendation, StyleReference
from .references import verify_reference

VERSION = 'style-recipe-recommendations.v1'


def verify_recommendation(decision, claims, rows, assets, scope):
    refs = verify_reference(decision, claims, rows)
    images = {a['alias']: a for a in assets}
    cited = {a for key in refs for a in claims[key].images}
    if cited - images.keys():
        raise ValueError('视觉推荐引用不存在的图片')
    originals = {a['alias'] for a in assets if not a.get('parent_alias')}
    origins = {images[a].get('parent_alias') or a for a in cited}
    if scope == 'project' and (not originals or len(origins & originals) <
            max(1 if len(originals) == 1 else 2, math.ceil(len(originals) * .6))):
        raise ValueError('局部推荐不能晋升为项目画风')
    return refs


def recommendation_cards(decisions):
    library = runtime_library()
    recipes = {r.id: r for r in library.recipes}
    result = []
    for decision in decisions:
        recipe = recipes[decision.id]
        result.append({
            'recipe_id': recipe.id, 'label': recipe.label,
            'fit_level': getattr(decision, 'fit_level', 'medium'),
            'reason': decision.reason,
            'controls': [{'claim_id': f.claim_id, 'quote': f.quote} for f in decision.matched_features],
            'differences': [a.difference for a in decision.alternative_assessments if a.verdict == 'distinguishable'],
            'overlap': [a.difference for a in decision.alternative_assessments if a.verdict == 'indistinguishable'],
            'library_prompt': recipe.compact_prompt,
            'detailed_description': recipe.detailed_description,
            'library_source_hash': library.source_hash,
        })
    return result


def historical_recommendations(result):
    """Read-only revalidation of an actual saved model decision; never ranking fallback.

    Does not replace profile/reference/history, or claim that an old run succeeded.
    Only the latest saved interpretation is considered, so a later null decision
    cannot silently resurrect an earlier, abandoned name.
    """
    if 'recommendations' in result:
        return result['recommendations'], False
    assets = result.get('assets', [])
    images = {a['alias']: a for a in assets}
    claims = {}
    for value in (result.get('evidence') or {}).get('claims', []):
        try:
            fact = EvidenceClaim.model_validate(value)
            if fact.confidence >= .5:
                for normalized in normalize_claim(fact, images):
                    claims[normalized.id] = normalized
        except (ValueError, TypeError):
            continue
    latest = None
    for row in reversed(result.get('raw_responses', [])):
        if row.get('phase') not in ('style.direct.interpretation', 'style.direct.refinement',
                'style.direct.verification', 'style.direct.correction'):
            continue
        try:
            raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', (row.get('response') or '').strip())
            value = json.loads(raw)
            value = value.get('interpretation', value) if isinstance(value, dict) else None
            if value is not None and ('reference' in value or 'recommendations' in value):
                latest = value
                break
        except (ValueError, TypeError, AttributeError):
            continue
    if latest is None:
        latest = result.get('interpretation') or {}
    raw_decisions = latest.get('recommendations', [latest['reference']] if latest.get('reference') else [])
    if not isinstance(raw_decisions, list) or len(raw_decisions) > 3:
        return [], True
    rows = candidates(list(claims.values())) if claims else []
    scope = (result.get('profile') or {}).get('reference_scope', 'reference')
    decisions, selected = [], set()
    for raw in raw_decisions:
        try:
            decision = RecipeRecommendation.model_validate(raw)
            if decision.id in selected:
                continue
            verify_recommendation(decision, claims, rows, assets, scope)
            selected.add(decision.id)
            decisions.append(decision)
        except (ValueError, TypeError):
            continue
    return recommendation_cards(decisions), True


def selected_recipe(result, source):
    """Validate membership plus the current compiled source, not client prose."""
    if not isinstance(source, dict) or set(source) != {'kind', 'recipe_id', 'source_hash'} or source['kind'] != 'library-recipe':
        raise ValueError('创作配方来源无效')
    cards, _ = historical_recommendations(result)
    card = next((c for c in cards if c['recipe_id'] == source['recipe_id']), None)
    library = runtime_library()
    if (not card or source['source_hash'] != card['library_source_hash']
            or source['source_hash'] != library.source_hash):
        raise ValueError('推荐配方或知识库版本已变化，请刷新后选择')
    recipe = next((r for r in library.recipes if r.id == source['recipe_id']), None)
    if (not recipe or card['library_prompt'] != recipe.compact_prompt
            or card['detailed_description'] != recipe.detailed_description):
        raise ValueError('推荐配方内容不再匹配当前知识库')
    return recipe, library.source_hash
