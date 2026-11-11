"""Content-isolated style evidence and user-confirmable Visual Bible."""
from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.perception_models import EvidenceRef, VisualObservationV2
from app.style_vector.ontology import CATEGORIES, RECIPES, terms


_CONTENT = re.compile(
    r"衣服|衣物|服装|穿着|发型|头发|卷发|黑发|少女|桌子|桌面|杯子|玻璃杯|液体|戒指|道具|"
    r"橙汁|饮料|鞋子|裙子|厨房|卧室|街道|姓名|身份|男性|女性|男孩|女孩|女人|男人"
)
_TECHNIQUE = re.compile(r"光追|路径追踪|全局光照|次表面散射|\bPBR\b|\bSSAO\b", re.I)

_STYLE_CUES = {
    "medium_rendering": re.compile(r"动画|摄影|绘画|剪纸|拼贴|水墨|水彩|油画|定格|像素|手绘|三维|二维|2D|3D|胶片|漫画"),
    "form_language": re.compile(r"轮廓|线条|几何|造型|比例|形体|平面|抽象|剪影|角色设计|夸张|风格化|卡通|写实|大眼"),
    "color_tone": re.compile(r"色|调|饱和|对比|冷暖|蓝灰|青灰|黑白|单色|综合色温"),
    "lighting_shading": re.compile(r"光|影|明暗|高调|低调|逆光|照明|网点"),
    "texture_material": re.compile(r"纹理|颗粒|表面|笔触|纤维|材质|纸质|质感|手工|磨砂|细腻|平滑|光滑|粗糙"),
    "motion_signature": re.compile(r"逐帧|动画|抖动|形变|惯性|节奏|运动语言|定格|拉伸|缓入|缓出"),
}


def clean_style_description(value: str, category: str = "") -> str:
    """Reject content clauses before any embedding request."""
    value = (value or "").strip()
    # Discard unsafe clauses, not the whole observation. Never embed the
    # evidence caption itself: it can legitimately mention clothes and props.
    clauses = re.split(r"[，,；;。\n]", value)
    safe = [clause.strip() for clause in clauses if clause.strip() not in ("", "未知")
            and not _CONTENT.search(clause) and not _TECHNIQUE.search(clause)
            and (not category or category in _STYLE_CUES and _STYLE_CUES[category].search(clause))]
    return "，".join(safe)[:180]


class StyleFeatureObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = ""
    observable: bool = False
    confidence: float = Field(default=0, ge=0, le=1)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check(self):
        if self.observable and (not self.description or not self.evidence):
            raise ValueError("可观察画风必须有描述和帧证据")
        return self


class ShotStyleObservationV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["shot-style.v2"] = "shot-style.v2"
    shot_id: str
    scene_group_id: str = ""
    start_time: float = 0
    end_time: float = 0
    medium_rendering: StyleFeatureObservation = Field(default_factory=StyleFeatureObservation)
    form_language: StyleFeatureObservation = Field(default_factory=StyleFeatureObservation)
    color_tone: StyleFeatureObservation = Field(default_factory=StyleFeatureObservation)
    lighting_shading: StyleFeatureObservation = Field(default_factory=StyleFeatureObservation)
    texture_material: StyleFeatureObservation = Field(default_factory=StyleFeatureObservation)
    motion_signature: StyleFeatureObservation = Field(default_factory=StyleFeatureObservation)
    shot_treatments: list[str] = Field(default_factory=list)
    local_effects: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


def from_perception(shot_id: str, scene_group_id: str, start: float, end: float,
                    visual: VisualObservationV2) -> ShotStyleObservationV2:
    style = visual.artistic_effects
    def convert(category: str, *fields) -> StyleFeatureObservation:
        for field in fields:
            if field.observable:
                description = clean_style_description(str(field.value), category)
                if description and field.evidence:
                    return StyleFeatureObservation(
                        description=description, observable=True, confidence=field.confidence,
                        evidence=field.evidence, uncertainties=field.uncertainties)
        return StyleFeatureObservation()
    motion = convert("motion_signature", style.motion_signature)
    return ShotStyleObservationV2(
        shot_id=shot_id, scene_group_id=scene_group_id or "", start_time=start, end_time=end,
        medium_rendering=convert("medium_rendering", style.medium, style.rendering),
        form_language=convert("form_language", style.character_design),
        color_tone=convert("color_tone", style.color_palette),
        lighting_shading=convert("lighting_shading", visual.photography.lighting_detail, style.rendering),
        texture_material=convert("texture_material", style.texture),
        motion_signature=motion,
        shot_treatments=[str(visual.photography.composition.value)]
            if visual.photography.composition.observable else [],
        local_effects=[str(item) for item in style.visible_effects.value
                       if style.visible_effects.observable and not _CONTENT.search(str(item))][:2],
    )


class StyleMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term_id: str
    category: str
    scope: Literal["GLOBAL", "SCENE", "SHOT", "UNCERTAIN"]
    shot_ids: list[str] = Field(default_factory=list)
    scene_id: str = ""
    evidence: list[EvidenceRef] = Field(default_factory=list)
    ranks: dict[str, int] = Field(default_factory=dict)
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = ""


class CoverageProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term_id: str
    observable_shots: int
    supporting_shots: int
    counterexample_shots: int = 0
    observable_scenes: int
    supporting_scenes: int
    early_support: bool = False
    middle_support: bool = False
    late_support: bool = False
    mean_evidence_confidence: float = 0
    retrieval_channels: list[str] = Field(default_factory=list)


class ProjectVisualBible(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["project-visual-bible.v2"] = "project-visual-bible.v2"
    status: Literal["draft", "confirmed"] = "draft"
    mode: Literal["observed", "creative"] = "observed"
    version: int = Field(default=1, ge=1)
    ontology_version: str = "style-ontology.v2"
    index_manifest: dict = Field(default_factory=dict)
    global_core_terms: list[str] = Field(default_factory=list)
    global_secondary_terms: list[str] = Field(default_factory=list)
    scene_overrides: dict[str, list[str]] = Field(default_factory=dict)
    shot_overrides: dict[str, list[str]] = Field(default_factory=dict)
    local_effects: dict[str, list[str]] = Field(default_factory=dict)
    forbidden_drift: list[str] = Field(default_factory=list)
    recommended_recipes: list[str] = Field(default_factory=list)
    # Recommendations are creative options, not observed facts. Missing recipe
    # traits never become global terms without the explicit recipe selection.
    recipe_recommendations: list[dict] = Field(default_factory=list)
    selected_recipe_id: Optional[str] = None
    selected_recipe_term_ids: list[str] = Field(default_factory=list)
    coverage_profiles: dict[str, CoverageProfile] = Field(default_factory=dict)
    unresolved_features: list[str] = Field(default_factory=list)
    rendered_global_prompt: str = ""
    model_provenance: dict = Field(default_factory=dict)
    bible_hash: str = ""

    @model_validator(mode="after")
    def valid_terms(self):
        catalogue = terms()
        for term_id in [*self.global_core_terms, *self.global_secondary_terms, *self.forbidden_drift,
                        *(term for values in [*self.scene_overrides.values(), *self.shot_overrides.values()] for term in values)]:
            if term_id not in catalogue:
                raise ValueError(f"Visual Bible 引用未知术语：{term_id}")
        if any(len(effect) > 150 or "\n" in effect or "\r" in effect for effects in self.local_effects.values() for effect in effects):
            raise ValueError("局部效果最多150字且不能包含换行")
        if len(self.global_core_terms) > 6 or len(set(self.global_core_terms)) != len(self.global_core_terms):
            raise ValueError("全局核心最多六项且不能重复")
        categories = [catalogue[item].category for item in self.global_core_terms]
        if len(categories) != len(set(categories)):
            raise ValueError("全局核心每类最多一项")
        if self.selected_recipe_id and self.selected_recipe_id not in RECIPES:
            raise ValueError("未知风格配方")
        if self.selected_recipe_term_ids and (not self.selected_recipe_id or self.selected_recipe_term_ids != list(RECIPES[self.selected_recipe_id].term_ids)):
            raise ValueError("配方展开术语与配方版本不一致")
        global_terms = [*self.global_core_terms, *self.global_secondary_terms]
        groups = [global_terms, *(global_terms + values for values in self.scene_overrides.values()),
                  *(global_terms + values for values in self.shot_overrides.values())]
        for group in groups:
            for index, left in enumerate(group):
                for right in group[index + 1:]:
                    if right in catalogue[left].conflicts_with or left in catalogue[right].conflicts_with:
                        raise ValueError("画风层级存在互斥术语")
        return self

    def digest(self) -> str:
        payload = self.model_dump(exclude={"bible_hash", "status", "version", "rendered_global_prompt"})
        return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def render_global(bible: ProjectVisualBible) -> str:
    categories = {terms()[item].category for item in bible.global_core_terms}
    if not bible.selected_recipe_id and not bible.global_core_terms:
        return "项目画风待确认。"
    if bible.selected_recipe_id:
        phrase = RECIPES[bible.selected_recipe_id].global_prompt
    else:
        phrases = [terms()[item].generation_phrase for category in CATEGORIES
                   for item in bible.global_core_terms if terms()[item].category == category]
        phrase = "，".join(phrases) + "。"
    if not phrase or phrase == "。":
        return "项目画风待确认。"
    if _CONTENT.search(phrase) or _TECHNIQUE.search(phrase):
        raise ValueError("全局画风包含内容实体或无法证实的渲染技术")
    if len(phrase) > 120:
        raise ValueError("全局画风超过120字")
    return "整体画面风格：" + phrase


def resolve_effective(bible: ProjectVisualBible, shot_id: str, scene_id: str) -> dict[str, str]:
    catalogue = terms()
    def sentence(label: str, ids: list[str]) -> str:
        return label + "：" + "，".join(catalogue[item].generation_phrase for item in dict.fromkeys(ids)) + "。" if ids else ""
    recipe = RECIPES.get(bible.selected_recipe_id) if bible.selected_recipe_id else None
    forbidden = list(dict.fromkeys([*bible.forbidden_drift, *(recipe.forbidden_drift if recipe else ())]))
    return {
        "global": render_global(bible),
        "scene": sentence("本场景视觉变化", bible.scene_overrides.get(scene_id, [])),
        "shot": sentence("本镜头视觉约束", bible.shot_overrides.get(shot_id, [])),
        "effect": ("局部效果：" + "；".join(bible.local_effects.get(shot_id, [])) + "。") if bible.local_effects.get(shot_id) else "",
        "avoid": ("风格漂移禁用：" + "，".join(catalogue[item].name_cn for item in forbidden) + "。") if forbidden else "",
        "recipe_constraint": ("配方镜头约束：" + recipe.shot_constraints) if recipe else "",
    }
