"""Evidence-backed visual reference review, distinct from strict signature anchors.

Scores retrieve candidates; they are not calibrated probabilities. Reference cards
are internally authored comparison guidance, not claims about every studio work.
"""
import re
import hashlib
import json

from .anchors import label, _blocked
from .evidence import global_claim
from .inheritance import medium_families
from .library import runtime_library
from .models import safe_visual_text
from .reliability import specificity_score
from .controls import visual_controls

VERSION = 'style-reference-review.v5'

# Do not encode an artist as a universal material/shader signature. These cards
# describe useful visual-development references; selection still needs image facts
# and explicit comparison with the competing references, not studio attribution.
CARDS = {
    'pixar-character-3d': {
        'label': '皮克斯式角色三维动画',
        'visual_reference': '风格化三维角色长片的视觉参考：以可读的角色体量、简化但有表现力的面部、体积光影和表面层次组织画面。',
        'review_questions': ['角色体量与面部简化具体如何组织？', '明暗与表面层次是否服务于体积而非平面装饰？'],
        'caution': '这些特征并非皮克斯独有；大眼、圆润和暖光不能单独区分迪士尼。不能据此认定出品方。',
    },
    'disney-modern-3d': {
        'label': '迪士尼式现代三维动画',
        'visual_reference': '现代角色三维动画的视觉参考：流畅的角色剪影、具有表现力的姿态和面部简化，以及协调角色与环境的色彩光影。',
        'review_questions': ['剪影与面部形状是否存在可描述的节奏和简化方式？', '与另一角色三维参考相比，实际图像中的形体和受光有什么差别？'],
        'caution': '流畅曲线和表情设计并非独有；不能把某种亮带或轮廓尖端当作全部作品的固定指纹。',
    },
    'shinkai-anime': {
        'label': '新海诚式动画电影视觉',
        'visual_reference': '二维动画电影的视觉参考：细致的背景层次、具有明确组织关系的天空与环境光色、简化人物与环境细节的对照。',
        'review_questions': ['人物与背景的细节层次如何分配？', '天空、亮部和暗部是否具有实际可见的光色组织，而不只是饱和度高？'],
        'caution': '蓝天、逆光或二维人物本身不足以命名；只核验帧中的视觉相似，不猜作品或导演。',
    },
}


def reference_label(identifier):
    return CARDS.get(identifier, {}).get('label') or label(identifier).split('｜')[0]


def reference_digest():
    return hashlib.sha256(json.dumps({'version': VERSION, 'cards': CARDS},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def safe_comparison_text(text, identifiers):
    """Names may occur in a comparison, never in quoted observation facts."""
    for identifier in identifiers:
        name = reference_label(identifier)
        text = text.replace(name, '')
        stem = name.split('式')[0]
        if '式' in name and len(stem) >= 2:
            text = text.replace(stem, '')
    return safe_visual_text(text, comparison=True)


def review_candidates(rows):
    """Bounded, concise cards; no complete generation recipe in model context."""
    return [{
        'id': row['id'], 'label': reference_label(row['id']), 'family': row['family'],
        'visual_reference': row['definition'],
        'review_questions': CARDS.get(row['id'], {}).get('review_questions',
            [group['description'] for group in row['signature_groups']]),
        'caution': CARDS.get(row['id'], {}).get('caution', '特征是观察方向，不是必须凑齐的清单；不复制生成配方。'),
        'counter_cues': list(dict.fromkeys(cue for group in row['signature_groups']
            for term in group['required_terms'] for cue in term['counter_cues'])),
        'nearest_alternatives': row['confusable_candidates'],
        'alternative_references': [{
            'id': a['id'], 'label': reference_label(a['id']),
            'visual_reference': a['definition'],
            'caution': CARDS.get(a['id'], {}).get('caution', '按实际可见的区别比较，不要求补造配方全部特征'),
        } for a in row['alternative_references']],
        'retrieval_basis': row['retrieval_basis'],
    } for row in rows]


def verify_reference(reference, claims, rows):
    """Validate evidence for contrastive names or non-exclusive visual-fit references.

    Neither mode is a calibrated classifier of authorship. The relaxed fit mode
    acknowledges near-neighbour overlap instead of inventing negative evidence.
    """
    recipes = {r.id: r for r in runtime_library().recipes}
    allowed = {r['id'] for r in rows}
    if reference.id not in allowed or reference.confidence < .85:
        raise ValueError('视觉参考非本轮候选或模型判断不足；不按排名回退')
    recipe = recipes[reference.id]
    families = medium_families('，'.join(c.observation for c in claims.values()
                                       if c.dimension == 'medium_rendering' and global_claim(c)))
    expected = {'3d': {'3d'}, '2d': {'2d'}, 'photo': {'photo'},
                'handmade': {'paper', 'clay'}, 'painting': {'painting'}, 'design': {'2d'}}[recipe.family]
    if not families or not families <= expected:
        raise ValueError('视觉参考与已观察媒介不一致或媒介仍混合不明')
    if _blocked(recipe, list(claims.values()), runtime_library()):
        raise ValueError('视觉参考与明确可见的反向证据冲突')
    selected, distinctive, controls = set(), set(), set()
    for feature in reference.matched_features:
        fact = claims.get(feature.claim_id)
        if (not fact or fact.confidence < .5 or not global_claim(fact)
                or feature.quote not in fact.observation or not safe_visual_text(feature.quote)):
            raise ValueError('参考特征必须逐字引用可靠、非局部观察')
        # A positive shape/light observation may finish with an explicit negative
        # contrast. Only the negative CLAUSE is excluded from positive support.
        # Never reject the whole useful observation because it says '没有强烈反差'.
        positive_quote = '，'.join(part for part in re.split(r'[，；。]', feature.quote)
            if not re.match(r'^(?:没有|未见|看不到|不具备|不存在|不能确认|无法判断|无(?:可见|强烈))', part.strip()))
        if not positive_quote:
            raise ValueError('否定或未知不能作为参考的正向支持')
        if feature.relevance == feature.quote or not safe_comparison_text(feature.relevance, allowed):
            raise ValueError('参考解释必须说明视觉关系，不能猜算法或混入内容')
        selected.add(fact.id)
        if specificity_score(positive_quote, fact.dimension) or fact.visual_aspects:
            distinctive.add(fact.dimension)
        aliases = {'volume_hierarchy': 'shape_hierarchy', 'surface_partition': 'material_partition',
                   'low_frequency_surface': 'surface_frequency', 'paper_structure': 'media_texture',
                   'brush_structure': 'media_texture', 'handmade_marks': 'media_texture', 'palette_limit': 'palette'}
        controls.update((dimension, aliases.get(aspect, aspect))
                        for dimension, aspect in visual_controls(positive_quote, fact.dimension))
        # New atomic observations carry typed aspects. A shortened quote must
        # still quote that observation; medium and local states never count.
        if (fact.dimension not in ('medium_rendering', 'color_tone') and fact.appearance_type == 'visual_language'
                and feature.quote.strip('。') == fact.observation.strip('。')):
            controls.update((fact.dimension, aspect) for aspect in fact.visual_aspects)
    if len(selected) < 2 or len({claims[k].dimension for k in selected}) < 2 or len(distinctive) < 2:
        raise ValueError('通用媒介、大眼和暖色不足以命名；至少两个维度的具体证据')
    if not set(reference.distinguishing_claim_ids) <= selected:
        raise ValueError('区别性证据不属于已核验参考特征')
    if not any(specificity_score(claims[k].observation, claims[k].dimension) or claims[k].visual_aspects
               for k in reference.distinguishing_claim_ids):
        raise ValueError('缺少具体区别性证据')
    # Common rounded eyes + soft shadows can be distinctive relative to real
    # photography, but do not distinguish nearby animation references. Require
    # at least one quoted, relational visual control rather than mere adjectives.
    relational = re.compile(r'分区|梯级|冷暖分离|交替|限定.{0,8}色|双色|网点|切线|随形|'
                            r'纸纤维|裁切|压痕|笔触.{0,8}方向|颗粒.{0,8}分布|'
                            r'亮.{0,8}暗.{0,8}(?:对照|对比)|层次.{0,8}(?:对照|对比)|'
                            r'轮廓.{0,12}(?:收束|渐细|折线)|反射.{0,12}(?:宽度|范围)')
    if reference.selection_basis == 'visual_fit':
        # Overlapping visual families are legitimate recommendations, but require
        # several concrete controls. A generic medium + warm light pair is not one.
        if len(controls) < 3 or len({dim for dim, _ in controls}) < 2:
            raise ValueError('视觉适配参考至少需要三条具体控制点、覆盖两个维度')
    elif not any(relational.search(feature.quote) for feature in reference.matched_features
                 if feature.claim_id in reference.distinguishing_claim_ids):
        raise ValueError('通用轮廓和柔光尚无近邻区别依据，需定向核验')
    comparisons = {a.id: a for a in reference.alternative_assessments}
    if len(comparisons) != len(reference.alternative_assessments):
        raise ValueError('相近候选裁决重复')
    # Always compare curated nearest alternatives, plus any other candidate that
    # is already fully supported by literal evidence. Missing words are not negatives.
    required = set(recipe.confusable_with) if reference.selection_basis == 'distinctive' else set()
    required.update(row['id'] for row in rows if row['selection_eligible'] and row['id'] != reference.id)
    required.discard(reference.id)
    if not required <= comparisons.keys():
        raise ValueError('视觉参考缺少相近候选比较')
    same_medium_nearby = {identifier for identifier in recipe.confusable_with
                          if identifier in recipes and recipes[identifier].family == recipe.family}
    if (reference.selection_basis == 'visual_fit' and same_medium_nearby
            and not (same_medium_nearby & comparisons.keys())):
        raise ValueError('视觉适配参考至少需要一个相关近邻比较')
    valid_ids = allowed | set(recipe.confusable_with)
    if not safe_comparison_text(reference.reason, valid_ids):
        raise ValueError('参考理由混入技术猜测或内容')
    for alternative in reference.alternative_assessments:
        if alternative.id == reference.id or alternative.id not in valid_ids:
            raise ValueError('比较对象不是相关候选')
        overlap = alternative.verdict == 'indistinguishable'
        if (alternative.verdict == 'insufficient_evidence'
                or (overlap and reference.selection_basis != 'visual_fit')
                or not alternative.support_claim_ids):
            raise ValueError('相近候选仍不可区分，不强制输出一个名称')
        if overlap and (alternative.id not in recipes or recipes[alternative.id].family != recipe.family):
            raise ValueError('不同媒介家族不能以相近视觉适配为由忽略区别')
        if (set(alternative.support_claim_ids) - claims.keys()
                or any(not global_claim(claims[k]) or claims[k].confidence < .5 for k in alternative.support_claim_ids)):
            raise ValueError('候选区别必须引用已核验的非局部事实')
        if not any(specificity_score(claims[k].observation, claims[k].dimension) or claims[k].visual_aspects
                   for k in alternative.support_claim_ids):
            raise ValueError('候选区别只引用了通用媒介词')
        # Comparisons may cite other validated observations (e.g. palette), not
        # just the selected reference's three strongest controls.
        if overlap and len(set(alternative.support_claim_ids)) < 2:
            # Shared proportions and hair construction can legitimately explain
            # why two character-film references overlap. The PRIMARY reference
            # still requires >=3 concrete controls across >=2 dimensions above.
            raise ValueError('重叠参考必须引用至少两条共同视觉依据')
        if re.search(r'未描述|未提到|没有提及|未观察到.{0,8}所以', alternative.difference):
            raise ValueError('观察未提及不是反例，不能据此排除相近候选')
        if not safe_comparison_text(alternative.difference, valid_ids):
            raise ValueError('候选区别混入技术猜测或内容')
    return sorted(selected)
