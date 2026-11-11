from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import artifacts, health, jobs, style_vector, style_direct, event_agent, perception, generations, projects
from app.config import load_cors_origins, load_settings
from app.db import Database
from app.events import EventBus
from app.services.job_service import JobService
from app.event_agent.service import EventAgentService
from app.generation.service import GenerationService
from app.generation.config import load_generation_settings
from app.projects.service import ProjectService
from app.projects.http import project_scope
from app.security import LocalSession, LocalSecurityMiddleware, router as session_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.local_session = LocalSession.create()
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.data_dir / "storyboard.sqlite3")
    db.init()
    app.state.settings = settings
    app.state.db = db
    app.state.events = EventBus()
    app.state.job_service = JobService(settings, db, app.state.events)
    app.state.event_agent_service = EventAgentService(app.state.job_service)
    app.state.generation_service = GenerationService(settings, db, app.state.events,
        generation_config=load_generation_settings())
    app.state.project_service = ProjectService(db, app.state.job_service, app.state.generation_service)
    logger = logging.getLogger("uvicorn.error")
    if not app.state.local_session.enabled:
        logger.warning("本机会话令牌验证已显式关闭，仅限隔离测试；Origin与Host检查仍生效")
    logger.info("Provider=%s | Model=%s | Key configured=%s | Host configured=%s",
                settings.provider, app.state.job_service.provider.health().model,
                bool(settings.cloud_api_key), bool(settings.cloud_base_url))
    logger.info("Style strategy=%s (qwen-direct-v3 does not use embedding)", settings.style_strategy)
    generation = app.state.generation_service.config
    logger.info("Generation enabled=%s | Image=%s | Video=%s | Inflight=%s | Frontend transport=project-sse",
                generation.enabled, generation.image_model, generation.video_model, generation.global_inflight)
    logger.info("New video event grouping=event-agent-v2 (inline single-worker, bounded evidence tools)")
    for note in settings.config_notes:
        logger.warning("%s", note)
    try:
        yield
    finally:
        app.state.generation_service.close()
        app.state.job_service.provider.unload()
        app.state.job_service.executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="FrameStage Backend", version="0.1.1", lifespan=lifespan)
app.middleware("http")(project_scope)
app.add_middleware(LocalSecurityMiddleware)
cors_origins = list(load_cors_origins())
app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(health.router, prefix="/api")
app.include_router(session_router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(style_vector.style_router, prefix="/api")
app.include_router(style_vector.bible_router, prefix="/api")
app.include_router(style_direct.router, prefix="/api")
app.include_router(artifacts.router, prefix="/api")
app.include_router(event_agent.router, prefix="/api")
app.include_router(perception.router, prefix="/api")
app.include_router(generations.router, prefix="/api")
app.include_router(projects.router, prefix="/api")


@app.get("/")
def root():
    return {"service": "ai-storyboard-backend", "docs": "/docs"}
