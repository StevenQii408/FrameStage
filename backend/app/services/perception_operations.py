"""Single-worker, revision-safe recovery of saved video observations.

Inference happens outside a transaction. A compare-and-swap protects observations,
manual Prompt edits, style and derived sequences from stale writes. Reading a job
or restarting the backend never queues an operation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import uuid
from math import gcd
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db import utc_now
from app.providers.base import FrameInput, ShotContext
from app.schemas import VisionObservation


class PerceptionOperationError(ValueError):
    def __init__(self, detail, status_code=409):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


class PerceptionOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["recover", "review"]
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    expected_revision: int = Field(ge=0)
    stages: list[Literal["static", "motion"]] = Field(default_factory=list, max_length=2)
    field_group: Optional[Literal["facts", "subject", "scene", "spatial", "action", "dynamics", "interaction", "expression", "accompanying"]] = None
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_mode(self):
        if self.mode == "recover":
            if not self.stages or len(set(self.stages)) != len(self.stages) or self.field_group:
                raise ValueError("阶段恢复必须指定不重复的 static/motion，不能同时指定纠错字段")
        elif not self.field_group or not self.reason.strip() or self.stages:
            raise ValueError("证据复核必须指定字段组及复核原因，不能同时指定恢复阶段")
        return self


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value, default=None):
    return json.loads(value) if value else ({} if default is None else default)


def _has_unexecuted_spans(record):
    return any(span.get("status") != "ready" and not span.get("perception_record")
               for span in record.get("analysis_span_observations", []))


class PerceptionOperationService:
    def __init__(self, jobs):
        self.jobs, self.db = jobs, jobs.db
        self.db.execute("UPDATE perception_operations SET status='interrupted',error='后端重启；未自动重发付费请求，请先查看审计记录',updated_at=? WHERE status IN ('queued','running')", (utc_now(),))

    def _source(self, conn, job_id, shot_id):
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        shot = conn.execute("SELECT * FROM shots WHERE job_id=? AND id=?", (job_id, shot_id)).fetchone()
        if job is None or shot is None:
            raise PerceptionOperationError("任务或来源镜头不存在", 404)
        if job["input_type"] != "video":
            raise PerceptionOperationError("此接口只恢复视频视觉证据，文字剧本请使用剧本规划")
        # Include all mutable render inputs, not unrelated job progress/usage.
        job_keys = ("id", "status", "video_path", "script_text", "provider", "model", "config_json",
                    "generation_plan_json", "event_sequence_json", "style_direct_json", "style_profile_json", "visual_bible_json")
        return {"job": {key: job[key] for key in job_keys}, "shot": dict(shot),
                "frames": [dict(row) for row in conn.execute("SELECT * FROM frames WHERE job_id=? AND shot_id=? ORDER BY timestamp,id", (job_id, shot_id))],
                "prompts": [dict(row) for row in conn.execute("SELECT * FROM prompts WHERE job_id=? AND shot_id=? ORDER BY id", (job_id, shot_id))]}

    def _hash(self, source):
        fingerprints = []
        for frame in source["frames"]:
            path = Path(frame["path"])
            fingerprints.append({"id": frame["id"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"})
        video = Path(source["job"]["video_path"] or "")
        stat = video.stat() if video.is_file() else None
        payload = {**self.db.media.snapshot(source), "frame_hashes": fingerprints,
                   "video_stat": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns} if stat else None}
        return hashlib.sha256(_json(payload).encode()).hexdigest()

    @staticmethod
    def _record(source):
        saved = _loads(source["shot"].get("perception_json"))
        if not saved:
            raise PerceptionOperationError("该历史镜头没有可恢复的 v2 记录；不会伪造阶段状态")
        from app.services.perception import infer_stage_records
        # Legacy inference remains evidence-based; unknown is not a failed request.
        saved["stages"] = {name: stage.model_dump(mode="json") for name, stage in infer_stage_records(saved).items()}
        return saved

    def status(self, job_id, shot_id):
        with self.db.connect() as conn:
            source = self._source(conn, job_id, shot_id)
        try:
            record = self._record(source)
            stages = record.get("stages", {})
        except PerceptionOperationError:
            record, stages = {}, {}
        previous = self.db.fetchall("SELECT * FROM perception_operations WHERE job_id=? AND shot_id=? ORDER BY created_at DESC", (job_id, shot_id))
        used = {stage for op in previous if op["mode"] == "recover" and (op["status"] != "cancelled" or _loads(op["result_json"]).get("model_call_started"))
                for stage in _loads(op["request_json"]).get("stages", [])}
        from app.services.perception import recoverable_stages
        recoverable = [name for name in recoverable_stages(record) if name not in used or _has_unexecuted_spans(record)] if record else []
        return {"job_id": job_id, "shot_id": shot_id, "revision": source["shot"]["perception_revision"],
                "stages": stages, "recoverable_stages": recoverable, "record_available": bool(record),
                "operations": [self._view(op) for op in previous], "read_only": source["job"]["status"] not in ("completed", "failed")}

    @staticmethod
    def _view(row):
        return {key: row[key] for key in ("id", "job_id", "shot_id", "mode", "status", "source_revision", "error", "created_at", "updated_at")} | {
            "request": _loads(row["request_json"]), "result": _loads(row["result_json"]),
            "usage": _loads(row["usage_json"], []), "cancel_requested": bool(row["cancel_requested"])}

    def get(self, job_id, shot_id, operation_id):
        row = self.db.fetchone("SELECT * FROM perception_operations WHERE id=? AND job_id=? AND shot_id=?", (operation_id, job_id, shot_id))
        if row is None:
            raise PerceptionOperationError("恢复操作不存在", 404)
        return self._view(row)

    def history(self, job_id, shot_id):
        self.status(job_id, shot_id)
        rows = self.db.fetchall("SELECT * FROM perception_revisions WHERE job_id=? AND shot_id=? ORDER BY version DESC", (job_id, shot_id))
        def public_snapshot(stored):
            snapshot = _loads(stored)
            if snapshot.get("job", {}).get("video_path"):
                snapshot["job"]["video_path"] = f"/api/jobs/{job_id}/video"
            for frame in snapshot.get("frames", []):
                frame["path"] = frame["thumbnail_path"] = f"/api/jobs/{job_id}/frames/{frame['id']}"
            return snapshot
        return {"revisions": [{"version": row["version"], "operation_id": row["operation_id"],
                "before": public_snapshot(row["before_json"]), "after": _loads(row["after_json"]),
                "changes": _loads(row["changes_json"]), "created_at": row["created_at"]} for row in rows]}

    def create(self, job_id, shot_id, payload):
        request = payload.model_dump(mode="json")
        now, operation_id = utc_now(), "perception-" + uuid.uuid4().hex[:16]
        try:
            with self.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                previous = conn.execute("SELECT * FROM perception_operations WHERE job_id=? AND shot_id=? AND idempotency_key=?", (job_id, shot_id, payload.idempotency_key)).fetchone()
                if previous:
                    if _loads(previous["request_json"]) != request:
                        raise PerceptionOperationError("同一幂等键不能用于不同请求")
                    return self._view(previous)
                source = self._source(conn, job_id, shot_id)
                job, shot = source["job"], source["shot"]
                if job["status"] not in ("completed", "failed"):
                    raise PerceptionOperationError("请等待当前任务停止后，再显式恢复或复核")
                if shot["perception_revision"] != payload.expected_revision:
                    raise PerceptionOperationError("视觉版本已改变，请刷新再操作")
                health = self.jobs.provider.health()
                if health.provider != job["provider"] or health.model != job["model"] or not health.ready:
                    raise PerceptionOperationError("当前 Provider/模型与原任务不一致或未就绪，未发出请求")
                if conn.execute("SELECT 1 FROM event_agent_runs WHERE job_id=? AND status IN ('queued','running')", (job_id,)).fetchone():
                    raise PerceptionOperationError("事件分析正在运行，请等待完成")
                record = self._record(source)
                prior = [row for row in conn.execute("SELECT * FROM perception_operations WHERE job_id=? AND shot_id=?", (job_id, shot_id)).fetchall()
                         if row["status"] != "cancelled" or _loads(row["result_json"]).get("model_call_started")]
                if payload.mode == "recover":
                    used = {stage for row in prior if row["mode"] == "recover" for stage in _loads(row["request_json"]).get("stages", [])}
                    if any(stage in used for stage in payload.stages) and not _has_unexecuted_spans(record):
                        raise PerceptionOperationError("每个失败阶段仅允许一次显式恢复；请查看记录，不会循环付费重试")
                    from app.services.perception import recoverable_stages
                    if any(stage not in recoverable_stages(record) for stage in payload.stages):
                        raise PerceptionOperationError("只允许恢复已记录的失败或依赖未执行阶段；成功阶段不重跑")
                elif any(row["mode"] == "review" and row["source_revision"] == payload.expected_revision and _loads(row["request_json"]).get("field_group") == payload.field_group for row in prior):
                    raise PerceptionOperationError("当前版本的这个字段组已复核过，不会重复调用")
                conn.execute("INSERT INTO perception_operations(id,job_id,shot_id,mode,status,idempotency_key,request_json,source_revision,source_hash,source_snapshot_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (operation_id, job_id, shot_id, payload.mode, "queued", payload.idempotency_key, _json(request), shot["perception_revision"], self._hash(source), _json(self.db.media.snapshot(source)), now, now))
        except sqlite3.IntegrityError as exc:
            raise PerceptionOperationError("该任务已有视觉恢复操作，请等待完成") from exc
        try:
            self.jobs.executor.submit(self._run, operation_id)
        except Exception as exc:
            self.db.execute("UPDATE perception_operations SET status='failed',error='执行器不可用，尚未发出请求',updated_at=? WHERE id=?", (utc_now(), operation_id))
            raise PerceptionOperationError("执行器不可用，尚未发出请求", 503) from exc
        return self.get(job_id, shot_id, operation_id)

    def cancel(self, job_id, shot_id, operation_id):
        self.get(job_id, shot_id, operation_id)
        # Running provider calls cannot be un-sent. Cancel stops publication, not billing.
        self.db.execute("UPDATE perception_operations SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,updated_at=? WHERE id=? AND status IN ('queued','running')", (utc_now(), operation_id))
        return self.get(job_id, shot_id, operation_id)

    @staticmethod
    def _frames(source):
        return [FrameInput(row["id"], row["timestamp"], row["path"], row["width"], row["height"], row["artifact_kind"], row["parent_frame_id"], tuple(_loads(row["bbox_json"], [])) or None) for row in source["frames"]]

    def _run(self, operation_id):
        row = self.db.fetchone("SELECT * FROM perception_operations WHERE id=?", (operation_id,))
        if row is None or row["status"] != "queued" or row["cancel_requested"]:
            return
        usage, details, state = [], {}, "failed"
        attempted = False
        usage_boundary = len(getattr(self.jobs.provider, "usage", []))
        new_frames = []
        try:
            with self.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM perception_operations WHERE id=?", (operation_id,)).fetchone()
                if row["status"] != "queued" or row["cancel_requested"]:
                    return
                current = self._source(conn, row["job_id"], row["shot_id"])
                if self._hash(current) != row["source_hash"]:
                    raise PerceptionOperationError("来源已变化，未发出付费请求")
                conn.execute("UPDATE perception_operations SET status='running',updated_at=? WHERE id=?", (utc_now(), operation_id))
            request = PerceptionOperationRequest.model_validate(_loads(row["request_json"]))
            source = self.db.media.snapshot(_loads(row["source_snapshot_json"]), reading=True)
            record = self._record(source)
            frames = self._frames(source)
            shot, job = source["shot"], source["job"]
            if not frames:
                raise ValueError("来源帧不可用，未发出付费请求")
            context = ShotContext(shot["id"], shot["number"], shot["start_time"], shot["end_time"], (job["script_text"] or "")[:3000],
                                  video_path=job["video_path"], artifact_dir=str(self.jobs.settings.data_dir / "jobs" / job["id"] / "evidence" / operation_id),
                                  source_edit_times=_loads(shot["source_edit_times_json"], []), register_frame=new_frames.append)
            def cancelled():
                current_op = self.db.fetchone("SELECT cancel_requested FROM perception_operations WHERE id=?", (operation_id,))
                return bool(current_op is None or current_op["cancel_requested"])
            from app.services.perception import PerceptionOrchestrator
            from app.services.perception_revision import review_observation
            long_source = bool(record.get("analysis_span_observations")) or shot["end_time"]-shot["start_time"] > 12
            if request.mode == "recover":
                if cancelled():
                    raise PerceptionOperationError("操作已取消，未发出新的请求")
                attempted = True
                if long_source:
                    from app.services.source_perception import analyze_source
                    prior = self.db.fetchall("SELECT request_json,result_json,status FROM perception_operations WHERE job_id=? AND shot_id=? AND mode='recover' AND id<>?", (job["id"], shot["id"], operation_id))
                    used = {stage for op in prior if op["status"] != "cancelled" or _loads(op["result_json"]).get("model_call_started")
                            for stage in _loads(op["request_json"]).get("stages", [])}
                    observed = analyze_source(self.jobs.provider, frames, context, seed_record=record,
                                              selected_stages=request.stages, cancelled=cancelled,
                                              only_unexecuted=bool(used & set(request.stages) and _has_unexecuted_spans(record)))
                else:
                    observed = PerceptionOrchestrator(self.jobs.provider, context, frames,
                        seed_record=record, selected_stages=request.stages, cancelled=cancelled).run()
            else:
                if long_source:
                    raise PerceptionOperationError("长来源镜头已按证据区间保存；整段复核不能覆盖区间事实，请先恢复待分析区间。原结果已保留，未发出付费请求")
                from app.services.visual_tools import LocalToolRegistry
                tools = LocalToolRegistry(context, frames)
                tools.call("sample_motion_peak_frames", {"count": 2})
                if cancelled():
                    raise PerceptionOperationError("操作已取消，未发出新的请求")
                attempted = True
                observed = review_observation(self.jobs.provider, context, tools.catalogue(), record,
                    request.field_group, request.reason, source_revision=row["source_revision"], cancelled=cancelled)
                observed.perception_record.tools.extend(tools.audit)
            saved = observed.perception_record
            if saved is None:
                raise ValueError("模型没有返回可审计的视觉记录，保留原值")
            attempted = len(saved.raw_responses) > len(record.get("raw_responses", []))
            details = {"record": saved.model_dump(mode="json"), "warnings": observed.analysis_warnings}
            previous_v1 = VisionObservation.model_validate(_loads(shot["observation_json"]))
            # Recovery may update its selected photography stage. Fact review must
            # never rewrite photography/style, which were outside its question.
            if request.mode == "review":
                for name in ("shot_size", "lens", "camera_movement", "lighting_vibe", "composition", "lighting_type", "lighting_detail"):
                    setattr(observed, name, getattr(previous_v1, name))
            observed.visual_style_evidence = previous_v1.visual_style_evidence
            before_v2 = _loads(shot["perception_json"]).get("observation", {})
            after_v2 = saved.observation.model_dump(mode="json")
            changed = any(before_v2.get(key) != after_v2.get(key) for key in ("facts", "dynamics", "photography", "artistic_effects"))
            state = "applied" if changed else "unchanged"
            if request.mode == "review" and saved.revisions and saved.revisions[-1].get("status") == "rejected":
                state = "rejected"
            with self.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                pending = conn.execute("SELECT * FROM perception_operations WHERE id=?", (operation_id,)).fetchone()
                current = self._source(conn, job["id"], shot["id"])
                if pending["cancel_requested"]:
                    state = "cancelled"
                elif self._hash(current) != row["source_hash"]:
                    state = "conflict"
                    details["warning"] = "来源、Prompt 或画风已被修改，未覆盖当前结果"
                else:
                    version = row["source_revision"] + int(changed or request.mode == "recover")
                    audit_version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM perception_revisions WHERE job_id=? AND shot_id=?", (job["id"], shot["id"])).fetchone()[0]
                    saved.source_revision = version
                    from app.perception_models import observation_review
                    quality = observation_review(saved.observation, saved)
                    saved.budgets["review"] = quality
                    observed.analysis_review = quality
                    details["record"] = saved.model_dump(mode="json")
                    # Even an unchanged recovery persists success/error and its
                    # audit. A revision is not a claim that facts improved.
                    for frame in new_frames:
                        conn.execute("INSERT INTO frames(id,job_id,shot_id,timestamp,path,thumbnail_path,width,height,selected,artifact_kind,parent_frame_id,bbox_json) VALUES(?,?,?,?,?,?,?,?,0,?,?,?)",
                            (frame.id, job["id"], shot["id"], frame.timestamp, self.db.media.store(frame.path), self.db.media.store(frame.path), frame.width, frame.height, frame.artifact_kind, frame.parent_frame_id, _json(frame.bbox) if frame.bbox else None))
                    conn.execute("INSERT INTO perception_revisions(job_id,shot_id,version,operation_id,before_json,after_json,changes_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (job["id"], shot["id"], audit_version, operation_id, _json(self.db.media.snapshot(source)), _json({"observation": observed.model_dump(mode="json"), "perception": saved.model_dump(mode="json")}), _json({"mode": request.mode, "stages": request.stages, "field_group": request.field_group, "changed": changed, "source_revision_before": row["source_revision"], "source_revision_after": version, "revisions": saved.revisions[len(record.get("revisions", [])):]}), utc_now()))
                    shot_status = "warning" if any(quality[scope]["status"] != "ready" for scope in ("static", "dynamic")) else "ready"
                    conn.execute("UPDATE shots SET observation_json=?,perception_json=?,perception_revision=?,status=? WHERE job_id=? AND id=?", (observed.model_dump_json(), saved.model_dump_json(), version, shot_status, job["id"], shot["id"]))
                    if changed:
                        self._render_prompts(conn, source, observed)
                        self._mark_sequences_stale(conn, source, operation_id, version)
                    details["revision"] = version
        except Exception as exc:
            state = "conflict" if isinstance(exc, PerceptionOperationError) else "failed"
            # Provider errors are already redacted. Do not save request headers.
            details["error"] = str(exc).splitlines()[0][:350]
        finally:
            drain = getattr(self.jobs.provider, "drain_usage", None)
            if drain and attempted:
                drained = drain()
                usage = drained[usage_boundary:]
                carry = drained[:usage_boundary]
                if carry and hasattr(self.jobs.provider, "usage"):
                    self.jobs.provider.usage[:0] = carry
            details["model_call_started"] = attempted
            with self.db.connect() as conn:
                final = conn.execute("SELECT status,cancel_requested FROM perception_operations WHERE id=?", (operation_id,)).fetchone()
                if final and final["cancel_requested"] and state not in ("applied", "unchanged", "rejected"):
                    state = "cancelled"
                conn.execute("UPDATE perception_operations SET status=?,result_json=?,usage_json=?,error=?,updated_at=? WHERE id=?", (state, _json(details), _json(usage), details.get("error"), utc_now(), operation_id))
                job_usage = conn.execute("SELECT model_usage_json FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
                if usage and job_usage:
                    conn.execute("UPDATE jobs SET model_usage_json=? WHERE id=?", (_json([*_loads(job_usage["model_usage_json"], []), *usage]), row["job_id"]))

    def _render_prompts(self, conn, source, observation):
        from app.services.prompt_service import build_prompts
        from app.style_direct.models import StyleProfileV3
        job, shot = source["job"], source["shot"]
        config = _loads(job["config_json"])
        strategy = config.get("style_strategy", "qwen-direct-v3")
        effective = None
        direct = _loads(job["style_direct_json"])
        baseline_contract = next((_loads(item["contract_json"]) for item in source["prompts"] if item["contract_json"]), {})
        if strategy == "qwen-direct-v3" and baseline_contract.get("observed_style"):
            # Source content revision is not permission to reclassify style.
            profile_version = str(baseline_contract.get("style_profile_version") or "").split(":")[-1]
            effective = StyleProfileV3(rendered_prompt=baseline_contract["observed_style"],
                profile_hash=baseline_contract.get("style_profile_hash") or "", version=int(profile_version) if profile_version.isdigit() else 1,
                scene_prompts={shot["scene_group_id"]: baseline_contract.get("delivery", {}).get("style_layers", {}).get("scene", "")} if shot["scene_group_id"] else {})
        elif strategy == "qwen-direct-v3" and direct.get("profile"):
            effective = StyleProfileV3.model_validate(direct["profile"])
        frame = next((item for item in source["frames"] if item["selected"]), source["frames"][0])
        divisor = gcd(frame["width"], frame["height"])
        aspect = f'{frame["width"] // divisor}:{frame["height"] // divisor}' if divisor else "待设定"
        old = {(item["prompt_type"], item["platform"]): item for item in source["prompts"]}
        legacy_template = next((item["template_id"] for item in source["prompts"]
            if item["template_id"] in {"script-first-frame-v1", "seedance-script-v1"}), None)
        for prompt in build_prompts(shot["id"], observation, (job["script_text"] or "")[:3000], source_edit_times=_loads(shot["source_edit_times_json"], []),
                generation_duration_seconds=shot["generation_duration_seconds"] or shot["duration"], aspect_ratio=aspect, style_strategy=strategy, direct_style=effective, scene_group_id=shot["scene_group_id"], template_id=legacy_template):
            prior = old.get((prompt.prompt_type, prompt.platform))
            if prior:
                prompt.id, prompt.version = prior["id"], prior["version"] + 1
                old_contract = _loads(prior["contract_json"])
                delivery = old_contract.get("delivery", {})
                baseline_key = "first_frame_prompt" if prior["prompt_type"] == "first_frame" else "seedance_video_prompt"
                warning = "视觉结论已修订，当前手动或历史 Prompt 已保留，请对照新证据检查"
                if delivery.get(baseline_key) != prior["positive_prompt"] or (effective is None and delivery.get("style_layers")):
                    conn.execute("UPDATE prompts SET status='warning',warnings_json=? WHERE id=?", (_json(list(dict.fromkeys([*_loads(prior["warnings_json"], []), warning]))), prior["id"]))
                    continue
                prompt.negative_prompt = prior["negative_prompt"]
                if prompt.contract:
                    for key in ("style_layers", "style_provenance"):
                        if key in delivery:
                            prompt.contract.delivery[key] = copy.deepcopy(delivery[key])
                # Preserve the exact previously selected style when no modern
                # baseline exists; legacy styles never silently reclassified.
            conn.execute("INSERT OR REPLACE INTO prompts(id,job_id,shot_id,prompt_type,platform,version,positive_prompt,negative_prompt,status,template_id,template_version,warnings_json,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (prompt.id, job["id"], shot["id"], prompt.prompt_type, prompt.platform, prompt.version, prompt.positive_prompt, prompt.negative_prompt, prompt.status, prompt.template_id, prompt.template_version, _json(prompt.warnings), prompt.contract.model_dump_json() if prompt.contract else None))

    @staticmethod
    def _mark_sequences_stale(conn, source, operation_id, version):
        payload = _loads(source["job"]["event_sequence_json"])
        affected = []
        for sequence in payload.get("generation_sequences", []):
            if source["shot"]["id"] in sequence.get("source_shot_ids", []):
                affected.append(sequence["id"])
                message = "来源视觉结论已修订，事件 Prompt 尚未重新编排；请显式刷新事件分析"
                sequence["status"] = "warning"
                sequence["warnings"] = list(dict.fromkeys([*sequence.get("warnings", []), message]))
                for name in ("first_frame_prompt", "seedance_prompt"):
                    if sequence.get(name):
                        sequence[name]["status"] = "warning"
                        sequence[name]["warnings"] = list(dict.fromkeys([*sequence[name].get("warnings", []), message]))
        if affected:
            metadata = payload.setdefault("sequence_metadata", {})
            metadata.setdefault("perception_revisions", []).append({"operation_id": operation_id, "shot_id": source["shot"]["id"], "revision": version, "sequence_ids": affected})
            metadata["source_refresh_needed"] = True
            # Preserve user-edited derived prompts. Stale marking is not a
            # rebuild and does not require/erase edits or send another request.
            old_version = int(metadata.get("version_number", 0))
            conn.execute("INSERT OR IGNORE INTO event_sequence_revisions(job_id,version,payload_json,created_at) VALUES(?,?,?,?)", (source["job"]["id"], old_version, source["job"]["event_sequence_json"], utc_now()))
            metadata["version_number"] = old_version + 1
            conn.execute("UPDATE jobs SET event_sequence_json=?,updated_at=? WHERE id=?", (_json(payload), utc_now(), source["job"]["id"]))
