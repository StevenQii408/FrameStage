"""Bounded signature recall. Retrieval scores are not identification confidence."""
import hashlib
import json
import re
from functools import lru_cache

from .library import (MATCHER_VERSION, group_evidence, positive_mention, runtime_library)
from .lexicon import ALIASES


@lru_cache(maxsize=1)
def catalog():
    library = runtime_library()
    terms = {term.id: term for term in library.terms}
    rows = []
    for recipe in library.recipes:
        row = recipe.model_dump()
        # Read compatibility only; these broad cues never determine eligibility.
        row['cues'] = list(dict.fromkeys(cue for group in recipe.signature_groups for identifier in group.term_ids
                                        for alternatives in terms[identifier].positive_cues for cue in alternatives))
        row['counter_cues'] = list(dict.fromkeys(cue for group in recipe.signature_groups
                                                for identifier in group.term_ids for cue in terms[identifier].negative_cues))
        rows.append(row)
    return {'version': library.version, 'source_hash': library.source_hash, 'terms': rows}


def _blocked(recipe, claims, library):
    from .evidence import global_claim
    terms = {term.id: term for term in library.terms}
    return any(positive_mention(claim.observation, cue)
               for group in recipe.signature_groups for identifier in group.term_ids
               for cue in terms[identifier].negative_cues for claim in claims
               if claim.dimension == group.dimension and global_claim(claim))


def candidates(claims, limit=12):
    from .inheritance import medium_families
    from .models import safe_visual_text
    from .evidence import global_claim
    claims = [claim for claim in claims if safe_visual_text(claim.observation) and global_claim(claim)
              and getattr(claim, 'confidence', 1) >= .5]
    families = set()
    for claim in claims:
        if claim.dimension == 'medium_rendering':
            families.update(medium_families(claim.observation))
    family = next(iter(families)) if len(families) == 1 else ''
    # An ambiguous or mixed medium never excludes other families before signatures.
    allowed = {'paper': {'handmade'}, 'clay': {'handmade'}, 'painting': {'painting'},
               '2d': {'2d', 'design'}, '3d': {'3d'}, 'photo': {'photo'}}.get(family)
    library = runtime_library()
    terms = {term.id: term for term in library.terms}
    ranked = []
    for recipe in library.recipes:
        if allowed and recipe.family not in allowed or _blocked(recipe, claims, library):
            continue
        matches = group_evidence(recipe, claims, library)
        eligible = len(matches) == len(recipe.signature_groups)
        # Partial predicates are recall signals, never final signature evidence.
        partial = sum(any(positive_mention(c.observation, cue) for c in claims
                          if c.dimension == group.dimension)
                      for group in recipe.signature_groups for tid in group.term_ids
                      for alternatives in terms[tid].positive_cues for cue in alternatives)
        if not matches and not partial and not allowed:
            continue
        row = {'id': recipe.id, 'label': recipe.label, 'family': recipe.family,
               'definition': recipe.definition, 'distinction': recipe.distinction,
               'insufficient_evidence': recipe.insufficient_evidence,
               'signature_groups': [{'id': group.id, 'dimension': group.dimension,
                    'description': '；'.join(terms[t].generation_phrase for t in group.term_ids),
                    'required_terms': [{'id': t, 'positive_cues': terms[t].positive_cues,
                                        'counter_cues': terms[t].negative_cues} for t in group.term_ids]}
                    for group in recipe.signature_groups],
               'matched_group_ids': list(matches),
               'missing_group_ids': [group.id for group in recipe.signature_groups if group.id not in matches],
               'selection_eligible': eligible,
               'semantic_review_allowed': True,
               'recall_signal_count': partial,
               'retrieval_basis': ('signature-combination' if eligible else 'partial-signature-review'
                                   if matches else 'medium-only-review')}
        # Coverage, not raw synonym count; mutually overlapping predicates never add points.
        score = len(matches) / len(recipe.signature_groups)
        ranked.append((eligible, score, partial, recipe.category, recipe.id, row))
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[4]))
    capacity = min(12, max(1, limit))
    # Small families are shown in full. Large all-zero families use category-diverse
    # exploration instead of pretending alphabetical IDs are nearest styles.
    if len(ranked) > capacity and not any(item[1] or item[2] for item in ranked):
        buckets = {}
        for item in ranked:
            buckets.setdefault(item[3], []).append(item)
        selected = []
        while len(selected) < capacity and any(buckets.values()):
            for bucket in buckets.values():
                if bucket and len(selected) < capacity:
                    selected.append(bucket.pop(0))
        rows = [item[-1] for item in selected]
    else:
        rows = [item[-1] for item in ranked[:capacity]]
    for row in rows:
        nearest = next(r.confusable_with for r in library.recipes if r.id == row['id'])
        row['confusable_candidates'] = nearest
        row['alternative_references'] = [{'id': r.id, 'label': r.label,
            'definition': r.definition, 'distinction': r.distinction}
            for r in library.recipes if r.id in nearest]
    return rows


def verify_signature_support(anchor, claims):
    """Deterministic evidence gates for NEW analyses; persisted legacy profiles remain readable."""
    library = runtime_library()
    recipe = next((recipe for recipe in library.recipes if recipe.id == anchor.id), None)
    if recipe is None:
        raise ValueError('锚点配方不存在')
    support = {item.group_id: item for item in anchor.feature_support}
    required = {group.id for group in recipe.signature_groups}
    if set(support) != required or len(support) != len(anchor.feature_support):
        raise ValueError('锚点缺少逐项特征组证据，通用三维/比例/材质不能代替区别性组合')
    union = set()
    for group in recipe.signature_groups:
        refs = set(support[group.id].support_claim_ids)
        if refs - set(anchor.support_claim_ids) or refs - claims.keys():
            raise ValueError('特征组引用必须属于真实锚点支持证据')
        selected = [claims[key] for key in refs]
        if any(claim.dimension != group.dimension for claim in selected):
            raise ValueError('特征组证据维度不一致')
        if group.id not in group_evidence(recipe, selected, library) or support[group.id].semantic_support:
            # The model may establish a semantic relationship without authored
            # keyword spelling. Require one exact quote and reason per required term.
            proofs = support[group.id].semantic_support
            if {p.term_id for p in proofs} != set(group.term_ids) or len(proofs) != len(group.term_ids):
                raise ValueError('事实不支持该完整特征组合，缺少语义证据：' + group.id)
            from .reliability import specificity_score
            from .evidence import global_claim
            for proof in proofs:
                if proof.claim_id not in refs:
                    raise ValueError('语义证据引用不属于该特征组')
                fact = claims[proof.claim_id]
                if proof.quote not in fact.observation or not global_claim(fact):
                    raise ValueError('语义证据必须逐字引用真实、非局部观察')
                if specificity_score(proof.quote, group.dimension) < 1:
                    raise ValueError('通用描述不能充当区别性语义证据')
                if re.search(r'没有|未见|看不到|不具备|不存在|不能确认|无法判断', proof.quote):
                    raise ValueError('否定或未知不能支持正向风格特征')
                if proof.explanation == proof.quote or not proof.explanation.strip():
                    raise ValueError('必须解释观察与配方条件的语义关系')
        union.update(refs)
    # Semantic selection must also address the nearest alternatives, not just
    # repeat a confident label. This remains model-grounded, not human accuracy.
    if any(item.semantic_support for item in support.values()) and recipe.confusable_with:
        if not re.search(r'区别|相比|而非|不同|区分', anchor.reason):
            raise ValueError('语义锚点缺少与相近风格的区别说明')
        alternatives = {a.id: a for a in anchor.alternative_assessments}
        if set(alternatives) != set(recipe.confusable_with) or len(alternatives) != len(anchor.alternative_assessments):
            raise ValueError('语义锚点缺少相近候选逐项裁决')
        for item in alternatives.values():
            if item.verdict != 'distinguishable' or not item.support_claim_ids:
                raise ValueError('相近风格尚不可区分，保留家族特征而非具体锚点')
            if set(item.support_claim_ids) - set(claims):
                raise ValueError('相近候选区别引用不存在')
            if any(not global_claim(claims[key]) for key in item.support_claim_ids):
                raise ValueError('局部特效不能区分全局风格')
    if set(anchor.distinguishing_claim_ids) - union:
        raise ValueError('区别性引用没有支持任何完整特征组合')
    if _blocked(recipe, list(claims.values()), library):
        raise ValueError('锚点与已观察反向线索冲突')
    # Declared nearest alternatives must not be equally fully supported. A confident
    # label cannot resolve this by itself; keep the concrete description instead.
    for other in library.recipes:
        if other.id in recipe.confusable_with and not _blocked(other, list(claims.values()), library):
            if len(group_evidence(other, list(claims.values()), library)) == len(other.signature_groups):
                raise ValueError('相近配方同时完整成立，现有证据不足以区分：' + other.id)
    return {group: support[group].support_claim_ids for group in sorted(support)}


def label(identifier):
    return next((item['label'] for item in catalog()['terms'] if item['id'] == identifier), '')


def digest():
    return hashlib.sha256(json.dumps({'source_hash': runtime_library().source_hash, 'aliases': ALIASES,
                                    'matcher_version': MATCHER_VERSION},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()
