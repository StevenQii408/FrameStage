"""Explicit, bounded shot recovery. GET endpoints never call a provider."""
from fastapi import APIRouter, HTTPException, Request
from app.services.perception_operations import PerceptionOperationError, PerceptionOperationRequest

router = APIRouter(prefix="/jobs/{job_id}/shots/{shot_id}/perception")

def _call(request, method, *args):
    try:
        return getattr(request.app.state.job_service.perception_operations, method)(*args)
    except PerceptionOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

@router.get("")
def status(request: Request, job_id: str, shot_id: str):
    return _call(request, "status", job_id, shot_id)

@router.get("/history")
def history(request: Request, job_id: str, shot_id: str):
    return _call(request, "history", job_id, shot_id)

@router.post("/operations", status_code=202)
def create(request: Request, job_id: str, shot_id: str, payload: PerceptionOperationRequest):
    return _call(request, "create", job_id, shot_id, payload)

@router.get("/operations/{operation_id}")
def operation(request: Request, job_id: str, shot_id: str, operation_id: str):
    return _call(request, "get", job_id, shot_id, operation_id)

@router.post("/operations/{operation_id}/cancel")
def cancel(request: Request, job_id: str, shot_id: str, operation_id: str):
    return _call(request, "cancel", job_id, shot_id, operation_id)
