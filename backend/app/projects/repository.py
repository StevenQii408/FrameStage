"""Additive project schema; existing result payloads and input hashes stay intact."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from app.db import utc_now
from app.projects.access import active_project, has_table
from app.projects.models import ProjectError


def ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, deleted_at TEXT
        );
        CREATE TABLE IF NOT EXISTS project_canvases (
          project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          schema_version TEXT NOT NULL DEFAULT 'canvas.v2', state_json TEXT NOT NULL DEFAULT '{}',
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_asset_refs (
          project_id TEXT NOT NULL, asset_id TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(project_id,asset_id)
        );
        CREATE TABLE IF NOT EXISTS project_create_keys (
          idempotency_key TEXT PRIMARY KEY, project_id TEXT NOT NULL, requested_name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_style_refs (
          analysis_id TEXT PRIMARY KEY, project_id TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS projects_recent ON projects(deleted_at,updated_at);
        CREATE INDEX IF NOT EXISTS project_asset_membership ON project_asset_refs(asset_id,project_id);
    """)
    for table in ("jobs", "generation_jobs"):
        if has_table(conn, table):
            if "project_id" not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT")
            conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_project ON {table}(project_id)")


def canvas_view(row):
    return {"project_id": row["project_id"], "revision": row["revision"],
            "schema_version": row["schema_version"], "state": json.loads(row["state_json"]),
            "updated_at": row["updated_at"]}


def _history_version_shape(version):
    """New history snapshots must survive the frontend persistence reader."""
    if not isinstance(version, dict) or not isinstance(version.get("asset"), dict):
        raise ProjectError(422, "生成历史版本格式无效")
    asset = version["asset"]
    if (not all(isinstance(version.get(key), str) and version[key]
                for key in ("id", "jobId", "createdAt"))
            or len(version["id"]) > 240 or not isinstance(version.get("prompt"), str)
            or not isinstance(version.get("settings"), dict)
            or not all(isinstance(asset.get(key), str) and asset[key] for key in ("id", "mime_type", "url"))
            or not all(type(asset.get(key)) is int and asset[key] > 0 for key in ("width", "height"))):
        raise ProjectError(422, "生成历史版本必须包含完整的真实产物与请求快照")
    try:
        url = urlsplit(asset["url"])
        if url.scheme not in {"", "http", "https"} or url.path != "/api/generation-assets/" + asset["id"]:
            raise ValueError("wrong asset path")
    except ValueError as exc:
        raise ProjectError(422, "生成历史产物 URL 必须指向同一个本机资产") from exc


def _validate_history_snapshot(version, job, asset):
    expected_id = job["id"] + ":" + asset["id"]
    if (version["id"] != expected_id or version["prompt"] != job.get("request", {}).get("prompt")
            or version["settings"] != job.get("request", {}).get("settings")
            or version["createdAt"] != (job.get("updated_at") or job.get("created_at"))
            or any(version["asset"][key] != asset.get(key) for key in ("id", "mime_type", "width", "height"))):
        raise ProjectError(422, "生成历史快照与真实成功任务不一致，不能改写既有版本")


def canvas_references(state):
    """Only declared identity fields count; arbitrary user Prompt text is opaque."""
    def identifier(value):
        if not isinstance(value, str) or not value or len(value) > 160:
            raise ProjectError(422, "画布资源标识无效")
        return value

    jobs = {identifier(value) for value in state.get("jobKeys", {})} if isinstance(state.get("jobKeys"), dict) else set()
    generations, assets = set(), set()
    for item in state.get("generators", []) if isinstance(state.get("generators"), list) else []:
        if not isinstance(item, dict):
            raise ProjectError(422, "生成节点状态格式无效")
        task = item.get("generationTask") or {}
        if isinstance(task, dict) and task.get("jobId"):
            generations.add(identifier(task["jobId"]))
        versions = item.get("versions") or []
        if not isinstance(versions, list):
            raise ProjectError(422, "生成历史格式无效")
        for version in versions:
            _history_version_shape(version)
            generations.add(identifier(version.get("jobId")))
            assets.add(identifier(version["asset"].get("id")))
        for key in ("backendReferenceAssetId", "referenceAssetId", "reference_asset_id"):
            if item.get(key):
                assets.add(identifier(item[key]))
        references = item.get("referenceImages", item.get("references", [])) or []
        if not isinstance(references, list):
            raise ProjectError(422, "生成节点图片引用格式无效")
        for ref in references:
            if isinstance(ref, dict):
                identity = ref.get("asset_id") or ref.get("assetId")
                if isinstance(identity, str) and identity.startswith("genasset-"):
                    assets.add(identity)
    for identity in state.get("asset_ids", []) if isinstance(state.get("asset_ids"), list) else []:
        assets.add(identifier(identity))
    return jobs, generations, assets


def validate_canvas(conn, project_id, state):
    try:
        encoded = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ProjectError(422, "画布状态必须为有效 JSON") from exc
    if len(encoded.encode()) > 4 * 1024 * 1024:
        raise ProjectError(413, "画布状态超过 4MB，请减少内嵌文本；图片应使用资产引用")
    jobs, generations, assets = canvas_references(state)
    for table, ids in (("jobs", jobs), ("generation_jobs", generations)):
        for identity in ids:
            if not has_table(conn, table):
                raise ProjectError(422, "画布引用了不存在的任务")
            row = conn.execute(f"SELECT project_id FROM {table} WHERE id=?", (identity,)).fetchone()
            if not row or row["project_id"] != project_id:
                raise ProjectError(422, "画布不能引用其他项目或尚未导入的任务")
    for identity in assets:
        if not conn.execute("SELECT 1 FROM project_asset_refs WHERE project_id=? AND asset_id=?", (project_id, identity)).fetchone():
            raise ProjectError(422, "画布不能引用其他项目的图片资产")
    # History is a reference to delivered results, never a client-created success.
    for item in state.get("generators", []) if isinstance(state.get("generators"), list) else []:
        for version in item.get("versions") or []:
            row = conn.execute("SELECT status,payload_json FROM generation_jobs WHERE id=?", (version["jobId"],)).fetchone()
            job = json.loads(row["payload_json"])
            asset = next((asset for asset in job.get("assets", []) if asset.get("id") == version["asset"]["id"]), None)
            if (row["status"] != "succeeded" or job.get("node_id") != item.get("id")
                    or job.get("kind") != item.get("kind")
                    or asset is None):
                raise ProjectError(422, "生成历史必须来自此节点已成功交付的真实产物")
            _validate_history_snapshot(version, job, asset)
    return encoded


def save_canvas(conn, project_id, expected_revision, state):
    active_project(conn, project_id)
    row = conn.execute("SELECT * FROM project_canvases WHERE project_id=?", (project_id,)).fetchone()
    if row["revision"] != expected_revision:
        raise ProjectError(409, "项目已在另一窗口更新，请重新读取后保存")
    encoded = validate_canvas(conn, project_id, state)
    now = utc_now()
    conn.execute("UPDATE project_canvases SET revision=revision+1,state_json=?,updated_at=? WHERE project_id=?",
                 (encoded, now, project_id))
    conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))
    return canvas_view(conn.execute("SELECT * FROM project_canvases WHERE project_id=?", (project_id,)).fetchone())
