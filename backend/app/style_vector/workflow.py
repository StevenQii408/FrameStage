"""Draft, inspect, confirm and apply an evidence-aware project Visual Bible."""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
import json
from math import gcd
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.db import utc_now
from app.perception_models import EvidenceRef, VisualObservationV2
from app.providers.base import FrameInput
from app.schemas import VisionObservation
from app.services.prompt_service import build_prompts
from app.style_vector.embedding import BailianStyleEmbedder, persist_usage
from app.style_vector.index import IndexNotReady, StyleVectorIndex, exact_aliases, rank_fusion
from app.style_vector.models import (
    CoverageProfile, ProjectVisualBible, ShotStyleObservationV2, StyleFeatureObservation, StyleMatch,
    from_perception, render_global,
)
from app.style_vector.ontology import CATEGORIES, RECIPES, VERSION, terms
from app.style_vector.recommendation import grounded_term, recipe_candidates, review_project
from app.style_vector.wire import error_details, parse_style_response


class AdjudicationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot_id: str
    category: str
    term_id: Optional[str]
    reason: str


class AdjudicationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[AdjudicationItem] = Field(default_factory=list)


def _usage(service, job_id: str, embedder) -> None:
    records = embedder.drain_usage()
    if records:
        persist_usage(service.db, records, job_id)
        row = service.db.fetchone("SELECT model_usage_json FROM jobs WHERE id=?", (job_id,))
        previous = json.loads(row["model_usage_json"] or "[]")
        service.db.update_job(job_id, model_usage_json=json.dumps([*previous, *records], ensure_ascii=False))


def _observations(service, job_id: str) -> list[ShotStyleObservationV2]:
    rows = service.db.fetchall(
        "SELECT id,scene_group_id,start_time,end_time,perception_json FROM shots WHERE job_id=? ORDER BY number", (job_id,))
    result = []
    for row in rows:
        try:
            record = json.loads(row["perception_json"] or "{}")
            visual = VisualObservationV2.model_validate(record["observation"])
            style = from_perception(row["id"], row["scene_group_id"] or "",
                                    row["start_time"], row["end_time"], visual)
            result.append(style)
            service.db.execute("UPDATE shots SET style_observation_v2_json=? WHERE id=?",
                               (style.model_dump_json(), row["id"]))
            service._emit(job_id, "style.observation_ready", {"shot_id": row["id"]})
        except (KeyError, ValueError, TypeError):
            # Missing/invalid textual perception must not hide available images
            # from the bounded static-style review.
            style = ShotStyleObservationV2(shot_id=row["id"], scene_group_id=row["scene_group_id"] or "",
                                           start_time=row["start_time"], end_time=row["end_time"],
                                           uncertainties=["逐镜文字观察不可用，仅允许图片核验静态特征"])
            result.append(style)
            service.db.execute("UPDATE shots SET style_observation_v2_json=? WHERE id=?",
                               (style.model_dump_json(), row["id"]))
    return result


def _frames(service, job_id: str) -> dict[str, FrameInput]:
    rows = service.db.fetchall(
        "SELECT shot_id,id,timestamp,path,width,height,blur_score FROM frames "
        "WHERE job_id=? AND selected=1 ORDER BY shot_id,COALESCE(blur_score,0) DESC,timestamp", (job_id,))
    return {row["shot_id"]: FrameInput(row["id"], row["timestamp"], row["path"],
                                      row["width"], row["height"]) for row in reversed(rows)}


def _adjudicate(service, job_id: str, ambiguous: list[dict], frames: dict[str, FrameInput], retrieval=None) -> dict[tuple[str, str], str]:
    if not ambiguous or service.provider.health().provider != "bailian":
        return {}
    decided = {}
    batches = []
    cursor = 0
    # One term-review call + one project/recipe-review call; never an unbounded
    # per-shot retry loop for style uncertainty.
    while cursor < len(ambiguous) and len(batches) < 1:
        batch = []
        shot_ids = []
        while cursor < len(ambiguous) and len(batch) < 20:
            item = ambiguous[cursor]
            if item["shot_id"] not in shot_ids and len(shot_ids) >= 10:
                break
            batch.append(item)
            if item["shot_id"] not in shot_ids:
                shot_ids.append(item["shot_id"])
            cursor += 1
        if not batch:
            break
        batches.append((batch, shot_ids))
    for batch_number, (batch, shot_ids) in enumerate(batches, start=1):
        selected_frames = [frames[item] for item in shot_ids if item in frames]
        choices = []
        for item in batch:
            choices.append({
                "shot_id": item["shot_id"], "category": item["category"],
                "observation": item["description"],
                "frame_id": frames[item["shot_id"]].id if item["shot_id"] in frames else None,
                "candidates": [{"id": candidate["term_id"], "definition": terms()[candidate["term_id"]].definition,
                                "positive": terms()[candidate["term_id"]].positive_cues,
                                "negative": terms()[candidate["term_id"]].negative_cues}
                               for candidate in item["candidates"][:5]],
            })
        prompt = ("仅基于已提供的观察和帧证据，逐项在候选 ID 中选择可证实的风格；观察为空时直接核验图片，不能因缺少文字而跳过。证据不足则 term_id=null。"
                  "不得推断人物身份、衣物、道具、渲染器或画外内容。"
                  "返回JSON：{\"decisions\":[{\"shot_id\":\"...\",\"category\":\"...\","
                  "\"term_id\":null,\"reason\":\"...\"}]}。"
                  f"待裁决项：{json.dumps(choices, ensure_ascii=False)}")
        try:
            raw = service.provider._call(prompt, selected_frames, "style.adjudication",
                                         max_tokens=2000, max_attempts=1, timeout_seconds=60, schema=AdjudicationResponse)
            trace = {"raw_response": raw, "batch": batch_number}
            if retrieval is not None:
                retrieval.setdefault("adjudication", []).append(trace)
            parsed = parse_style_response(raw, AdjudicationResponse, array_key="decisions", trace=trace)
            trace["status"] = "parsed"
            service._emit(job_id, "style.adjudication_ready", {"batch": batch_number})
            by_key = {(item["shot_id"], item["category"]): item for item in batch}
            for decision in parsed.decisions:
                key = decision.shot_id, decision.category
                if key in by_key and decision.term_id in {
                    item["term_id"] for item in by_key[key]["candidates"]
                } and not any(cue in by_key[key]["description"]
                              for cue in terms()[decision.term_id].negative_cues):
                    decided[key] = decision.term_id
        except Exception as exc:
            if retrieval is not None:
                retrieval.setdefault("errors", []).append({"phase": "style.adjudication", "error": type(exc).__name__, "detail": error_details(exc)})
            service._emit(job_id, "style.review_needed", {"reason": f"画风歧义裁决失败：{type(exc).__name__}"})
    return decided


def _aggregate(observations: list[ShotStyleObservationV2], matches: list[StyleMatch],
               manifest: dict, *, mode: str) -> ProjectVisualBible:
    catalogue = terms()
    bible = ProjectVisualBible(mode=mode, index_manifest=manifest)
    if not observations:
        bible.unresolved_features = ["缺少逐镜视觉风格证据，请人工设定画风"]
        bible.rendered_global_prompt = render_global(bible)
        bible.bible_hash = bible.digest()
        return bible
    by_id = {item.shot_id: item for item in observations}
    scenes = {item.scene_group_id or item.shot_id for item in observations}
    support: dict[str, list[StyleMatch]] = defaultdict(list)
    for item in matches:
        support[item.term_id].append(item)
    thirds = {item.shot_id: min(2, index * 3 // len(observations))
              for index, item in enumerate(observations)}
    for term_id, items in support.items():
        category = catalogue[term_id].category
        observable = [shot for shot in observations if getattr(shot, category).observable]
        scene_observable = {shot.scene_group_id or shot.shot_id for shot in observable}
        shot_ids = {item.shot_ids[0] for item in items}
        supporting_scenes = {by_id[sid].scene_group_id or sid for sid in shot_ids}
        periods = {thirds[sid] for sid in shot_ids}
        conflicting_ids = {other for other, term in catalogue.items() if other in catalogue[term_id].conflicts_with or term_id in term.conflicts_with}
        counterexample_ids = {match.shot_ids[0] for other in conflicting_ids for match in support.get(other, [])}
        coverage = CoverageProfile(
            term_id=term_id, observable_shots=len(observable), supporting_shots=len(shot_ids),
            counterexample_shots=len(counterexample_ids - shot_ids),
            observable_scenes=len(scene_observable), supporting_scenes=len(supporting_scenes),
            early_support=0 in periods, middle_support=1 in periods, late_support=2 in periods,
            mean_evidence_confidence=sum(item.confidence for item in items) / len(items),
            retrieval_channels=list(dict.fromkeys(channel for item in items for channel in item.ranks)),
        )
        bible.coverage_profiles[term_id] = coverage
        ratio = len(shot_ids) / max(len(observable), 1)
        global_ok = len(shot_ids) >= 3 and ratio >= .70 and len(periods) >= 2 and (
            len(scenes) == 1 or len(supporting_scenes) >= 2)
        if global_ok:
            for item in items:
                item.scope = "GLOBAL"
            bible.global_core_terms.append(term_id)
            continue
        scene_winner = None
        for scene_id in supporting_scenes:
            within = {shot.shot_id for shot in observable if (shot.scene_group_id or shot.shot_id) == scene_id}
            inside = len(shot_ids & within)
            outside = len(shot_ids - within)
            outside_observable = len(observable) - len(within)
            if inside >= 2 and inside / max(len(within), 1) >= .65 and outside / max(outside_observable, 1) < .35:
                scene_winner = scene_id
                break
        if scene_winner:
            bible.scene_overrides.setdefault(scene_winner, []).append(term_id)
            for item in items:
                item.scope = "SCENE" if (by_id[item.shot_ids[0]].scene_group_id or item.shot_ids[0]) == scene_winner else "UNCERTAIN"
        else:
            for item in items:
                item.scope = "SHOT"
                bible.shot_overrides.setdefault(item.shot_ids[0], []).append(term_id)
    # An almost tied rival is an uncertainty, not a forced global Top 1.
    by_category = defaultdict(list)
    for term_id in bible.global_core_terms:
        by_category[catalogue[term_id].category].append(term_id)
    bible.global_core_terms = []
    for category, candidates in by_category.items():
        ordered = sorted(candidates, key=lambda item: (
            -bible.coverage_profiles[item].supporting_shots, item))
        winner = ordered[0]
        support_gap = (bible.coverage_profiles[winner].supporting_shots -
                       bible.coverage_profiles[ordered[1]].supporting_shots) if len(ordered) > 1 else None
        if support_gap is not None and support_gap <= max(1, bible.coverage_profiles[winner].observable_shots * .15):
            bible.unresolved_features.append(f"{category} 存在覆盖接近的冲突候选，需人工裁决")
            for term_id in ordered:
                for match in support[term_id]:
                    match.scope = "UNCERTAIN"
            continue
        bible.global_core_terms.append(winner)
        for term_id in ordered[1:]:
            bible.unresolved_features.append(f"同维度全局候选未采纳：{term_id}")
            for match in support[term_id]:
                match.scope = "UNCERTAIN"
    if "medium_rendering" not in {
        catalogue[item].category for item in bible.global_core_terms}:
        bible.unresolved_features.append("全局媒介尚未确定；已有特征保留，其他维度不强制补齐")
    bible.rendered_global_prompt = render_global(bible)
    bible.bible_hash = bible.digest()
    return bible


def draft_visual_bible(service, job_id: str, *, force: bool = False) -> ProjectVisualBible | None:
    row = service.db.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not row:
        raise ValueError("任务不存在")
    if row["visual_bible_json"] and not force:
        return ProjectVisualBible.model_validate_json(row["visual_bible_json"])
    previous = ProjectVisualBible.model_validate_json(row["visual_bible_json"]) if row["visual_bible_json"] else None
    mode = "creative" if row["input_type"] == "script" else "observed"
    observations = _observations(service, job_id) if mode == "observed" else []
    embedder = None
    retrieval: dict = {"version": "style-vector.v2", "matches": [], "unresolved": [], "errors": []}
    try:
        if mode == "creative":
            bible = _aggregate([], [], {}, mode=mode)
            bible.recommended_recipes = list(RECIPES)
        else:
            embedder = BailianStyleEmbedder(service.settings)
            index = StyleVectorIndex(service.db, embedder)
            if not index.health()["ready"]:
                raise IndexNotReady(index.health()["reason"])
            frames = _frames(service, job_id)
            service._set_stage(job_id, "style.embedding", 95)
            service._emit(job_id, "style.embedding_started", {"shots": len(observations)})
            descriptions = list(dict.fromkeys(
                getattr(shot, category).description for shot in observations for category in CATEGORIES
                if getattr(shot, category).observable))
            vectors = {}
            if descriptions:
                try:
                    vectors = dict(zip(descriptions, index._text_vectors(descriptions, visual=False)))
                except Exception as exc:
                    retrieval["errors"].append({"phase": "style.text_embedding", "error": type(exc).__name__, "detail": error_details(exc)})
            image_vectors = {}
            with ThreadPoolExecutor(max_workers=max(1, min(4, service.settings.style_embed_concurrency))) as executor:
                pending = {executor.submit(index.query_image, frame.path): shot_id
                           for shot_id, frame in frames.items() if shot_id in {item.shot_id for item in observations}}
                for future in as_completed(pending):
                    shot_id = pending[future]
                    try:
                        image_vectors[shot_id] = future.result()
                    except Exception as exc:
                        retrieval["errors"].append({"shot_id": shot_id, "error": type(exc).__name__})
                    completed = len(image_vectors) + len(retrieval["errors"])
                    service._set_stage(job_id, "style.embedding", 95 + int(completed / max(len(pending), 1) * 3))
                    service._emit(job_id, "style.embedding_progress",
                                  {"completed": completed, "total": len(pending)})
            preliminary = []
            ambiguous = []
            for shot in observations:
                for category in CATEGORIES:
                    feature = getattr(shot, category)
                    if not feature.observable and (category == "motion_signature" or shot.shot_id not in image_vectors):
                        continue
                    text_rows = index.search("text", category, vectors[feature.description], 5) if feature.observable and feature.description in vectors else []
                    image_rows = index.search("vision", category, image_vectors[shot.shot_id], 5) \
                        if category != "motion_signature" and shot.shot_id in image_vectors else []
                    aliases = exact_aliases(feature.description, category) if feature.observable else []
                    candidates = rank_fusion(text_rows, image_rows, aliases)
                    chosen = next((item["term_id"] for item in candidates if item["term_id"] in aliases
                                   and grounded_term(item["term_id"], feature.description)), None)
                    # Top-N overlap is only recall, never proof of a subtype.
                    image_missing = category != "motion_signature" and not image_rows
                    if image_missing:
                        chosen = None
                    item = {"shot_id": shot.shot_id, "category": category, "description": feature.description,
                            "candidates": candidates, "chosen": chosen, "image_missing": image_missing}
                    preliminary.append(item)
                    if not chosen and candidates and not image_missing:
                        ambiguous.append(item)
            service._set_stage(job_id, "style.retrieval", 98)
            service._emit(job_id, "style.retrieval_ready",
                          {"candidate_count": len(preliminary), "ambiguous_count": len(ambiguous)})
            # Spread the bounded review across shots instead of spending every
            # slot on the first few shots. Static image retrieval is independent
            # of the text-observation gate; motion still requires sequence data.
            ambiguous.sort(key=lambda item: (CATEGORIES.index(item["category"]), item["shot_id"]))
            decisions = _adjudicate(service, job_id, ambiguous[:20], frames, retrieval)
            matches = []
            for item in preliminary:
                chosen = item["chosen"] or decisions.get((item["shot_id"], item["category"]))
                if not chosen:
                    retrieval["unresolved"].append(item)
                    continue
                source = next(shot for shot in observations if shot.shot_id == item["shot_id"])
                feature = getattr(source, item["category"])
                if not feature.observable:
                    frame = frames.get(item["shot_id"])
                    if frame is None:
                        retrieval["unresolved"].append(item)
                        continue
                    # The image is evidence only AFTER the bounded Flash review
                    # accepts the candidate. Never promote a retrieval rank.
                    description = terms()[chosen].definition
                    feature = StyleFeatureObservation(
                        description=description, observable=True, confidence=.6,
                        evidence=[EvidenceRef(frame_id=frame.id, timestamp=float(frame.timestamp), description=description)],
                        uncertainties=["由图片候选核验补充，使用保守置信度"])
                    setattr(source, item["category"], feature)
                candidate = next((candidate for candidate in item["candidates"] if candidate["term_id"] == chosen), None)
                matches.append(StyleMatch(
                    term_id=chosen, category=item["category"], scope="UNCERTAIN",
                    shot_ids=[item["shot_id"]], scene_id=source.scene_group_id,
                    evidence=feature.evidence, confidence=feature.confidence,
                    ranks=candidate["ranks"] if candidate else {},
                    reason="明确视觉描述别名命中" if item["chosen"] else "Flash 证据裁决",
                ))
            recommendations = []
            try:
                recommendations = recipe_candidates(index, observations, matches, image_vectors, retrieval=retrieval)
                service._set_stage(job_id, "style.recipe_review", 98)
                review_project(service, job_id, observations, matches, frames, recommendations, retrieval)
            except Exception as exc:
                retrieval["errors"].append({"phase": "style.recipe_retrieval", "error": type(exc).__name__})
            for observation in observations:
                service.db.execute("UPDATE shots SET style_observation_v2_json=? WHERE id=?",
                                   (observation.model_dump_json(), observation.shot_id))
            bible = _aggregate(observations, matches, index.manifest() or {}, mode=mode)
            bible.recipe_recommendations = recommendations
            bible.recommended_recipes = [item["recipe_id"] for item in recommendations if item["status"] != "rejected"]
            retrieval["recipe_candidates"] = recommendations
            resolved_keys = {(match.shot_ids[0], match.category) for match in matches}
            retrieval["unresolved"] = [item for item in retrieval["unresolved"]
                                       if (item["shot_id"], item["category"]) not in resolved_keys]
            if retrieval["unresolved"]:
                unresolved_count = len(retrieval["unresolved"])
                bible.unresolved_features.append(f"{unresolved_count} 个逐镜画风特征证据不足或向量通道缺失，未自动选词")
            retrieval["matches"] = [item.model_dump() for item in matches]
            service._set_stage(job_id, "style.aggregation", 99)
            service._emit(job_id, "style.aggregation_ready", {"global_count": len(bible.global_core_terms)})
        bible.model_provenance = {"vision_model": service.provider.health().model,
                                   "recognition_pipeline": "style-evidence.v2.2",
                                   "text_embedding_model": "text-embedding-v4",
                                   "image_embedding_model": "qwen3-vl-embedding"}
        bible.version = previous.version + 1 if previous else 1
        bible.bible_hash = bible.digest()
        now = utc_now()
        with service.db.connect() as conn:
            conn.execute("UPDATE jobs SET visual_bible_json=?,style_retrieval_json=?,updated_at=? WHERE id=?",
                         (bible.model_dump_json(), json.dumps(retrieval, ensure_ascii=False), now, job_id))
            conn.execute("INSERT INTO visual_bible_revisions(job_id,version,bible_json,created_at) VALUES(?,?,?,?)",
                         (job_id, bible.version, bible.model_dump_json(), now))
        service._emit(job_id, "style.bible_draft_ready", {"version": bible.version,
                                                          "global_count": len(bible.global_core_terms)})
        if row["status"] == "completed" and not (
            previous and previous.status == "confirmed"
            and previous.model_provenance.get("application_mode") != "auto-evidence"
        ):
            return auto_apply_visual_bible(service, job_id) or bible
        return bible
    except Exception as exc:
        retrieval["errors"].append({"error": str(exc)[:300]})
        service.db.update_job(job_id, style_retrieval_json=json.dumps(retrieval, ensure_ascii=False))
        service._emit(job_id, "style.review_needed", {"reason": str(exc)[:200]})
        return previous
    finally:
        drain = getattr(service.provider, "drain_usage", None)
        if callable(drain):
            records = drain()
            if records:
                current = service.db.fetchone("SELECT model_usage_json FROM jobs WHERE id=?", (job_id,))
                previous_usage = json.loads(current["model_usage_json"] or "[]")
                service.db.update_job(job_id, model_usage_json=json.dumps([*previous_usage, *records], ensure_ascii=False))
        if embedder:
            try:
                _usage(service, job_id, embedder)
            finally:
                embedder.close()


def save_visual_bible(service, job_id: str, payload: dict) -> ProjectVisualBible:
    row = service.db.fetchone("SELECT status,visual_bible_json FROM jobs WHERE id=?", (job_id,))
    if not row:
        raise ValueError("任务不存在")
    if row["status"] != "completed":
        raise ValueError("任务完成后才能编辑视觉规范")
    previous = ProjectVisualBible.model_validate_json(row["visual_bible_json"]) if row["visual_bible_json"] else None
    values = dict(payload)
    # Recommendation evidence is server-produced; editing a creative recipe
    # must not rewrite the record of what was actually observed/reviewed.
    values["recipe_recommendations"] = previous.recipe_recommendations if previous else []
    values.update(schema_version="project-visual-bible.v2", status="draft",
                  version=(previous.version + 1 if previous else 1), ontology_version=VERSION)
    values.pop("bible_hash", None)
    values.pop("rendered_global_prompt", None)
    recipe_id = values.get("selected_recipe_id")
    values["selected_recipe_term_ids"] = list(RECIPES[recipe_id].term_ids) if recipe_id in RECIPES else []
    if recipe_id in RECIPES:
        seen_categories = set()
        core = []
        for term_id in RECIPES[recipe_id].term_ids:
            category = terms()[term_id].category
            if category not in seen_categories:
                seen_categories.add(category)
                core.append(term_id)
        values["global_core_terms"] = core
        values["global_secondary_terms"] = []
    bible = ProjectVisualBible.model_validate(values)
    shot_rows = service.db.fetchall("SELECT id,scene_group_id FROM shots WHERE job_id=?", (job_id,))
    shot_ids = {row["id"] for row in shot_rows}
    scene_ids = {row["scene_group_id"] for row in shot_rows if row["scene_group_id"]}
    if (set(bible.shot_overrides) | set(bible.local_effects)) - shot_ids:
        raise ValueError("镜头覆盖项引用了其他任务")
    if set(bible.scene_overrides) - scene_ids:
        raise ValueError("场次覆盖项引用了其他任务")
    bible.rendered_global_prompt = render_global(bible)
    bible.bible_hash = bible.digest()
    now = utc_now()
    with service.db.connect() as conn:
        conn.execute("UPDATE jobs SET visual_bible_json=?,updated_at=? WHERE id=?",
                     (bible.model_dump_json(), now, job_id))
        conn.execute("INSERT INTO visual_bible_revisions VALUES(?,?,?,?)",
                     (job_id, bible.version, bible.model_dump_json(), now))
    service._emit(job_id, "style.bible_draft_ready", {"version": bible.version})
    return bible


def auto_apply_visual_bible(service, job_id: str) -> Optional[ProjectVisualBible]:
    try:
        return _auto_apply_visual_bible(service, job_id)
    except Exception as exc:
        service._emit(job_id, "style.review_needed", {"reason": "自动应用画风失败，已保留草案：" + str(exc)[:200]})
        return None


def _auto_apply_visual_bible(service, job_id: str) -> Optional[ProjectVisualBible]:
    """Apply only aggregated observed facts; never auto-select a creative recipe.

    No visual evidence (including scripts) means no fabricated style. Old jobs
    are untouched until an explicit redraft; user edits remain user-controlled.
    """
    job = service.db.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or job["status"] != "completed" or not job["visual_bible_json"]:
        return None
    bible = ProjectVisualBible.model_validate_json(job["visual_bible_json"])
    if bible.status == "confirmed" or bible.mode != "observed" or bible.selected_recipe_id:
        return None
    if not any(terms()[item].category == "medium_rendering" for item in bible.global_core_terms):
        service._emit(job_id, "style.review_needed", {"reason": "媒介证据不足，未自动套用完整画风"})
        return None
    for term_id in [*bible.global_core_terms, *bible.global_secondary_terms]:
        coverage = bible.coverage_profiles.get(term_id)
        if not coverage or coverage.supporting_shots < 3 or coverage.mean_evidence_confidence < .6:
            return None
    return confirm_visual_bible(service, job_id, automatic=True)


def confirm_visual_bible(service, job_id: str, *, automatic: bool = False) -> ProjectVisualBible:
    job = service.db.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or not job["visual_bible_json"]:
        raise ValueError("请先创建 Visual Bible 草案")
    if job["status"] != "completed":
        raise ValueError("任务完成后才能应用 Visual Bible")
    bible = ProjectVisualBible.model_validate_json(job["visual_bible_json"])
    if bible.status == "confirmed":
        raise ValueError("当前版本已经确认")
    categories = {terms()[item].category for item in bible.global_core_terms}
    if not bible.selected_recipe_id and "medium_rendering" not in categories:
        raise ValueError("需确认至少一个媒介特征或选择创作配方；不要求凑齐三个维度")
    if bible.selected_recipe_id:
        bible.selected_recipe_term_ids = list(RECIPES[bible.selected_recipe_id].term_ids)
    bible.rendered_global_prompt = render_global(bible)
    bible.status = "confirmed"
    bible.model_provenance["application_mode"] = "auto-evidence" if automatic else "user-confirmed"
    if automatic:
        bible.model_provenance["application_detail"] = "仅自动应用已聚合的帧证据特征；配方与工作室锚点未自动选用"
    bible.bible_hash = bible.digest()
    rows = service.db.fetchall("SELECT * FROM shots WHERE job_id=? ORDER BY number", (job_id,))
    frames = _frames(service, job_id)
    legacy_templates = {item["shot_id"]: item["template_id"] for item in service.db.fetchall(
        "SELECT shot_id,template_id FROM prompts WHERE job_id=? AND template_id IN ('script-first-frame-v1','seedance-script-v1')", (job_id,))}
    prepared = []
    for row in rows:
        if not row["observation_json"]:
            continue
        observation = VisionObservation.model_validate_json(row["observation_json"])
        frame = frames.get(row["id"])
        divisor = gcd(frame.width, frame.height) if frame and frame.width and frame.height else 0
        ratio = f"{frame.width//divisor}:{frame.height//divisor}" if divisor else "待设定"
        prepared.extend(build_prompts(
            row["id"], observation, (job["script_text"] or "")[:3000],
            source_edit_times=json.loads(row["source_edit_times_json"] or "[]"),
            generation_duration_seconds=row["generation_duration_seconds"] or row["duration"],
            aspect_ratio=ratio, visual_bible=bible, scene_group_id=row["scene_group_id"] or "",
            template_id=legacy_templates.get(row["id"]),
        ))
    if not prepared:
        raise ValueError("没有可重新渲染的镜头 Prompt")
    now = utc_now()
    with service.db.connect() as conn:
        for prompt in prepared:
            old = conn.execute("SELECT * FROM prompts WHERE job_id=? AND shot_id=? AND prompt_type=? AND platform=?",
                               (job_id, prompt.shot_id, prompt.prompt_type, prompt.platform)).fetchone()
            if old:
                conn.execute("INSERT OR REPLACE INTO prompt_revisions VALUES(?,?,?,?,?)",
                             (job_id, old["id"], bible.version, json.dumps(dict(old), ensure_ascii=False), now))
                conn.execute("DELETE FROM prompts WHERE id=?", (old["id"],))
            conn.execute(
                "INSERT INTO prompts(id,job_id,shot_id,prompt_type,platform,version,positive_prompt,negative_prompt,"
                "status,template_id,template_version,warnings_json,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (prompt.id, job_id, prompt.shot_id, prompt.prompt_type, prompt.platform, prompt.version,
                 prompt.positive_prompt, prompt.negative_prompt, prompt.status, prompt.template_id,
                 prompt.template_version, json.dumps(prompt.warnings, ensure_ascii=False),
                 prompt.contract.model_dump_json() if prompt.contract else None))
        conn.execute("UPDATE jobs SET visual_bible_json=?,updated_at=? WHERE id=?",
                     (bible.model_dump_json(), now, job_id))
        conn.execute("UPDATE visual_bible_revisions SET bible_json=? WHERE job_id=? AND version=?",
                     (bible.model_dump_json(), job_id, bible.version))
    service._emit(job_id, "style.bible_confirmed", {"version": bible.version, "prompt_count": len(prepared)})
    return bible


def bible_history(service, job_id: str) -> dict:
    rows = service.db.fetchall("SELECT version,bible_json,created_at FROM visual_bible_revisions WHERE job_id=? ORDER BY version",
                               (job_id,))
    return {"revisions": [{"version": row["version"], "bible": json.loads(row["bible_json"]),
                           "created_at": row["created_at"]} for row in rows]}
