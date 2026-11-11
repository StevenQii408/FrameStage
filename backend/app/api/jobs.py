import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.services.style_workflow import catalog_payload, confirm_style, draft_job_style, save_style_draft, style_history
from app.projects.http import request_project
from app.projects.models import ProjectError
from app.projects.access import active_project

router = APIRouter(prefix="/jobs")


def _job_or_404(request: Request, job_id: str):
    service = request.app.state.job_service
    if not service.status(job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return service


@router.post("", status_code=202)
async def create_job(request: Request, video: Optional[UploadFile] = File(None), script_text: str = Form(""), input_type: str = Form(""), force_replan: bool = Form(False), project_id: Optional[str] = Form(None)):
    service = request.app.state.job_service
    settings = request.app.state.settings
    try:
        project_id = request_project(request, project_id)
        with service.db.connect() as conn:
            active_project(conn, project_id)
        service.assert_capacity()
    except ProjectError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    provider = service.provider.health()
    if provider.provider == "bailian" and not provider.ready:
        raise HTTPException(status_code=503, detail=f"云端模型未配置完成：{provider.detail}。请在后端运行 python -m app.cloud_setup，保存后重启服务。")
    normalized_type = (input_type or ("video" if video else "script")).lower().strip()
    if normalized_type not in {"video", "script"}:
        raise HTTPException(status_code=400, detail="input_type 必须是 video 或 script")
    if len(script_text) > 10000:
        raise HTTPException(status_code=422, detail=f"文本共 {len(script_text)} 字，超过 10000 字上限；请按场次分批提交，原文未被截断。")
    if normalized_type == "script":
        if not script_text.strip():
            raise HTTPException(status_code=400, detail="剧本模式必须提供 script_text")
        try:
            job_id = service.create_script_job(script_text, **({"project_id": project_id} if project_id else {}))
        except ProjectError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {"job_id": job_id, "status": "queued", "input_type": "script"}
    if video is None:
        raise HTTPException(status_code=400, detail="视频模式必须上传 MP4")
    filename = Path(video.filename or "input.mp4").name
    if not filename.lower().endswith(".mp4"):
        raise HTTPException(status_code=400, detail="MVP 当前只支持 MP4")
    fd, temp_name = tempfile.mkstemp(prefix="storyboard-upload-", suffix=".mp4", dir=str(settings.data_dir))
    os.close(fd)
    temp = Path(temp_name)
    size = 0
    try:
        with temp.open("wb") as target:
            while True:
                chunk = await video.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_video_size_mb * 1024 * 1024:
                    raise HTTPException(status_code=413, detail=f"视频超过 {settings.max_video_size_mb}MB 限制")
                target.write(chunk)
        try:
            job_id = service.create_job(filename, script_text, temp, force_replan=force_replan,
                                        **({"project_id": project_id} if project_id else {}))
        except ProjectError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {"job_id": job_id, "status": "queued", "input_type": "video"}
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


@router.post("/preflight")
def preflight(request: Request, input_type: str = Form("video"), script_text: str = Form(""), duration_seconds: Optional[float] = Form(None)):
    """Advisory, no upload or inference; exact capacity is checked before billing."""
    from app.services.capacity import script_preflight, video_preflight
    config = request.app.state.job_service.config()
    try:
        if input_type == "script":
            return script_preflight(script_text, config.max_shots)
        if input_type != "video" or duration_seconds is None:
            raise ValueError("视频预检需要 duration_seconds，input_type必须为video或script")
        return video_preflight(duration_seconds, config.max_shots, config.max_video_duration_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/style/catalog")
def get_style_catalog():
    return catalog_payload()


@router.get("/{job_id}/style")
def get_style(request: Request, job_id: str):
    service = _job_or_404(request, job_id)
    row = service.status(job_id)
    return {
        "profile": json.loads(row["style_profile_json"]) if row["style_profile_json"] else None,
        "observation": json.loads(row["style_observation_json"]) if row["style_observation_json"] else None,
    }


@router.post("/{job_id}/style/draft")
def create_style_draft(request: Request, job_id: str):
    service = _job_or_404(request, job_id)
    strategy = json.loads(service.status(job_id)["config_json"]).get("style_strategy")
    if strategy in ("vector-v2", "qwen-direct-v3"):
        raise HTTPException(status_code=409, detail="新任务请使用对应的Visual Bible或直接画风分析接口")
    if service.status(job_id)["status"] != "completed":
        raise HTTPException(status_code=409, detail="任务完成后才能重新生成画风草案")
    def draft_and_record():
        try:
            return draft_job_style(service, job_id, force=True)
        finally:
            drain = getattr(service.provider, "drain_usage", None)
            if callable(drain):
                records = drain()
                if records:
                    previous = json.loads(service.status(job_id).get("model_usage_json") or "[]")
                    service.db.update_job(job_id, model_usage_json=json.dumps([*previous, *records], ensure_ascii=False))

    try:
        # Share the existing single worker with analysis jobs.
        future = service.executor.submit(draft_and_record)
        profile = future.result(timeout=service.settings.vlm_timeout_seconds * 2 + 30)
        style_row = service.status(job_id)
        return {"profile": profile.model_dump() if profile else None,
                "observation": json.loads(style_row["style_observation_json"]) if style_row["style_observation_json"] else None}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"画风草案生成失败：{exc}") from exc


@router.put("/{job_id}/style")
def update_style_draft(request: Request, job_id: str, payload: dict):
    service = _job_or_404(request, job_id)
    if json.loads(service.status(job_id)["config_json"]).get("style_strategy") in ("vector-v2", "qwen-direct-v3"):
        raise HTTPException(status_code=409, detail="新任务请使用对应的Visual Bible或直接画风分析接口")
    try:
        return save_style_draft(service, job_id, payload).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{job_id}/style/confirm")
def confirm_style_draft(request: Request, job_id: str):
    service = _job_or_404(request, job_id)
    if json.loads(service.status(job_id)["config_json"]).get("style_strategy") in ("vector-v2", "qwen-direct-v3"):
        raise HTTPException(status_code=409, detail="新任务请使用对应的Visual Bible或直接画风分析接口")
    try:
        return confirm_style(service, job_id).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{job_id}/style/history")
def get_style_history(request: Request, job_id: str):
    return style_history(_job_or_404(request, job_id), job_id)


@router.get("/{job_id}")
def get_job(request: Request, job_id: str):
    service = _job_or_404(request, job_id)
    row = service.status(job_id)
    row["config"] = json.loads(row.pop("config_json"))
    row.pop("video_path", None)
    row.pop("script_plan_json", None)
    row.pop("script_raw_response_json", None)
    row.pop("planning_warnings_json", None)
    row.pop("style_profile_json", None)
    row.pop("style_observation_json", None)
    row.pop("visual_bible_json", None)
    row.pop("style_retrieval_json", None)
    row.pop("generation_plan_json", None)
    row.pop("event_sequence_json", None)
    return row


@router.get("/{job_id}/result")
def get_result(request: Request, job_id: str):
    service = _job_or_404(request, job_id)
    result = service.result(job_id)
    return result.model_dump() if result else JSONResponse(status_code=404, content={"detail": "结果不存在"})


@router.get("/{job_id}/sequences")
def get_sequences(request: Request, job_id: str):
    """Read-only derived preview; opening this endpoint never calls a model."""
    service = _job_or_404(request, job_id)
    from app.services.event_sequences import sequence_payload_for_job
    try:
        return sequence_payload_for_job(service, job_id, persist=False)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{job_id}/sequences/rebuild")
def rebuild_sequences(request: Request, job_id: str):
    """Explicitly save a local derived plan; source segmentation is immutable."""
    service = _job_or_404(request, job_id)
    if service.status(job_id)["status"] != "completed":
        raise HTTPException(status_code=409, detail="任务完成后才能保存生成片段；不会重新调用模型")
    from app.services.event_sequences import sequence_payload_for_job
    try:
        return sequence_payload_for_job(service, job_id, persist=True)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{job_id}/sequences/history")
def sequence_history(request: Request, job_id: str):
    service = _job_or_404(request, job_id)
    rows = service.db.fetchall(
        "SELECT version,payload_json,created_at FROM event_sequence_revisions WHERE job_id=? ORDER BY version",
        (job_id,),
    )
    return {"revisions": [{"version": row["version"], "payload": json.loads(row["payload_json"]),
                           "created_at": row["created_at"]} for row in rows]}


@router.get("/{job_id}/shots")
def get_shots(request: Request, job_id: str):
    service = _job_or_404(request, job_id)
    result = service.result(job_id)
    return {"job_id": job_id, "shots": [shot.model_dump() for shot in result.shots]} if result else JSONResponse(status_code=404, content={"detail": "结果不存在"})


@router.get("/{job_id}/export")
def export_result(request: Request, job_id: str):
    service = _job_or_404(request, job_id)
    result = service.result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="结果不存在")
    payload = json.dumps(result.model_dump(), ensure_ascii=False, indent=2)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="storyboard-{job_id}.json"'},
    )


@router.get("/{job_id}/events")
async def events(request: Request, job_id: str, after: int = 0):
    service = _job_or_404(request, job_id)
    try:
        cursor_start = max(after, int(request.headers.get("last-event-id", "0")))
        if after < 0 or int(request.headers.get("last-event-id", "0")) < 0 or cursor_start > 2**63 - 1:
            raise ValueError("negative cursor")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="事件游标必须为非负整数") from exc

    async def stream():
        cursor = cursor_start
        while not await request.is_disconnected():
            pending = service.db.events_since(job_id, cursor)
            for event in pending:
                cursor = event["sequence"]
                yield f"id: {event['sequence']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            row = service.status(job_id)
            if row and row["status"] in ("completed", "failed", "cancelled", "paused_memory_pressure", "interrupted"):
                # A worker can commit the terminal event after the first query
                # but before this status read. Send that tail before closing.
                for event in service.db.events_since(job_id, cursor):
                    cursor = event["sequence"]
                    yield f"id: {event['sequence']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                break
            await service.events.wait(job_id, timeout=2.0)
            yield ": heartbeat\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/{job_id}/cancel")
def cancel(request: Request, job_id: str):
    service = _job_or_404(request, job_id)
    if not service.cancel(job_id):
        raise HTTPException(status_code=409, detail="任务当前无法取消")
    return {"job_id": job_id, "status": "cancelled"}


@router.post("/{job_id}/resume")
def resume(request: Request, job_id: str):
    service = _job_or_404(request, job_id)
    try:
        resumed = service.resume(job_id)
    except ProjectError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if not resumed:
        raise HTTPException(status_code=409, detail="仅同一 Provider 和模型的中断、内存压力暂停或可恢复云端连接失败任务可恢复；已取消、已完成或模型切换的任务不可恢复")
    return {"job_id": job_id, "status": "queued"}
