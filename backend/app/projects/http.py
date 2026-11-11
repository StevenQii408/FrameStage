"""Optional project context for legacy-compatible task and asset routes."""
from __future__ import annotations

from starlette.responses import JSONResponse

from app.projects.access import active_project, has_table, owned_asset, owned_resource, owned_style
from app.projects.models import ProjectError


def request_project(request, supplied=None):
    header = request.headers.get("x-project-id")
    query = request.query_params.get("project_id")
    if header and query and header != query:
        raise ProjectError(422, "请求头与链接中的项目不一致")
    context = header or query
    if context and supplied and context != supplied:
        raise ProjectError(422, "请求项目与当前工作台不一致")
    return supplied or context


async def project_scope(request, call_next):
    database = getattr(request.app.state, "db", None)
    if database is None:
        return await call_next(request)
    parts = request.url.path.strip("/").split("/")
    try:
        context = request_project(request)
        with database.connect() as conn:
            if context and parts[:2] != ["api", "projects"]:
                active_project(conn, context)
            if len(parts) >= 3 and parts[0] == "api" and parts[1] in {"jobs", "generations"}:
                identity = parts[2]
                if identity not in {"preflight", "capabilities", "style", "events"}:
                    owned_resource(conn, "jobs" if parts[1] == "jobs" else "generation_jobs", identity, context)
            if len(parts) >= 3 and parts[:2] == ["api", "generation-assets"] and has_table(conn, "project_asset_refs"):
                if context:
                    owned_asset(conn, context, parts[2])
                else:
                    memberships = conn.execute("SELECT p.deleted_at FROM project_asset_refs r JOIN projects p ON p.id=r.project_id WHERE r.asset_id=?", (parts[2],)).fetchall()
                    if memberships and all(row["deleted_at"] for row in memberships):
                        raise ProjectError(410, "该图片所属项目已移入回收站")
            if len(parts) >= 4 and parts[:3] == ["api", "style", "analyses"]:
                owned_style(conn, parts[3], context)
    except ProjectError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
    return await call_next(request)
