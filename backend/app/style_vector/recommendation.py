"""Recipe retrieval and one bounded visual review; recommendations are not facts."""
from __future__ import annotations

import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.perception_models import EvidenceRef
from app.style_vector.index import rank_fusion
from app.style_vector.models import StyleFeatureObservation, StyleMatch, clean_style_description
from app.style_vector.ontology import CATEGORIES, RECIPES, terms
from app.style_vector.wire import error_details, parse_style_response

Category = Literal["medium_rendering", "form_language", "color_tone", "lighting_shading", "texture_material", "motion_signature"]


def media_family(term_id):
    # Photography sublabels are not mutually exclusive (a documentary frame
    # can have a film-like treatment). Do not eliminate all realistic recipes.
    if term_id in {"medium.photography", "medium.documentary-photo", "medium.film-still"}:
        return "photography"
    return term_id


def grounded_term(term_id: str, description: str) -> bool:
    """An overlapping rank alone is never semantic support for a subtype."""
    term = terms()[term_id]
    if any(cue in description for cue in term.negative_cues):
        return False
    for phrase in (*term.aliases, *term.positive_cues):
        match = re.search(re.escape(phrase), description, re.I)
        if match and not re.search(r"(?:不是|没有|非|无|不属于)\s*$", description[max(0, match.start() - 5):match.start()]):
            return True
    return False


def representative_ids(observations, frames, limit=4):
    available = [item.shot_id for item in observations if item.shot_id in frames]
    if len(available) <= limit:
        return available
    return [available[round(index * (len(available) - 1) / (limit - 1))] for index in range(limit)]


def recipe_candidates(index, observations, matches, image_vectors, retrieval=None):
    descriptions = list(dict.fromkeys(
        f"{category}: {getattr(item, category).description}"
        for item in observations for category in CATEGORIES
        if getattr(item, category).observable))
    if not descriptions and not image_vectors:
        return []
    query = "；".join(descriptions)[:2400]
    text_rows = []
    if descriptions:
        try:
            text_rows = index.search_recipes("text", index.query_text(query))
        except Exception as exc:
            if retrieval is not None:
                retrieval.setdefault("errors", []).append({"phase": "style.recipe_text_retrieval", "error": type(exc).__name__, "detail": error_details(exc)})
    # Rank each frame separately: a mean image vector can erase contrasting
    # scene styles. These are candidates only, never automatic global facts.
    votes = {}
    for shot_id in representative_ids(observations, image_vectors):
        try:
            image_results = index.search_recipes("vision", image_vectors[shot_id])
        except Exception as exc:
            if retrieval is not None:
                retrieval.setdefault("errors", []).append({"phase": "style.recipe_image_retrieval", "shot_id": shot_id, "error": type(exc).__name__, "detail": error_details(exc)})
            continue
        for row in image_results:
            vote = votes.setdefault(row["term_id"], {"weight": 0., "shot_ids": []})
            vote["weight"] += 1. / (60 + row["rank"])
            vote["shot_ids"].append(shot_id)
    image_rows = [{"term_id": item, "rank": rank} for rank, (item, _) in enumerate(
        sorted(votes.items(), key=lambda pair: (-pair[1]["weight"], pair[0])), 1)]
    rows = rank_fusion(text_rows, image_rows, [], limit=len(RECIPES))
    medium_ids = {item.term_id for item in matches if item.category == "medium_rendering"}
    known = {item.term_id for item in matches}
    result = []
    for row in rows:
        recipe = RECIPES[row["term_id"]]
        recipe_media = {term for term in recipe.term_ids if terms()[term].category == "medium_rendering"}
        if medium_ids and recipe_media and not {media_family(item) for item in recipe_media} & {media_family(item) for item in medium_ids}:
            continue
        conflicts = [term for term in known if term in recipe.forbidden_drift or any(
            term in terms()[other].conflicts_with or other in terms()[term].conflicts_with
            for other in recipe.term_ids)]
        if conflicts:
            continue
        supported = [term for term in recipe.term_ids if term in known]
        result.append({"recipe_id": recipe.id, "status": "candidate", "ranks": row["ranks"],
                       "supported_term_ids": supported,
                       "missing_term_ids": [term for term in recipe.term_ids if term not in known],
                       "reason": "向量召回的创作候选，尚未完成视觉核验；不是原片来源判断",
                       "image_support_shot_ids": votes.get(recipe.id, {}).get("shot_ids", []),
                       "evidence": [], "optional_anchors": list(recipe.optional_anchors)})
        if len(result) == 3:
            break
    return result


class ReviewedFeature(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot_id: str
    category: Category
    description: str
    term_id: Optional[str]
    confidence: float = Field(ge=0, le=1)
    frame_id: str


class RecipeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe_id: str
    status: Literal["supported", "partial", "rejected"]
    reason: str
    frame_ids: list[str] = Field(max_length=4)


class ProjectStyleReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    features: list[ReviewedFeature] = Field(default_factory=list, max_length=24)
    recipes: list[RecipeVerdict] = Field(default_factory=list, max_length=3)


def review_project(service, job_id, observations, matches, frames, candidates, retrieval):
    """At most ONE call, no repair retry; verify candidates and fill sparse facts.

    Single representative stills cannot establish motion, even if the recipe
    contains a plausible animation style. Original known fields are preserved.
    """
    if not candidates or service.provider.health().provider != "bailian":
        return
    shot_ids = representative_ids(observations, frames)
    if not shot_ids:
        return
    selected = [frames[item] for item in shot_ids]
    by_shot = {item.shot_id: item for item in observations}
    allowed = {term for candidate in candidates for term in RECIPES[candidate["recipe_id"]].term_ids}
    allowed.update(match.term_id for match in matches)
    context = [{"shot_id": shot_id, "frame_id": frames[shot_id].id,
                "known": {category: getattr(by_shot[shot_id], category).description for category in CATEGORIES
                          if getattr(by_shot[shot_id], category).observable}}
               for shot_id in shot_ids]
    prompt = (
        "项目级画风核验。图片按 context 顺序输入。只描述可见的媒介、轮廓/五官比例、整体色调、"
        "阴影表现和稳定表面语言。不要说作者或工作室来源；不要将服装、发型、道具、地点写成画风。"
        "只补充 known 中缺失的维度。单张静帧不能证明 motion_signature，不得补动态。"
        "每条事实须引用该镜头提供的 frame_id；term_id 只能来自 allowed，不能确定则 null。"
        "核验每个 recipe：supported 表示视觉特征整体支持，partial 表示部分支持或有未观察特征，"
        "rejected 表示有明确反证。缺失不是支持，不为配方补齐事实。配方仍为创作建议，需用户确认。"
        "返回符合Schema的JSON，features含shot_id/category/description/term_id/confidence/frame_id，"
        "recipes含recipe_id/status/reason/frame_ids。"
        f"context={json.dumps(context, ensure_ascii=False)}; "
        f"allowed={json.dumps([{ 'id': item, 'category': terms()[item].category, 'definition': terms()[item].definition, 'positive': terms()[item].positive_cues, 'negative': terms()[item].negative_cues } for item in sorted(allowed)], ensure_ascii=False)}; "
        f"recipes={json.dumps([{ 'id': item['recipe_id'], 'visual_traits': RECIPES[item['recipe_id']].retrieval_text(), 'negative_cues': RECIPES[item['recipe_id']].negative_cues } for item in candidates], ensure_ascii=False)}"
    )
    trace = {"phase": "style.recipe_review", "max_attempts": 1, "frame_ids": [frame.id for frame in selected]}
    retrieval.setdefault("project_review", []).append(trace)
    try:
        raw = service.provider._call(prompt, selected, "style.recipe_review", max_tokens=3200,
                                     max_attempts=1, timeout_seconds=60, schema=ProjectStyleReview)
        trace["raw_response"] = raw
        parsed = parse_style_response(raw, ProjectStyleReview, array_key="features", trace=trace)
        trace["status"] = "parsed"
        for feature in parsed.features:
            if feature.shot_id not in shot_ids or feature.category == "motion_signature":
                continue
            frame = frames[feature.shot_id]
            if feature.frame_id != frame.id or getattr(by_shot[feature.shot_id], feature.category).observable:
                continue
            description = clean_style_description(feature.description, feature.category)
            if not description or feature.confidence < .6:
                continue
            evidence = [EvidenceRef(frame_id=frame.id, timestamp=float(frame.timestamp), description=description)]
            setattr(by_shot[feature.shot_id], feature.category, StyleFeatureObservation(
                description=description, observable=True, confidence=feature.confidence, evidence=evidence))
            if feature.term_id not in allowed or terms()[feature.term_id].category != feature.category:
                continue
            if any(cue in description for cue in terms()[feature.term_id].negative_cues):
                continue
            matches.append(StyleMatch(term_id=feature.term_id, category=feature.category, scope="UNCERTAIN",
                                      shot_ids=[feature.shot_id], scene_id=by_shot[feature.shot_id].scene_group_id,
                                      evidence=evidence, confidence=feature.confidence, reason="项目级帧证据补充核验"))
        valid_frames = {frame.id: frame for frame in selected}
        for verdict in parsed.recipes:
            candidate = next((item for item in candidates if item["recipe_id"] == verdict.recipe_id), None)
            if not candidate or not verdict.frame_ids or any(item not in valid_frames for item in verdict.frame_ids):
                continue
            candidate["status"] = verdict.status
            candidate["reason"] = verdict.reason + "（创作推荐，不代表原片来源）"
            candidate["evidence"] = [{"frame_id": item, "timestamp": valid_frames[item].timestamp,
                                       "description": verdict.reason} for item in verdict.frame_ids]
    except Exception as exc:
        trace.update(status="failed", error=type(exc).__name__, detail=error_details(exc))
        retrieval.setdefault("errors", []).append({"phase": "style.recipe_review", "error": type(exc).__name__, "detail": error_details(exc)})
        service._emit(job_id, "style.review_needed", {"reason": "配方核验未完成，已保留逐镜观察与候选"})
    known = {item.term_id for item in matches}
    for candidate in candidates:
        recipe = RECIPES[candidate["recipe_id"]]
        candidate["supported_term_ids"] = [term for term in recipe.term_ids if term in known]
        candidate["missing_term_ids"] = [term for term in recipe.term_ids if term not in known]
        # Recipe-wide unknowns cannot be converted into 'fully supported'.
        if candidate["status"] == "supported" and candidate["missing_term_ids"]:
            candidate["status"] = "partial"
