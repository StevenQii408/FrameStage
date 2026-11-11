import json
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.style_direct.service import (MAX_BYTES, apply_style, create_analysis, get_analysis,
                                      update_profile)
from app.style_direct.diagnostics import job_diagnostics, legacy_diagnostics, markdown_report
from app.projects.access import owned_resource, owned_style
from app.projects.http import request_project
from app.projects.models import ProjectError

router = APIRouter()


def _get(request, analysis_id):
    try:
        with request.app.state.db.connect() as conn:
            owned_style(conn, analysis_id, request_project(request))
        return get_analysis(request.app.state.job_service, analysis_id)
    except ProjectError as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.post("/style/analyses", status_code=202)
async def create(request: Request, image: Optional[UploadFile] = File(None),
                 job_id: str = Form(""), frame_id: str = Form(""), force: bool = Form(False)):
    service = request.app.state.job_service
    health = service.provider.health()
    if not health.ready:
        raise HTTPException(503, detail=health.detail or "云端模型未就绪")
    if bool(image) == bool(job_id or frame_id):
        raise HTTPException(400, detail="请选择上传图片或一个任务帧引用，不能同时提供")
    try:
        project_id = request_project(request)
        if job_id:
            with service.db.connect() as conn:
                owned_resource(conn, "jobs", job_id, project_id)
        if image:
            chunks, size = [], 0
            while True:
                chunk = await image.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, detail="参考图片超过25MB")
                chunks.append(chunk)
            sources = [(b"".join(chunks), {})]
        else:
            frame = service.db.fetchone("SELECT * FROM frames WHERE job_id=? AND id=?", (job_id, frame_id))
            if not frame or not Path(frame["path"]).is_file():
                raise HTTPException(404, detail="任务帧不存在")
            sources = [(Path(frame["path"]).read_bytes(), {"origin_job_id": job_id, "origin_frame_id": frame_id,
                         "timestamp": frame["timestamp"]})]
        return create_analysis(service, sources, force=force, project_id=project_id)
    except ProjectError as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("/style/analyses/{analysis_id}")
def get(request: Request, analysis_id: str):
    return _get(request, analysis_id)


@router.get("/style/analyses/{analysis_id}/history")
def history(request: Request, analysis_id: str):
    _get(request, analysis_id)
    rows = request.app.state.db.fetchall("SELECT version,profile_json,created_at FROM style_analysis_revisions WHERE analysis_id=? ORDER BY version", (analysis_id,))
    return {"revisions": [{"version": row["version"], "profile": json.loads(row["profile_json"]), "created_at": row["created_at"]} for row in rows]}


@router.get("/style/analyses/{analysis_id}/diagnostics")
def diagnostics(request: Request, analysis_id: str, format: Literal["json", "markdown"] = "json"):
    analysis = _get(request, analysis_id)
    data = (analysis.get("result") or {}).get("diagnostics") or legacy_diagnostics()
    if format == "markdown":
        return Response(markdown_report(data), media_type="text/markdown; charset=utf-8")
    return data


@router.get("/jobs/{job_id}/analysis/diagnostics")
def task_diagnostics(request: Request, job_id: str):
    try:
        return job_diagnostics(request.app.state.job_service, job_id)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.get("/style/analyses/{analysis_id}/images/{alias}")
def image(request: Request, analysis_id: str, alias: str):
    _get(request, analysis_id)
    data = get_analysis(request.app.state.job_service, analysis_id, private=True)
    row = request.app.state.db.fetchone("SELECT source_json FROM style_analyses WHERE id=?", (analysis_id,))
    assets = data["result"].get("assets", json.loads(row["source_json"])["assets"])
    asset = next((a for a in assets if a["alias"] == alias), None)
    path = request.app.state.db.media.open_path(asset["path"]) if asset else None
    if not path or not Path(path).is_file():
        raise HTTPException(404, detail="证据图片不存在")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/style/analyses/{analysis_id}/events")
async def events(request: Request, analysis_id: str, after: int = 0):
    _get(request, analysis_id)
    service = request.app.state.job_service
    async def stream():
        cursor = after
        while not await request.is_disconnected():
            for event in service.db.events_since(analysis_id, cursor):
                cursor = event["sequence"]
                yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if get_analysis(service, analysis_id)["status"] in ("completed", "partial", "failed"):
                break
            await service.events.wait(analysis_id, timeout=2)
            yield ": heartbeat\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


class LibraryRecipeSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['library-recipe']
    recipe_id: str = Field(min_length=1, max_length=120)
    source_hash: str = Field(min_length=1, max_length=128)


class EditProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rendered_prompt: str = Field(min_length=4, max_length=1200)
    expected_version: int = Field(ge=1)
    source: Optional[LibraryRecipeSource] = None


@router.put("/style/analyses/{analysis_id}/profile")
def edit(request: Request, analysis_id: str, payload: EditProfile):
    _get(request, analysis_id)
    try:
        return update_profile(request.app.state.job_service, analysis_id, payload.rendered_prompt, payload.expected_version,
                              payload.source.model_dump() if payload.source else None)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


class ApplyStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: str
    expected_version: int = Field(ge=1)


@router.post("/jobs/{job_id}/style/apply")
def apply(request: Request, job_id: str, payload: ApplyStyle):
    _get(request, payload.analysis_id)
    try:
        return apply_style(request.app.state.job_service, job_id, payload.analysis_id, payload.expected_version)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/style/direct")
def project_style(request: Request, job_id: str):
    job = request.app.state.job_service.status(job_id)
    if not job:
        raise HTTPException(404, detail="任务不存在")
    state = json.loads(job["style_direct_json"] or "{}")
    analysis_id = state.get("pending_analysis_id") or state.get("analysis_id")
    return {"state": state, "analysis": _get(request, analysis_id) if analysis_id else None}


@router.post("/jobs/{job_id}/style/direct/analyze", status_code=202)
def analyze_project(request: Request, job_id: str):
    service = request.app.state.job_service
    job = service.status(job_id)
    if not job or job["status"] != "completed":
        raise HTTPException(409, detail="视频任务完成后才能重分析画风")
    if job["input_type"] != "video":
        raise HTTPException(422, detail="剧本没有画面证据，请先导入参考图片")
    if not service.provider.health().ready:
        raise HTTPException(503, detail="云端模型未就绪")
    from app.style_direct.service import project_sources
    sources = project_sources(service, job_id)
    if not sources:
        raise HTTPException(422, detail="任务没有可用帧")
    try:
        return create_analysis(service, sources, scope="project", force=True, project_id=request_project(request),
                               pending_job_id=job_id)
    except ProjectError as exc:
        raise HTTPException(exc.status_code, detail=str(exc)) from exc
