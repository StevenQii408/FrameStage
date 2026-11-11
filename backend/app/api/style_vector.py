"""Visual Bible v2 and explicit style-index administration."""
from __future__ import annotations

from dataclasses import asdict
import json

from fastapi import APIRouter, HTTPException, Request

from app.style_vector.embedding import BailianStyleEmbedder, persist_usage
from app.style_vector.index import StyleVectorIndex
from app.style_vector.ontology import RECIPES, VERSION, ontology_hash, terms
from app.style_vector.workflow import bible_history, confirm_visual_bible, draft_visual_bible, save_visual_bible


style_router = APIRouter(prefix="/style")
bible_router = APIRouter(prefix="/jobs")


def _job(request: Request, job_id: str):
    service = request.app.state.job_service
    row = service.status(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if json.loads(row["config_json"]).get("style_strategy") != "vector-v2":
        raise HTTPException(status_code=409, detail="该任务使用旧版画风配置，只能通过 V1 接口读取")
    return service


@style_router.get("/catalog/v2")
def catalog():
    return {"version": VERSION, "ontology_hash": ontology_hash(),
            "categories": ["medium_rendering", "form_language", "color_tone",
                           "lighting_shading", "texture_material", "motion_signature"],
            "terms": [asdict(item) for item in terms().values()],
            "recipes": [asdict(item) for item in RECIPES.values()]}


@style_router.get("/index/health")
def index_health(request: Request):
    return StyleVectorIndex(request.app.state.db, None).health()


@style_router.post("/index/rebuild")
def rebuild_index(request: Request):
    service = request.app.state.job_service
    if not service.settings.style_allow_api_rebuild or not request.client or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="付费索引重建默认仅允许本机 CLI；显式开启本机 API 后才能调用")
    try:
        def work():
            embedder = BailianStyleEmbedder(service.settings)
            index = StyleVectorIndex(service.db, embedder)
            try:
                manifest = index.build()
            finally:
                usage = embedder.drain_usage()
                persist_usage(service.db, usage)
                embedder.close()
            return {"manifest": manifest, "requests": len(usage),
                    "estimated_list_price_cny": round(sum(row.get("estimated_list_price_cny") or 0 for row in usage), 6)}
        return service.executor.submit(work).result(timeout=900)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"向量索引构建失败：{exc}") from exc


@bible_router.get("/{job_id}/visual-bible")
def get_bible(request: Request, job_id: str):
    service = _job(request, job_id)
    row = service.status(job_id)
    return {"bible": json.loads(row["visual_bible_json"]) if row.get("visual_bible_json") else None,
            "retrieval": json.loads(row["style_retrieval_json"]) if row.get("style_retrieval_json") else None}


@bible_router.post("/{job_id}/visual-bible/draft")
def create_bible(request: Request, job_id: str):
    service = _job(request, job_id)
    if service.status(job_id)["status"] != "completed":
        raise HTTPException(status_code=409, detail="任务完成后才能重新分析项目画风")
    bible = service.executor.submit(draft_visual_bible, service, job_id, force=True).result(
        timeout=max(120, service.settings.vlm_timeout_seconds * 2 + 120))
    return {"bible": bible.model_dump() if bible else None,
            "retrieval": json.loads(service.status(job_id).get("style_retrieval_json") or "{}")}


@bible_router.put("/{job_id}/visual-bible")
def update_bible(request: Request, job_id: str, payload: dict):
    service = _job(request, job_id)
    try:
        return save_visual_bible(service, job_id, payload).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@bible_router.post("/{job_id}/visual-bible/confirm")
def confirm_bible(request: Request, job_id: str):
    service = _job(request, job_id)
    try:
        return confirm_visual_bible(service, job_id).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@bible_router.get("/{job_id}/visual-bible/history")
def history(request: Request, job_id: str):
    return bible_history(_job(request, job_id), job_id)


@bible_router.get("/{job_id}/shots/{shot_id}/style-observation")
def shot_style_observation(request: Request, job_id: str, shot_id: str):
    service = _job(request, job_id)
    row = service.db.fetchone(
        "SELECT style_observation_v2_json FROM shots WHERE id=? AND job_id=?", (shot_id, job_id))
    if row is None:
        raise HTTPException(status_code=404, detail="镜头不存在")
    return json.loads(row["style_observation_v2_json"]) if row["style_observation_v2_json"] else None
