"""Ownership checks shared by the canvas, analysis and generation layers."""
from __future__ import annotations

import json

from app.projects.models import ProjectError


def has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def active_project(conn, project_id):
    if project_id is None:
        return None
    if not has_table(conn, "projects"):
        raise ProjectError(404, "项目不存在")
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise ProjectError(404, "项目不存在")
    if project["deleted_at"]:
        raise ProjectError(410, "项目已移入回收站，请先恢复")
    return project


def owned_resource(conn, table, resource_id, project_id=None):
    if table not in {"jobs", "generation_jobs"}:
        raise ValueError("不支持此项目资源类型")
    if not has_table(conn, table):
        raise ProjectError(404, "任务不存在")
    row = conn.execute(f"SELECT id,project_id FROM {table} WHERE id=?", (resource_id,)).fetchone()
    if not row or (project_id is not None and row["project_id"] != project_id):
        raise ProjectError(404, "任务不属于当前项目")
    active_project(conn, row["project_id"])
    if project_id is not None:
        active_project(conn, project_id)
    return row


def owned_asset(conn, project_id, asset_id):
    if project_id is None:
        return
    active_project(conn, project_id)
    if not conn.execute("SELECT 1 FROM project_asset_refs WHERE project_id=? AND asset_id=?", (project_id, asset_id)).fetchone():
        raise ProjectError(422, "参考图片不属于当前项目，请重新导入")


def style_owner(conn, analysis_id):
    member = conn.execute("SELECT project_id FROM project_style_refs WHERE analysis_id=?", (analysis_id,)).fetchone()
    if member:
        return member["project_id"]
    # Legacy video analyses retain original task/frame evidence. Resolve their
    # ownership without rewriting cached analyses during project migration.
    row = conn.execute("SELECT source_json FROM style_analyses WHERE id=?", (analysis_id,)).fetchone()
    if not row:
        raise ProjectError(404, "画风分析不存在")
    source = json.loads(row["source_json"])
    origins = {asset.get("origin_job_id") for asset in source.get("assets", []) if asset.get("origin_job_id")}
    owners = {job["project_id"] for identity in origins for job in conn.execute("SELECT project_id FROM jobs WHERE id=?", (identity,))}
    return next(iter(owners)) if len(owners) == 1 else None


def owned_style(conn, analysis_id, project_id=None):
    owner = style_owner(conn, analysis_id)
    if project_id is not None and owner != project_id:
        raise ProjectError(404, "画风分析不属于当前项目")
    active_project(conn, owner)
    return owner
