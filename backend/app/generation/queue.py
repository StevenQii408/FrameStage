"""SQLite checkpoints for short generation steps, not sleeping worker threads."""
from __future__ import annotations

import json
import os
import time

from app.generation.repository import encoded


class GenerationQueue:
    def __init__(self, db):
        self.db = db
        with db.connect() as conn:
            # The queue stays on the local filesystem. Network work never runs
            # inside a database transaction.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS generation_queue (
                  job_id TEXT PRIMARY KEY, phase TEXT NOT NULL,
                  next_run_at REAL NOT NULL, deadline REAL,
                  polls INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                  lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
                  output_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS generation_queue_due ON generation_queue(phase,next_run_at);
                CREATE TABLE IF NOT EXISTS generation_submit_limits (
                  kind TEXT PRIMARY KEY, next_submit_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generation_remote_slots (
                  job_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                  provider_task_id TEXT, state TEXT NOT NULL DEFAULT 'reserved',
                  next_check_at REAL NOT NULL,
                  lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS generation_remote_slots_due
                  ON generation_remote_slots(next_check_at);
            """)

    def enqueue(self, job_id, phase="submit"):
        with self.db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO generation_queue(job_id,phase,next_run_at) VALUES(?,?,?)",
                         (job_id, phase, time.time()))

    def reconcile_phase(self, job_id, phase):
        self.db.execute("UPDATE generation_queue SET phase=? WHERE job_id=? AND lease_owner IS NULL", (phase, job_id))

    def recover_dead_leases(self):
        """A crashed local PID cannot own work; live processes retain their lease."""
        rows = self.db.fetchall("SELECT lease_owner FROM generation_queue WHERE lease_owner IS NOT NULL "
            "UNION SELECT lease_owner FROM generation_remote_slots WHERE lease_owner IS NOT NULL")
        for row in rows:
            owner = row["lease_owner"]
            try:
                pid = int(owner.split(":", 1)[0])
                os.kill(pid, 0)
            except ProcessLookupError:
                self.release_owner(owner)
            except (ValueError, PermissionError):
                continue
        # A canceled reservation whose submission never started is safe to
        # reclaim. An ambiguous POST always changed billing to unknown first.
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT s.job_id,j.payload_json FROM generation_remote_slots s "
                "JOIN generation_jobs j ON j.id=s.job_id WHERE s.provider_task_id IS NULL "
                "AND j.status NOT IN ('queued','submitting','running','finalizing')").fetchall()
            for row in rows:
                if json.loads(row["payload_json"]).get("billing", {}).get("status") == "not_submitted":
                    conn.execute("DELETE FROM generation_remote_slots WHERE job_id=?", (row["job_id"],))

    def release_owner(self, owner):
        with self.db.connect() as conn:
            conn.execute("UPDATE generation_queue SET lease_owner=NULL,lease_until=0 WHERE lease_owner=?", (owner,))
            conn.execute("UPDATE generation_remote_slots SET lease_owner=NULL,lease_until=0 WHERE lease_owner=?", (owner,))

    def release_owner_for_job(self, job_id, owner):
        self.db.execute("UPDATE generation_queue SET lease_owner=NULL,lease_until=0 WHERE job_id=? AND lease_owner=?", (job_id, owner))

    def claim(self, owner, config, *, finalizing=False, excluded_kinds=(), now=None):
        now = time.time() if now is None else now
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT q.*,j.payload_json,j.status FROM generation_queue q JOIN generation_jobs j ON j.id=q.job_id "
                "WHERE q.next_run_at<=? AND (q.lease_owner IS NULL OR q.lease_until<?) "
                "AND j.status IN ('queued','submitting','running','finalizing') "
                "AND " + ("q.phase='finalize' " if finalizing else "q.phase IN ('submit','poll') ") +
                "ORDER BY q.next_run_at,q.job_id", (now, now)).fetchall()
            for row in rows:
                job = json.loads(row["payload_json"])
                kind = job["kind"]
                if kind in excluded_kinds:
                    continue
                if row["phase"] == "submit":
                    # Submitting without a remote handle is never re-submitted.
                    if job["status"] != "queued":
                        continue
                    own = conn.execute("SELECT * FROM generation_remote_slots WHERE job_id=?", (row["job_id"],)).fetchone()
                    if own and (own["state"] != "reserved" or own["provider_task_id"]):
                        continue  # A possibly accepted POST must never be repeated.
                    counts = conn.execute("SELECT kind FROM generation_remote_slots WHERE job_id<>?", (row["job_id"],)).fetchall()
                    if len(counts) >= config.global_inflight:
                        continue
                    if sum(item["kind"] == kind for item in counts) >= getattr(config, kind + "_inflight"):
                        continue
                    limit = conn.execute("SELECT next_submit_at FROM generation_submit_limits WHERE kind=?", (kind,)).fetchone()
                    if limit and limit[0] > now:
                        continue
                    # Cloud capacity outlives local UI status and worker leases.
                    # Cancelling locally does not prove remote generation stopped.
                    conn.execute("INSERT OR IGNORE INTO generation_remote_slots(job_id,kind,state,next_check_at) VALUES(?,?,'reserved',?)",
                                 (row["job_id"], kind, now))
                    rpm = getattr(config, kind + "_submit_rpm")
                    conn.execute("INSERT INTO generation_submit_limits VALUES(?,?) ON CONFLICT(kind) DO UPDATE SET next_submit_at=excluded.next_submit_at",
                                 (kind, now + 60.0 / rpm))
                deadline = row["deadline"] or (now + config.job_timeout_seconds)
                conn.execute("UPDATE generation_queue SET lease_owner=?,lease_until=?,deadline=? WHERE job_id=?",
                             (owner, now + config.job_timeout_seconds + 300, deadline, row["job_id"]))
                return {**dict(row), "deadline": deadline, "job": job}
        return None

    def checkpoint(self, job_id, owner, *, phase, delay=0, output=None, failure=False):
        with self.db.connect() as conn:
            fields = "phase=?,next_run_at=?,lease_owner=NULL,lease_until=0"
            values = [phase, time.time() + max(0, delay)]
            if phase == "poll" and not failure:
                fields += ",polls=polls+1,failures=0"
            elif failure:
                fields += ",failures=failures+1"
            if output is not None:
                fields += ",output_json=?"
                values.append(encoded(output))
            conn.execute(f"UPDATE generation_queue SET {fields} WHERE job_id=? AND lease_owner=?",
                         (*values, job_id, owner))

    def finish(self, job_id, owner=None):
        query = "DELETE FROM generation_queue WHERE job_id=?"
        args = [job_id]
        if owner is not None:
            query += " AND lease_owner=?"
            args.append(owner)
        self.db.execute(query, args)

    def peek(self, job_id):
        row = self.db.fetchone("SELECT * FROM generation_queue WHERE job_id=?", (job_id,))
        return dict(row) if row else None

    def remote_slot(self, job_id):
        row = self.db.fetchone("SELECT * FROM generation_remote_slots WHERE job_id=?", (job_id,))
        return dict(row) if row else None

    def slot_counts(self):
        rows = self.db.fetchall("SELECT kind,state,count(*) AS n FROM generation_remote_slots GROUP BY kind,state")
        counts = {"total": 0, "image": 0, "video": 0, "unknown": 0, "reserved": 0, "active": 0}
        for row in rows:
            counts["total"] += row["n"]
            counts[row["kind"]] = counts.get(row["kind"], 0) + row["n"]
            counts[row["state"]] = counts.get(row["state"], 0) + row["n"]
        return counts

    def reserve_existing(self, job):
        """Recover accounting before any queued task may claim new capacity."""
        handle = job.get("provider_task_id")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO generation_remote_slots(job_id,kind,provider_task_id,state,next_check_at) VALUES(?,?,?,?,?)",
                         (job["id"], job["kind"], handle, "active" if handle else "unknown", time.time()))
            if handle:
                conn.execute("UPDATE generation_remote_slots SET provider_task_id=?,state='active' WHERE job_id=? AND provider_task_id IS NULL",
                             (handle, job["id"]))

    def attach_remote(self, job_id, remote_id):
        if not isinstance(remote_id, str) or not remote_id:
            raise ValueError("远端任务 ID 不能为空")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload_json FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return
            own = conn.execute("SELECT provider_task_id FROM generation_remote_slots WHERE job_id=?", (job_id,)).fetchone()
            if own and own[0] and own[0] != remote_id:
                raise ValueError("远端任务 ID 与已预留名额不一致")
            kind = json.loads(row[0])["kind"]
            conn.execute("INSERT INTO generation_remote_slots(job_id,kind,provider_task_id,state,next_check_at) VALUES(?,?,?,'active',?) "
                         "ON CONFLICT(job_id) DO UPDATE SET provider_task_id=excluded.provider_task_id,state='active',next_check_at=excluded.next_check_at",
                         (job_id, kind, remote_id, time.time()))

    def release_slot(self, job_id):
        """Only call after proven non-submission or confirmed remote termination."""
        self.db.execute("DELETE FROM generation_remote_slots WHERE job_id=?", (job_id,))

    def mark_remote_unknown(self, job_id):
        self.db.execute("UPDATE generation_remote_slots SET state='unknown' WHERE job_id=?", (job_id,))

    def claim_reconciliation(self, owner, config, *, now=None):
        now = time.time() if now is None else now
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT s.*,j.payload_json,j.project_id FROM generation_remote_slots s JOIN generation_jobs j ON j.id=s.job_id "
                "WHERE j.status NOT IN ('queued','submitting','running','finalizing') AND s.provider_task_id IS NOT NULL "
                "AND s.next_check_at<=? AND (s.lease_owner IS NULL OR s.lease_until<?) ORDER BY s.next_check_at,s.job_id LIMIT 1",
                (now, now)).fetchone()
            if not row:
                return None
            until = now + config.request_timeout_seconds + 30
            conn.execute("UPDATE generation_remote_slots SET lease_owner=?,lease_until=? WHERE job_id=?", (owner, until, row["job_id"]))
            job = json.loads(row["payload_json"])
            if row["project_id"] is not None:
                job["project_id"] = row["project_id"]
            return {**dict(row), "lease_owner": owner, "lease_until": until, "job": job}

    def defer_reconciliation(self, job_id, owner, delay):
        self.db.execute("UPDATE generation_remote_slots SET next_check_at=?,lease_owner=NULL,lease_until=0 WHERE job_id=? AND lease_owner=?",
                        (time.time() + max(0, delay), job_id, owner))

    def finish_reconciliation(self, job_id, owner):
        self.db.execute("DELETE FROM generation_remote_slots WHERE job_id=? AND lease_owner=?", (job_id, owner))

    def next_due(self):
        row = self.db.fetchone("SELECT min(due) FROM (SELECT q.next_run_at AS due FROM generation_queue q JOIN generation_jobs j ON j.id=q.job_id "
            "WHERE q.lease_owner IS NULL AND j.status IN ('queued','running','finalizing') "
            "UNION ALL SELECT s.next_check_at AS due FROM generation_remote_slots s JOIN generation_jobs j ON j.id=s.job_id "
            "WHERE s.lease_owner IS NULL AND s.provider_task_id IS NOT NULL AND j.status NOT IN ('queued','submitting','running','finalizing'))")
        return row[0] if row and row[0] is not None else None
