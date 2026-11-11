from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

Dimension = Literal["medium_rendering", "form_language", "color_tone", "lighting_shading", "texture_material", "motion_signature"]
DIMENSIONS = ("medium_rendering", "form_language", "color_tone", "lighting_shading", "texture_material", "motion_signature")
RENDERER_VERSION = "style-compact.v10"
TECHNIQUE = re.compile(r"次表面散射|全局光照|路径追踪|光线追踪|(?<![A-Za-z0-9_])(?:SSS|PBR|GLSL|SSAO|Octane|Blender|Unreal|Kodak|Pixar|Disney|Ghibli|Shinkai)(?![A-Za-z0-9_])|ray.?tracing|subsurface\s+scattering|global\s+illumination|path\s+tracing|皮克斯|迪士尼|新海诚|吉卜力", re.I)
CONTENT = re.compile(r"黑发(?:少女|女孩)|白(?:色)?裙|(?:穿着|身穿)|(?:木质|木头)桌|银色戒指|橙色饮料|"
                     r"衣服|衣物|服装|布料|织物|发丝|毛发|果汁|饮料|卷曲|卷发|(?:头发|发型).{0,8}(?:蓬松|黑色|棕色|金色)|家具|道具|"
                     r"\b(?:clothing|clothes|costume|wardrobe|fabric|fabrics|shirt|dress|furniture|props|hairstyle|hairstyles)\b|"
                     r"\bhair\s+(?:strands|color|colour)|\bcurly\s+hair|\bwood(?:en)?\s+table|\bsilver\s+ring|"
                     r"\b(?:juice|drink|drinks)\b|\b(?:kitchen|bedroom|living\s+room)\b", re.I)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceClaim(StrictModel):
    id: str = Field(min_length=1, max_length=60)
    dimension: Dimension
    observation: str = Field(min_length=2, max_length=300)
    images: list[str] = Field(min_length=1, max_length=6)
    confidence: float = Field(ge=0, le=1)
    # Optional on historical records; new observations identify one target and scope.
    target: str = Field(default="", max_length=80)
    evidence_scope: Literal["unspecified", "shared", "local", "uncertain"] = "unspecified"
    appearance_type: Literal["unspecified", "visual_language", "state_change", "content", "uncertain"] = "unspecified"
    # Typed semantic aspects supplement, not replace, the actual quoted observation.
    visual_aspects: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def valid_aspects(self):
        allowed = {
            "medium_rendering": {"volume", "flat_layers", "mixed_media"},
            "form_language": {"proportions", "contour", "face_simplification", "hair_blocks", "shape_hierarchy"},
            "color_tone": {"palette", "temperature_relationship", "saturation", "value_structure"},
            "lighting_shading": {"shadow_edge", "tonal_structure", "highlight_extent", "shape_following_light"},
            "texture_material": {"media_texture", "surface_simplification", "surface_frequency", "material_partition"},
            "motion_signature": set(),
        }
        if len(set(self.visual_aspects)) != len(self.visual_aspects) or set(self.visual_aspects) - allowed[self.dimension]:
            raise ValueError("visual_aspects 必须为本维度的不同视觉控制类型")
        return self


class EvidenceGap(StrictModel):
    dimension: Dimension
    question: str = Field(min_length=2, max_length=200)
    image: str
    tool: Literal["crop_image", "compare_regions", "compare_color"]
    regions: list[list[float]] = Field(min_length=1, max_length=2)
    expected_evidence: str = Field(min_length=2, max_length=200)

    @model_validator(mode="after")
    def valid_regions(self):
        for box in self.regions:
            if len(box) != 4 or not 0 <= box[0] < box[2] <= 1 or not 0 <= box[1] < box[3] <= 1:
                raise ValueError("裁剪区域必须是归一化 [x1,y1,x2,y2]")
        return self


class StyleEvidenceV3(StrictModel):
    claims: list[EvidenceClaim] = Field(default_factory=list, max_length=30)
    gaps: list[EvidenceGap] = Field(default_factory=list, max_length=4)
    uncertainties: list[str] = Field(default_factory=list, max_length=12)


class InterpretationClaim(StrictModel):
    dimension: Dimension
    core_concept: str = Field(min_length=2, max_length=80)
    professional_description: str = Field(min_length=2, max_length=300)
    support_claim_ids: list[str] = Field(min_length=1, max_length=12)
    scope: Literal["reference", "global", "scene", "local"] = "reference"
    scene_id: Optional[str] = None
    image_alias: Optional[str] = None
    concise_phrase: str = Field(default="", max_length=80)


class SemanticTermSupport(StrictModel):
    term_id: str
    claim_id: str
    quote: str = Field(min_length=6, max_length=300)
    explanation: str = Field(min_length=12, max_length=240)


class AnchorFeatureSupport(StrictModel):
    group_id: str = Field(min_length=1, max_length=80)
    support_claim_ids: list[str] = Field(min_length=1, max_length=12)
    semantic_support: list[SemanticTermSupport] = Field(default_factory=list, max_length=8)


class AnchorAlternativeAssessment(StrictModel):
    id: str
    verdict: Literal["distinguishable", "indistinguishable", "insufficient_evidence"]
    support_claim_ids: list[str] = Field(default_factory=list, max_length=12)
    difference: str = Field(min_length=12, max_length=240)


class StyleAnchor(StrictModel):
    id: str
    confidence: float = Field(ge=0, le=1)
    support_claim_ids: list[str] = Field(min_length=2, max_length=12)
    distinguishing_claim_ids: list[str] = Field(min_length=1, max_length=8)
    reason: str = Field(min_length=8, max_length=240)
    # Optional for old saved profiles; new pipeline decisions require complete groups.
    feature_support: list[AnchorFeatureSupport] = Field(default_factory=list, max_length=5)
    alternative_assessments: list[AnchorAlternativeAssessment] = Field(default_factory=list, max_length=8)


class ReferenceFeature(StrictModel):
    claim_id: str
    quote: str = Field(min_length=6, max_length=300)
    relevance: str = Field(min_length=12, max_length=240)


class StyleReference(StrictModel):
    """Closest generation reference, never an attribution of studio/authorship."""
    id: str
    confidence: float = Field(ge=0, le=1)
    # Historical references retain their stricter, contrastive interpretation.
    # New visual-fit recommendations may acknowledge overlap with nearby styles.
    selection_basis: Literal["distinctive", "visual_fit"] = "distinctive"
    matched_features: list[ReferenceFeature] = Field(min_length=2, max_length=8)
    distinguishing_claim_ids: list[str] = Field(min_length=1, max_length=8)
    reason: str = Field(min_length=12, max_length=240)
    alternative_assessments: list[AnchorAlternativeAssessment] = Field(min_length=1, max_length=12)


class GenerationConstraint(StrictModel):
    text: str = Field(min_length=2, max_length=150)
    support_claim_ids: list[str] = Field(min_length=1, max_length=12)


class RecipeRecommendation(StyleReference):
    """Model fit decision; all recipe text is attached from the trusted library."""
    fit_level: Literal["high", "medium"] = "medium"


class StyleInterpretationV3(StrictModel):
    descriptive_name: str = Field(default="参考画风", max_length=80)
    descriptions: list[InterpretationClaim] = Field(default_factory=list, max_length=18)
    generation_constraints: list[GenerationConstraint] = Field(default_factory=list, max_length=6)
    uncertainties: list[str] = Field(default_factory=list, max_length=12)
    anchor: Optional[StyleAnchor] = None
    reference: Optional[StyleReference] = None
    recommendations: list[RecipeRecommendation] = Field(default_factory=list, max_length=3)
    investigation_gaps: list[EvidenceGap] = Field(default_factory=list, max_length=2)


class StyleVerificationV1(StrictModel):
    new_evidence: StyleEvidenceV3
    interpretation: StyleInterpretationV3


class StyleProfileV3(StrictModel):
    schema_version: str = "style-profile.v3"
    reference_scope: Literal["reference", "project"] = "reference"
    descriptive_name: str = "参考画风"
    descriptions: list[InterpretationClaim] = Field(default_factory=list)
    generation_constraints: list[GenerationConstraint] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    rendered_prompt: str = ""
    detailed_prompt: str = ""
    scene_prompts: dict[str, str] = Field(default_factory=dict)
    local_prompts: dict[str, str] = Field(default_factory=dict)
    edited_prompt: Optional[str] = Field(default=None, max_length=1200)
    application_status: Literal["ready", "partial", "user-edited"] = "partial"
    version: int = 1
    profile_hash: str = ""
    provenance: dict = Field(default_factory=dict)
    anchor: Optional[StyleAnchor] = None
    reference: Optional[StyleReference] = None

    def digest(self):
        payload = self.model_dump(exclude={"profile_hash"})
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def safe_visual_text(text: str, *, comparison: bool = False) -> bool:
    if TECHNIQUE.search(text):
        return False
    # Representation of hair is form language; hair identity/colour is content.
    candidate = text
    if comparison:
        # A negative comparison of depiction is not a description of hair identity.
        # Keep this exception narrow; clothing/colour/prop checks still apply.
        candidate = re.sub(r"(?:区别于|而非|不是|不同于|非)(?:写实|真实|单根|细碎)?(?:毛发|发丝)(?:模拟|表现|描绘)?", "", candidate)
    if re.search(r"块面|体块|几何化|成束|简化.*发|发.*简化", text) and re.search(r"头发|发丝|毛发|发束", text):
        candidate = re.sub(r"卷曲|卷发|发丝|毛发", "", candidate)
    return not bool(CONTENT.search(candidate) or re.search(r"玻璃杯|木桌|餐桌|沙发|银戒指", candidate))


def visible_clauses(text):
    from .lexicon import clauses
    # Do not detach '呈现哑光质感' from a rejected clothing/prop sentence.
    output = []
    for sentence in re.split(r"[。；;]|(?<!\d)\.(?!\d)", text):
        blocked_subject = False
        for part in clauses(sentence):
            if not safe_visual_text(part):
                blocked_subject = bool(CONTENT.search(part) or re.search(r"玻璃杯|木桌|餐桌|沙发", part))
                continue
            independent = bool(re.search(r"^(?:皮肤|画面|整体|角色|轮廓|色彩|阴影|高光|光照|头发|表面|背景|前景|身体)", part))
            if blocked_subject and not independent:
                continue
            if independent:
                blocked_subject = False
            if len(part) >= 2:
                output.append(part)
    return output


def safe_generation_text(text, anchor=None, *, recipe=None):
    from .anchors import label
    if recipe:
        # A user-selected library recipe may name only that trusted identity.
        # No fabricated reference/confidence is needed to permit its own title.
        text = text.replace(recipe.label, '').replace(recipe.label.split('｜')[0], '')
        stem = recipe.label.split('式')[0]
        if '式' in recipe.label and len(stem) >= 2:
            text = text.replace(stem, '')
    elif anchor:
        from .references import reference_label
        anchor_name = reference_label(anchor.id) if isinstance(anchor, StyleReference) else label(anchor.id)
        text = text.replace(anchor_name, '').replace(anchor_name.split('｜')[0], '')
        # Historical titles can be shorter than the current recipe title.
        # Strip only the verified identity, never other artists or techniques.
        stem = anchor_name.split('式')[0]
        if '式' in anchor_name and len(stem) >= 2:
            text = text.replace(stem + '式', '')
    return safe_visual_text(text)


def compact_features(items, anchor_id="", limit=120, *, descriptive_name="", reference_id=""):
    """Choose whole visual clauses; detailed evidence stays intact. Never cut mid-word."""
    from .anchors import label
    from .consistency import consistent_phrase
    from .reliability import specificity_score
    from .evidence import grounded_name
    from .references import reference_label
    from .controls import visual_controls, generic_control_clause
    name = (reference_label(reference_id) + "（视觉参考）" if reference_id else
            label(anchor_id).split('｜')[0] if anchor_id else grounded_name(items, descriptive_name))
    chunks = [name] if name else []
    chunks = [value for value in chunks if value]
    if not chunks:
        # A compact phrase such as 'smooth surfaces' must not erase the observed medium.
        for item in items:
            if item.dimension != 'medium_rendering':
                continue
            for part in re.split(r'[，；。]', item.professional_description):
                part = re.sub(r'^(?:(?:画面|图像)(?:呈现为?|具有)|视觉媒介为)', '', part).strip(' ：，；。')
                if safe_visual_text(part) and re.search(r'三维|3[Dd]|二维|2[Dd]|摄影|实拍|动画|手绘|水彩|水墨|剪纸|黏土|油画|拼贴|矢量|像素', part) and len(part) <= 35:
                    chunks.append(part)
                    break
            if chunks:
                break
    # Pool complementary observations before choosing clauses. Do not let the
    # first face-proportion description erase a second, evidenced hair-shape one.
    pools = {}
    for dim in DIMENSIONS:
        if dim == 'medium_rendering' and chunks:
            continue  # A family name already covers medium; do not spend density on exclusions.
        options = [item for item in items if item.dimension == dim]
        queues = []
        for item in options:
            if anchor_id and dim == "medium_rendering":
                continue
            # A lossy model short phrase cannot outrank richer existing shape/shadow facts.
            long_text = item.professional_description
            short_text = consistent_phrase(item)
            strength = lambda value: sum(specificity_score(p, dim) for p in re.split(r'[，；。]', value))
            text = short_text if short_text and strength(short_text) >= strength(long_text) else long_text
            # Parentheses can contain the strongest visible controls (大眼、小鼻).
            # Flatten them; do not throw away their contents as if all were notes.
            # Object examples interrupt an otherwise complete lighting clause;
            # unlike shape evidence '(大眼、小鼻)', they are not style controls.
            text = re.sub(r"[（(](?:如|例如|比如)[^）)]*[）)]", "", text)
            text = re.sub(r"[（(]", "，", text)
            text = re.sub(r"[）)]", "", text)
            text = re.sub(r"^(?:画面|图像)(?:呈现为?|具有)|^角色采用|^整体画面", "", text)
            # Photography and explanatory effects do not belong in a global style sentence.
            parts = [p.strip(" ：，；。") for p in re.split(r"[，；。]", text)]
            parts = [re.sub(r"(?:以|用于|用以|从而)(?:强化|强调|增强|传达|突出)表情.*$", "", p).strip()
                     for p in parts]
            parts = [p for p in parts if p and safe_visual_text(p) and not re.search(
                r"景深|焦点|焦段|构图|摄影镜头|模拟.*光学|营造|增强|吸引力|亲和力|强调.*(?:而非|传达)|温馨|氛围|立体感强|质感真实|室内|厨房|客厅|肤色|^(?:高光区域|角色造型采用风格化比例|高保真(?:纹理|渲染)?|细节丰富|高品质|精美)$", p)]
            parts = [p for p in parts if not re.search(r'^(?:无可见|未见|没有|看不到|未观察到)', p)
                     and not re.search(r'仅在|只在|单镜|偶发|局部湿润', p)]
            parts = [re.sub(r'^角色造型采用风格化(?:比例)?(?:处理|设计)[：:]?', '', p) for p in parts]
            parts = [re.sub(r'^(?:材质|表面|纹理)(?:渲染|处理|表现)?[^：:]{0,30}(?:高保真|艺术简化)[：:]', '', p) for p in parts]
            parts = [re.sub(r'头部比例显著大于写实标准', '夸张头部比例', p) for p in parts]
            parts = [re.sub(r'头发表现为大块的卷曲体块而非单根发丝', '块面化发束', p) for p in parts]
            parts = [re.sub(r'(?:大)?卷曲(?:的)?|卷发', lambda m: '发' if m.group() == '卷发' else '', re.sub(r'^卷发', '头发', p)) if visual_controls(p, dim) & {('form_language', 'hair_blocks')} else p
                     for p in parts]
            if dim == 'medium_rendering' and chunks:
                parts = [p for p in parts if not re.fullmatch(r'(?:三维|3[Dd]|二维|2[Dd])(?:角色|数字|计算机生成)?动画', p)]
            name_controls = visual_controls(name, dim)
            parts = [part for part in parts if not any(part in old or old in part for old in chunks)
                     and not (generic_control_clause(part, dim) and visual_controls(part, dim) <= name_controls)]
            parts.sort(key=lambda part: specificity_score(part, dim), reverse=True)
            # A simple palette is still a usable control. Generic 'smooth skin' or
            # 'fine materials' stays in details, not the representative style sentence.
            # Specificity orders clauses, it is not a semantic rejection gate.
            # Keep concrete observed clauses even when their word order is unseen.
            from .controls import generic_control_clause
            parts = [p for p in parts if specificity_score(p, dim)
                     or (dim in ('medium_rendering', 'color_tone'))
                     or not generic_control_clause(p, dim)]
            if parts:
                queues.append(parts)
        # Round-robin across descriptions, so separate observed control points
        # outrank several clauses from a single long model explanation.
        pool = []
        for i in range(max((len(q) for q in queues), default=0)):
            for queue in queues:
                if i < len(queue) and not any(queue[i] in p or p in queue[i] for p in pool):
                    pool.append(queue[i])
        pools[dim] = pool[:3 if dim == 'form_language' else 2]
    chosen = {dim: [] for dim in pools}
    # Reserve the first pass for every observed dimension before adding detail.
    for index in range(3):
        for dim, pool in pools.items():
            if index >= len(pool):
                continue
            part = pool[index]
            existing = chunks + [p for values in chosen.values() for p in values]
            part_controls = visual_controls(part, dim)
            if any(part in p or p in part or (generic_control_clause(part, dim) and part_controls and
                       part_controls <= visual_controls(p, dim)) for p in existing):
                continue
            # Include separators and final punctuation in the actual length cap.
            if len('，'.join(existing + [part])) + 1 <= limit:
                chosen[dim].append(part)
    chunks.extend(p for dim in DIMENSIONS for p in chosen.get(dim, []))
    return "，".join(chunks) + ("。" if chunks else "")


def render_profile(profile: StyleProfileV3) -> StyleProfileV3:
    """Render approved professional descriptions, not a closed list of labels."""
    def sentence(items, short=False):
        chunks = []
        for dim in DIMENSIONS:
            for item in items:
                if item.dimension != dim:
                    continue
                value = item.professional_description.strip("；，。 ")
                if short:
                    # Keep complete clauses; never slice half a sentence to meet a quota.
                    parts = re.split(r"[；。]", value)
                    value = "，".join(part for part in parts[:2] if part)
                if value and value not in chunks:
                    chunks.append(value)
        return "；".join(chunks) + ("。" if chunks else "")

    applicable = [d for d in profile.descriptions if d.scope in ("reference", "global")]
    profile.detailed_prompt = "整体画面风格：" + sentence(applicable) if applicable else "画风未能确定，未添加未经证实的特征。"
    # Length is a target, not a reason to fabricate traits or discard distinctive details.
    concise = compact_features(applicable, profile.anchor.id if profile.anchor else "",
        descriptive_name=profile.descriptive_name, reference_id=profile.reference.id if profile.reference else "")
    profile.rendered_prompt = profile.edited_prompt or (
        "整体画面风格：" + concise if concise else "画风未能确定，未添加未经证实的特征。")
    chosen = profile.provenance.get('creative_override') or {}
    if (profile.edited_prompt and chosen.get('kind') == 'user-selected-library'
            and not chosen.get('edited_after_selection')):
        from .library import runtime_library
        from .references import reference_label
        library = runtime_library()
        recipe = next((r for r in library.recipes if r.id == chosen.get('recipe_id')), None)
        if (recipe and chosen.get('source_hash') == library.source_hash
                and chosen.get('library_prompt') == recipe.compact_prompt
                and profile.edited_prompt in (recipe.compact_prompt, '整体画面风格：' + recipe.compact_prompt)):
            profile.rendered_prompt = ('整体画面风格：' + reference_label(recipe.id)
                + '（创作参考），' + recipe.compact_prompt)
    profile.scene_prompts = {scene: "本场景视觉变化：" + sentence([d for d in profile.descriptions if d.scene_id == scene])
                             for scene in {d.scene_id for d in profile.descriptions if d.scope == "scene" and d.scene_id}}
    profile.local_prompts = {alias: "本镜局部效果：" + sentence([d for d in profile.descriptions if d.scope == 'local' and d.image_alias == alias])
                            for alias in {d.image_alias for d in profile.descriptions if d.scope == 'local' and d.image_alias}}
    # An explicit re-render/edit is a new projection. Record the renderer that
    # actually produced it; plain history reads never call this function.
    profile.provenance['renderer_version'] = RENDERER_VERSION
    profile.profile_hash = profile.digest()
    return profile
