"""Project homepage and one versioned canvas per project."""
from fastapi import APIRouter, HTTPException, Query, Request

from app.projects.models import AssetBinding, CanvasSave, LegacyImport, ProjectCreate, ProjectError


router = APIRouter(prefix="/projects")


def invoke(call):
    try:
        return call()
    except ProjectError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("")
def list_projects(request: Request, deleted: bool = False, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), q: str = Query("", max_length=100)):
    return request.app.state.project_service.list(deleted, limit, offset, q)


@router.post("", status_code=201)
def create_project(request: Request, payload: ProjectCreate):
    return invoke(lambda: request.app.state.project_service.create(payload.name, payload.idempotency_key))


@router.get("/{project_id}")
def get_project(request: Request, project_id: str):
    return invoke(lambda: request.app.state.project_service.get(project_id))


@router.patch("/{project_id}")
def rename_project(request: Request, project_id: str, payload: ProjectCreate):
    return invoke(lambda: request.app.state.project_service.rename(project_id, payload.name))


@router.delete("/{project_id}")
def delete_project(request: Request, project_id: str):
    return invoke(lambda: request.app.state.project_service.delete(project_id))


@router.post("/{project_id}/restore")
def restore_project(request: Request, project_id: str):
    return invoke(lambda: request.app.state.project_service.restore(project_id))


@router.get("/{project_id}/canvas")
def get_canvas(request: Request, project_id: str):
    return invoke(lambda: request.app.state.project_service.canvas(project_id))


@router.put("/{project_id}/canvas")
def put_canvas(request: Request, project_id: str, payload: CanvasSave):
    return invoke(lambda: request.app.state.project_service.save(project_id, payload.expected_revision, payload.state))


@router.post("/{project_id}/import-legacy")
def import_legacy(request: Request, project_id: str, payload: LegacyImport):
    return invoke(lambda: request.app.state.project_service.import_legacy(project_id, payload))


@router.post("/{project_id}/assets")
def bind_assets(request: Request, project_id: str, payload: AssetBinding):
    return invoke(lambda: request.app.state.project_service.bind_assets(project_id, payload.asset_ids))
