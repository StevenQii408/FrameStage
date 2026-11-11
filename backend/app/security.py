"""Local browser write protection, independent of project ownership and CORS.

This is a single-user session boundary, not multi-tenant authentication. Secrets
are process-local by default, never placed in a URL, cookie, log or health reply.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.config import load_cors_origins, load_local_security


SESSION_HEADER = "X-FrameForge-Session"
SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))


@dataclass(frozen=True)
class LocalSession:
    enabled: bool
    allowed_hosts: tuple[str, ...]
    origins: frozenset[str]
    token: str = field(repr=False)

    @classmethod
    def create(cls):
        config = load_local_security()
        origins = frozenset(load_cors_origins())
        if "*" in origins or "null" in origins:
            raise ValueError("本机工作台必须配置明确的浏览器 Origin，不能使用 * 或 null")
        return cls(config.enabled, config.allowed_hosts, origins, config.api_token or secrets.token_urlsafe(32))

    def token_matches(self, value):
        return bool(value) and secrets.compare_digest(value.encode("utf-8"), self.token.encode("utf-8"))


def _error(code, detail, status=403):
    return JSONResponse(status_code=status, content={"detail": detail, "error": {"code": code}},
                        headers={"Cache-Control": "no-store"})


def _trusted_host(headers, session):
    value = headers.get("host", "")
    try:
        parsed = urlsplit("//" + value)
        # Force validation of malformed/out-of-range ports, not just hostname.
        parsed.port
        return bool(value) and not parsed.username and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment and parsed.hostname in session.allowed_hosts
    except ValueError:
        return False


def _trusted_origin(origin, scope, headers, session):
    if origin in session.origins:
        return True
    # Supports a same-origin static/reverse-proxy frontend on a trusted Host.
    return origin == f"{scope.get('scheme', 'http')}://{headers.get('host', '')}"


class LocalSecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        session = getattr(scope["app"].state, "local_session", None)
        if session is None:
            # ASGI lifespan is required for API startup; never run an unguarded
            # partial startup if a custom carrier omitted it.
            return await _error("session_not_ready", "后端尚未完成启动", 503)(scope, receive, send)
        headers = Headers(scope=scope)
        if not _trusted_host(headers, session):
            return await _error("host_not_allowed", "请求主机不在本机工作台白名单中", 400)(scope, receive, send)
        method = scope["method"].upper()
        origin = headers.get("origin")
        if method not in SAFE_METHODS:
            if origin and not _trusted_origin(origin, scope, headers, session):
                return await _error("origin_not_allowed", "拒绝来自不受信任网页的写入请求")(scope, receive, send)
            if not origin and headers.get("sec-fetch-site") == "cross-site":
                return await _error("origin_not_allowed", "拒绝缺少可信来源的跨站写入请求")(scope, receive, send)
            if session.enabled:
                supplied = headers.get(SESSION_HEADER)
                if not supplied:
                    return await _error("session_token_missing", "需要本机会话令牌，请刷新工作台；CLI请携带会话请求头")(scope, receive, send)
                if not session.token_matches(supplied):
                    return await _error("session_token_invalid", "本机会话已更新，请重新连接工作台")(scope, receive, send)
        return await self.app(scope, receive, send)


router = APIRouter()


@router.get("/session")
def session_bootstrap(request: Request):
    session = request.app.state.local_session
    origin = request.headers.get("origin")
    # CORS alone does not protect a credential-bearing bootstrap response. Only
    # trusted browser fetches or a CLI already holding the configured token may
    # retrieve it. A plain navigated page / form / image cannot obtain a token.
    trusted_browser = bool(origin and _trusted_origin(origin, request.scope, request.headers, session))
    same_origin_fetch = not origin and request.headers.get("sec-fetch-site") == "same-origin" and request.headers.get("sec-fetch-mode") in ("cors", "same-origin")
    if not (trusted_browser or same_origin_fetch or session.token_matches(request.headers.get(SESSION_HEADER))):
        return _error("origin_not_allowed", "会话初始化仅供已允许的工作台网页或已认证的CLI")
    return JSONResponse({"token": session.token, "header": SESSION_HEADER, "enabled": session.enabled},
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache", "Vary": "Origin"})
