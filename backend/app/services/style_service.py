"""Project-level, evidence-aware visual style drafting and deterministic delivery."""

from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.perception_models import EvidenceRef
from app.providers.base import FrameInput
from app.style_ontology.catalog import VERSION, conflicts, terms


DIMENSIONS = (
    "medium", "style_family", "line_language", "character_design", "palette",
    "shading", "material", "composition", "graphic_motif", "visible_effect",
    "motion_language", "negative_constraint",
)
GLOBAL_DIMENSIONS = tuple(dim for dim in DIMENSIONS if dim != "visible_effect")
ANCHORS = {
    "warhol": {"name": "安迪·沃霍尔", "type": "artist", "traits": ["style_family.pop-art", "palette.primary-four-color", "graphic_motif.screenprint-offset"]},
    "mucha": {"name": "阿尔丰斯·穆夏", "type": "artist", "traits": ["style_family.art-deco", "composition.dense-ornament", "graphic_motif.ornate-scrollwork"]},
    "hokusai": {"name": "葛饰北斋", "type": "artist", "traits": ["style_family.ukiyoe", "line_language.thin-ink-outline", "composition.layered-depth"]},
    "bauhaus": {"name": "包豪斯", "type": "movement", "traits": ["style_family.minimalism", "composition.repeating-shapes", "line_language.geometric-outline"]},
    "pixar": {"name": "皮克斯", "type": "studio", "traits": ["medium.three-d-animation", "character_design.expressive-eyes", "shading.soft-gradient"]},
    "spiderverse": {"name": "蜘蛛侠：平行宇宙", "type": "film-ip", "traits": ["medium.two-d-animation", "shading.duotone-halftone", "graphic_motif.circular-halftone"]},
}
_DEFAULTS = {
    "medium": ["medium.two-d-animation", "medium.photography", "medium.three-d-animation"],
    "style_family": ["style_family.pop-art", "style_family.fairytale"],
    "line_language": ["line_language.bold-black-outline", "line_language.soft-paint-edge"],
    "character_design": ["character_design.expressive-limbs", "character_design.subtle-expression"],
    "palette": ["palette.primary-four-color", "palette.cool-blue-gray"],
    "shading": ["shading.duotone-halftone", "shading.soft-gradient"],
    "material": ["material.paper-fiber", "material.smooth-animation-surface"],
    "composition": ["composition.clean-negative-space", "composition.layered-depth"],
    "graphic_motif": ["graphic_motif.handdrawn-speedlines", "graphic_motif.abstract-burst"],
    "visible_effect": ["visible_effect.film-grain", "visible_effect.motion-blur"],
    "motion_language": ["motion_language.exaggerated-pose", "motion_language.physical-inertia"],
    "negative_constraint": ["negative_constraint.no-readable-text", "negative_constraint.no-logo"],
}


class StyleProposalError(ValueError):
    def __init__(self, message: str, *, raw_tags: str = "", raw_response: str = ""):
        super().__init__(message)
        self.raw_tags = raw_tags
        self.raw_response = raw_response


class StyleTagDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    medium: str
    style_family: str
    line_language: str
    character_design: str
    palette: str
    shading: str
    material: str
    composition: str
    graphic_motif: str
    visible_effect: str
    motion_language: str


class StyleTagResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: StyleTagDimensions


class StyleSelectionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term_id: str
    frames: List[str]
    confidence: float = Field(ge=0, le=1)
    note: str


class StyleSelectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selections: List[StyleSelectionItem]
    anchors: List[str]
    uncertainties: List[str]


class StyleClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term_id: str
    confidence: float = Field(ge=0, le=1)
    evidence: List[EvidenceRef] = Field(default_factory=list)
    note: str = ""


class StyleObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["style-observation.v1"] = "style-observation.v1"
    mode: Literal["observed", "creative", "hybrid"]
    dimensions: Dict[str, List[StyleClaim]] = Field(default_factory=dict)
    uncertainties: List[str] = Field(default_factory=list)
    raw_response: str = ""
    raw_tags_response: str = ""
    base_tags: Dict[str, str] = Field(default_factory=dict)
    model: str = ""
    ontology_version: str = VERSION

    @model_validator(mode="after")
    def valid_claims(self):
        catalogue = terms()
        for dimension, claims in self.dimensions.items():
            if dimension not in DIMENSIONS or len(claims) > 2:
                raise ValueError(f"无效画风维度或超出单维度词数：{dimension}")
            for claim in claims:
                if claim.term_id not in catalogue or catalogue[claim.term_id].category != dimension:
                    raise ValueError(f"未知画风术语：{claim.term_id}")
                if self.mode == "observed" and not claim.evidence:
                    raise ValueError(f"观察词条缺少帧证据：{claim.term_id}")
        return self


class StyleProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["style-profile.v1"] = "style-profile.v1"
    status: Literal["draft", "confirmed"] = "draft"
    mode: Literal["observed", "creative", "hybrid"]
    version: int = Field(default=1, ge=1)
    global_terms: List[str]
    optional_anchors: List[str] = Field(default_factory=list)
    expanded_anchor_traits: List[str] = Field(default_factory=list)
    include_anchor_names: bool = True
    negative_constraints: List[str] = Field(default_factory=lambda: ["negative_constraint.no-readable-text", "negative_constraint.no-logo"])
    per_shot_effect_overrides: Dict[str, List[str]] = Field(default_factory=dict)
    rendered_style_prompt: str = ""
    source_term_ids: List[str] = Field(default_factory=list)
    model: str = ""
    provenance: str = ""
    ontology_version: str = VERSION
    profile_hash: str = ""

    @model_validator(mode="after")
    def valid_terms(self):
        catalogue = terms()
        for term_id in [*self.global_terms, *self.negative_constraints,
                        *(item for values in self.per_shot_effect_overrides.values() for item in values)]:
            if term_id not in catalogue:
                raise ValueError(f"未知画风术语：{term_id}")
        if any(catalogue[item].category not in GLOBAL_DIMENSIONS or catalogue[item].category == "negative_constraint"
               for item in self.global_terms):
            raise ValueError("全局画风术语不能包含禁用项或局部特效")
        if len(set(self.global_terms)) != len(self.global_terms):
            raise ValueError("画风术语重复")
        if any(sum(catalogue[item].category == dimension for item in self.global_terms) > 2 for dimension in GLOBAL_DIMENSIONS):
            raise ValueError("每个画风维度最多保留两个术语")
        all_visual_terms = list(dict.fromkeys([*self.global_terms, *self.expanded_anchor_traits]))
        if any(item not in catalogue for item in self.expanded_anchor_traits):
            raise ValueError("锚点视觉特征必须来自词典")
        if conflicts(all_visual_terms):
            raise ValueError("画风术语冲突：" + "、".join(conflicts(all_visual_terms)))
        if any(anchor not in ANCHORS for anchor in self.optional_anchors):
            raise ValueError("未知风格锚点")
        if any(catalogue[item].category != "negative_constraint" for item in self.negative_constraints):
            raise ValueError("禁用项必须来自负面约束词典")
        if any(catalogue[item].category != "visible_effect" for values in self.per_shot_effect_overrides.values() for item in values):
            raise ValueError("单镜仅能覆盖局部可见效果")
        return self


def retrieve_candidates(text: str, limit: int = 30) -> List[str]:
    """Small lexical shortlist; no full ontology is sent to the model."""
    catalogue = terms()
    normalized = text.lower()
    selected: Dict[str, List[str]] = {dimension: [] for dimension in DIMENSIONS}
    chinese_aliases = {
        "3d动画": "medium.three-d-animation", "三维动画": "medium.three-d-animation",
        "2d动画": "medium.two-d-animation", "真人": "medium.photography",
        "水墨": "medium.ink-animation", "水彩": "medium.watercolor-animation",
        "剪纸": "medium.paper-cut", "黏土": "medium.clay-stop-motion",
        "绘本": "medium.crayon-animation", "波普": "style_family.pop-art",
        "粗黑线": "line_language.bold-black-outline", "网点": "shading.duotone-halftone",
        "大眼睛": "character_design.expressive-eyes", "高对比": "palette.high-contrast",
        "低饱和": "palette.low-saturation", "纸张纹理": "material.paper-fiber",
        "速度线": "graphic_motif.handdrawn-speedlines",
    }
    for alias, term_id in chinese_aliases.items():
        if alias in normalized:
            selected[catalogue[term_id].category].append(term_id)
    for term in catalogue.values():
        if term.zh_label in text or any(len(alias) > 3 and alias in normalized for alias in term.aliases):
            selected[term.category].append(term.id)
    result: List[str] = []
    for dimension in DIMENSIONS:
        for term_id in [*selected[dimension][:2], *_DEFAULTS[dimension]]:
            if term_id not in result and len([item for item in result if catalogue[item].category == dimension]) < 2:
                result.append(term_id)
    return result[:limit]


def _render_fragment(category: str, labels: List[str]) -> str:
    joined = "与".join(labels)
    if category == "character_design" and not joined.startswith("角色"):
        return "角色" + joined
    return joined


def render_style(profile: StyleProfile) -> str:
    """Stable Chinese phrase, shared byte-for-byte by every shot in a project."""
    catalogue = terms()
    by_category = {category: [] for category in GLOBAL_DIMENSIONS}
    for term_id in dict.fromkeys([*profile.global_terms, *profile.expanded_anchor_traits]):
        category = catalogue[term_id].category
        if category in by_category:
            by_category[category].append(catalogue[term_id].generation_phrase)
    covered = sum(bool(values) for category, values in by_category.items() if category != "negative_constraint")
    if covered < 8:
        raise ValueError("整体画风需覆盖至少8个不同视觉维度")
    pieces = [_render_fragment(category, by_category[category]) for category in GLOBAL_DIMENSIONS
              if category != "negative_constraint" and by_category[category]]
    anchors = [ANCHORS[anchor]["name"] for anchor in profile.optional_anchors if anchor in ANCHORS]
    if anchors and profile.include_anchor_names:
        pieces.insert(1, "参考" + "与".join(anchors) + "的图形语言")
    negative = "、".join(catalogue[item].zh_label.replace("不生成", "无").replace("不出现", "无")
                       for item in profile.negative_constraints[:3])
    if negative:
        pieces.append(negative)
    sentence = "整体画面风格：" + "，".join(pieces) + "。"
    if len(sentence) > 140 and anchors:
        pieces = [part for part in pieces if not part.startswith("参考")]
        sentence = "整体画面风格：" + "，".join(pieces) + "。"
    if len(sentence) > 140:
        # Keep eight distinct controls and omit lower-priority optional details.
        protected = {catalogue[item].category for item in profile.expanded_anchor_traits}
        for category in ("motion_language", "graphic_motif", "style_family", "material", "character_design"):
            if len(sentence) <= 140:
                break
            if category in protected:
                continue
            if sum(bool(values) for key, values in by_category.items() if key != "negative_constraint") <= 8:
                break
            pieces = [part for part in pieces if part != _render_fragment(category, by_category[category])]
            sentence = "整体画面风格：" + "，".join(pieces) + "。"
        if len(sentence) > 140:
            raise ValueError("画风描述超过140字，请减少或缩短词条")
    if len(sentence) < 80:
        sentence = sentence[:-1] + "，各镜保持统一的造型、线条、色板和材质表现。"
    if not 80 <= len(sentence) <= 140:
        raise ValueError("整体画风需保持80–140字")
    return sentence


def profile_hash(profile: StyleProfile) -> str:
    payload = profile.model_dump(exclude={"profile_hash", "rendered_style_prompt", "version", "status"})
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def build_profile(observation: StyleObservation, selected: List[str], anchors: Optional[List[str]] = None) -> StyleProfile:
    selected = list(dict.fromkeys(selected))
    catalogue = terms()
    global_terms = [item for item in selected if item in catalogue and catalogue[item].category not in ("negative_constraint", "visible_effect")]
    anchor_ids = [item for item in (anchors or []) if item in ANCHORS][:2]
    anchor_traits = list(dict.fromkeys(term for anchor in anchor_ids for term in ANCHORS[anchor]["traits"]))
    profile = StyleProfile(mode=observation.mode, global_terms=global_terms, optional_anchors=anchor_ids,
                           expanded_anchor_traits=anchor_traits,
                           source_term_ids=selected, model=observation.model,
                           provenance="云端 Flash 项目级画风草案" if observation.model else "人工画风草案")
    profile.rendered_style_prompt = render_style(profile)
    profile.profile_hash = profile_hash(profile)
    return profile


def propose_style(provider, context: str, frames: List[FrameInput], mode: Literal["observed", "creative", "hybrid"]):
    """Two bounded project-level calls; invalid responses never become a profile."""
    tag_prompt = ("画风基础标签（style.tags）。只输出JSON对象：{\"tags\":{\"medium\":\"可见媒介\",\"style_family\":\"流派\",\"line_language\":\"轮廓线\",\"character_design\":\"角色设计\",\"palette\":\"色板\",\"shading\":\"明暗表现\",\"material\":\"材质\",\"composition\":\"构图\",\"graphic_motif\":\"图形母题\",\"visible_effect\":\"可见后期\",\"motion_language\":\"动态表现\"}}。每项用短中文视觉描述；不可判断写未知；禁止猜测渲染器、光追和摄影机。")
    tag_raw = provider.propose_style(tag_prompt + f"\n模式：{mode}；上下文：{context[:2500]}", frames)
    try:
        tag_payload = StyleTagResponse.model_validate_json(tag_raw)
    except ValueError as exc:
        raise StyleProposalError(f"画风基础标签结构错误：{exc}", raw_tags=tag_raw) from exc
    tags = tag_payload.tags.model_dump()
    tag_text = "；".join(value for value in tags.values() if value != "未知")
    shortlist = [item for item in retrieve_candidates(context + "；" + tag_text)
                 if terms()[item].category != "negative_constraint"]
    catalogue = terms()
    candidates = [{"id": item, "category": catalogue[item].category, "label": catalogue[item].zh_label,
                   "cue": catalogue[item].observable_cues[:1]} for item in shortlist]
    frame_catalogue = [{"frame": f"F{index+1}", "timestamp": round(frame.timestamp, 3)}
                       for index, frame in enumerate(frames)]
    prompt = (
        "输出一个JSON对象，字段为 selections（数组）、anchors（数组）、uncertainties（数组）。"
        "selections每项为{term_id,frames:[F1],confidence,note}。仅能使用候选term_id；"
        "每类最多选2项，至少覆盖8个不同类别，禁止选择冲突词。"
        "观察模式每项必须引用能支持该特征的帧；没有证据的词不选。"
        "剧本创作模式不引用帧，选择连贯的创作方案。"
        "艺术家、流派、工作室或影视IP锚点只可从warhol、mucha、hokusai、bauhaus、pixar、spiderverse中选，"
        "不能把锚点当作视频中可见的事实。禁止猜测光追、渲染软件或摄影机型号。"
        f"\n模式：{mode}。上下文：{context[:2500]}。"
        f"\n帧目录：{json.dumps(frame_catalogue, ensure_ascii=False)}。"
        f"\n候选词：{json.dumps(candidates, ensure_ascii=False)}。"
    )
    raw = provider.propose_style(prompt, frames)
    try:
        payload = StyleSelectionResponse.model_validate_json(raw)
    except ValueError as exc:
        raise StyleProposalError(f"画风候选选择结构错误：{exc}", raw_tags=tag_raw, raw_response=raw) from exc
    try:
        by_dimension: Dict[str, List[StyleClaim]] = {}
        known_frames = {f"F{index+1}": frame for index, frame in enumerate(frames)}
        for item in payload.selections:
            term_id = item.term_id
            if term_id not in shortlist:
                raise ValueError(f"画风模型选择了候选外词条：{term_id}")
            refs = []
            for label in item.frames:
                if label not in known_frames:
                    raise ValueError(f"画风证据帧不存在：{label}")
                frame = known_frames[label]
                refs.append(EvidenceRef(frame_id=frame.id, timestamp=float(frame.timestamp), description=item.note or catalogue[term_id].definition))
            category = catalogue[term_id].category
            by_dimension.setdefault(category, []).append(StyleClaim(term_id=term_id, confidence=item.confidence, evidence=refs, note=item.note))
        observation = StyleObservation(mode=mode, dimensions=by_dimension,
                                       uncertainties=[str(value) for value in payload.uncertainties][:12],
                                       raw_response=raw, raw_tags_response=tag_raw, base_tags=tags,
                                       model=provider.health().model)
        chosen = [claim.term_id for values in by_dimension.values() for claim in values]
        if conflicts(chosen):
            raise ValueError("画风响应包含冲突词条")
        try:
            profile = build_profile(observation, chosen, payload.anchors)
        except ValueError as exc:
            observation.uncertainties.append(f"画风草案需人工补全：{exc}")
            profile = None
        return observation, profile
    except Exception as exc:
        raise StyleProposalError(f"画风候选语义校验失败：{exc}", raw_tags=tag_raw, raw_response=raw) from exc
