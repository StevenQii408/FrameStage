import gc
import json
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from typing import Dict, Optional

from app.config import Settings
from app.db import Database, utc_now
from app.events import EventBus
from app.memory_guard import MemoryGuard, MemoryPressureError
from app.providers.base import FrameInput, ShotContext
from app.providers.bailian import BailianVisionProvider
from app.providers.mock import MockVisionProvider
from app.schemas import JobConfig, Shot, StoryboardResult, VisionObservation
from app.services.prompt_service import build_prompts
from app.services.generation_timing import ceil_half_seconds
from app.services.style_workflow import draft_job_style
from app.style_vector.workflow import auto_apply_visual_bible, draft_visual_bible
from app.style_direct.service import apply_style, draft_project_style, frame_quality
from app.style_direct.models import StyleProfileV3
from app.style_direct.inheritance import resolve_shot_style
from app.services.video_processor import DetectedShot, ExtractedFrame, RawDetectedShot, VideoProcessResult, assign_scene_groups, process_video


class JobService:
    def __init__(self, settings: Settings, db: Database, events: EventBus):
        self.settings = settings
        self.db = db
        self.events = events
        self.memory = MemoryGuard(settings.min_available_memory_gb, settings.target_available_memory_gb)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storyboard-worker")
        self.running_lock = threading.Lock()
        self.running_job: Optional[str] = None
        self._source_perception_budgets: Dict[str, Dict] = {}
        providers = {"mock": MockVisionProvider, "bailian": BailianVisionProvider}
        if settings.provider not in providers:
            raise ValueError(f"未知 Provider：{settings.provider}")
        self.provider = providers[settings.provider](settings)
        from app.services.perception_operations import PerceptionOperationService
        self.perception_operations = PerceptionOperationService(self)
        # Never leave an interrupted standalone analysis apparently running forever.
        self.db.execute("UPDATE style_analyses SET status='failed',stage='failed',error='后端已重启，分析未完成；请显式重新分析' WHERE status IN ('running','queued')")
        # A process-local queue cannot survive restart. Preserve checkpoints and
        # require an explicit resume instead of silently replaying paid work.
        with self.db.connect() as conn:
            interrupted = conn.execute("SELECT id,stage,config_json FROM jobs WHERE status IN ('queued','processing','running')").fetchall()
            for previous in interrupted:
                config = json.loads(previous["config_json"])
                config["interruption"] = {"stage": previous["stage"], "at": utc_now(),
                    "in_flight_cost_status": "unknown", "automatic_replay": False}
                conn.execute("UPDATE jobs SET status='interrupted',stage='interrupted',error=?,config_json=?,updated_at=? WHERE id=?",
                    ("后端已重启，任务已中断；已保存结果保留，请显式恢复。未完成请求的费用状态未知。",
                     json.dumps(config, ensure_ascii=False), utc_now(), previous["id"]))
        for previous in interrupted:
            self._emit(previous["id"], "job.interrupted", {"stage": "interrupted", "recoverable": True,
                "in_flight_cost_status": "unknown"})

    def _draft_style(self, job_id, strategy):
        if strategy == "qwen-direct-v3":
            return draft_project_style(self, job_id)
        return draft_visual_bible(self, job_id) if strategy == "vector-v2" else draft_job_style(self, job_id)

    def _apply_direct_style(self, job_id):
        current = self.status(job_id)
        state = json.loads(current["style_direct_json"] or "{}")
        if state.get("analysis_id"):
            try:
                apply_style(self, job_id, state["analysis_id"], automatic=True)
            except Exception as exc:
                self._emit(job_id, "style.direct.warning", {"detail": "已保留画风观察，自动应用未完成：" + str(exc)[:180]})

    def _inference_slot(self):
        return nullcontext() if self.settings.provider == "bailian" else self.memory.inference_slot()

    def _can_start_provider_inference(self, health: object) -> bool:
        if getattr(health, "provider", "") == "bailian":
            return True
        available = self.memory.snapshot().get("available_gb")
        if available is None:
            return True
        return available >= self.settings.min_available_memory_gb

    def config(self) -> JobConfig:
        return JobConfig(script_strategy=self.settings.script_strategy, max_video_duration_seconds=self.settings.max_video_duration_seconds, max_video_size_mb=self.settings.max_video_size_mb, max_shots=self.settings.max_shots, frames_per_shot=self.settings.frames_per_shot, max_frames_per_shot=self.settings.max_frames_per_shot, frame_max_long_edge=self.settings.frame_max_long_edge, jpeg_quality=self.settings.jpeg_quality, vlm_max_context_tokens=self.settings.vlm_max_context_tokens, vlm_max_new_tokens=self.settings.vlm_max_new_tokens, script_max_new_tokens=self.settings.script_max_new_tokens, vlm_timeout_seconds=self.settings.vlm_timeout_seconds, provider_concurrency=self.settings.provider_concurrency, segmentation_profile=self.settings.segmentation_profile, narrative_min_seconds=self.settings.narrative_min_seconds, narrative_max_seconds=self.settings.narrative_max_seconds, max_raw_shots_per_narrative=self.settings.max_raw_shots_per_narrative)

    def _record_capacity(self, job_id, result, config):
        from app.services.capacity import video_preflight
        capacity = video_preflight(result.metadata.get("duration") or result.shots[-1].end_time,
                                   config.max_shots, config.max_video_duration_seconds, result.shots)
        if not capacity["accepted"]:
            raise ValueError("；".join(capacity["reasons"]))
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT config_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            saved = json.loads(row["config_json"])
            saved["capacity_preflight"] = capacity
            conn.execute("UPDATE jobs SET config_json=? WHERE id=?", (json.dumps(saved, ensure_ascii=False), job_id))
        self._emit(job_id, "capacity.ready", capacity)

    @staticmethod
    def _shot_id(job_id: str, number: int) -> str:
        return f"{job_id}-shot-{number:03d}"

    @staticmethod
    def _frame_id(job_id: str, frame_id: str) -> str:
        return f"{job_id}-{frame_id}"

    def _insert_job(self, *, input_type: str, video_filename: Optional[str], script_text: str, video_path: Optional[Path], force_replan: bool = False, project_id: Optional[str] = None) -> str:
        # Fail early before moving a potentially large upload. The authoritative
        # check runs inside insert_job's SQLite write transaction.
        self.assert_capacity()
        from app.projects.access import active_project
        with self.db.connect() as conn:
            active_project(conn, project_id)
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        job_dir = self.settings.data_dir / "jobs" / job_id
        final_video = None
        job_dir.mkdir(parents=True)
        try:
            if video_path is not None and video_filename:
                final_video = job_dir / "input" / video_filename
                final_video.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(video_path), str(final_video))
            now = utc_now()
            provider = self.provider.health()
            config = {
                **self.config().model_dump(), "style_strategy": self.settings.style_strategy,
                "force_replan": force_replan, "source_grouping_policy": "raw-boundaries-v3",
                "event_grouping_policy": ("event-agent-v2" if input_type == "video"
                                          else "script-director-v3" if self.settings.script_strategy == "director-v3"
                                          else "legacy-pairs-v1"),
            }
            self.db.insert_job({
                "id": job_id, "status": "queued", "stage": "validating", "progress": 0,
                "video_filename": video_filename, "video_path": str(final_video) if final_video else None,
                "input_type": input_type, "script_text": script_text, "provider": provider.provider,
                "model": provider.model, "project_id": project_id, "config": config,
                "created_at": now, "updated_at": now,
            }, capacity=self.settings.max_pending_jobs)
        except Exception:
            if final_video is not None and final_video.exists() and not video_path.exists():
                shutil.move(str(final_video), str(video_path))
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        if input_type == "script":
            from app.services.capacity import script_preflight
            try:
                capacity = script_preflight(script_text, self.settings.max_shots)
                saved = json.loads(self.status(job_id)["config_json"])
                saved["capacity_preflight"] = capacity
                self.db.update_job(job_id, config_json=json.dumps(saved, ensure_ascii=False))
            except ValueError:
                pass  # The API rejects invalid length; legacy direct callers retain task error reporting.
        self._emit(job_id, "job.started", {"stage": "validating", "progress": 0})
        self.executor.submit(self._run, job_id)
        return job_id

    def assert_capacity(self):
        with self.db.connect() as conn:
            self.db.assert_job_capacity(conn, self.settings.max_pending_jobs)

    def create_job(self, video_filename: str, script_text: str, video_path: Path, *, force_replan: bool = False, project_id: Optional[str] = None) -> str:
        return self._insert_job(input_type="video", video_filename=video_filename, script_text=script_text, video_path=video_path, force_replan=force_replan, project_id=project_id)

    def create_script_job(self, script_text: str, *, project_id: Optional[str] = None) -> str:
        return self._insert_job(input_type="script", video_filename=None, script_text=script_text, video_path=None, project_id=project_id)

    def _emit(self, job_id: str, event_type: str, payload: Dict):
        event = self.db.add_event(job_id, event_type, payload)
        self.events.notify(job_id)
        return event

    def _is_cancelled(self, job_id: str) -> bool:
        row = self.db.fetchone("SELECT j.status,p.deleted_at FROM jobs j LEFT JOIN projects p ON p.id=j.project_id WHERE j.id = ?", (job_id,))
        return bool(row and (row["status"] == "cancelled" or row["deleted_at"]))

    def _set_stage(self, job_id: str, stage: str, progress: int):
        if self._is_cancelled(job_id):
            return
        from app.runtime import check_execution
        check_execution()
        self.db.update_job(job_id, stage=stage, progress=progress)
        self._emit(job_id, "job.stage_changed", {"stage": stage, "progress": progress})

    @staticmethod
    def _source_has_observations(observation, perception=None):
        from app.event_agent.engine import source_has_saved_observations
        saved = perception or observation.get("perception_record") or {}
        shot = Shot(id="quality-check", number=1, start_time=0, end_time=1, duration=1, time="",
            action=observation.get("subject_action", ""),
            action_details=observation.get("action_details", {}), visual_facts=observation.get("visual_facts", {}),
            visual_observation_v2=saved.get("observation"))
        return source_has_saved_observations(shot)

    @classmethod
    def _network_recovery_candidate(cls, saved):
        observation = json.loads(saved["observation_json"] or "{}")
        record = json.loads(saved["perception_json"] or "{}")
        budgets = record.get("budgets", {})
        return (not cls._source_has_observations(observation, record)
            and bool(budgets.get("network_failure") or cls._legacy_connection_failure(record))
            and not budgets.get("network_recovery_attempts"))

    @staticmethod
    def _legacy_connection_failure(record):
        """Compatibility with previously saved, explicitly typed cloud errors."""
        for raw in record.get("raw_responses", []):
            error = raw.get("error", "") if isinstance(raw, dict) else ""
            if isinstance(error, str) and error.startswith(("百炼网络请求失败:", "百炼网络请求失败：")):
                if "ConnectTimeout" in error:
                    return "connect_timeout"
                if "ConnectError" in error:
                    return "network_connection"
        return None

    def _restore_network_sources(self, row):
        """An explicitly resumed cloud failure reuses the exact saved source plan."""
        job_id = row["id"]
        shot_rows = self.db.fetchall("SELECT * FROM shots WHERE job_id=? ORDER BY number", (job_id,))
        if not shot_rows:
            raise ValueError("缺少已冻结的来源镜头，请重新提交素材")
        shots = [DetectedShot(s["number"], s["start_time"], s["end_time"],
            source_raw_numbers=[int(value.rsplit("-", 1)[-1]) for value in json.loads(s["source_raw_shot_ids_json"] or "[]")],
            scene_group_id=s["scene_group_id"], split_reason=s["cut_reason"] or "",
            source_edit_times=json.loads(s["source_edit_times_json"] or "[]"), scene_reason=s["scene_reason"] or "") for s in shot_rows]
        prefix = job_id + "-"
        frames = [ExtractedFrame(f["id"][len(prefix):] if f["id"].startswith(prefix) else f["id"],
            f"shot-{next(s['number'] for s in shot_rows if s['id'] == f['shot_id']):03d}",
            f["timestamp"], f["path"], f["thumbnail_path"], f["width"], f["height"])
            for f in self.db.fetchall("SELECT * FROM frames WHERE job_id=? AND selected=1 ORDER BY timestamp,id", (job_id,))]
        raw = [RawDetectedShot(r["number"], r["start_time"], r["end_time"],
            boundary_metrics=json.loads(r["boundary_metrics_json"]), decision=r["decision"], reason=r["reason"],
            narrative_number=int(r["narrative_shot_id"].rsplit("-", 1)[-1]) if r["narrative_shot_id"] else 0,
            generation_numbers=[int(value.rsplit("-", 1)[-1]) for value in json.loads(r["generation_shot_ids_json"] or "[]")])
            for r in self.db.fetchall("SELECT * FROM raw_shots WHERE job_id=? ORDER BY number", (job_id,))]
        from app.services.video_processor import probe_video
        metadata = {"duration": shots[-1].end_time}
        if frames:
            metadata.update(width=frames[0].width, height=frames[0].height)
        try:
            if row.get("video_path") and Path(row["video_path"]).is_file():
                metadata.update(probe_video(Path(row["video_path"])))
        except RuntimeError:
            # Already-validated legacy artifacts can outlive a decoder upgrade.
            # Their frame dimensions still preserve the original aspect ratio.
            pass
        return VideoProcessResult(metadata, shots, frames, [], raw,
            segmentation_profile=row["segmentation_profile"])

    def _mark_network_recoverable(self, job_id):
        current = self.status(job_id)
        if current["status"] == "cancelled":
            return
        config = json.loads(current["config_json"])
        config.update(recoverable_network_failure=True, network_resume_requested=False)
        self.db.update_job(job_id, config_json=json.dumps(config, ensure_ascii=False))

    def _evidence_frame_id(self, job_id, frame_id):
        # Restored artifacts can include pre-canonical IDs; keep their real ID.
        if self.db.fetchone("SELECT id FROM frames WHERE job_id=? AND id=?", (job_id, frame_id)):
            return frame_id
        return self._frame_id(job_id, frame_id)

    def _analyze_video_shot(self, job_id, row, detected, index, result, frames_by_shot,
                            job_config, job_dir, global_overview, style_strategy, baseline_style,
                            *, previous_record=None, saved_observation=None):
        """The same bounded perception/persistence path is used for a single recovery."""
        shot_id = self._shot_id(job_id, detected.number)
        with self._inference_slot():
            script = (row["script_text"] or "")[:3000]
            context = ShotContext(shot_id=shot_id, number=detected.number, start_time=detected.start_time, end_time=detected.end_time, script_text=script, global_style=global_overview)
            context.video_path = row["video_path"]
            context.source_edit_times = list(detected.source_edit_times)
            def report_progress(phase, state):
                labels = {'shot.static': '静态场景识别', 'shot.motion': '动态动作分析',
                          'shot.static.correction': '静态字段格式纠正', 'shot.motion.correction': '动态字段格式纠正',
                          'shot.supplement': '视觉证据补充分析'}
                labels['shot.span'] = '长镜头分区分析'
                self._emit(job_id, 'perception.stage', {'shot_id': shot_id, 'phase': phase,
                    'state': state, 'message': f"分镜 {detected.number} · {labels.get(phase, phase)}"})
            context.report_perception_progress = report_progress
            context.artifact_dir = str(job_dir / "evidence" / shot_id)
            for neighbor_index in (index - 1, index + 1):
                if 0 <= neighbor_index < len(result.shots):
                    neighbor_id = self._shot_id(job_id, result.shots[neighbor_index].number)
                    neighbors = frames_by_shot.get(neighbor_id, [])
                    if neighbors:
                        neighbor = neighbors[-1] if neighbor_index < index else neighbors[0]
                        context.neighbor_frames.append(FrameInput(
                            self._evidence_frame_id(job_id, neighbor.id), neighbor.timestamp, neighbor.path,
                            neighbor.width, neighbor.height,
                        ))
            def register_evidence(frame):
                self.db.execute(
                    "INSERT INTO frames (id,job_id,shot_id,timestamp,path,thumbnail_path,width,height,selected,artifact_kind,parent_frame_id,bbox_json) VALUES (?,?,?,?,?,?,?,?,0,?,?,?)",
                    (frame.id, job_id, shot_id, frame.timestamp, self.db.media.store(frame.path), self.db.media.store(frame.path),
                     frame.width, frame.height, frame.artifact_kind, frame.parent_frame_id,
                     json.dumps(frame.bbox) if frame.bbox else None),
                )
                self._emit(job_id, "frame.evidence_created", {"frame_id": frame.id, "shot_id": shot_id, "timestamp": frame.timestamp})
            context.register_frame = register_evidence
            def checkpoint_perception(record):
                if self._is_cancelled(job_id):
                    return
                from app.perception_models import observation_review, project_v1
                partial = project_v1(record.observation, partial_failure=True)
                partial.analysis_review = observation_review(record.observation, record)
                partial.analysis_span_observations = record.analysis_span_observations
                self.db.execute("UPDATE shots SET perception_json=?,observation_json=? WHERE job_id=? AND id=?",
                    (record.model_dump_json(), partial.model_dump_json(), job_id, shot_id))
            context.checkpoint_perception = checkpoint_perception
            provider_frames = [FrameInput(self._evidence_frame_id(job_id, f.id), f.timestamp, f.path, f.width, f.height) for f in frames_by_shot.get(shot_id, [])[: job_config.max_frames_per_shot]]
            if saved_observation is not None:
                observation = VisionObservation.model_validate({"shot_size": "未知", "lens": "未知",
                    "camera_movement": "未知", "lighting_vibe": "未知", **saved_observation})
            elif detected.duration > 12 and self.settings.provider == 'bailian':
                from app.services.source_perception import analyze_source
                observation = analyze_source(self.provider, provider_frames, context,
                    shared_budget=self._source_perception_budgets.setdefault(job_id, {}),
                    seed_record=json.loads(previous_record) if previous_record else None,
                    cancelled=lambda: self._is_cancelled(job_id))
            elif previous_record and self.settings.provider == "bailian":
                from app.services.perception import PerceptionOrchestrator, recoverable_stages
                seed = json.loads(previous_record)
                selected = recoverable_stages(seed)
                if not selected:
                    from app.perception_models import PerceptionRecord, project_v1
                    record = PerceptionRecord.model_validate(seed)
                    observation = project_v1(record.observation)
                    observation.perception_record = record
                else:
                    observation = PerceptionOrchestrator(self.provider, context, provider_frames,
                        seed_record=seed, selected_stages=selected).run()
            else:
                observation = self.provider.analyze_shot(provider_frames, context)
        if self._is_cancelled(job_id):
            return
        if detected.duration < 0.8:
            observation.analysis_warnings.append("源片镜头短于 0.8 秒，建议复核生成时长与动作可读性")
        effective_style = None
        if style_strategy == 'qwen-direct-v3':
            try:
                effective_style = resolve_shot_style(baseline_style, observation, shot_id)
            except Exception as exc:
                # Style is optional enrichment, never a reason to lose successful perception.
                observation.analysis_warnings.append('本镜画风转换未完成，动作与摄影结果已保留')
                self._emit(job_id, 'style.direct.warning', {'shot_id': shot_id,
                    'detail': '本镜画风转换失败，保留已识别内容：' + str(exc).splitlines()[0][:180]})
        record = observation.perception_record
        if record:
            from app.perception_models import observation_review
            observation.analysis_review = observation_review(record.observation, record)
        review = observation.analysis_review
        shot_status = ('warning' if any(review.get(scope, {}).get('status') == 'pending_review'
                                       for scope in ('static', 'dynamic')) else 'ready') if review else ('warning' if observation.analysis_warnings else 'ready')
        if previous_record:
            previous = json.loads(previous_record)
            if record:
                if self.settings.provider != "bailian":
                    record.raw_responses[:0] = previous.get("raw_responses", [])
                    record.trace[:0] = [*previous.get("trace", []), {"state": "network_recovery", "attempt": 1}]
                    record.tools[:0] = previous.get("tools", [])
                    record.elapsed_seconds += previous.get("elapsed_seconds", 0)
                else:
                    record.trace.append({"state": "network_recovery", "attempt": 1})
                record.budgets["network_recovery_attempts"] = 1
                record.budgets["previous_network_failure"] = previous.get("budgets", {})
        usable = self._source_has_observations(observation.model_dump(), record.model_dump() if record else None)
        if not usable:
            shot_status = "warning"
        if saved_observation is None:
            self.db.execute(
                "UPDATE shots SET status = ?, observation_json = ?, perception_json = ? WHERE id = ?",
                (shot_status, json.dumps(observation.model_dump(), ensure_ascii=False),
                 record.model_dump_json() if record else None, shot_id),
            )
        from math import gcd
        cover = provider_frames[0] if provider_frames else None
        divisor = gcd(cover.width, cover.height) if cover and cover.width and cover.height else 0
        aspect_ratio = f"{cover.width // divisor}:{cover.height // divisor}" if divisor else "待设定"
        prompts = build_prompts(shot_id, observation, (row["script_text"] or "")[:3000], source_edit_times=detected.source_edit_times, generation_duration_seconds=ceil_half_seconds(detected.duration), aspect_ratio=aspect_ratio, style_strategy=style_strategy, direct_style=effective_style, scene_group_id=detected.scene_group_id)
        for prompt in prompts:
            if saved_observation is not None and self.db.fetchone("SELECT id FROM prompts WHERE id=?", (prompt.id,)):
                continue
            prompt.warnings = list(dict.fromkeys([*prompt.warnings, *observation.analysis_warnings]))
            self.db.execute("INSERT OR REPLACE INTO prompts (id,job_id,shot_id,prompt_type,platform,version,positive_prompt,negative_prompt,status,template_id,template_version,warnings_json,contract_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (prompt.id, job_id, prompt.shot_id, prompt.prompt_type, prompt.platform, prompt.version, prompt.positive_prompt, prompt.negative_prompt, prompt.status, prompt.template_id, prompt.template_version, json.dumps(prompt.warnings, ensure_ascii=False), json.dumps(prompt.contract.model_dump(), ensure_ascii=False) if prompt.contract else None))
        self._emit(job_id, "shot.analyzed", {"shot_id": shot_id, "confidence": observation.confidence, "status": shot_status, "analysis_warnings": observation.analysis_warnings,
            "analysis_review": review, "observation_available": usable, "recoverable": bool(record and record.budgets.get("network_failure") and not usable)})
        self._emit(job_id, "prompt.ready", {"shot_id": shot_id, "prompt_ids": [p.id for p in prompts],
            "style_profile_hash": effective_style.profile_hash if effective_style else '',
            "style_resolution_status": effective_style.provenance.get('resolution_status') if effective_style else 'unknown',
            "style_resolution": effective_style.provenance.get('resolution') if effective_style else 'unknown'})
        return usable

    def _build_event_sequences(self, job_id: str, *, allow_agent=True, warning="") -> None:
        from app.services.sequence_orchestration import build_event_sequences
        return build_event_sequences(self, job_id, allow_agent=allow_agent, warning=warning)

    def _emit_sequence_ready(self, job_id, payload):
        from app.services.sequence_orchestration import emit_sequence_ready
        return emit_sequence_ready(self, job_id, payload)

    def _stabilize_video_plan(self, row, result, job_config, provider_health, job_dir, *, semantic=True):
        from app.services.source_planning import stabilize_video_plan
        return stabilize_video_plan(self, row, result, job_config, provider_health, job_dir, semantic=semantic)

    def _run(self, job_id: str):
        from app.runtime import ExecutionContext, execution_scope
        # A request budget includes failed attempts. Window budgets remain
        # stricter; this outer bound also covers accidental cross-stage loops.
        context = ExecutionContext(run_id=job_id,
            cancelled=lambda: self._is_cancelled(job_id),
            deadline=time.monotonic() + self.settings.job_timeout_seconds,
            max_model_calls=self.settings.job_max_model_calls,
            on_event=lambda name, payload: self._emit(job_id, name, payload))
        with execution_scope(context):
            return self._execute_job(job_id)

    def _execute_job(self, job_id: str):
        with self.running_lock:
            self.running_job = job_id
        try:
            row = self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
            if not row:
                return
            row = dict(row)
            if self._is_cancelled(job_id):
                return
            job_config = JobConfig(**json.loads(row["config_json"]))
            provider_health = self.provider.health()
            if row["input_type"] == "video" and not provider_health.ready:
                raise RuntimeError(provider_health.detail or "视觉 Provider 未就绪")
            if row["input_type"] == "script":
                self._run_script(row, job_config)
                return
            job_dir = self.settings.data_dir / "jobs" / job_id
            restart_resume = bool(json.loads(row["config_json"]).get("restart_resume_requested"))
            network_resume = bool(json.loads(row["config_json"]).get("network_resume_requested"))
            if restart_resume:
                # Only restore a fully persisted source catalog. A crash during
                # local extraction can be rebuilt locally using its frozen plan.
                frozen = json.loads(row.get("generation_plan_json") or "{}")
                saved = self.db.fetchall("SELECT id FROM shots WHERE job_id=?", (job_id,))
                frame_rows = self.db.fetchall("SELECT shot_id,path FROM frames WHERE job_id=? AND selected=1", (job_id,))
                network_resume = bool(saved and len(saved) == len(frozen.get("shots", []))
                    and all(any(f["shot_id"] == s["id"] and Path(f["path"]).is_file() for f in frame_rows) for s in saved))
            self._set_stage(job_id, "probing", 5)
            result = self._restore_network_sources(row) if network_resume else process_video(Path(row["video_path"]), job_dir, job_config)
            if result.raw_shots and not network_resume:
                semantic = provider_health.provider == "bailian" and len(result.raw_shots) > 1 and self._can_start_provider_inference(provider_health)
                if semantic:
                    self._set_stage(job_id, "grouping", 18)
                self._stabilize_video_plan(dict(row), result, job_config, provider_health, job_dir, semantic=semantic)
            if self._is_cancelled(job_id):
                return
            self._record_capacity(job_id, result, job_config)
            self.db.update_job(job_id, status="processing", stage="detecting", progress=22, segmentation_profile=result.segmentation_profile, raw_shot_count=len(result.raw_shots), narrative_shot_count=len(result.shots))
            self._emit(job_id, "source.ready", {"input_type": "video", "duration": result.metadata.get("duration", 0), "shot_count": len(result.shots), "raw_shot_count": len(result.raw_shots), "narrative_shot_count": len(result.shots), "segmentation_profile": result.segmentation_profile})
            for warning in result.warnings:
                self._emit(job_id, "job.warning", {"message": warning})
            for raw in ([] if network_resume else result.raw_shots):
                raw_id = f"{job_id}-raw-{raw.number:03d}"
                narrative_id = self._shot_id(job_id, raw.narrative_number) if raw.narrative_number else None
                self.db.execute(
                    "INSERT OR REPLACE INTO raw_shots (id,job_id,number,start_time,end_time,duration,boundary_metrics_json,decision,reason,narrative_shot_id,generation_shot_ids_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (raw_id, job_id, raw.number, raw.start_time, raw.end_time, raw.duration, json.dumps(raw.boundary_metrics, ensure_ascii=False), raw.decision, raw.reason, narrative_id, json.dumps([self._shot_id(job_id, number) for number in raw.generation_numbers])),
                )
            for detected in ([] if network_resume else result.shots):
                shot_id = self._shot_id(job_id, detected.number)
                self.db.execute(
                    """INSERT INTO shots (id,job_id,number,start_time,end_time,duration,time,status,source_raw_shot_ids_json,scene_group_id,cut_reason,source_edit_times_json,generation_duration_seconds,scene_reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         start_time=excluded.start_time,
                         end_time=excluded.end_time,
                         duration=excluded.duration,
                         time=excluded.time,
                         source_raw_shot_ids_json=excluded.source_raw_shot_ids_json,
                         scene_group_id=excluded.scene_group_id,
                         cut_reason=excluded.cut_reason,
                         source_edit_times_json=excluded.source_edit_times_json,
                         generation_duration_seconds=excluded.generation_duration_seconds,
                         scene_reason=excluded.scene_reason""",
                    (shot_id, job_id, detected.number, detected.start_time, detected.end_time, detected.duration,
                     f"{detected.start_time:06.2f} — {detected.end_time:06.2f}", "queued",
                     json.dumps([f"{job_id}-raw-{number:03d}" for number in detected.source_raw_numbers]),
                     detected.scene_group_id, detected.split_reason, json.dumps(detected.source_edit_times),
                     ceil_half_seconds(detected.duration), detected.scene_reason),
                )
                self._emit(job_id, "shot.created", {"shot_id": shot_id, "number": detected.number, "start_time": detected.start_time, "end_time": detected.end_time, "source_raw_shot_ids": detected.source_raw_numbers, "scene_group_id": detected.scene_group_id, "cut_reason": detected.split_reason, "source_edit_times": detected.source_edit_times})
            for frame in ([] if network_resume else result.frames):
                number = int(frame.shot_id.rsplit("-", 1)[-1])
                shot_id = self._shot_id(job_id, number)
                frame_id = self._frame_id(job_id, frame.id)
                try:
                    sharpness, brightness = frame_quality(frame.path)
                except Exception:
                    sharpness, brightness = None, None
                self.db.execute("INSERT OR REPLACE INTO frames (id,job_id,shot_id,timestamp,path,thumbnail_path,width,height,blur_score,brightness_score,selected) VALUES (?,?,?,?,?,?,?,?,?,?,1)", (frame_id, job_id, shot_id, frame.timestamp, self.db.media.store(frame.path), self.db.media.store(frame.thumbnail_path), frame.width, frame.height, sharpness, brightness))
                self._emit(job_id, "frame.created", {"frame_id": frame_id, "shot_id": shot_id, "timestamp": frame.timestamp})
            style_strategy = json.loads(row['config_json']).get('style_strategy', 'legacy-v1')
            baseline_style = None
            if style_strategy == 'qwen-direct-v3':
                self._set_stage(job_id, 'styling:preflight', 24)
                try:
                    saved_style = json.loads(row["style_direct_json"] or "{}").get("profile")
                    early = None if (network_resume or restart_resume) and saved_style else self._draft_style(job_id, style_strategy)
                    payload = (early or {}).get('result', {}).get('profile') or saved_style
                    if payload:
                        baseline_style = StyleProfileV3.model_validate(payload)
                    if not baseline_style or not baseline_style.descriptions:
                        self._emit(job_id, 'job.warning', {'message': '项目画风分析未完成；拆镜与动作分析继续，本镜基础观察不代表已确定具体风格'})
                except Exception as exc:
                    self._emit(job_id, 'job.warning', {'message': '基础画风未完成，各镜将使用自身可见证据：' + str(exc)[:180]})
                if self._is_cancelled(job_id):
                    return
            self._set_stage(job_id, "analyzing", 30)
            frames_by_shot: Dict[str, list] = {}
            for frame in result.frames:
                number = int(frame.shot_id.rsplit("-", 1)[-1])
                frames_by_shot.setdefault(self._shot_id(job_id, number), []).append(frame)
            global_overview = f"本片包含 {len(result.shots)} 个叙事分镜，由 {len(result.raw_shots)} 个原始切段合并"
            try:
                representatives = []
                for detected in result.shots:
                    shot_frames = frames_by_shot.get(self._shot_id(job_id, detected.number), [])
                    if shot_frames:
                        frame = shot_frames[len(shot_frames) // 2]
                        representatives.append(FrameInput(self._frame_id(job_id, frame.id), frame.timestamp, frame.path))
                if baseline_style and baseline_style.descriptions:
                    global_overview = json.dumps({'visual_style': baseline_style.rendered_prompt,
                        'style_policy': '仅作生成参考，逐镜画面不同则以本镜证据为准'}, ensure_ascii=False)
                elif not network_resume and style_strategy != 'qwen-direct-v3' and representatives and self._can_start_provider_inference(provider_health):
                    with self._inference_slot():
                        overview = self.provider.summarize_video(representatives)
                    if overview:
                        global_overview = overview
                        self._emit(job_id, "source.overview_ready", {"overview": overview})
            except Exception as exc:
                self._emit(job_id, "job.warning", {"message": f"全片视觉概览失败，继续逐镜分析：{exc}"})
            total = max(len(result.shots), 1)
            successful_shots = 0
            failed_shots = 0
            consecutive_connection_failures = 0
            sources_changed = False
            for index, detected in enumerate(result.shots):
                if self._is_cancelled(job_id):
                    return
                shot_id = self._shot_id(job_id, detected.number)
                previous_record = None
                saved_observation = None
                existing = self.db.fetchone("SELECT status, observation_json, perception_json FROM shots WHERE id = ?", (shot_id,))
                if existing and existing["status"] in ("ready", "warning") and existing["observation_json"]:
                    usable = self._source_has_observations(json.loads(existing["observation_json"]), json.loads(existing["perception_json"] or "{}"))
                    from app.services.perception import recoverable_stages
                    unfinished_stage = restart_resume and existing["perception_json"] and recoverable_stages(json.loads(existing["perception_json"]))
                    prompt_count = self.db.fetchone("SELECT COUNT(*) AS n FROM prompts WHERE job_id=? AND shot_id=?", (job_id, shot_id))["n"]
                    if restart_resume and usable and not unfinished_stage and prompt_count < 2:
                        saved_observation = json.loads(existing["observation_json"])
                    elif (usable and not unfinished_stage) or (not network_resume and not restart_resume):
                        successful_shots += bool(usable)
                        failed_shots += not usable
                        if usable:
                            consecutive_connection_failures = 0
                        self._set_stage(job_id, "analyzing", int(30 + ((index + 1) / total) * 55))
                        continue
                    previous_record = existing["perception_json"]
                if restart_resume and existing and existing["perception_json"] and not previous_record and saved_observation is None:
                    from app.services.perception import recoverable_stages
                    from app.perception_models import PerceptionRecord
                    saved_record = PerceptionRecord.model_validate_json(existing["perception_json"])
                    if saved_record.normalized_stages:
                        previous_record = existing["perception_json"]
                    else:
                        # Completed unknown observations are not failed calls.
                        failed_shots += 1
                        continue
                if not self._can_start_provider_inference(provider_health):
                    message = "预计加载模型后可用内存低于安全阈值"
                    self.db.update_job(job_id, status="paused_memory_pressure", stage="analyzing", progress=int(30 + index / total * 55), error=message)
                    self._emit(job_id, "job.paused_memory_pressure", {"message": message})
                    return
                try:
                    sources_changed = sources_changed or saved_observation is None
                    usable = self._analyze_video_shot(job_id, row, detected, index, result, frames_by_shot,
                        job_config, job_dir, global_overview, style_strategy, baseline_style,
                        previous_record=previous_record, saved_observation=saved_observation)
                    if self._is_cancelled(job_id):
                        return
                    successful_shots += bool(usable)
                    failed_shots += not usable
                except MemoryPressureError as exc:
                    self.db.update_job(job_id, status="paused_memory_pressure", stage="analyzing", progress=int(30 + index / total * 55), error=str(exc))
                    self._emit(job_id, "job.paused_memory_pressure", {"message": str(exc)})
                    return
                except Exception as exc:
                    message = str(exc) or "镜头视觉分析失败"
                    from app.providers.cloud_errors import CloudTransportError
                    if isinstance(exc, CloudTransportError):
                        from app.perception_models import PerceptionRecord, merge_stages, project_v1
                        v2 = merge_stages(None, None)
                        observation = project_v1(v2, partial_failure=True)
                        record = PerceptionRecord(observation=v2,
                            raw_responses=[{"stage": "transport", "error": message, "category": exc.category}],
                            trace=[{"state": "transport_failed", "category": exc.category}],
                            budgets={"network_failure": True, "transport_error_category": exc.category})
                        if previous_record:
                            previous = json.loads(previous_record)
                            record.raw_responses[:0] = previous.get("raw_responses", [])
                            record.trace[:0] = previous.get("trace", [])
                            record.budgets["network_recovery_attempts"] = 1
                        self.db.execute("UPDATE shots SET observation_json=?,perception_json=? WHERE id=?",
                            (observation.model_dump_json(), record.model_dump_json(), shot_id))
                    self.db.execute("UPDATE shots SET status = 'failed' WHERE id = ?", (shot_id,))
                    self._emit(job_id, "shot.failed", {"shot_id": shot_id, "message": message})
                    self._emit(job_id, "job.warning", {"message": f"分镜 {detected.number:02d} 分析失败：{message}"})
                    failed_shots += 1
                finally:
                    gc.collect()
                saved = self.db.fetchone("SELECT observation_json,perception_json FROM shots WHERE id=?", (shot_id,))
                budget = json.loads(saved["perception_json"] or "{}").get("budgets", {}) if saved else {}
                connection_failed = (provider_health.provider == "bailian" and budget.get("network_failure")
                    and budget.get("transport_error_category") in ("connect_timeout", "network_connection")
                    and not self._source_has_observations(json.loads(saved["observation_json"] or "{}"), json.loads(saved["perception_json"] or "{}")))
                consecutive_connection_failures = consecutive_connection_failures + 1 if connection_failed else 0
                if consecutive_connection_failures >= 2:
                    if self._is_cancelled(job_id):
                        return
                    message = "连续两个镜头云端连接失败，已停止后续付费分析；已保留帧与取证审计。请修复网络连接后显式恢复任务。"
                    for remaining in result.shots[index + 1:]:
                        remaining_id = self._shot_id(job_id, remaining.number)
                        current = self.db.fetchone("SELECT observation_json,perception_json FROM shots WHERE id=?", (remaining_id,))
                        if current and not self._source_has_observations(json.loads(current["observation_json"] or "{}"), json.loads(current["perception_json"] or "{}")):
                            self.db.execute("UPDATE shots SET status='failed' WHERE id=?", (remaining_id,))
                            self._emit(job_id, "shot.failed", {"shot_id": remaining_id, "message": message, "recoverable": True})
                    self._mark_network_recoverable(job_id)
                    self._build_event_sequences(job_id, allow_agent=False, warning=message)
                    self.db.update_job(job_id, status="failed", stage="failed", error=message)
                    self._emit(job_id, "job.failed", {"message": message, "recoverable": True})
                    return
                self._set_stage(job_id, "analyzing", int(30 + ((index + 1) / total) * 55))
            # A recovered connection is permission for one bounded retry of only
            # its network-only unknown sources, never a replay of the whole film.
            if successful_shots and json.loads(row["config_json"]).get("event_grouping_policy") == "event-agent-v2":
                for index, detected in enumerate(result.shots):
                    if self._is_cancelled(job_id):
                        return
                    shot_id = self._shot_id(job_id, detected.number)
                    saved = self.db.fetchone("SELECT observation_json,perception_json FROM shots WHERE id=?", (shot_id,))
                    if not saved or not self._network_recovery_candidate(saved):
                        continue
                    if not self._can_start_provider_inference(provider_health):
                        break
                    previous_record = saved["perception_json"]
                    previous = json.loads(previous_record)
                    previous.setdefault("budgets", {})["network_recovery_attempts"] = 1
                    self.db.execute("UPDATE shots SET perception_json=? WHERE id=?", (json.dumps(previous, ensure_ascii=False), shot_id))
                    self._emit(job_id, "shot.network_recovery_started", {"shot_id": shot_id, "attempt": 1,
                        "transport_error_category": previous["budgets"].get("transport_error_category")})
                    try:
                        usable = self._analyze_video_shot(job_id, row, detected, index, result, frames_by_shot,
                            job_config, job_dir, global_overview, style_strategy, baseline_style,
                            previous_record=previous_record)
                        if self._is_cancelled(job_id):
                            return
                        successful_shots += bool(usable)
                        failed_shots -= bool(usable)
                        self._emit(job_id, "shot.network_recovery_finished", {"shot_id": shot_id, "attempt": 1,
                            "observation_available": bool(usable)})
                    except MemoryPressureError as exc:
                        self.db.update_job(job_id, status="paused_memory_pressure", stage="analyzing", error=str(exc))
                        self._emit(job_id, "job.paused_memory_pressure", {"message": str(exc)})
                        return
                    except Exception as exc:
                        # The first attempt's frames, unknown observation and
                        # failure audit stay intact even when recovery throws.
                        self._emit(job_id, "job.warning", {"message": f"分镜 {detected.number:02d} 单次网络恢复未完成，已保留原取证记录：{str(exc)[:180]}"})
                    finally:
                        gc.collect()
            if failed_shots and successful_shots == 0:
                self._build_event_sequences(job_id)
                unknown = self.db.fetchone("SELECT 1 FROM shots WHERE job_id=? AND status IN ('ready','warning') LIMIT 1", (job_id,))
                message = "所有分镜视觉分析均失败或没有可追溯的视觉观察" if unknown else "所有分镜视觉分析均失败"
                self.db.update_job(job_id, status="failed", stage="failed", error=message)
                self._emit(job_id, "job.failed", {"message": message})
                return
            use_event_agent = json.loads(row["config_json"]).get("event_grouping_policy") == "event-agent-v2"
            if not use_event_agent and provider_health.provider == "bailian" and len(result.shots) > 1 and successful_shots > 1:
                try:
                    montage_shots = []
                    montage_frames = {}
                    for detected in result.shots:
                        shot_id = self._shot_id(job_id, detected.number)
                        current = self.db.fetchone("SELECT status,observation_json FROM shots WHERE id = ?", (shot_id,))
                        montage_shots.append({
                            "id": shot_id, "number": detected.number,
                            "start_time": detected.start_time, "end_time": detected.end_time,
                            "status": current["status"] if current else "failed",
                            "observation": json.loads(current["observation_json"] or "{}") if current else {},
                        })
                        montage_frames[shot_id] = [
                            FrameInput(self._frame_id(job_id, frame.id), frame.timestamp, frame.path)
                            for frame in frames_by_shot.get(shot_id, [])
                        ]
                    montage = self.provider.analyze_montage(montage_shots, montage_frames)
                    starts = montage["scene_starts"]
                    if not starts or starts[0] != 1:
                        raise ValueError("跨镜场次缺少首镜")
                    assign_scene_groups(result.shots, result.raw_shots, semantic_starts=starts)
                    for detected in result.shots:
                        self.db.execute("UPDATE shots SET scene_group_id = ?, scene_reason = ? WHERE id = ?",
                                        (detected.scene_group_id, detected.scene_reason, self._shot_id(job_id, detected.number)))
                    self.db.update_job(job_id, montage_json=json.dumps(montage, ensure_ascii=False))
                    if montage.get('rejected_pairs'):
                        self._emit(job_id, 'job.warning', {'message': f"跨镜分析部分完成：{len(montage['rejected_pairs'])} 个边界未通过校验；有效关系已保存，失败边界不合并"})
                    self._emit(job_id, "scene.regrouped", {"scene_count": len(set(s.scene_group_id for s in result.shots))})
                except Exception as exc:
                    self.db.update_job(job_id, montage_json=json.dumps({'version': 'montage-relations.v3',
                        'status': 'failed', 'relations': [], 'raw_responses': [],
                        'rejected_pairs': [[a['id'], b['id']] for a, b in zip(montage_shots, montage_shots[1:])],
                        'validation_audit': [{'status': 'failed', 'error': str(exc)[:300]}]}, ensure_ascii=False))
                    self._emit(job_id, "job.warning", {"message": f"跨镜关系或真实场次分析失败，保留视觉保守分组并建议复核：{exc}"})
            # A committed event projection is a completed paid stage. Reuse it
            # on explicit restart recovery when no source observation changed.
            if not (restart_resume and not sources_changed and row.get("event_sequence_json")
                    and json.loads(row["event_sequence_json"]).get("generation_sequences")):
                self._build_event_sequences(job_id)
            if self._is_cancelled(job_id):
                return
            self._set_stage(job_id, "finalizing", 95)
            if style_strategy != 'qwen-direct-v3' and not network_resume:
                try:
                    self._draft_style(job_id, style_strategy)
                except Exception as exc:
                    self._emit(job_id, "job.warning", {"message": f"全局画风草案未生成，逐镜分析与 Prompt 已保留：{exc}"})
            self.db.update_job(job_id, status="completed", stage="completed", progress=100, error=None)
            if json.loads(row["config_json"]).get("style_strategy") == "vector-v2":
                auto_apply_visual_bible(self, job_id)
            self._emit(job_id, "job.completed", {"progress": 100})
        except Exception as exc:
            if self._is_cancelled(job_id):
                return
            self.db.update_job(job_id, status="failed", stage="failed", error=str(exc))
            self._emit(job_id, "job.failed", {"message": str(exc)})
        finally:
            self._source_perception_budgets.pop(job_id, None)
            try:
                drain = getattr(self.provider, "drain_usage", None)
                if callable(drain):
                    records = drain()
                    if records:
                        old = self.db.fetchone("SELECT model_usage_json FROM jobs WHERE id = ?", (job_id,))
                        previous = json.loads(old["model_usage_json"] or "[]") if old else []
                        self.db.update_job(job_id, model_usage_json=json.dumps([*previous, *records], ensure_ascii=False))
            finally:
                with self.running_lock:
                    self.running_job = None

    def _run_script(self, row, job_config: JobConfig) -> None:
        if json.loads(row["config_json"]).get("script_strategy") == "director-v3":
            from app.script_director.service import run_script_director
            return run_script_director(self, row, job_config)
        from app.services.legacy_script import run_legacy_script
        return run_legacy_script(self, row, job_config)

    def resume(self, job_id: str) -> bool:
        health = self.provider.health()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status, provider, model, config_json, error, project_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return False
            config = json.loads(row["config_json"])
            error = row["error"]
            network = (row["status"] == "failed" and config.get("recoverable_network_failure")
                and (error or "").startswith("连续两个镜头云端连接失败"))
            interrupted = row["status"] == "interrupted"
            if row["status"] != "paused_memory_pressure" and not network and not interrupted:
                return False
            if not health.ready or row["provider"] != health.provider or row["model"] != health.model:
                return False
            from app.projects.access import active_project
            active_project(conn, row["project_id"])
            self.db.assert_job_capacity(conn, self.settings.max_pending_jobs)
            if network:
                config.update(network_resume_requested=True, recoverable_network_failure=False,
                    network_resume_count=int(config.get("network_resume_count", 0)) + 1)
            if interrupted:
                config.update(restart_resume_requested=True,
                    restart_resume_count=int(config.get("restart_resume_count", 0)) + 1)
            conn.execute("UPDATE jobs SET status='queued',stage='resuming',error=NULL,config_json=?,updated_at=? WHERE id=?",
                (json.dumps(config, ensure_ascii=False), utc_now(), job_id))
        self.executor.submit(self._run, job_id)
        return True

    def cancel(self, job_id: str) -> bool:
        row = self.db.fetchone("SELECT status FROM jobs WHERE id = ?", (job_id,))
        if not row or row["status"] in ("completed", "failed", "cancelled"):
            return False
        self.db.update_job(job_id, status="cancelled", stage="cancelled")
        self._emit(job_id, "job.cancelled", {})
        return True

    def status(self, job_id: str) -> Optional[dict]:
        row = self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not row:
            return None
        return dict(row)

    def result(self, job_id: str) -> Optional[StoryboardResult]:
        from app.services.job_results import build_job_result
        return build_job_result(self, job_id)
