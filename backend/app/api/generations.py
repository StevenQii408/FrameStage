"""Generation contracts and durable, project-multiplexed SSE."""
import json

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError

from app.generation.assets import MAX_UPLOAD_BYTES
from app.generation.models import TERMINAL, GenerationCreate, GenerationError, GenerationRequest, RetryRequest, RemoteStopConfirmation
from app.projects.models import ProjectError
from app.projects.http import request_project
from app.projects.access import active_project


router = APIRouter()


def _service(request):
    return request.app.state.generation_service


def _invoke(call):
    try:
        return call()
    except (GenerationError, ProjectError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json(include_url=False))) from exc


@router.get("/generations/capabilities")
def capabilities(request: Request):
    return _service(request).capabilities()


def _cursor(request, after):
    try:
        last_id = int(request.headers.get("last-event-id", "0"))
        if min(after, last_id) < 0 or max(after, last_id) > 2**63 - 1:
            raise ValueError("invalid cursor")
        return max(after, last_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="事件游标必须为非负整数") from exc


def _wire_event(item):
    event = {key: value for key, value in item.items() if key != "payload_json"}
    return f"id: {item['sequence']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/generations/events")
async def project_events(request: Request, after: int = 0):
    """One stream per project; a browser subscription never starts cloud polling."""
    service = _service(request)
    project_id = _invoke(lambda: request_project(request))
    scope = request.query_params.get("scope")
    if scope not in {None, "legacy"} or (scope == "legacy" and project_id):
        raise HTTPException(status_code=422, detail="生成事件范围与项目不一致")
    if project_id:
        def validate():
            with service.repository.db.connect() as conn:
                active_project(conn, project_id)
        await run_in_threadpool(_invoke, validate)
    cursor_start = _cursor(request, after)

    async def stream():
        cursor = cursor_start
        while not await request.is_disconnected():
            if project_id:
                try:
                    await run_in_threadpool(validate)
                except ProjectError:
                    break
            batch = await run_in_threadpool(service.repository.project_events_since, project_id, cursor)
            for item in batch:
                cursor = item["sequence"]
                yield _wire_event(item)
            if len(batch) == 200:
                continue
            await service.events.wait("generation-project:" + (project_id or "legacy"), timeout=2.0)
            yield ": heartbeat\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/generation-assets", status_code=201)
async def upload_asset(request: Request, image: UploadFile = File(...), project_id: str = Form(None)):
    data = bytearray()
    try:
        while True:
            chunk = await image.read(1024 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="参考图片超过 20MB 限制")
        def save():
            project = request_project(request, project_id)
            if project:
                with _service(request).repository.db.connect() as conn:
                    active_project(conn, project)
            asset = _service(request).assets.image(bytes(data))
            if project:
                request.app.state.project_service.bind_assets(project, [asset["id"]], uploaded=True)
            return asset
        return await run_in_threadpool(_invoke, save)
    finally:
        await image.close()


@router.get("/generation-assets/{asset_id}")
def get_asset(request: Request, asset_id: str, download: bool = False):
    asset, path = _invoke(lambda: _service(request).assets.path(asset_id))
    return FileResponse(path, media_type=asset["mime_type"], filename=path.name if download else None,
                        headers={"X-Content-Type-Options": "nosniff"})


@router.get("/generation-assets/{asset_id}/poster")
async def get_video_poster(request: Request, asset_id: str):
    path = await run_in_threadpool(_invoke, lambda: _service(request).assets.video_poster(asset_id))
    return FileResponse(path, media_type="image/jpeg", headers={
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"})


@router.post("/generations/preflight")
def preflight(request: Request, payload: GenerationRequest):
    def run():
        payload.project_id = request_project(request, payload.project_id)
        return _service(request).preflight(payload)
    return _invoke(run)


@router.post("/generations", status_code=201)
def create(request: Request, payload: dict):
    def run():
        project = request_project(request, payload.get("project_id"))
        if project is not None:
            payload["project_id"] = project
        return _service(request).create(payload)
    return _invoke(run)


@router.get("/generations/{job_id}")
def get_generation(request: Request, job_id: str):
    return _invoke(lambda: _service(request).get(job_id))


@router.post("/generations/{job_id}/cancel")
def cancel(request: Request, job_id: str):
    return _invoke(lambda: _service(request).cancel(job_id))


@router.post("/generations/{job_id}/retry", status_code=201)
def retry(request: Request, job_id: str, payload: RetryRequest):
    return _invoke(lambda: _service(request).retry(job_id, payload.idempotency_key))


@router.post("/generations/{job_id}/confirm-cloud-stopped")
def confirm_cloud_stopped(request: Request, job_id: str, payload: RemoteStopConfirmation):
    """Operator recovery only, after checking the vendor console; no implicit retry."""
    return _invoke(lambda: _service(request).confirm_remote_stopped(job_id))


@router.get("/generations/{job_id}/events")
async def events(request: Request, job_id: str, after: int = 0):
    service = _service(request)
    _invoke(lambda: service.get(job_id))
    cursor_start = _cursor(request, after)

    async def stream():
        cursor = cursor_start
        while not await request.is_disconnected():
            for item in service.repository.events_since(job_id, cursor):
                cursor = item["sequence"]
                event = {key: value for key, value in item.items() if key != "payload_json"}
                yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if service.get(job_id)["status"] in TERMINAL:
                # The terminal transaction may commit between the first event
                # query and status read. Drain its final event before closing.
                for item in service.repository.events_since(job_id, cursor):
                    cursor = item["sequence"]
                    event = {key: value for key, value in item.items() if key != "payload_json"}
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                break
            await service.events.wait("generation:" + job_id, timeout=2.0)
            yield ": heartbeat\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
