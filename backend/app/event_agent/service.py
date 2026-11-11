"""Independent, durable runs on the source job's existing single-worker queue."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from app.db import utc_now
from app.event_agent.models import EventAgentConfig
from app.schemas import FrameArtifact, MontageRelation, PromptBundle, Shot


ACTIVE_STATUSES = {"queued", "running"}
RESULT_STATUSES = {"ready", "degraded"}


class EventAgentError(ValueError):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _usage_summary(records, core=None):
    costs = [record.get("estimated_list_price_cny") for record in records]
    measured = [cost for cost in costs if isinstance(cost, (int, float))]
    model_calls = max((core or {}).get("model_calls", 0), len(records))
    complete = bool(records) and len(measured) == len(records) and model_calls <= len(records)
    return {
        **(core or {}), "request_count": len(records),
        "model_calls": model_calls,
        "prompt_tokens": sum(record.get("prompt_tokens") or 0 for record in records),
        "completion_tokens": sum(record.get("completion_tokens") or 0 for record in records),
        "total_tokens": sum(record.get("total_tokens") or 0 for record in records),
        "estimated_list_price_cny": sum(measured) if complete else None,
        "known_estimated_list_price_cny": sum(measured) if measured else None,
        "cost_estimate_complete": complete, "unknown_cost_count": model_calls - len(measured),
    }


class EventAgentService:
    def __init__(self, job_service):
        self.jobs = job_service
        # One coordinator serves both explicit experiments and the main worker.
        self.jobs.event_agent_service = self
        self.db = job_service.db
        self.settings = job_service.settings
        # A restart is never permission to replay paid inference or reset a budget.
        self.db.execute(
            "UPDATE event_agent_runs SET status='interrupted',stage='interrupted',"
            "error=?,updated_at=?,completed_at=? WHERE status IN ('queued','running')",
            ("后端已重启；本次实验已中断，检查点和用量已保留。重新运行需显式创建新实验。", utc_now(), utc_now()),
        )

    def _job(self, job_id):
        job = self.jobs.status(job_id)
        if not job:
            raise EventAgentError(404, "任务不存在")
        return job

    def _provider(self):
        provider = self.jobs.provider
        health = provider.health()
        if not getattr(health, "ready", False):
            raise EventAgentError(503, "实验模型未配置完成：" + str(getattr(health, "detail", "")))
        if getattr(health, "provider", "") not in {"bailian", "mock"} or not callable(getattr(provider, "analyze_event_window", None)):
            raise EventAgentError(409, "当前 Provider 不支持事件 Agent 实验")
        return health

    @staticmethod
    def _source_rows(conn, job_id):
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise EventAgentError(404, "任务不存在")
        return {"job": dict(row), **{
            table: [dict(item) for item in conn.execute(
                f"SELECT * FROM {table} WHERE job_id=? ORDER BY {ordering}", (job_id,)).fetchall()]
            for table, ordering in (("shots", "number,id"), ("raw_shots", "number,id"),
                                    ("prompts", "id"), ("frames", "timestamp,id"))
        }}

    @staticmethod
    def _fingerprints(source):
        fingerprints = {}
        for frame in source["frames"]:
            path = Path(frame["path"])
            if path.is_file():
                fingerprints[frame["id"]] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                fingerprints[frame["id"]] = None
        video = Path(source["job"]["video_path"] or "")
        if video.is_file():
            stat = video.stat()
            fingerprints["video"] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        else:
            fingerprints["video"] = None
        return fingerprints

    def _snapshot(self, job_id):
        with self.db.connect() as conn:
            # One short read transaction produces a coherent source snapshot.
            conn.execute("BEGIN")
            source = self._source_rows(conn, job_id)
        source["file_fingerprints"] = self._fingerprints(source)
        return source

    @staticmethod
    def _source_signature(source, *, inline=False):
        """Progress/accounting changes are not edits to visual source evidence."""
        if not inline:
            return source
        value = {**source, "job": dict(source["job"])}
        for key in ("status", "stage", "progress", "error", "updated_at", "model_usage_json", "event_sequence_json"):
            value["job"].pop(key, None)
        return value

    def _prepare_run(self, job_id, config=None, *, inline=False):
        job = self._job(job_id)
        if config is None:
            capacity = json.loads(job["config_json"]).get("capacity_preflight", {}) if inline else {}
            config = EventAgentConfig(max_windows=max(12, min(64, int(capacity.get("agent_max_windows", 12)))))
        if job["input_type"] != "video":
            raise EventAgentError(409, "事件 Agent 实验仅支持已完成的视频任务")
        expected_status = "processing" if inline else "completed"
        if job["status"] != expected_status:
            raise EventAgentError(409, "请等待来源视频任务完成后再运行实验")
        if self.db.fetchone("SELECT id FROM event_agent_runs WHERE job_id=? AND status IN ('queued','running')", (job_id,)):
            raise EventAgentError(409, "该视频已有排队或运行中的事件 Agent 实验")
        health = self._provider()
        source = self._snapshot(job_id)
        if source["job"]["input_type"] != "video" or source["job"]["status"] != expected_status:
            raise EventAgentError(409, "来源任务状态已变化，请等待视频任务完成")
        if not source["shots"] or not source["file_fingerprints"]["video"]:
            raise EventAgentError(409, "来源视频或镜头不可用，无法运行取证实验")
        if not any(frame["selected"] and source["file_fingerprints"].get(frame["id"]) for frame in source["frames"]):
            raise EventAgentError(409, "来源任务缺少可读取的关键帧")
        run_id = "event-run-" + uuid.uuid4().hex[:16]
        now = utc_now()
        try:
            with self.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM perception_operations WHERE job_id=? AND status IN ('queued','running')", (job_id,)).fetchone():
                    raise EventAgentError(409, "该任务正在恢复视觉证据，请等待完成后再分析事件")
                # Source editing during snapshot collection invalidates this submission.
                current = self._source_rows(conn, job_id)
                baseline = {key: value for key, value in source.items() if key != "file_fingerprints"}
                if self._source_signature(current, inline=inline) != self._source_signature(baseline, inline=inline):
                    raise EventAgentError(409, "来源结果已变化，请刷新后重新创建实验")
                stored = self.db.media.snapshot(source)
                conn.execute(
                    "INSERT INTO event_agent_runs(id,job_id,status,stage,provider,model,config_json,"
                    "source_snapshot_json,source_snapshot_hash,usage_summary_json,created_at,updated_at) "
                    "VALUES(?,?,'queued','queued',?,?,?,?,?,?,?,?)",
                    (run_id, job_id, health.provider, health.model, _json(config.model_dump(mode="json")),
                     _json(stored), _hash(stored), _json(_usage_summary([])), now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise EventAgentError(409, "该视频已有排队或运行中的事件 Agent 实验") from exc
        self._emit(job_id, run_id, "event_agent.queued", {"status": "queued", "progress": 0})
        return run_id

    def create(self, job_id, config=None):
        run_id = self._prepare_run(job_id, config)
        try:
            # All cloud work uses the established serialized worker, including other jobs/styles.
            self.jobs.executor.submit(self._run, job_id, run_id)
        except Exception:
            self._finish(run_id, "failed", error="后端执行器不可用，本次实验尚未开始")
            raise EventAgentError(503, "后端执行器不可用")
        return self.get(job_id, run_id)

    def run_inline(self, job_id, config=None):
        """Run inside the existing single worker; never enqueue and wait on itself."""
        if self.jobs.running_job != job_id:
            raise EventAgentError(409, "主流程事件分析必须由当前任务的串行 Worker 执行")
        run_id = self._prepare_run(job_id, config, inline=True)
        self._run(job_id, run_id, inline=True)
        row = self._row(job_id, run_id)
        if row["status"] not in RESULT_STATUSES or not row["result_json"]:
            raise EventAgentError(409, row["error"] or "事件分析未完成；来源镜头已保留")
        # Internal projection needs actual artifact paths, not public URL views.
        result = json.loads(row["result_json"])
        return self.db.media.frame_tree(result, reading=True)

    def _row(self, job_id, run_id):
        row = self.db.fetchone("SELECT * FROM event_agent_runs WHERE job_id=? AND id=?", (job_id, run_id))
        if not row:
            raise EventAgentError(404, "实验运行不存在")
        return dict(row)

    @staticmethod
    def _summary(row):
        return {"run_id": row["id"], **{key: row[key] for key in (
            "job_id", "status", "stage", "progress", "provider", "model", "source_snapshot_hash",
            "window_total", "window_completed",
            "error", "created_at", "updated_at", "started_at", "completed_at")},
            "config": json.loads(row["config_json"]), "cancel_requested": bool(row["cancel_requested"]),
            "warnings": json.loads(row["warnings_json"]), "usage_summary": json.loads(row["usage_summary_json"]),
            "checkpoint_count": len(json.loads(row["checkpoints_json"])),
            "has_result": row["status"] in RESULT_STATUSES and bool(row["result_json"])}

    def list(self, job_id):
        self._job(job_id)
        rows = self.db.fetchall("SELECT * FROM event_agent_runs WHERE job_id=? ORDER BY created_at DESC,id DESC", (job_id,))
        return {"job_id": job_id, "runs": [self._summary(dict(row)) for row in rows]}

    @staticmethod
    def _public(value, job_id):
        if isinstance(value, list):
            return [EventAgentService._public(item, job_id) for item in value]
        if isinstance(value, dict):
            converted = {key: EventAgentService._public(item, job_id) for key, item in value.items()}
            if "id" in value and "shot_id" in value and "timestamp" in value and "path" in value:
                converted["path"] = converted["thumbnail_path"] = f"/api/jobs/{job_id}/frames/{value['id']}"
            return converted
        return value

    def get(self, job_id, run_id):
        row = self._row(job_id, run_id)
        return {**self._summary(row), **{key: self._public(json.loads(row[column]), job_id)
            for key, column in (("checkpoints", "checkpoints_json"), ("audit", "audit_json"),
                                ("raw_responses", "raw_responses_json"), ("usage", "usage_json"))}}

    def result(self, job_id, run_id):
        row = self._row(job_id, run_id)
        if row["status"] not in RESULT_STATUSES or not row["result_json"]:
            raise EventAgentError(409, "本次实验尚无已提交结果；可读取运行状态和取证检查点")
        return self._public(json.loads(row["result_json"]), job_id)

    def cancel(self, job_id, run_id):
        self._row(job_id, run_id)
        changed = False
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM event_agent_runs WHERE job_id=? AND id=?", (job_id, run_id)).fetchone()
            if row["status"] in ACTIVE_STATUSES:
                changed = True
                queued = row["status"] == "queued"
                conn.execute("UPDATE event_agent_runs SET cancel_requested=1,status=?,stage=?,updated_at=?,completed_at=? WHERE id=?",
                             ("cancelled" if queued else "running", "cancelled" if queued else "cancelling",
                              utc_now(), utc_now() if queued else None, run_id))
        if changed:
            self._emit(job_id, run_id, "event_agent.cancel_requested", {"cancel_requested": True})
        return self.get(job_id, run_id)

    def _cancelled(self, run_id):
        row = self.db.fetchone("SELECT status,cancel_requested FROM event_agent_runs WHERE id=?", (run_id,))
        return not row or bool(row["cancel_requested"]) or row["status"] in {"cancelled", "interrupted"}

    def _emit(self, job_id, run_id, event_type, payload):
        return self.jobs._emit(job_id, event_type, {**payload, "run_id": run_id})

    def _finish(self, run_id, status, *, error=None):
        self.db.execute("UPDATE event_agent_runs SET status=?,stage=?,error=?,updated_at=?,completed_at=? WHERE id=? AND status IN ('queued','running')",
                        (status, status, error, utc_now(), utc_now(), run_id))

    @staticmethod
    def _inputs(source):
        frames = [FrameArtifact(**{key: frame[key] for key in ("id", "shot_id", "timestamp", "path", "thumbnail_path", "width", "height", "blur_score", "brightness_score", "selected", "artifact_kind", "parent_frame_id")},
                                bbox=json.loads(frame["bbox_json"]) if frame["bbox_json"] else None) for frame in source["frames"]]
        prompts = [PromptBundle(**{key: prompt[key] for key in ("id", "shot_id", "prompt_type", "platform", "version", "positive_prompt", "negative_prompt", "status", "template_id", "template_version")},
                                warnings=json.loads(prompt["warnings_json"]), contract=json.loads(prompt["contract_json"]) if prompt["contract_json"] else None) for prompt in source["prompts"]]
        shots = []
        for row in source["shots"]:
            observation = json.loads(row["observation_json"] or "{}")
            perception = json.loads(row["perception_json"] or "{}")
            shots.append(Shot(**{key: row[key] for key in ("id", "number", "start_time", "end_time", "duration", "time", "source_text", "status", "scene_group_id", "cut_reason", "generation_duration_seconds", "scene_reason")},
                **{key: observation[key] for key in ("shot_size", "lens", "camera_movement", "lighting_vibe", "composition", "confidence", "action_details", "spatial_evidence", "visual_style_evidence", "visual_facts", "analysis_warnings") if key in observation},
                analysis_span_observations=observation.get("analysis_span_observations", perception.get("analysis_span_observations", [])),
                action=observation.get("subject_action", ""), source_raw_shot_ids=json.loads(row["source_raw_shot_ids_json"]),
                source_edit_times=json.loads(row["source_edit_times_json"]),
                visual_observation_v2=(json.loads(row["perception_json"])["observation"] if row["perception_json"] else None),
                frame_ids=[frame.id for frame in frames if frame.shot_id == row["id"] and frame.selected],
                prompts=[prompt for prompt in prompts if prompt.shot_id == row["id"]]))
            # Histogram/edit heuristics are only a presentation hint. The Agent
            # must inspect continuity itself; actual semantic IDs remain hard.
            reason = row["scene_reason"] or ""
            if reason.startswith("视觉") and ("待" in reason or "需复核" in reason):
                shots[-1].scene_reason = "legacy-presentation"
        # A low-confidence montage guess cannot become a hard boundary merely
        # because its scene ID was copied onto all subsequent source rows.
        by_id = {shot.id: shot for shot in shots}
        candidate_scenes = {by_id[item["right_shot_id"]].scene_group_id
            for item in EventAgentService._boundary_assessments(source)
            if item["strength"] == "candidate" and item["right_shot_id"] in by_id
            and by_id[item["left_shot_id"]].scene_group_id != by_id[item["right_shot_id"]].scene_group_id}
        for shot in shots:
            if shot.scene_group_id in candidate_scenes and not (shot.scene_reason or "").startswith(
                    ("人工确认", "已确认", "已保存且校验", "事件 Agent 真实")):
                shot.scene_reason = "candidate-boundary: 场次变化证据不足，供事件 Agent 复核"
        return shots, frames

    @staticmethod
    def _protected_sources(source):
        """Preserve previously validated reveals/reactions, not just new model labels."""
        try:
            montage = json.loads(source["job"].get("montage_json") or "{}")
        except (TypeError, ValueError):
            return set()
        if not isinstance(montage, dict):
            return set()
        order = {shot["id"]: index for index, shot in enumerate(source["shots"])}
        protected = set()
        relations = montage.get("relations", [])
        if not isinstance(relations, list):
            return set()
        for item in relations:
            try:
                relation = MontageRelation.model_validate(item)
            except (TypeError, ValueError):
                continue
            if (relation.left_shot_id in order and relation.right_shot_id in order
                    and order[relation.right_shot_id] == order[relation.left_shot_id] + 1
                    and relation.boundary_role in {"reveal", "independent-reaction"}):
                protected.add(relation.right_shot_id)
        return protected

    @staticmethod
    def _boundary_assessments(source):
        """Separate recorded high-confidence changes from advisory guesses.

        Historical evidence is free text, so its provenance remains explicitly
        labelled legacy rather than pretending it has structured bilateral refs.
        """
        try:
            montage = json.loads(source["job"].get("montage_json") or "{}")
        except (TypeError, ValueError):
            return []
        if not isinstance(montage, dict):
            return []
        order = {shot["id"]: index for index, shot in enumerate(source["shots"])}
        assessments = []
        for item in montage.get("relations", []) if isinstance(montage.get("relations"), list) else []:
            try:
                relation = MontageRelation.model_validate(item)
            except (TypeError, ValueError):
                continue
            if (relation.left_shot_id in order and relation.right_shot_id in order
                    and order[relation.right_shot_id] == order[relation.left_shot_id] + 1
                    and (relation.scene_change or relation.relation_type == "时间省略")):
                bilateral = {ref.shot_id for ref in relation.context_evidence} >= {
                    relation.left_shot_id, relation.right_shot_id}
                modern = montage.get("version") == "montage-relations.v3"
                trusted = relation.confidence >= .8 and bool(relation.evidence) and (bilateral or not modern)
                assessments.append({"left_shot_id": relation.left_shot_id,
                    "right_shot_id": relation.right_shot_id, "confidence": relation.confidence,
                    "strength": "trusted" if trusted else "candidate",
                    "evidence_basis": "bilateral-source-context" if bilateral else "legacy-recorded-evidence" if relation.evidence else "missing",
                    "reason": relation.reason or relation.observed_edit_fact,
                    "evidence": relation.evidence})
        return assessments

    @staticmethod
    def _protected_boundaries(source):
        """Only evidenced, high-confidence scene/time changes are hard gates."""
        return {(row["left_shot_id"], row["right_shot_id"])
                for row in EventAgentService._boundary_assessments(source) if row["strength"] == "trusted"}

    @classmethod
    def scene_projection(cls, result, source, shots, frames):
        """Local scene decisions, independent of event and generation boundaries.

        A new goal alone does not create a new scene. Unknown transitions retain
        the saved visual grouping; trusted changes can never be merged away.
        """
        from app.event_agent.engine import _actual_scene_id
        original = {row["id"]: row for row in source["shots"]}
        owned = {sid: event for event in result.events for sid in event.source_shot_ids}
        boundaries = cls._protected_boundaries(source)
        frame_map = {frame.id: frame for frame in [*frames, *result.frames]}
        projected = []
        scene_number = 0
        previous = None
        for shot in sorted(shots, key=lambda item: (item.start_time, item.number, item.id)):
            event = owned[shot.id]
            refs = []
            explicit = "unknown"
            if previous and event.source_shot_ids[0] == shot.id and event.scene_continuity != "unknown":
                refs = event.scene_evidence
                valid_owners = set()
                for ref in refs:
                    frame = frame_map.get(ref.frame_id)
                    if (not frame or frame.shot_id not in {previous.id, shot.id}
                            or abs(frame.timestamp - ref.timestamp) > .025
                            or not original[frame.shot_id]["start_time"] <= ref.timestamp < original[frame.shot_id]["end_time"]):
                        raise ValueError("场次投影缺少相邻两侧的真实帧证据")
                    valid_owners.add(frame.shot_id)
                if valid_owners != {previous.id, shot.id}:
                    raise ValueError("场次投影必须有相邻两侧的真实帧证据")
                explicit = event.scene_continuity
            hard = bool(previous and ((previous.id, shot.id) in boundaries or (
                _actual_scene_id(previous) and _actual_scene_id(shot)
                and _actual_scene_id(previous) != _actual_scene_id(shot))))
            within_event = bool(previous and owned[previous.id].id == event.id and event.status == "ready")
            if hard and (within_event or explicit == "same_scene"):
                raise ValueError("场次投影不能吞掉可信真实场次或时间省略边界")
            same = bool(previous and not hard and explicit not in {"scene_change", "time_elision"}
                and (within_event or explicit == "same_scene" or (
                    original[previous.id]["scene_group_id"] == original[shot.id]["scene_group_id"])))
            if not same:
                scene_number += 1
            reason = original[shot.id]["scene_reason"] or ""
            if hard:
                reason = f"已保存且校验的真实场次或时间省略边界；运行 {result.run_id}"
            elif explicit in {"scene_change", "time_elision"}:
                reason = f"事件 Agent 真实{'时间省略' if explicit == 'time_elision' else '场次变化'}，双侧帧证据；运行 {result.run_id}"
            elif explicit == "same_scene" or within_event or (event.status == "ready" and len(event.source_shot_ids) > 1):
                reason = f"事件 Agent 确认可见事件时空连续；运行 {result.run_id}"
            evidence = [ref.model_dump(mode="json") for ref in refs]
            if not evidence and event.status == "ready":
                evidence = [ref.model_dump(mode="json") for fact in event.causal_facts
                    for ref in fact.evidence if shot.id in fact.source_shot_ids]
            projected.append({"shot_id": shot.id, "number": shot.number,
                "scene_group_id": f"scene-{scene_number:03d}", "scene_reason": reason,
                "scene_continuity": explicit, "evidence": evidence})
            previous = shot
        return projected

    def _register_frame(self, job_id, run_id, frame, source, artifact_dir):
        if self._cancelled(run_id):
            raise EventAgentError(409, "实验已取消")
        owners = {shot["id"]: shot for shot in source["shots"]}
        owner = owners.get(frame.shot_id)
        if owner is None or not owner["start_time"] <= frame.timestamp < owner["end_time"]:
            raise ValueError("取证帧必须绑定其时间范围内的真实来源镜头")
        path, thumbnail = Path(frame.path).resolve(), Path(frame.thumbnail_path).resolve()
        directory = artifact_dir.resolve()
        if directory not in path.parents or directory not in thumbnail.parents or not path.is_file() or not thumbnail.is_file():
            raise ValueError("取证帧路径不属于本次实验目录")
        payload = self.db.media.record(frame.model_dump(mode="json"))
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT status,cancel_requested FROM event_agent_runs WHERE id=? AND job_id=?", (run_id, job_id)).fetchone()
            if not run or run["cancel_requested"] or run["status"] not in ACTIVE_STATUSES:
                raise EventAgentError(409, "实验已取消或结束")
            conn.execute("INSERT INTO event_agent_frames(id,run_id,job_id,shot_id,timestamp,path,payload_json) VALUES(?,?,?,?,?,?,?)",
                         (frame.id, run_id, job_id, frame.shot_id, frame.timestamp, payload["path"], _json(payload)))
        self._emit(job_id, run_id, "event_agent.frame", {"frame_id": frame.id, "shot_id": frame.shot_id, "timestamp": frame.timestamp})

    def _run(self, job_id, run_id, *, inline=False):
        from app.event_agent import build_event_windows, run_event_agent
        from app.event_agent.tools import EventWindowToolRegistry

        if self._cancelled(run_id):
            return
        row = self._row(job_id, run_id)
        source = self.db.media.snapshot(json.loads(row["source_snapshot_json"]), reading=True)
        records, carry = [], []
        core_usage = {}
        window_usage = {}
        drain = getattr(self.jobs.provider, "drain_usage", None)
        claimed = False

        def cancelled():
            return self._cancelled(run_id) or (inline and self.jobs._is_cancelled(job_id))

        def source_hash(value):
            return _hash(self._source_signature(value, inline=inline))

        def collect_usage():
            if callable(drain):
                for record in drain():
                    if str(record.get("phase", "")).startswith("event_agent."):
                        records.append({**record, "job_id": job_id, "run_id": run_id})
                    else:
                        carry.append(record)
            self.db.execute("UPDATE event_agent_runs SET usage_json=?,usage_summary_json=?,updated_at=? WHERE id=?",
                            (_json(records), _json(_usage_summary(records, core_usage)), utc_now(), run_id))

        def checkpoint(event_type, payload):
            payload = self.db.media.frame_tree(payload)
            if event_type == "event_agent.window":
                window_usage[payload["window_id"]] = payload.get("usage", {})
                core_usage.update({key: sum(item.get(key, 0) for item in window_usage.values())
                                   for key in ("model_calls", "tool_calls", "extra_frames", "elapsed_seconds")})
                core_usage["windows"] = len(window_usage)
            collect_usage()
            with self.db.connect() as conn:
                current = conn.execute("SELECT * FROM event_agent_runs WHERE id=?", (run_id,)).fetchone()
                if event_type == "event_agent.window":
                    checkpoints = [*json.loads(current["checkpoints_json"]), payload]
                    audit = [*json.loads(current["audit_json"]), payload]
                    raw = [*json.loads(current["raw_responses_json"]), *payload.get("raw_responses", [])]
                    warnings = list(dict.fromkeys([*json.loads(current["warnings_json"]), *payload.get("warnings", [])]))
                    conn.execute("UPDATE event_agent_runs SET checkpoints_json=?,audit_json=?,raw_responses_json=?,warnings_json=?,updated_at=? WHERE id=?",
                                 (_json(checkpoints), _json(audit), _json(raw), _json(warnings), utc_now(), run_id))
                total = max(current["window_total"], payload.get("window_total", payload.get("window_count", 0)))
                completed = max(len(window_usage), payload.get("window_completed", 0))
                conn.execute("UPDATE event_agent_runs SET window_total=?,window_completed=? WHERE id=?",
                             (total, completed, run_id))
                if not current["cancel_requested"] and current["status"] == "running":
                    progress = max(current["progress"], min(99, int(payload.get("progress", 100 * completed / max(total, 1)))))
                    conn.execute("UPDATE event_agent_runs SET progress=?,stage=?,updated_at=? WHERE id=? AND status='running' AND cancel_requested=0",
                                 (progress, payload.get("stage", "analyzing"), utc_now(), run_id))
            self._emit(job_id, run_id, event_type, self._public(payload, job_id))
            if inline and not cancelled():
                progress = min(94, 88 + int(payload.get("progress", 100 * len(window_usage) / max(total, 1)) * .06))
                current_job = self.jobs.status(job_id)
                self.jobs._set_stage(job_id, "event_planning", max(progress, current_job["progress"]))

        try:
            with self.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                changed = conn.execute("UPDATE event_agent_runs SET status='running',stage='validating',started_at=?,updated_at=? WHERE id=? AND status='queued' AND cancel_requested=0",
                                       (utc_now(), utc_now(), run_id)).rowcount
            if not changed:
                return
            health = self._provider()
            if health.provider != row["provider"] or health.model != row["model"]:
                raise ValueError("排队期间 Provider 或模型已变化，请显式创建新的实验")
            if source_hash(self._snapshot(job_id)) != source_hash(source):
                raise ValueError("来源结果或关键帧在实验开始前已变化")
            if cancelled():
                self._finish(run_id, "cancelled")
                return
            # Stale usage belongs to earlier workflows, never this run.
            if callable(drain):
                carry.extend(drain())
            claimed = True
            shots, frames = self._inputs(source)
            config = EventAgentConfig.model_validate_json(row["config_json"])
            self.db.execute("UPDATE event_agent_runs SET window_total=? WHERE id=?",
                            (len(build_event_windows(shots, config)), run_id))
            artifact_dir = self.settings.data_dir / "jobs" / job_id / "event_agent" / run_id
            artifact_dir.mkdir(parents=True, exist_ok=True)
            def tool_factory(window, known_frames, remaining_extra_frames):
                return EventWindowToolRegistry(window, shots, known_frames,
                    video_path=source["job"]["video_path"], artifact_dir=artifact_dir,
                    max_extra_frames=remaining_extra_frames,
                    register_frame=lambda frame: self._register_frame(job_id, run_id, frame, source, artifact_dir))
            style = (json.loads(source["job"].get("style_direct_json") or "{}").get("profile") or {}).get("rendered_prompt", "")
            result = run_event_agent(job_id=job_id, run_id=run_id, shots=shots, frames=frames,
                provider=self.jobs.provider, tool_factory=tool_factory, config=config, style_text=style,
                protected_source_shot_ids=self._protected_sources(source),
                protected_boundaries=self._protected_boundaries(source),
                candidate_boundaries=[row for row in self._boundary_assessments(source) if row["strength"] == "candidate"],
                cancelled=cancelled, emit=checkpoint)
            payload = self.db.media.frame_tree(result.model_dump(mode="json"))
            core_usage.update(payload.get("usage_summary", {}))
            collect_usage()
            payload["usage_summary"] = _usage_summary(records, core_usage)
            if cancelled() or payload["status"] == "cancelled":
                self._finish(run_id, "cancelled")
                return
            final_source = self._snapshot(job_id)
            if source_hash(final_source) != source_hash(source):
                raise ValueError("来源结果或关键帧在实验过程中已变化；实验结果未提交")
            with self.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = self._source_rows(conn, job_id)
                baseline = {key: value for key, value in final_source.items() if key != "file_fingerprints"}
                if inline and current["job"]["status"] == "cancelled":
                    raise EventAgentError(409, "任务已取消，未提交事件结果")
                if self._source_signature(current, inline=inline) != self._source_signature(baseline, inline=inline):
                    raise ValueError("来源结果在结果提交时已变化")
                committed = conn.execute("UPDATE event_agent_runs SET status=?,stage=?,progress=100,result_json=?,audit_json=?,raw_responses_json=?,warnings_json=?,usage_summary_json=?,updated_at=?,completed_at=? WHERE id=? AND status='running' AND cancel_requested=0",
                    (payload["status"], payload["status"], _json(payload), _json(payload["audit"]), _json(payload["raw_responses"]),
                     _json(payload["warnings"]), _json(payload["usage_summary"]), utc_now(), utc_now(), run_id)).rowcount
            if not committed:
                self._finish(run_id, "cancelled")
        except Exception as exc:
            self._finish(run_id, "cancelled" if cancelled() else "failed", error=str(exc)[:1000])
        finally:
            if claimed:
                collect_usage()
                # Preserve unrelated pending records for their original owner.
                pending = [*carry, *records] if inline else carry
                if pending and isinstance(getattr(self.jobs.provider, "usage", None), list):
                    self.jobs.provider.usage[:0] = pending
            final = self._row(job_id, run_id)
            self._emit(job_id, run_id, "event_agent.finished", self._summary(final))
