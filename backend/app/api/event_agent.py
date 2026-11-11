"""Explicit experimental runs; reading status or a result never invokes a model."""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.event_agent.models import EventAgentConfig
from app.event_agent.service import EventAgentError


router = APIRouter(prefix="/jobs/{job_id}/event-agent")


class CreateEventRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: EventAgentConfig = Field(default_factory=EventAgentConfig)


def _call(request, method, *args):
    try:
        return getattr(request.app.state.event_agent_service, method)(*args)
    except EventAgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/runs", status_code=202)
def create_run(request: Request, job_id: str, payload: Optional[CreateEventRun] = None):
    return _call(request, "create", job_id, payload.config if payload else EventAgentConfig())


@router.get("/runs")
def list_runs(request: Request, job_id: str):
    return _call(request, "list", job_id)


@router.get("/runs/{run_id}")
def run_status(request: Request, job_id: str, run_id: str):
    return _call(request, "get", job_id, run_id)


@router.get("/runs/{run_id}/result")
def run_result(request: Request, job_id: str, run_id: str):
    return _call(request, "result", job_id, run_id)


@router.post("/runs/{run_id}/cancel")
def cancel_run(request: Request, job_id: str, run_id: str):
    return _call(request, "cancel", job_id, run_id)
