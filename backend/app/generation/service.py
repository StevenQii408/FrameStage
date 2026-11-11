"""Immutable generation attempts with explicit, bounded provider execution."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.db import utc_now
from app.generation.assets import GenerationAssets
from app.generation.models import (ACTIVE, GenerationCreate, GenerationReplay, GenerationError, GenerationRequest,
    VIDEO_DURATION_MIN_SECONDS, VIDEO_DURATION_MAX_SECONDS, VIDEO_DURATION_EXTENSION_MAX_SECONDS)
from app.generation.providers import CLOUD_CANCELLED_MESSAGE, DisabledGenerationProvider, ProviderResult
from app.generation.failures import (PUBLIC_FAILURES, TRANSIENT_QUERY_FAILURES,
    SUBMISSION_REJECTION_FAILURES, public_failure)
from app.generation.repository import GenerationRepository, encoded, public_job
from app.projects.access import active_project, owned_asset
from app.projects.models import ProjectError


class GenerationService:
    def __init__(self, settings, db, events, *, provider=None, executor=None, poll_interval=1.0, max_polls=300,
                 generation_config=None):
        self.repository = GenerationRepository(db)
        self.assets = GenerationAssets(settings.data_dir, self.repository)
        self.events = events
        self.config = generation_config
        if provider is None and generation_config is not None:
            from app.generation.cloud import CloudGenerationProvider
            provider = CloudGenerationProvider(generation_config, self.assets.path, settings.data_dir)
        self.provider = provider or DisabledGenerationProvider()
        self._owns_executor = executor is None
        self.executor = executor
        self.poll_interval, self.max_polls = max(0.0, poll_interval), max(1, max_polls)
        self._closed = threading.Event()
        self._schedule_lock = threading.Lock()
        self.scheduler = None
        self.queue = None
        if generation_config is not None:
            from app.generation.queue import GenerationQueue
            self.queue = GenerationQueue(db)
            self.queue.recover_dead_leases()
            self._recover()
        else:
            for job_id in self.repository.interrupt_pending():
                self._notify(job_id)

    def capabilities(self):
        limits = ({"max_inflight": self.config.global_inflight, "max_pending": self.config.max_pending,
            "image_inflight": self.config.image_inflight, "video_inflight": self.config.video_inflight,
            "poll_interval_seconds": self.config.poll_interval_seconds,
            "remote_slots": self.queue.slot_counts()} if self.config else {})
        return {**self.provider.capabilities(), "execution_limits": limits, "request_limits": {"video": {
            "min_duration_seconds": VIDEO_DURATION_MIN_SECONDS,
            "max_duration_seconds": VIDEO_DURATION_MAX_SECONDS,
            "duration_step_seconds": 1,
            "extensible_max_duration_seconds": VIDEO_DURATION_EXTENSION_MAX_SECONDS,
        }}}

    def _notify(self, job_id):
        self.events.notify("generation:" + job_id)
        job = self.repository.get(job_id)
        if job:
            self.events.notify("generation-project:" + (job.get("project_id") or "legacy"))

    def _start_scheduler(self):
        with self._schedule_lock:
            if self.scheduler is None and not self._closed.is_set():
                from app.generation.scheduler import GenerationScheduler
                self.scheduler = GenerationScheduler(self, self.config)
        if self.scheduler:
            self.scheduler.wake()

    def _recover(self):
        resume = False
        capability = self.provider.capabilities()
        for job in self.repository.pending():
            available = capability.get("configured") and capability.get(job["kind"], {}).get("available")
            compatible = available and job["provider"] == self.provider.name
            lease = self.queue.peek(job["id"])
            if lease and lease["lease_owner"] and lease["lease_until"] > time.time():
                # Another still-live local process may be finishing its POST.
                # It must be allowed to persist the accepted remote task ID.
                resume = resume or bool(compatible)
                continue
            if compatible and (job["status"] == "queued" or
                    (job["status"] in {"running", "finalizing"} and job.get("provider_task_id"))):
                phase = {"queued": "submit", "running": "poll", "finalizing": "finalize"}[job["status"]]
                self.queue.enqueue(job["id"], phase)
                self.queue.reconcile_phase(job["id"], phase)
                if job.get("provider_task_id") and (phase == "poll" or not (lease and json.loads(lease["output_json"]))):
                    self.queue.reserve_existing(job)
                resume = True
            else:
                self._advance(job["id"], "interrupted", error={"code": "server_restarted",
                    "message": "生成提交状态或服务商配置无法安全恢复；请核查原任务后显式重试，未自动重新提交。", "retryable": True})
                self.queue.finish(job["id"])
                if self.queue.remote_slot(job["id"]):
                    self.queue.mark_remote_unknown(job["id"])
        if capability.get("configured") and self.queue.slot_counts()["total"]:
            resume = True
        if resume:
            self._start_scheduler()

    def get(self, job_id):
        job = self.repository.get(job_id)
        if not job:
            raise GenerationError(404, "生成任务不存在", "generation_missing")
        return public_job(job)

    def _prepare(self, request):
        request = request if isinstance(request, GenerationRequest) else GenerationRequest.model_validate(request)
        snapshot = request.model_dump(mode="json", exclude={"idempotency_key", "project_id"})
        # Additive optional fields must not invalidate historical idempotency
        # hashes or rewrite the immutable content of a legacy attempt.
        if snapshot["settings"].get("model_id") is None:
            snapshot["settings"].pop("model_id", None)
            if self.config is not None:
                supported = self.provider.capabilities().get(request.kind, {})
                model = getattr(self.config, request.kind + "_model")
                if supported.get("available") and any(isinstance(item, dict) and item.get("id") == model
                        for item in supported.get("models", [])):
                    snapshot["settings"]["model_id"] = model
        if not snapshot.get("reference_images"):
            snapshot.pop("reference_images", None)
        issues, asset = [], None
        try:
            with self.repository.db.connect() as conn:
                active_project(conn, request.project_id)
                for asset_id in {ref.asset_id for ref in request.reference_images} | ({request.reference_asset_id} if request.reference_asset_id else set()):
                    owned_asset(conn, request.project_id, asset_id)
        except ProjectError as exc:
            raise GenerationError(exc.status_code, str(exc), "invalid_project_reference") from exc
        if request.reference_asset_id:
            try:
                asset, _ = self.assets.path(request.reference_asset_id)
                if not asset["mime_type"].startswith("image/"):
                    issues.append("首帧或参考输入必须为图片资产")
            except GenerationError as exc:
                issues.append(str(exc))
        if request.kind == "video" and not asset:
            issues.append("视频生成需要有效的首帧图片")
        bindings = request.reference_images
        if len({ref.reference_id for ref in bindings}) != len(bindings) or len({ref.mention for ref in bindings}) != len(bindings):
            issues.append("参考图片标识和 @图片 编号必须唯一")
        for ref in bindings:
            try:
                metadata, _ = self.assets.path(ref.asset_id)
                if not metadata["mime_type"].startswith("image/"):
                    issues.append("参考输入必须为图片资产")
            except GenerationError as exc:
                issues.append(str(exc))
        if bindings and request.reference_asset_id and request.reference_asset_id not in {ref.asset_id for ref in bindings}:
            issues.append("首帧或主参考图必须属于已选择的参考图片")
        first_frames = [ref for ref in bindings if ref.role == "first_frame"]
        if len(first_frames) > 1 or (first_frames and first_frames[0].asset_id != request.reference_asset_id):
            issues.append("首帧绑定必须唯一并与指定首帧一致")
        mentions = {"图片" + number for number in re.findall(r"@图片([1-9][0-9]*)", request.prompt)}
        declared = {ref.mention for ref in bindings} or ({"图片1"} if request.reference_asset_id else set())
        if mentions - declared:
            issues.append("Prompt 引用了未选择的参考图片：" + "、".join(sorted(mentions - declared)))
        snapshot["reference_asset"] = ({key: value for key, value in asset.items()
            if key not in {"path", "role", "job_id"}} if asset else None)
        return snapshot, list(dict.fromkeys(issues))

    def preflight(self, request):
        snapshot, issues = self._prepare(request)
        capability = self.capabilities()
        issues.extend(self._capability_issues(snapshot, capability))
        available = bool(capability.get("configured") and capability.get(snapshot["kind"], {}).get("available"))
        return {"valid": not issues, "can_submit": not issues and available,
                "configured": bool(capability.get("configured")), "issues": issues,
                "reason": capability.get("reason", "") if not available else "",
                "input_hash": hashlib.sha256(encoded(snapshot).encode()).hexdigest()}

    def _capability_issues(self, snapshot, capability):
        supported = capability.get(snapshot["kind"], {})
        selected = snapshot["settings"].get("model_id")
        if selected:
            model = next((item for item in supported.get("models", []) if isinstance(item, dict) and item.get("id") == selected), None)
            if model is None:
                return ["所选生成模型尚未接入或不支持此任务类型"]
            supported = {**supported, **model}
        if (not capability.get("configured") or not supported.get("available")) and not supported.get("models"):
            return []
        settings = snapshot["settings"]
        issues = []
        for field, declared in (("aspect_ratio", "aspect_ratios"), ("resolution", "resolutions")):
            allowed = supported.get(declared)
            if not isinstance(allowed, list) or not allowed:
                issues.append("生成服务商尚未声明支持的" + ("画幅" if field == "aspect_ratio" else "分辨率"))
            elif settings[field] not in allowed:
                issues.append("生成服务商不支持所选" + ("画幅" if field == "aspect_ratio" else "分辨率"))
        if snapshot["kind"] == "video":
            low, high = supported.get("min_duration_seconds"), supported.get("max_duration_seconds")
            if (isinstance(low, bool) or isinstance(high, bool) or not isinstance(low, (int, float))
                    or not isinstance(high, (int, float)) or not math.isfinite(low) or not math.isfinite(high)
                    or not 0 < low <= high <= VIDEO_DURATION_EXTENSION_MAX_SECONDS):
                issues.append("生成服务商尚未声明有效的视频时长范围")
            elif not low <= settings["duration_seconds"] <= high:
                issues.append("所选视频时长超出生成服务商支持范围")
        if supported.get("supports_reference") is False and snapshot.get("reference_asset_id"):
            issues.append("此生成服务商不支持参考图片")
        if len(snapshot.get("reference_images", [])) > supported.get("max_reference_images", 1):
            issues.append("参考图片数量超出此生成模型支持范围")
        if hasattr(self.provider, "validate_request"):
            issues.extend(self.provider.validate_request(snapshot))
        return issues

    def create(self, request, *, retry_of=None):
        if isinstance(request, dict):
            replay = self._existing_replay(request, retry_of=retry_of)
            if replay is not None:
                return replay
        request = request if isinstance(request, GenerationCreate) else GenerationCreate.model_validate(request)
        snapshot, issues = self._prepare(request)
        capability = self.capabilities()
        issues.extend(self._capability_issues(snapshot, capability))
        if issues:
            raise GenerationError(422, "；".join(issues), "invalid_generation_inputs")
        return self._create_snapshot(snapshot, capability, request.project_id, request.idempotency_key, retry_of=retry_of)

    def _existing_replay(self, payload, *, retry_of=None):
        key = payload.get("idempotency_key")
        if not isinstance(key, str):
            return None  # New-request validation supplies the actionable error.
        previous = self.repository.find_by_key(key)
        if previous is None:
            return None
        request = GenerationReplay.model_validate(payload)
        project = previous.get("project_id")
        if request.project_id != project or previous.get("retry_of") != retry_of:
            raise GenerationError(409, "此幂等键已用于另一份输入，请显式创建新的请求", "idempotency_conflict")
        try:
            with self.repository.db.connect() as conn:
                active_project(conn, project)
        except ProjectError as exc:
            raise GenerationError(exc.status_code, str(exc), "invalid_project") from exc
        snapshot = request.model_dump(mode="json", exclude={"idempotency_key", "project_id"})
        if request.kind == "video":
            original_default = previous["request"].get("settings", {}).get("duration_seconds")
            incoming = (payload.get("settings") or {}).get("duration_seconds")
            if incoming is None and original_default == 5:
                snapshot["settings"]["duration_seconds"] = original_default
            elif (type(original_default) is float and original_default.is_integer()
                    and type(incoming) is int and incoming == original_default):
                # JSON.stringify cannot preserve 5.0: the browser sends 5.
                # Only a read of this existing attempt reuses its old float;
                # new requests still pass today's strict integer contract.
                snapshot["settings"]["duration_seconds"] = original_default
        if snapshot["settings"].get("model_id") is None:
            locked_model = previous["request"]["settings"].get("model_id")
            if locked_model:
                snapshot["settings"]["model_id"] = locked_model
            else:
                snapshot["settings"].pop("model_id", None)
        if not snapshot.get("reference_images"):
            snapshot.pop("reference_images", None)
        # Replays are reads: do not fetch, rewrite or revalidate an old file.
        snapshot["reference_asset"] = previous["request"].get("reference_asset")
        if hashlib.sha256(encoded(snapshot).encode()).hexdigest() != previous["input_hash"]:
            raise GenerationError(409, "此幂等键已用于另一份输入，请显式创建新的请求", "idempotency_conflict")
        return public_job(previous)

    def _create_snapshot(self, snapshot, capability, project_id, key, *, retry_of=None):
        """Persist validated content verbatim, including legacy numeric snapshots."""
        available = bool(capability.get("configured") and capability.get(snapshot["kind"], {}).get("available"))
        now = utc_now()
        reason = capability.get("reason") or "当前生成 Provider 不支持此任务类型"
        job = {"id": "generation-" + uuid.uuid4().hex, "node_id": snapshot["node_id"], "kind": snapshot["kind"],
               "status": "queued" if available else "blocked", "stage": "queued" if available else "awaiting_provider",
               "progress": None, "error": None if available else {"code": "provider_unconfigured", "message": reason, "retryable": True},
               "revision": 1, "provider": self.provider.name, "request": snapshot,
               "input_hash": hashlib.sha256(encoded(snapshot).encode()).hexdigest(),
               "assets": [], "provider_task_id": None, "retry_of": retry_of,
               "billing": {"status": "not_submitted", "estimated_cost_cny": None},
               "created_at": now, "updated_at": now}
        if project_id is not None:
            job["project_id"] = project_id
        saved, created = self.repository.create(job, key, max_pending=self.config.max_pending if self.config else 100)
        if created:
            self._notify(saved["id"])
            if available:
                if self.queue is not None and self.executor is None:
                    self.queue.enqueue(saved["id"])
                    self._start_scheduler()
                else:
                    # Compatibility for explicitly injected synchronous adapters
                    # and deterministic unit-test executors.
                    with self._schedule_lock:
                        if self.executor is None:
                            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="generation-worker")
                    self.executor.submit(self._run, saved["id"])
        return self.get(saved["id"])

    def _advance(self, job_id, status, **kwargs):
        changed = self.repository.advance(job_id, status, **kwargs)
        if changed:
            self._notify(job_id)
        return changed

    def cancel(self, job_id):
        job = self.get(job_id)
        if job["status"] == "cancelled":
            return job
        changed = self._advance(job_id, "cancelled", error={"code": "user_cancelled",
            "message": "已停止本地生成任务；云端已受理的任务可能继续执行并计费，不会自动重试。", "retryable": True})
        if not changed:
            raise GenerationError(409, "此生成任务已经结束，无法取消", "generation_not_cancellable")
        if changed.get("provider_task_id"):
            try:
                self.provider.cancel(changed["provider_task_id"])
            except Exception:
                # Local cancellation is authoritative even if a remote request
                # is already running; unknown billing must not become a refund.
                pass
        if self.queue:
            self.queue.finish(job_id)
            if changed.get("billing", {}).get("status") == "not_submitted":
                self.queue.release_slot(job_id)
            else:
                self.queue.mark_remote_unknown(job_id)
        if self.scheduler:
            self.scheduler.wake()
        return public_job(changed)

    def _deliver(self, job, result):
        assets = result.assets
        if result.remote_assets:
            assets = self.provider.collect(job["provider_task_id"], result)
        try:
            if len(assets) != 1:
                raise ValueError("每个生成节点必须交付一个实际生成资产")
            asset = assets[0]
            if self._closed.is_set() or self.get(job["id"])["status"] != "finalizing":
                return
            if job["kind"] == "image" and asset.mime_type.startswith("image/"):
                if asset.path is not None:
                    if asset.path.stat().st_size > 20 * 1024 * 1024:
                        raise GenerationError(413, "图片产物超限", "asset_size_limit")
                    data = asset.path.read_bytes()
                else:
                    data = asset.data
                delivered = self.assets.image(data, role="output", job_id=job["id"], settings=job["request"]["settings"])
            elif job["kind"] == "video" and asset.mime_type == "video/mp4":
                delivered = self.assets.video(asset.path if asset.path is not None else asset.data,
                    job_id=job["id"], settings=job["request"]["settings"], provider_spec=asset.provider_spec)
            else:
                raise ValueError("Provider 产物类型与生成任务不一致")
            if not self._closed.is_set():
                self._advance(job["id"], "succeeded", progress=100, updates={"assets": [delivered]})
        finally:
            for asset in assets:
                if asset.path is not None:
                    asset.path.unlink(missing_ok=True)

    def _step(self, entry, owner):
        """Execute one short checkpoint; waiting schedules the next due time."""
        job_id = entry["job_id"]
        phase = entry["phase"]
        try:
            job = self.repository.get(job_id)
            if not job or job["status"] not in ACTIVE:
                if job and job.get("billing", {}).get("status") == "not_submitted":
                    self.queue.release_slot(job_id)
                self.queue.finish(job_id, owner)
                return
            if self._closed.is_set():
                return
            if time.time() >= entry["deadline"]:
                self._advance(job_id, "failed", error={"code": "generation_timeout",
                    "message": "生成等待已超过时间预算；原服务商任务及费用可能仍在处理中，请核查后显式重试。", "retryable": True})
                self.queue.finish(job_id, owner)
                self.queue.mark_remote_unknown(job_id)
                return
            if phase == "submit":
                if not self._advance(job_id, "submitting", event="generation.submitting",
                        updates={"billing": {"status": "unknown", "estimated_cost_cny": None}}):
                    self.queue.finish(job_id, owner)
                    self.queue.release_slot(job_id)
                    return
                remote_id = self.provider.submit(json.loads(encoded(job["request"])), idempotency_key=job_id)
                if not isinstance(remote_id, str) or not remote_id:
                    raise ValueError("缺少远端生成任务 ID")
                self.queue.attach_remote(job_id, remote_id)
                # Persist an accepted remote ID even during shutdown. A restart
                # can query it, whereas repeating this paid POST would be unsafe.
                if not self._advance(job_id, "running", updates={"provider_task_id": remote_id}, event="generation.started"):
                    self.repository.retain_remote_handle(job_id, remote_id)
                    try:
                        self.provider.cancel(remote_id)
                    except Exception:
                        pass
                    self.queue.finish(job_id, owner)
                    return
                self.queue.checkpoint(job_id, owner, phase="poll", delay=self.config.poll_interval_seconds)
            elif phase == "poll":
                result = self.provider.poll(job["provider_task_id"])
                if self.get(job_id)["status"] not in ACTIVE:
                    self.queue.finish(job_id, owner)
                    return
                if result.state == "succeeded":
                    self.queue.release_slot(job_id)
                    if not result.remote_assets:
                        # Local fake adapters may already own bytes. Production
                        # adapters always checkpoint URLs and stream them later.
                        if not self._advance(job_id, "finalizing", event="generation.finalizing"):
                            return
                        self._deliver(job, result)
                        self.queue.finish(job_id, owner)
                    elif self._advance(job_id, "finalizing", event="generation.finalizing"):
                        self.queue.checkpoint(job_id, owner, phase="finalize", output=result.remote_assets)
                elif result.state in {"failed", "cancelled"}:
                    self._finish_unsuccessful(job_id, result)
                    self.queue.finish(job_id, owner)
                elif result.state == "unknown":
                    self.queue.mark_remote_unknown(job_id)
                    self._advance(job_id, "failed", error={"code": "remote_status_unknown",
                        "message": PUBLIC_FAILURES["remote_status_unknown"], "retryable": False})
                    self.queue.finish(job_id, owner)
                elif result.state in {"queued", "running"}:
                    if result.progress is not None:
                        self._advance(job_id, "running", progress=result.progress, event="generation.progress")
                    # Stable per-job staggering avoids a synchronized query burst.
                    jitter = (int(hashlib.sha256(job_id.encode()).hexdigest()[:4], 16) % 100) / 1000
                    self.queue.checkpoint(job_id, owner, phase="poll", delay=self.config.poll_interval_seconds * (1 + jitter))
                else:
                    raise ValueError("未知生成状态")
            elif phase == "finalize":
                outputs = json.loads(entry["output_json"])
                if not outputs:
                    # Recover an older finalizing checkpoint by querying its
                    # existing task, never by creating a new generation.
                    result = self.provider.poll(job["provider_task_id"])
                else:
                    result = ProviderResult("succeeded", remote_assets=outputs)
                if result.state in {"queued", "running"}:
                    self.queue.checkpoint(job_id, owner, phase="finalize", delay=self.config.poll_interval_seconds)
                    return
                if result.state in {"failed", "cancelled"}:
                    self._finish_unsuccessful(job_id, result)
                    self.queue.finish(job_id, owner)
                    return
                if result.state == "unknown":
                    raise GenerationError(502, PUBLIC_FAILURES["remote_status_unknown"], "remote_status_unknown")
                if result.state != "succeeded":
                    raise ValueError("未知生成状态")
                self.queue.release_slot(job_id)
                self._deliver(job, result)
                self.queue.finish(job_id, owner)
        except Exception as exc:
            if self._closed.is_set():
                return  # Keep the last safe checkpoint for restart/reconciliation.
            code = exc.code if isinstance(exc, GenerationError) and exc.code in PUBLIC_FAILURES else "generation_provider_error"
            # Status GETs and artifact downloads can be retried within the same
            # remote task. Submissions are never automatically repeated.
            transient = code in TRANSIENT_QUERY_FAILURES
            if phase != "submit" and transient and entry["failures"] < 5 and not self._closed.is_set():
                delay = max(self.config.poll_interval_seconds, min(120, 5 * 2**entry["failures"]),
                    min(300, getattr(exc, "retry_after", 0)))
                self.queue.checkpoint(job_id, owner, phase=phase, delay=delay, failure=True)
            else:
                if phase == "submit" and code in SUBMISSION_REJECTION_FAILURES:
                    self.queue.release_slot(job_id)
                else:
                    self.queue.mark_remote_unknown(job_id)
                self._advance(job_id, "failed", error=public_failure(code))
                self.queue.finish(job_id, owner)
        finally:
            self.queue.release_owner_for_job(job_id, owner)

    def _finish_unsuccessful(self, job_id, result):
        if self.queue:
            self.queue.release_slot(job_id)
        if result.state == "cancelled":
            self._advance(job_id, "cancelled", error={"code": "provider_cancelled",
                "message": CLOUD_CANCELLED_MESSAGE, "retryable": False})
        else:
            code = result.error_code if result.error_code in PUBLIC_FAILURES else "provider_failed"
            self._advance(job_id, "failed", error=public_failure(code, retryable=result.retryable))

    def _reconcile_remote(self, entry, owner):
        """A local terminal state does not prove the cloud has stopped computing."""
        job_id = entry["job_id"]
        try:
            result = self.provider.poll(entry["provider_task_id"])
            if result.state in {"succeeded", "failed", "cancelled"}:
                self.queue.finish_reconciliation(job_id, owner)
            else:
                if result.state == "unknown":
                    self.queue.mark_remote_unknown(job_id)
                self.queue.defer_reconciliation(job_id, owner, self.config.poll_interval_seconds)
        except Exception as exc:
            self.queue.mark_remote_unknown(job_id)
            self.queue.defer_reconciliation(job_id, owner,
                max(30, self.config.poll_interval_seconds, min(300, getattr(exc, "retry_after", 0))))

    def confirm_remote_stopped(self, job_id):
        job = self.get(job_id)
        if job["status"] in ACTIVE:
            raise GenerationError(409, "运行中的本地任务不能手动释放云端名额，请先取消并核对云端状态", "generation_still_active")
        if self.queue and self.queue.remote_slot(job_id):
            self.queue.release_slot(job_id)
            self.repository.record_remote_resolution(job_id)
            self._notify(job_id)
            if self.scheduler:
                self.scheduler.wake()
        return {"job_id": job_id, "cloud_slot_released": True}

    def retry(self, job_id, key):
        previous = self.get(job_id)
        if previous["status"] not in {"blocked", "failed", "cancelled", "interrupted"}:
            raise GenerationError(409, "仅受阻、失败、取消或中断的任务可显式重试", "generation_not_retryable")
        if previous["status"] == "cancelled" and (previous.get("error") or {}).get("code") == "provider_cancelled":
            raise GenerationError(409, "云端已取消，请创建新任务", "generation_not_retryable")
        if previous["status"] == "failed" and previous.get("error", {}).get("retryable") is False:
            raise GenerationError(409, "此失败不能使用相同输入重试，请修改输入后创建新任务", "generation_not_retryable")
        payload = {field: value for field, value in previous["request"].items() if field != "reference_asset"}
        if previous["kind"] == "video":
            duration = payload.get("settings", {}).get("duration_seconds")
            if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration)
                    or not VIDEO_DURATION_MIN_SECONDS <= duration <= VIDEO_DURATION_MAX_SECONDS or int(duration) != duration):
                raise GenerationError(409, "历史任务时长不符合当前2–30秒整数规则，请编辑时长并创建新任务；原任务及输入快照已保留。", "legacy_duration_policy")
            # Old requests stored e.g. 5.0. Validate a copy under today's policy,
            # then retain the original 5.0 and content hash in the new attempt.
            payload = json.loads(encoded(payload))
            payload["settings"]["duration_seconds"] = int(duration)
        checked = GenerationCreate(**payload, project_id=previous.get("project_id"), idempotency_key=key)
        resolved, issues = self._prepare(checked)
        snapshot = json.loads(encoded(previous["request"]))
        if "model_id" not in snapshot["settings"] and resolved["settings"].get("model_id"):
            snapshot["settings"]["model_id"] = resolved["settings"]["model_id"]
        capability = self.capabilities()
        issues.extend(self._capability_issues(snapshot, capability))
        if issues:
            raise GenerationError(422, "；".join(issues), "invalid_generation_inputs")
        return self._create_snapshot(snapshot, capability, previous.get("project_id"), key, retry_of=job_id)

    def _run(self, job_id):
        if self._closed.is_set() or not self._advance(job_id, "submitting", event="generation.submitting",
                updates={"billing": {"status": "unknown", "estimated_cost_cny": None}}):
            return
        try:
            job = self.get(job_id)
            # Internal adapters may read the immutable local asset via an
            # injected repository; no caller can choose a server path or URL.
            remote_id = self.provider.submit(json.loads(encoded(job["request"])), idempotency_key=job_id)
            if not isinstance(remote_id, str) or not remote_id:
                raise ValueError("生成 Provider 未返回有效任务 ID")
            if not self._advance(job_id, "running", updates={"provider_task_id": remote_id}, event="generation.started"):
                try:
                    self.provider.cancel(remote_id)
                except Exception:
                    pass
                return
            for _ in range(self.max_polls):
                if self._closed.is_set() or self.get(job_id)["status"] not in ACTIVE:
                    return
                result = self.provider.poll(remote_id)
                if self._closed.is_set() or self.get(job_id)["status"] not in ACTIVE:
                    return
                if result.state == "succeeded":
                    if len(result.assets) != 1:
                        raise ValueError("每个生成节点必须交付一个实际生成资产")
                    if not self._advance(job_id, "finalizing", event="generation.finalizing"):
                        return
                    delivered = []
                    for asset in result.assets:
                        if job["kind"] == "image" and asset.mime_type.startswith("image/"):
                            delivered.append(self.assets.image(asset.data, role="output", job_id=job_id, settings=job["request"]["settings"]))
                        elif job["kind"] == "video" and asset.mime_type == "video/mp4":
                            delivered.append(self.assets.video(asset.data, job_id=job_id, settings=job["request"]["settings"]))
                        else:
                            raise ValueError("Provider 产物类型与生成任务不一致")
                    self._advance(job_id, "succeeded", progress=100, updates={"assets": delivered})
                    return
                if result.state in {"failed", "cancelled"}:
                    self._finish_unsuccessful(job_id, result)
                    return
                if result.state not in {"queued", "running"}:
                    raise ValueError("生成 Provider 返回未知任务状态")
                if result.progress is not None:
                    self._advance(job_id, "running", progress=result.progress, event="generation.progress")
                if self.poll_interval:
                    self._closed.wait(self.poll_interval)
            self._advance(job_id, "failed", error={"code": "generation_timeout",
                "message": "生成查询超过预算；原服务商状态与费用可能未知，请检查后显式重试。", "retryable": True})
        except Exception as exc:
            code = exc.code if isinstance(exc, GenerationError) and exc.code in PUBLIC_FAILURES else "generation_provider_error"
            self._advance(job_id, "failed", error=public_failure(code))

    def close(self):
        self._closed.set()
        if self.scheduler:
            self.scheduler.close()
        if self.queue is None:
            for job_id in self.repository.interrupt_pending():
                self._notify(job_id)
        if self.executor is not None and self._owns_executor:
            self.executor.shutdown(wait=False, cancel_futures=True)
