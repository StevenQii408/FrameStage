"""Conservative scope repair for old prose; new prompts request atomic observations.

No repair invents a location, subject, frame reference or additional visual fact.
"""
import re

from .models import visible_clauses

VERSION = "style-evidence-scope.v3"
LOCAL = re.compile(r"仅在|只在|只出现于|单镜|偶发|这一帧|此帧|特定(?:情境|情况|场景)|"
                   r"特定(?:情绪|状态)|随(?:着)?(?:情绪|状态)|(?:在|当).{0,8}(?:发热|出汗)|"
                   r"(?:皮肤|肤色|脸颊).{0,25}(?:泛红|变红|变为|变成)|汗水|汗珠")
INFERENCE = re.compile(r"暗示|推测|推断|可能.*(?:情绪|环境)|情绪渲染")


def normalize_claim(claim, images):
    """Keep a stable historical ID for shared facts, split explicitly local clauses."""
    if claim.appearance_type == 'content':
        return []
    if claim.appearance_type in ('state_change', 'uncertain'):
        claim = claim.model_copy(update={'evidence_scope': 'local' if claim.appearance_type == 'state_change' else 'uncertain'})
    shared, local = [], []
    for sentence in re.split(r"[。；;]|(?<!\d)\.(?!\d)", claim.observation):
        if not sentence.strip():
            continue
        parts = visible_clauses(sentence)
        stable_parts, local_parts, local_refs = [], [], set()
        in_local = False
        for part in parts:
            if INFERENCE.search(part):
                continue  # Narrative/lighting explanations are not direct appearance facts.
            mentioned = set(re.findall(r"(?<![A-Za-z0-9_])I\d+(?![A-Za-z0-9_])", part))
            if LOCAL.search(part) or mentioned and mentioned < set(claim.images):
                in_local = True
                local_refs.update(mentioned)
            if in_local:
                local_parts.append(part)
            else:
                stable_parts.append(part)
        shared.extend(stable_parts)
        if local_parts:
            # A historical all-frame citation does not establish which single frame
            # a vague 'only this frame' refers to. Preserve it as uncertain, not global.
            refs = sorted(local_refs & set(claim.images) & set(images))
            local.append(("，".join(local_parts), refs or claim.images,
                          "local" if refs else "uncertain"))
    results = []
    if shared:
        results.append(claim.model_copy(update={"observation": "，".join(shared),
            "evidence_scope": claim.evidence_scope}))
    for number, (text, refs, scope) in enumerate(local, 1):
        key = claim.id if not results else claim.id[:50] + "-local-" + str(number)
        results.append(claim.model_copy(update={"id": key, "observation": text,
            "images": refs, "evidence_scope": scope}))
    return results


def global_claim(claim):
    return (getattr(claim, 'appearance_type', 'unspecified') not in ('state_change', 'content', 'uncertain')
            and getattr(claim, 'evidence_scope', 'unspecified') not in ("local", "uncertain")
            and not LOCAL.search(claim.observation))


def representative_name(descriptions, anchor_label=""):
    """A grounded family descriptor, not inferred IP authorship or a catalogue fill."""
    if anchor_label:
        return anchor_label.split("｜")[0]
    medium = "，".join(d.professional_description for d in descriptions if d.dimension == "medium_rendering")
    form = "，".join(d.professional_description for d in descriptions if d.dimension == "form_language")
    from .inheritance import medium_families
    from .library import positive_mention
    families = medium_families(medium)
    if len(families) > 1:
        return '混合媒介视觉'
    if families == {'3d'}:
        expanded = any(not re.search(r'没有|并非|不是|不具备|未见|无法|不明显', m.group())
                       and positive_mention(form, m.group()) for m in re.finditer(
                           r'头部[^，；。]{0,6}眼睛[^，；。]{0,12}(?:大于|偏大|放大)|头部[^，；。]{0,15}(?:大于|偏大)', form))
        if any(positive_mention(form, word) for word in ('大头', '大眼', '头部比例显著大于写实标准',
                '头部偏大', '夸张头身比例', '头身比例夸张', '眼睛占据面部较大面积', '眼睛比例放大', '卡通化')) or expanded:
            return "夸张比例的风格化三维角色动画"
        return "三维动画" if "动画" in medium else "三维数字影像"
    for name, words in (("手工剪纸拼贴风格", ('剪纸', '拼贴')), ("手塑黏土视觉", ('黏土', '粘土')),
                        ("水墨绘画风格", ('水墨',)), ("水彩绘画风格", ('水彩',)),
                        ("二维动画", ('二维', '2D', '赛璐璐', '手绘动画')),
                        ("真人摄影", ('真人摄影', '实拍', '摄影'))):
        if any(positive_mention(medium, word) for word in words):
            return name
    return ""


def grounded_name(descriptions, proposed=""):
    """Preserve a safe semantic family name; never introduce an unrelated medium."""
    from .models import safe_visual_text
    from .inheritance import medium_families
    applicable = [d for d in descriptions if d.scope in ('reference', 'global')]
    observed = medium_families('，'.join(d.professional_description for d in applicable
                                       if d.dimension == 'medium_rendering'))
    named = medium_families(proposed)
    from .consistency import check_consistency
    from .library import runtime_library
    catalog_name = any(r.label.split('｜')[0] in proposed for r in runtime_library().recipes)
    consistent = all(check_consistency(proposed, [d.professional_description for d in applicable
                         if d.dimension == dim], dim, reject_new_controls=True)['acceptable']
                     for dim in ('form_language', 'color_tone', 'lighting_shading', 'texture_material'))
    generic = {'', '参考画风', '未知', '画风未知', '整体画面风格', '三维数字影像', '三维动画', '二维动画', '真人摄影', '本镜可见画风'}
    # A family heading summarizes media, not locations, creators or new technical facts.
    if (proposed not in generic and not catalog_name and consistent and len(proposed) <= 40 and safe_visual_text(proposed)
            and not INFERENCE.search(proposed) and not re.search(r'风格不明|无法判断|待确定|渲染器', proposed)
            and named and named == observed):
        return proposed
    return representative_name(applicable)
