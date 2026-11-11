"""Transactional generation state and replayable events in the existing SQLite."""
from __future__ import annotations

import json
import math
import uuid

from app.db import utc_now
from app.generation.models import ACTIVE, TRANSITIONS, GenerationError
from app.projects.access import active_project
from app.projects.models import ProjectError


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def public_job(job):
    # SDK handles are internal opaque transport data, not user-visible output.
    return {key: value for key, value in job.items() if key != "provider_task_id"}


class GenerationRepository:
    def __init__(self, db):
        self.db = db
        with db.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS generation_jobs (
                  id TEXT PRIMARY KEY, node_id TEXT NOT NULL, status TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL UNIQUE, input_hash TEXT NOT NULL,
                  retry_of TEXT, payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS generation_jobs_node ON generation_jobs(node_id,status);
                CREATE TABLE IF NOT EXISTS generation_assets (
                  id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, path TEXT NOT NULL,
                  metadata_json TEXT NOT NULL, role TEXT NOT NULL, job_id TEXT
                );
                CREATE TABLE IF NOT EXISTS generation_events (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                  job_id TEXT NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS generation_events_job ON generation_events(job_id,sequence);
            """)
            from app.projects.repository import ensure_schema
            ensure_schema(conn)

    def get(self, job_id):
        row = self.db.fetchone("SELECT payload_json,project_id FROM generation_jobs WHERE id=?", (job_id,))
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        if row["project_id"] is not None:
            payload["project_id"] = row["project_id"]
        return payload

    def find_by_key(self, key):
        row = self.db.fetchone("SELECT id FROM generation_jobs WHERE idempotency_key=?", (key,))
        return self.get(row["id"]) if row else None

    @staticmethod
    def _event(conn, job, kind):
        conn.execute("INSERT INTO generation_events(event_id,job_id,type,payload_json,created_at) VALUES(?,?,?,?,?)",
                     ("genevt-" + uuid.uuid4().hex, job["id"], kind, encoded({"job": public_job(job)}), utc_now()))

    def create(self, job, key, *, max_pending=None):
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                active_project(conn, job.get("project_id"))
            except ProjectError as exc:
                raise GenerationError(exc.status_code, str(exc), "invalid_project") from exc
            saved = conn.execute("SELECT payload_json,retry_of,input_hash,project_id FROM generation_jobs WHERE idempotency_key=?", (key,)).fetchone()
            if saved:
                if saved["input_hash"] != job["input_hash"] or saved["retry_of"] != job.get("retry_of") or saved["project_id"] != job.get("project_id"):
                    raise GenerationError(409, "此幂等键已用于另一份输入，请显式创建新的请求", "idempotency_conflict")
                return json.loads(saved["payload_json"]), False
            active = conn.execute("SELECT id FROM generation_jobs WHERE node_id=? AND project_id IS ? AND status IN ('queued','submitting','running','finalizing')", (job["node_id"], job.get("project_id"))).fetchone()
            if active:
                raise GenerationError(409, "此节点已有进行中的生成任务，请先取消或等待完成", "node_busy")
            if job["status"] == "queued" and max_pending is not None:
                pending = conn.execute("SELECT count(*) FROM generation_jobs WHERE status='queued'").fetchone()[0]
                if pending >= max_pending:
                    raise GenerationError(429, "生成等待队列已满，请等待已有任务完成后再提交", "generation_queue_full")
            conn.execute("INSERT INTO generation_jobs(id,node_id,status,idempotency_key,input_hash,retry_of,payload_json,project_id) VALUES(?,?,?,?,?,?,?,?)", (
                job["id"], job["node_id"], job["status"], key, job["input_hash"], job.get("retry_of"), encoded(job), job.get("project_id")))
            self._event(conn, job, "generation.created")
            self._event(conn, job, "generation." + ("blocked" if job["status"] == "blocked" else "queued"))
            return job, True

    def advance(self, job_id, state, *, progress=None, error=None, updates=None, event=None):
        if updates and set(updates) - {"provider_task_id", "assets", "billing"}:
            raise ValueError("生成输入与来源快照不可变")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload_json,project_id FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            job = json.loads(row["payload_json"])
            if row["project_id"] is not None:
                job["project_id"] = row["project_id"]
                if state not in {"cancelled", "interrupted"}:
                    try:
                        active_project(conn, row["project_id"])
                    except ProjectError:
                        return None
            if state not in TRANSITIONS.get(job["status"], set()):
                return None  # Terminal states absorb late provider/poll completion.
            if progress is not None:
                if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not math.isfinite(progress) or not 0 <= progress <= 100:
                    raise GenerationError(422, "Provider 进度必须为 0–100 的有限数值", "invalid_provider_progress")
                if job["progress"] is not None and progress < job["progress"]:
                    return None  # Out-of-order callbacks cannot move the UI backwards.
                if state == job["status"] and progress == job["progress"] and not updates and error is None:
                    return None
            job.update(status=state, stage=state, revision=job["revision"] + 1, updated_at=utc_now())
            if progress is not None:
                job["progress"] = progress
            if error is not None:
                job["error"] = error
            if updates:
                job.update(updates)
            conn.execute("UPDATE generation_jobs SET status=?,payload_json=? WHERE id=?", (state, encoded(job), job_id))
            self._event(conn, job, event or "generation." + state)
            return job

    def interrupt_pending(self):
        rows = self.db.fetchall("SELECT id,status FROM generation_jobs WHERE status IN ('queued','submitting','running','finalizing')")
        changed = []
        for row in rows:
            job = self.advance(row["id"], "interrupted", error={"code": "server_restarted",
                "message": "后端已重启，生成任务已中断；请检查原服务商状态后显式重试，未自动重新提交。", "retryable": True})
            if job:
                changed.append(job["id"])
        return changed

    def events_since(self, job_id, after=0):
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in self.db.fetchall(
            "SELECT sequence,event_id,job_id,type,payload_json,created_at FROM generation_events WHERE job_id=? AND sequence>? ORDER BY sequence", (job_id, after))]

    def project_events_since(self, project_id, after=0, limit=200):
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in self.db.fetchall(
            "SELECT e.* FROM generation_events e JOIN generation_jobs j ON j.id=e.job_id "
            "WHERE j.project_id IS ? AND e.sequence>? ORDER BY e.sequence LIMIT ?", (project_id, after, limit))]

    def pending(self):
        return [self.get(row["id"]) for row in self.db.fetchall(
            "SELECT id FROM generation_jobs WHERE status IN ('queued','submitting','running','finalizing')")]

    def retain_remote_handle(self, job_id, remote_id):
        """A cancellation racing an accepted POST must not lose its billing trace."""
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload_json FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
            if row:
                job = json.loads(row[0])
                if not job.get("provider_task_id"):
                    job["provider_task_id"] = remote_id
                    conn.execute("UPDATE generation_jobs SET payload_json=? WHERE id=?", (encoded(job), job_id))

    def record_remote_resolution(self, job_id):
        with self.db.connect() as conn:
            row = conn.execute("SELECT payload_json FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
            if row:
                self._event(conn, json.loads(row[0]), "generation.remote_stop_confirmed")

    def asset(self, asset_id):
        row = self.db.fetchone("SELECT * FROM generation_assets WHERE id=?", (asset_id,))
        return ({**json.loads(row["metadata_json"]), "path": row["path"], "role": row["role"], "job_id": row["job_id"]} if row else None)

    def save_asset(self, metadata, path, *, role, job_id=None):
        with self.db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO generation_assets VALUES(?,?,?,?,?,?)", (
                metadata["id"], metadata["sha256"], self.db.media.store(path), encoded(metadata), role, job_id))
            if job_id:
                job = conn.execute("SELECT project_id FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
                if job and job["project_id"]:
                    conn.execute("INSERT OR IGNORE INTO project_asset_refs VALUES(?,?,?)", (job["project_id"], metadata["id"], utc_now()))
        return metadata
