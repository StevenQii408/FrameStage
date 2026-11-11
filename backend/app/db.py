import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from app.media_paths import MediaPaths


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.media = MediaPaths(path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = self.media.row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
                  progress INTEGER NOT NULL DEFAULT 0, video_filename TEXT,
                  video_path TEXT, input_type TEXT NOT NULL DEFAULT 'video', script_text TEXT NOT NULL DEFAULT '',
                  provider TEXT NOT NULL DEFAULT 'legacy-unknown', model TEXT NOT NULL DEFAULT 'legacy-unknown',
                  segmentation_profile TEXT NOT NULL DEFAULT 'legacy-v1',
                  raw_shot_count INTEGER NOT NULL DEFAULT 0,
                  narrative_shot_count INTEGER NOT NULL DEFAULT 0,
                  script_plan_json TEXT,
                  script_raw_response_json TEXT NOT NULL DEFAULT '[]',
                  planning_warnings_json TEXT NOT NULL DEFAULT '[]',
                  model_usage_json TEXT NOT NULL DEFAULT '[]',
                  montage_json TEXT NOT NULL DEFAULT '{}',
                  style_observation_json TEXT, style_profile_json TEXT,
                  visual_bible_json TEXT, style_retrieval_json TEXT,
                  config_json TEXT NOT NULL, error TEXT,
                  result_version TEXT NOT NULL DEFAULT 'storyboard.v1',
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shots (
                  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, number INTEGER NOT NULL,
                  start_time REAL NOT NULL, end_time REAL NOT NULL, duration REAL NOT NULL,
                  time TEXT NOT NULL, source_text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued',
                  observation_json TEXT, perception_json TEXT, style_observation_v2_json TEXT, source_raw_shot_ids_json TEXT NOT NULL DEFAULT '[]',
                  scene_group_id TEXT, cut_reason TEXT NOT NULL DEFAULT '',
                  source_edit_times_json TEXT NOT NULL DEFAULT '[]', generation_duration_seconds REAL,
                  scene_reason TEXT NOT NULL DEFAULT '', FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE TABLE IF NOT EXISTS raw_shots (
                  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, number INTEGER NOT NULL,
                  start_time REAL NOT NULL, end_time REAL NOT NULL, duration REAL NOT NULL,
                  boundary_metrics_json TEXT NOT NULL DEFAULT '{}', decision TEXT NOT NULL DEFAULT 'keep',
                  reason TEXT NOT NULL DEFAULT '', narrative_shot_id TEXT,
                  generation_shot_ids_json TEXT NOT NULL DEFAULT '[]',
                  FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE TABLE IF NOT EXISTS frames (
                  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, shot_id TEXT NOT NULL,
                  timestamp REAL NOT NULL, path TEXT NOT NULL, thumbnail_path TEXT NOT NULL,
                  width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
                  blur_score REAL, brightness_score REAL, selected INTEGER NOT NULL DEFAULT 1,
                  artifact_kind TEXT NOT NULL DEFAULT 'keyframe', parent_frame_id TEXT, bbox_json TEXT,
                  FOREIGN KEY(job_id) REFERENCES jobs(id), FOREIGN KEY(shot_id) REFERENCES shots(id)
                );
                CREATE TABLE IF NOT EXISTS prompts (
                  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, shot_id TEXT NOT NULL,
                  prompt_type TEXT NOT NULL, platform TEXT NOT NULL, version INTEGER NOT NULL,
                  positive_prompt TEXT NOT NULL, negative_prompt TEXT NOT NULL,
                  status TEXT NOT NULL, template_id TEXT NOT NULL, template_version INTEGER NOT NULL,
                  warnings_json TEXT NOT NULL DEFAULT '[]', contract_json TEXT,
                  FOREIGN KEY(job_id) REFERENCES jobs(id), FOREIGN KEY(shot_id) REFERENCES shots(id)
                );
                CREATE TABLE IF NOT EXISTS events (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                  job_id TEXT NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS style_profile_revisions (
                  job_id TEXT NOT NULL, version INTEGER NOT NULL,
                  profile_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(job_id, version)
                );
                CREATE TABLE IF NOT EXISTS prompt_revisions (
                  job_id TEXT NOT NULL, prompt_id TEXT NOT NULL,
                  style_version INTEGER NOT NULL, payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(job_id, prompt_id, style_version)
                );
                CREATE TABLE IF NOT EXISTS style_terms_v2 (
                  id TEXT PRIMARY KEY, category TEXT NOT NULL, payload_json TEXT NOT NULL,
                  ontology_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS style_relations_v2 (
                  source_id TEXT NOT NULL, relation TEXT NOT NULL, target_id TEXT NOT NULL,
                  PRIMARY KEY(source_id,relation,target_id)
                );
                CREATE TABLE IF NOT EXISTS style_embeddings (
                  term_id TEXT NOT NULL, channel TEXT NOT NULL, view_index INTEGER NOT NULL,
                  model TEXT NOT NULL, dimension INTEGER NOT NULL, ontology_hash TEXT NOT NULL,
                  vector BLOB NOT NULL, PRIMARY KEY(term_id,channel,view_index)
                );
                CREATE TABLE IF NOT EXISTS style_embedding_cache (
                  cache_key TEXT PRIMARY KEY, model TEXT NOT NULL, dimension INTEGER NOT NULL,
                  ontology_hash TEXT NOT NULL, vector BLOB NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS style_index_manifest (
                  id INTEGER PRIMARY KEY CHECK(id=1), payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS style_embedding_usage (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, phase TEXT NOT NULL,
                  model TEXT NOT NULL, request_id TEXT, prompt_tokens INTEGER,
                  image_tokens INTEGER, total_tokens INTEGER, elapsed_seconds REAL,
                  estimated_list_price_cny REAL, status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS visual_bible_revisions (
                  job_id TEXT NOT NULL, version INTEGER NOT NULL, bible_json TEXT NOT NULL,
                  created_at TEXT NOT NULL, PRIMARY KEY(job_id,version)
                );
                CREATE INDEX IF NOT EXISTS idx_events_job_sequence ON events(job_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_shots_job ON shots(job_id, number);
                CREATE INDEX IF NOT EXISTS idx_raw_shots_job ON raw_shots(job_id, number);
                CREATE TABLE IF NOT EXISTS generation_plan_cache (
                  cache_key TEXT PRIMARY KEY, plan_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_agent_runs (
                  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL,
                  stage TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                  window_total INTEGER NOT NULL DEFAULT 0, window_completed INTEGER NOT NULL DEFAULT 0,
                  provider TEXT NOT NULL, model TEXT NOT NULL, config_json TEXT NOT NULL,
                  source_snapshot_json TEXT NOT NULL, source_snapshot_hash TEXT NOT NULL,
                  result_json TEXT, checkpoints_json TEXT NOT NULL DEFAULT '[]',
                  raw_responses_json TEXT NOT NULL DEFAULT '[]', audit_json TEXT NOT NULL DEFAULT '[]',
                  usage_json TEXT NOT NULL DEFAULT '[]', usage_summary_json TEXT NOT NULL DEFAULT '{}',
                  warnings_json TEXT NOT NULL DEFAULT '[]', cancel_requested INTEGER NOT NULL DEFAULT 0,
                  error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  started_at TEXT, completed_at TEXT, FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_event_agent_runs_job ON event_agent_runs(job_id,created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_event_agent_active_job ON event_agent_runs(job_id)
                  WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS event_agent_frames (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, job_id TEXT NOT NULL,
                  shot_id TEXT NOT NULL, timestamp REAL NOT NULL, path TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  FOREIGN KEY(run_id) REFERENCES event_agent_runs(id),
                  FOREIGN KEY(job_id) REFERENCES jobs(id), FOREIGN KEY(shot_id) REFERENCES shots(id)
                );
                CREATE INDEX IF NOT EXISTS idx_event_agent_frames_run ON event_agent_frames(run_id);
                CREATE TABLE IF NOT EXISTS perception_operations (
                  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, shot_id TEXT NOT NULL,
                  mode TEXT NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                  request_json TEXT NOT NULL, source_revision INTEGER NOT NULL,
                  source_hash TEXT NOT NULL, source_snapshot_json TEXT NOT NULL,
                  result_json TEXT, usage_json TEXT NOT NULL DEFAULT '[]',
                  error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  UNIQUE(job_id,shot_id,idempotency_key)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_perception_active_job ON perception_operations(job_id)
                  WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS perception_revisions (
                  job_id TEXT NOT NULL, shot_id TEXT NOT NULL, version INTEGER NOT NULL,
                  operation_id TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                  changes_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(job_id,shot_id,version)
                );
                """
            )
            self._ensure_column(conn, "jobs", "input_type", "TEXT NOT NULL DEFAULT 'video'")
            self._ensure_column(conn, "jobs", "provider", "TEXT NOT NULL DEFAULT 'legacy-unknown'")
            self._ensure_column(conn, "jobs", "model", "TEXT NOT NULL DEFAULT 'legacy-unknown'")
            self._ensure_column(conn, "jobs", "segmentation_profile", "TEXT NOT NULL DEFAULT 'legacy-v1'")
            self._ensure_column(conn, "jobs", "raw_shot_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "jobs", "narrative_shot_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "jobs", "script_plan_json", "TEXT")
            self._ensure_column(conn, "jobs", "script_raw_response_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "jobs", "planning_warnings_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "jobs", "model_usage_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "jobs", "montage_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "jobs", "style_observation_json", "TEXT")
            self._ensure_column(conn, "jobs", "style_profile_json", "TEXT")
            self._ensure_column(conn, "jobs", "visual_bible_json", "TEXT")
            self._ensure_column(conn, "jobs", "style_retrieval_json", "TEXT")
            self._ensure_column(conn, "jobs", "style_direct_json", "TEXT")
            self._ensure_column(conn, "jobs", "generation_plan_json", "TEXT")
            self._ensure_column(conn, "jobs", "event_sequence_json", "TEXT")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS event_sequence_revisions (
                  job_id TEXT NOT NULL, version INTEGER NOT NULL,
                  payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(job_id,version)
                );
            """)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS style_analyses (
                  id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
                  cache_key TEXT NOT NULL, source_json TEXT NOT NULL, result_json TEXT,
                  error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_style_analysis_cache ON style_analyses(cache_key,status);
                CREATE TABLE IF NOT EXISTS style_analysis_revisions (
                  analysis_id TEXT NOT NULL, version INTEGER NOT NULL,
                  profile_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(analysis_id,version)
                );
            """)
            self._ensure_column(conn, "shots", "source_text", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "shots", "source_raw_shot_ids_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "shots", "perception_json", "TEXT")
            self._ensure_column(conn, "shots", "perception_revision", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "frames", "artifact_kind", "TEXT NOT NULL DEFAULT 'keyframe'")
            self._ensure_column(conn, "frames", "parent_frame_id", "TEXT")
            self._ensure_column(conn, "frames", "bbox_json", "TEXT")
            self._ensure_column(conn, "shots", "scene_group_id", "TEXT")
            self._ensure_column(conn, "shots", "style_observation_v2_json", "TEXT")
            self._ensure_column(conn, "shots", "cut_reason", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "shots", "source_edit_times_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "shots", "generation_duration_seconds", "REAL")
            self._ensure_column(conn, "shots", "scene_reason", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "raw_shots", "generation_shot_ids_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "prompts", "contract_json", "TEXT")
            self._ensure_column(conn, "event_agent_runs", "window_total", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "event_agent_runs", "window_completed", "INTEGER NOT NULL DEFAULT 0")
            from app.projects.repository import ensure_schema
            ensure_schema(conn)

    @staticmethod
    def _ensure_column(conn, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql, tuple(params))

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, tuple(params)).fetchone()

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list:
        with self.connect() as conn:
            return conn.execute(sql, tuple(params)).fetchall()

    @staticmethod
    def assert_job_capacity(conn, limit: int) -> None:
        from app.projects.models import ProjectError
        total = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing','running')").fetchone()[0]
        if total >= limit:
            raise ProjectError(429, f"分析队列已满（最多{limit}个运行或等待任务），请等待完成后再提交")

    def insert_job(self, job: Dict[str, Any], *, capacity: Optional[int] = None) -> None:
        from app.projects.access import active_project
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_project(conn, job.get("project_id"))
            if capacity is not None:
                self.assert_job_capacity(conn, capacity)
            conn.execute(
                "INSERT INTO jobs (id,status,stage,progress,video_filename,video_path,input_type,script_text,provider,model,config_json,result_version,created_at,updated_at,project_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job["id"], job["status"], job["stage"], job["progress"], job.get("video_filename"), self.media.store(job.get("video_path")), job.get("input_type", "video"), job.get("script_text", ""), job.get("provider", "legacy-unknown"), job.get("model", "legacy-unknown"), json.dumps(job["config"]), job.get("result_version", "storyboard.v1"), job["created_at"], job["updated_at"], job.get("project_id")),
            )

    def update_job(self, job_id: str, **values: Any) -> None:
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        # A worker response can arrive after project deletion or cancellation.
        # Keep cancellation authoritative while still recording usage/costs.
        guard = ""
        if {"status", "stage", "progress", "error"} & set(values) and values.get("status") != "cancelled":
            guard = " AND status!='cancelled' AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=jobs.project_id AND p.deleted_at IS NOT NULL)"
        self.execute(f"UPDATE jobs SET {assignments} WHERE id = ?{guard}", list(values.values()) + [job_id])

    def add_event(self, job_id: str, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        event_id = f"evt-{job_id}-{datetime.now(timezone.utc).timestamp()}"
        created_at = utc_now()
        with self.connect() as conn:
            cursor = conn.execute("INSERT INTO events (event_id,job_id,type,payload_json,created_at) VALUES (?,?,?,?,?)", (event_id, job_id, event_type, json.dumps(payload), created_at))
            sequence = cursor.lastrowid
        return {"event_id": event_id, "type": event_type, "job_id": job_id, "sequence": sequence, "payload": payload, "created_at": created_at}

    def events_since(self, job_id: str, after: int = 0):
        rows = self.fetchall("SELECT sequence,event_id,job_id,type,payload_json,created_at FROM events WHERE job_id = ? AND sequence > ? ORDER BY sequence", (job_id, after))
        return [{"sequence": row["sequence"], "event_id": row["event_id"], "job_id": row["job_id"], "type": row["type"], "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]} for row in rows]
