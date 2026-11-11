"""Project lifecycle, explicit legacy import and optimistic canvas saves."""
from __future__ import annotations

import json
import uuid

from app.db import utc_now
from app.projects.access import active_project, has_table
from app.projects.models import ProjectError
from app.projects.repository import canvas_references, canvas_view, ensure_schema, save_canvas


class ProjectService:
    def __init__(self, db, analysis=None, generation=None):
        self.db, self.analysis, self.generation = db, analysis, generation
        with db.connect() as conn:
            ensure_schema(conn)

    def list(self, deleted=False, limit=100, offset=0, q=""):
        with self.db.connect() as conn:
            clause = "IS NOT NULL" if deleted else "IS NULL"
            escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = conn.execute(f"SELECT p.*,c.revision FROM projects p JOIN project_canvases c ON c.project_id=p.id WHERE p.deleted_at {clause} AND p.name LIKE ? ESCAPE '\\' ORDER BY p.updated_at DESC,p.id LIMIT ? OFFSET ?", ("%" + escaped + "%", limit, offset)).fetchall()
            summaries = []
            for row in rows:
                item = dict(row)
                item.update(analysis_count=0, generation_count=0, active_count=0)
                for table, field in (("jobs", "analysis_count"), ("generation_jobs", "generation_count")):
                    if has_table(conn, table):
                        counts = conn.execute(f"SELECT status,COUNT(*) AS count FROM {table} WHERE project_id=? GROUP BY status", (row["id"],)).fetchall()
                        item[field] = sum(count["count"] for count in counts)
                        item["active_count"] += sum(count["count"] for count in counts if count["status"] in {"queued", "processing", "running", "submitting", "finalizing"})
                summaries.append(item)
            return summaries

    def create(self, name, idempotency_key=None):
        now, identity = utc_now(), "project-" + uuid.uuid4().hex
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            saved = conn.execute("SELECT * FROM project_create_keys WHERE idempotency_key=?", (idempotency_key,)).fetchone() if idempotency_key else None
            if saved:
                if saved["requested_name"] != name:
                    raise ProjectError(409, "此创建请求已用于其他项目名称")
                identity = saved["project_id"]
                active_project(conn, identity)
            else:
                conn.execute("INSERT INTO projects VALUES(?,?,?,?,NULL)", (identity, name, now, now))
                conn.execute("INSERT INTO project_canvases(project_id,updated_at) VALUES(?,?)", (identity, now))
                if idempotency_key:
                    conn.execute("INSERT INTO project_create_keys VALUES(?,?,?)", (idempotency_key, identity, name))
        return self.get(identity)

    def get(self, project_id, *, include_deleted=False):
        with self.db.connect() as conn:
            row = conn.execute("SELECT p.*,c.revision FROM projects p JOIN project_canvases c ON c.project_id=p.id WHERE p.id=?", (project_id,)).fetchone()
            if not row:
                raise ProjectError(404, "项目不存在")
            if not include_deleted:
                active_project(conn, project_id)
            detail = dict(row)
            for table, field in (("jobs", "job_ids"), ("generation_jobs", "generation_ids")):
                detail[field] = [item["id"] for item in conn.execute(
                    f"SELECT id FROM {table} WHERE project_id=? ORDER BY rowid", (project_id,)
                )] if has_table(conn, table) else []
            return detail

    def rename(self, project_id, name):
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_project(conn, project_id)
            conn.execute("UPDATE projects SET name=?,updated_at=? WHERE id=?", (name, utc_now(), project_id))
        return self.get(project_id)

    def canvas(self, project_id):
        with self.db.connect() as conn:
            active_project(conn, project_id)
            return canvas_view(conn.execute("SELECT * FROM project_canvases WHERE project_id=?", (project_id,)).fetchone())

    def save(self, project_id, expected_revision, state):
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return save_canvas(conn, project_id, expected_revision, state)

    @staticmethod
    def bind_in_transaction(conn, project_id, asset_ids, *, uploaded=False):
        active_project(conn, project_id)
        for asset_id in set(asset_ids):
            asset = conn.execute("SELECT * FROM generation_assets WHERE id=?", (asset_id,)).fetchone() if has_table(conn, "generation_assets") else None
            if not asset:
                raise ProjectError(404, "图片资产不存在")
            members = {row["project_id"] for row in conn.execute("SELECT project_id FROM project_asset_refs WHERE asset_id=?", (asset_id,))}
            if not uploaded and members - {project_id} and project_id not in members:
                raise ProjectError(409, "资产已属于其他项目，请重新上传或显式复制")
            if asset["role"] == "output":
                owner = conn.execute("SELECT project_id,status FROM generation_jobs WHERE id=?", (asset["job_id"],)).fetchone()
                if not owner or owner["status"] != "succeeded" or owner["project_id"] != project_id:
                    raise ProjectError(422, "生成产物尚未交付或不属于当前项目")
            conn.execute("INSERT OR IGNORE INTO project_asset_refs VALUES(?,?,?)", (project_id, asset_id, utc_now()))

    def bind_assets(self, project_id, asset_ids, *, uploaded=False):
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.bind_in_transaction(conn, project_id, asset_ids, uploaded=uploaded)
        return {"project_id": project_id, "asset_ids": list(dict.fromkeys(asset_ids))}

    def import_legacy(self, project_id, payload):
        state = payload.state
        state_jobs, state_generations, state_assets = canvas_references(state or {})
        jobs = set(payload.job_ids) | state_jobs
        generations = set(payload.generation_ids) | state_generations
        assets = set(payload.asset_ids) | state_assets
        proven_assets = set()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_project(conn, project_id)
            for table, identities in (("jobs", jobs), ("generation_jobs", generations)):
                for identity in identities:
                    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone() if has_table(conn, table) else None
                    if not row:
                        raise ProjectError(404, "待迁移任务不存在，旧画布未被修改")
                    if row["project_id"] not in (None, project_id):
                        raise ProjectError(409, "任务已属于其他项目，不能重复迁移")
                    conn.execute(f"UPDATE {table} SET project_id=? WHERE id=?", (project_id, identity))
                    if table == "generation_jobs":
                        saved = json.loads(row["payload_json"])
                        if saved.get("request", {}).get("reference_asset_id"):
                            proven_assets.add(saved["request"]["reference_asset_id"])
                        proven_assets.update(ref["asset_id"] for ref in saved.get("request", {}).get("reference_images", []))
                        proven_assets.update(ref["id"] for ref in saved.get("assets", []))
            self.bind_in_transaction(conn, project_id, proven_assets, uploaded=True)
            self.bind_in_transaction(conn, project_id, assets - proven_assets)
            assets.update(proven_assets)
            if state is not None:
                current = canvas_view(conn.execute("SELECT * FROM project_canvases WHERE project_id=?", (project_id,)).fetchone())
                # A dropped HTTP response may replay the exact legacy import.
                # It must not manufacture a new revision or loosen normal PUT CAS.
                if current["state"] == state:
                    canvas = current
                else:
                    canvas = save_canvas(conn, project_id, payload.expected_revision if payload.expected_revision is not None else 0, state)
            else:
                canvas = canvas_view(conn.execute("SELECT * FROM project_canvases WHERE project_id=?", (project_id,)).fetchone())
        return {**canvas, "imported": {"job_ids": sorted(jobs), "generation_ids": sorted(generations), "asset_ids": sorted(assets)}}

    def delete(self, project_id):
        analysis_ids, generation_ids = [], []
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise ProjectError(404, "项目不存在")
            if not row["deleted_at"]:
                now = utc_now()
                conn.execute("UPDATE projects SET deleted_at=?,updated_at=? WHERE id=?", (now, now, project_id))
                if has_table(conn, "jobs"):
                    analysis_ids = [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE project_id=? AND status IN ('queued','processing','running','paused_memory_pressure','interrupted')", (project_id,))]
                    conn.execute("UPDATE jobs SET status='cancelled',stage='cancelled',error='所属项目已删除，任务已取消',updated_at=? WHERE project_id=? AND status IN ('queued','processing','running','paused_memory_pressure','interrupted')", (now, project_id))
                if has_table(conn, "generation_jobs"):
                    generation_ids = [r["id"] for r in conn.execute("SELECT id FROM generation_jobs WHERE project_id=? AND status IN ('queued','submitting','running','finalizing','blocked')", (project_id,))]
                if has_table(conn, "event_agent_runs"):
                    conn.execute("UPDATE event_agent_runs SET cancel_requested=1,status='cancelled',stage='cancelled',updated_at=?,completed_at=? WHERE job_id IN (SELECT id FROM jobs WHERE project_id=?) AND status IN ('queued','running')", (now, now, project_id))
                if has_table(conn, "perception_operations"):
                    conn.execute("UPDATE perception_operations SET cancel_requested=1,status='cancelled',updated_at=? WHERE job_id IN (SELECT id FROM jobs WHERE project_id=?) AND status IN ('queued','running')", (now, project_id))
                if has_table(conn, "style_analyses"):
                    from app.projects.access import style_owner
                    analyses = list(conn.execute("SELECT id FROM style_analyses WHERE status IN ('queued','running')"))
                    for analysis in analyses:
                        if style_owner(conn, analysis["id"]) == project_id:
                            conn.execute("UPDATE style_analyses SET status='failed',stage='failed',error='所属项目已删除，画风分析已停止',updated_at=? WHERE id=?", (now, analysis["id"]))
        for identity in analysis_ids:
            if self.analysis:
                self.analysis._emit(identity, "job.cancelled", {"message": "所属项目已删除"})
        for identity in generation_ids:
            if self.generation:
                try:
                    self.generation.cancel(identity)
                except ValueError:
                    pass  # A competing completion remains authoritative; never resubmit.
        return {**self.get(project_id, include_deleted=True), "cancelled_analysis_count": len(analysis_ids), "cancelled_generation_count": len(generation_ids)}

    def restore(self, project_id):
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise ProjectError(404, "项目不存在")
            conn.execute("UPDATE projects SET deleted_at=NULL,updated_at=? WHERE id=?", (utc_now(), project_id))
        return self.get(project_id)
