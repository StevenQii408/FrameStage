from fastapi import APIRouter, Request
from app.event_agent.rendering import VERSION as EVENT_RENDER_VERSION
from app.script_director.engine import VERSION as SCRIPT_DIRECTOR_VERSION

router = APIRouter()


@router.get("/health")
def health(request: Request):
    service = request.app.state.job_service
    memory = service.memory.snapshot()
    memory["last_inference_before"] = service.memory.last_inference_before
    memory["last_inference_after"] = service.memory.last_inference_after
    settings = request.app.state.settings
    return {"ok": True, "provider": service.provider.health().model_dump(), "memory": memory,
            "running_job": service.running_job,
            "configuration": {"style_strategy": settings.style_strategy,
                              "event_grouping_policy": "event-agent-v2",
                              "event_render_version": EVENT_RENDER_VERSION,
                              "script_strategy": settings.script_strategy,
                              "script_director_version": SCRIPT_DIRECTOR_VERSION,
                              "cloud_connect_timeout_seconds": settings.cloud_connect_timeout_seconds,
                              "cloud_read_timeout_seconds": settings.cloud_read_timeout_seconds,
                              "cloud_trust_env": settings.cloud_trust_env,
                              "api_key_configured": bool(settings.cloud_api_key),
                              "api_host_configured": bool(settings.cloud_base_url),
                              "api_key_source": settings.cloud_key_source,
                              "api_host_source": settings.cloud_host_source,
                              "notes": list(settings.config_notes)}}
