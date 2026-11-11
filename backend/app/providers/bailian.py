"""Beijing Bailian multimodal provider; local files stay on this host except selected frames."""
import asyncio
import base64
from contextlib import suppress
import json
import hashlib
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from app.config import Settings, normalize_cloud_base_url
from app.providers.base import FrameInput, ScriptPlanError, ShotContext
from app.providers.cloud_errors import CloudTransportError
from app.providers.vision_common import VisionAnalysisTools
from app.schemas import MontageRelation, MontageResponse, ProviderHealth, ScriptBeat, ScriptPlan, ScriptPlanResult, VisionObservation
from app.services.montage_wire import normalize_relation
from app.services.script_processor import compact_oversplit_plan, reconcile_protected_boundaries, validate_window_plan
from app.services.perception import PerceptionOrchestrator, structured_schema
from app.runtime import (check_execution, current_execution, execution_deadline,
                         ExecutionCancelled, ExecutionDeadlineExceeded)

CUT_GROUP_PROMPT_VERSION = "cuts-group.v2"


# Beijing list prices in USD per million tokens, checked 2026-09-22.
# These are estimates, not billed amounts: cache discounts and promotions can differ.
LIST_PRICES = {
    "qwen3.8-flash": [(1_000_000, 0.113, 0.382)],
    "qwen3.8-max-0902": [(1_000_000, 1.65, 4.951)],
    "qwen3.7-plus-2026-05-26": [(256_000, 0.276, 1.101), (1_000_000, 0.826, 3.301)],
    "qwen3-vl-plus-2025-12-19": [(32_000, 0.143, 1.434), (128_000, 0.215, 2.15), (256_000, 0.43, 4.301)],
}
LIST_PRICES_CNY = {"qwen3.8-flash": [(1_000_000, 0.8, 2.7)]}


def estimate_list_price(model: str, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
    for limit, input_rate, output_rate in LIST_PRICES.get(model, []):
        if prompt_tokens <= limit:
            return round((prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000, 8)
    return None


def estimate_list_price_cny(model: str, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
    for limit, input_rate, output_rate in LIST_PRICES_CNY.get(model, []):
        if prompt_tokens <= limit:
            return round((prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000, 8)
    return None


class BailianVisionProvider:
    """Synchronous provider for the existing single-worker queue."""

    def __init__(self, settings: Settings, transport: Optional[httpx.BaseTransport] = None):
        self.settings = settings
        self.helper = VisionAnalysisTools(settings)
        self.client = httpx.Client(
            transport=transport, trust_env=settings.cloud_trust_env,
            timeout=self._request_timeout(settings.vlm_timeout_seconds),
        )
        self.usage: List[Dict[str, Any]] = []
        self._closed = False

    def _request_timeout(self, remaining: float) -> httpx.Timeout:
        remaining = max(0.001, remaining)
        return httpx.Timeout(
            connect=min(remaining, self.settings.cloud_connect_timeout_seconds),
            read=min(remaining, self.settings.cloud_read_timeout_seconds),
            write=min(remaining, 20.0), pool=min(remaining, 5.0),
        )

    def _post_bounded(self, url, body, deadline):
        """Cancel real async I/O at an absolute deadline; no abandoned threads.

        The public provider remains synchronous because its caller is the serial
        worker. A supplied MockTransport is reused for deterministic tests.
        """
        async def request():
            transport = getattr(self.client, "_transport", None)
            if not isinstance(transport, httpx.AsyncBaseTransport):
                if transport is not None and not isinstance(transport, httpx.HTTPTransport):
                    raise TypeError("自定义云端传输必须支持异步取消；测试可使用httpx.MockTransport")
                transport = None
            async with httpx.AsyncClient(transport=transport, trust_env=self.settings.cloud_trust_env) as client:
                task = asyncio.create_task(client.post(
                    url, json=body, headers={"Authorization": f"Bearer {self.settings.cloud_api_key}"},
                    timeout=self._request_timeout(deadline - time.monotonic())))
                try:
                    while True:
                        if self._closed:
                            raise ExecutionCancelled("Provider已关闭，请求已停止")
                        check_execution()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ExecutionDeadlineExceeded("云端请求总时间预算耗尽")
                        done, _ = await asyncio.wait({task}, timeout=min(.05, remaining))
                        if done:
                            response = await task
                            check_execution()
                            if time.monotonic() >= deadline:
                                response.close()
                                raise ExecutionDeadlineExceeded("云端响应超过总时间预算，已停止后续请求")
                            return response
                finally:
                    if not task.done():
                        task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
        return asyncio.run(request())

    def health(self) -> ProviderHealth:
        base_url = normalize_cloud_base_url(self.settings.cloud_base_url)
        parsed = urlparse(base_url)
        endpoint_ok = (
            bool(base_url)
            and base_url.startswith("https://")
            and base_url.endswith("/compatible-mode/v1")
            and "你的工作区ID" not in base_url
            and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and not any(char.isspace() or char in "<>" for char in base_url)
        )
        detail = None
        if not self.settings.cloud_api_key:
            detail = "未配置 STORYBOARD_BAILIAN_API_KEY"
        elif not endpoint_ok:
            detail = "请配置百炼北京区域 HTTPS compatible-mode/v1 工作区地址"
        return ProviderHealth(
            provider="bailian", model=self.settings.cloud_model, ready=detail is None,
            quantization="cloud", estimated_memory_mb=0, max_images_per_shot=4,
            max_context_tokens=32768, max_new_tokens=max(512, self.settings.vlm_max_new_tokens),
            timeout_seconds=self.settings.vlm_timeout_seconds, detail=detail,
        )

    def propose_style(self, prompt: str, frames: List[FrameInput]) -> str:
        """One project-level JSON call; the caller validates every term and frame."""
        from app.services.style_service import StyleSelectionResponse, StyleTagResponse

        is_tags = "画风基础标签（style.tags）" in prompt
        return self._call(prompt, frames, phase="style.tags" if is_tags else "style.project",
                          max_tokens=550 if is_tags else 1100, max_attempts=1,
                          schema=StyleTagResponse if is_tags else StyleSelectionResponse)

    def analyze_style(self, prompt, frames, *, phase, schema, timeout_seconds, max_tokens):
        """Public bounded interface: no hidden HTTP retry or model upgrade."""
        return self._call(prompt, frames, phase=phase, schema=schema, timeout_seconds=timeout_seconds,
                          max_tokens=max_tokens, max_attempts=1)

    def analyze_event_window(self, prompt, frames, *, phase, schema, timeout_seconds, max_tokens):
        """One charged attempt; the event Agent owns repair, evidence and budgets."""
        return self._call(prompt, frames, phase=phase, schema=schema,
                          timeout_seconds=timeout_seconds, max_tokens=max_tokens, max_attempts=1)

    def _call(self, prompt: str, frames: List[FrameInput], phase: str, max_tokens: Optional[int] = None, timeout_seconds: Optional[float] = None, video_clip_path: Optional[Path] = None, schema=None, max_attempts: int = 2, shot_id: Optional[str] = None) -> str:
        check_execution()
        health = self.health()
        if not health.ready:
            raise RuntimeError(health.detail)
        schema_payload = structured_schema(schema) if schema is not None else None
        # Bailian currently downgrades multimodal JSON Schema to JSON Object.
        # Include the generated schema for guidance, enforce it locally, and use
        # caller-controlled correction with images when structural repair is needed.
        if schema_payload is not None and (frames or video_clip_path):
            prompt += "\n输出 JSON Schema：" + json.dumps(schema_payload, ensure_ascii=False)
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        if video_clip_path is not None:
            if frames or video_clip_path.suffix.lower() != ".mp4" or not video_clip_path.is_file() or video_clip_path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError("只允许独立的小于 20MB 的 MP4 短片段，且不能与帧列表混用")
            encoded_video = base64.b64encode(video_clip_path.read_bytes()).decode("ascii")
            content.append({"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{encoded_video}", "fps": 4}})
        for index, frame in enumerate(sorted(frames, key=lambda item: item.timestamp)):
            path = Path(frame.path)
            if not path.is_file():
                raise ValueError(f"关键帧不存在：{path}")
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            content.append({"type": "text", "text": f"Frame {index + 1} | {frame.timestamp:.3f}s | {frame.id}"})
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
        body = {
            "model": self.settings.cloud_model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
            ("max_tokens" if "-vl-" in self.settings.cloud_model else "max_completion_tokens"): max_tokens or max(1024, self.settings.vlm_max_new_tokens),
            "temperature": 0.1,
        }
        if schema_payload is not None and not (frames or video_clip_path):
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True, "schema": schema_payload},
            }
        if phase == "cuts.group" or phase.startswith(("event_agent.", "script_director.")):
            body["temperature"] = 0
        url = normalize_cloud_base_url(self.settings.cloud_base_url) + "/chat/completions"
        started = time.monotonic()
        deadline = execution_deadline(timeout_seconds if timeout_seconds is not None else self.settings.vlm_timeout_seconds)
        for attempt in range(max_attempts):
            check_execution()
            if time.monotonic() >= deadline:
                raise TimeoutError("云端请求总时间预算耗尽")
            execution = current_execution()
            if execution:
                execution.admit_model_call()
            call_record = {
                "phase": phase, "model": self.settings.cloud_model,
                "shot_id": shot_id,
                "attempt": attempt + 1, "status": "failed", "request_id": None,
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "usage_available": False, "estimated_list_price_cny": None,
                "estimated_list_price_usd": None,
                "schema_enforcement": ("local-multimodal" if frames or video_clip_path else "server-and-local-text") if schema else "json-object",
            }
            self.usage.append(call_record)
            attempt_id = f"attempt-{uuid.uuid4().hex}"
            if execution:
                # Content hashes and stable IDs make an attempt diagnosable
                # without logging Key, Base64 images, or the user's whole text.
                execution.record("runtime.request_started", {
                    "attempt_id": attempt_id, "phase": phase, "model": self.settings.cloud_model,
                    "shot_id": shot_id, "attempt": attempt + 1, "billing_status": "unknown",
                    "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "schema_sha256": hashlib.sha256(json.dumps(schema_payload, sort_keys=True).encode()).hexdigest(),
                    "frame_ids": [frame.id for frame in frames],
                    "request_format": body["response_format"]["type"]})
            attempt_started = time.monotonic()
            try:
                response = self._post_bounded(url, body, deadline)
                if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < max_attempts:
                    call_record["error"] = f"HTTP {response.status_code}"
                    time.sleep(min(0.5, max(0, deadline - time.monotonic())))
                    continue
                response.raise_for_status()
                payload = response.json()
                choices = payload.get("choices") or []
                message = choices[0].get("message", {}) if choices else {}
                usage = payload.get("usage") or {}
                call_record.update({
                    "finish_reason": choices[0].get("finish_reason") if choices else None,
                    "phase": phase, "model": payload.get("model") or self.settings.cloud_model,
                    "request_id": response.headers.get("x-request-id") or payload.get("id"),
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "total_tokens": int(usage.get("total_tokens") or 0),
                    "status": "success", "usage_available": bool(usage),
                    "estimated_list_price_usd": estimate_list_price(self.settings.cloud_model, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
                    "estimated_list_price_cny": estimate_list_price_cny(self.settings.cloud_model, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
                    "price_basis": "Beijing list price checked 2026-09-27; excludes discounts and cache adjustments",
                })
                if not usage:
                    call_record["estimated_list_price_cny"] = call_record["estimated_list_price_usd"] = None
                output = message.get("content", "")
                call_record["content_length"] = len(output) if isinstance(output, str) else 0
                call_record["output_truncated"] = call_record["finish_reason"] == "length"
                if not isinstance(output, str) or not output.strip():
                    raise ValueError("云端模型返回空文本")
                return output
            except (ExecutionCancelled, ExecutionDeadlineExceeded) as exc:
                call_record.update(status="cancelled" if isinstance(exc, ExecutionCancelled) else "timed_out",
                                   error=str(exc), billing_status="unknown", usage_available=False)
                raise
            except httpx.HTTPStatusError as exc:
                call_record["status"] = "failed"
                call_record["error"] = f"HTTP {exc.response.status_code}"
                raise RuntimeError(f"百炼请求失败，HTTP {exc.response.status_code}") from exc
            except httpx.RequestError as exc:
                call_record["error"] = type(exc).__name__
                category = (
                    "connect_timeout" if isinstance(exc, httpx.ConnectTimeout) else
                    "read_timeout" if isinstance(exc, httpx.ReadTimeout) else
                    "write_timeout" if isinstance(exc, httpx.WriteTimeout) else
                    "pool_timeout" if isinstance(exc, httpx.PoolTimeout) else
                    "network_connection"
                )
                call_record["transport_error_category"] = category
                # Preserve a safe causal type, not str(exc): proxy URLs can
                # contain credentials and must never enter job/API records.
                call_record["transport_cause_type"] = type(exc.__cause__).__name__ if exc.__cause__ else None
                call_record["trust_environment_proxy"] = self.settings.cloud_trust_env
                if attempt + 1 < max_attempts:
                    call_record["error"] = type(exc).__name__
                    time.sleep(min(0.5, max(0, deadline - time.monotonic())))
                    continue
                raise CloudTransportError(category, type(exc).__name__) from exc
            except Exception as exc:
                call_record["status"] = "failed"
                call_record["error"] = str(exc)
                raise
            finally:
                call_record["elapsed_seconds"] = round(time.monotonic() - attempt_started, 3)
                if execution:
                    execution.record("runtime.request_finished", {"attempt_id": attempt_id, **call_record})
        raise RuntimeError("百炼请求失败")

    def analyze_shot(self, frames: List[FrameInput], context: ShotContext) -> VisionObservation:
        if self.settings.perception_v2_enabled:
            return PerceptionOrchestrator(self, context, frames).run()
        return self.analyze_shot_legacy(frames, context)

    def analyze_shot_legacy(self, frames: List[FrameInput], context: ShotContext) -> VisionObservation:
        ordered = sorted(frames, key=lambda frame: frame.timestamp)[:4]
        if not ordered:
            raise ValueError("当前镜头没有可用关键帧")
        started = time.monotonic()
        static = motion = None
        static_raw = motion_raw = ""
        warnings: List[str] = []
        transport_failed = False
        try:
            sharpest = self.helper._sharpest_image([frame.path for frame in ordered])
            representative = next(frame for frame in ordered if frame.path == sharpest)
            static_raw = self._call(
                self.helper._build_static_prompt(context, [representative])
                + "\n另外记录可见艺术效果及证据，不能确认的摄影技法写未知；不要从剧情推断画面事实。",
                [representative], "shot.static", timeout_seconds=self.settings.vlm_timeout_seconds,
                shot_id=context.shot_id,
            )
            static = self.helper._parse_static(static_raw)
        except Exception as exc:
            static_raw = static_raw or f"ERROR: {exc}"
            warnings.append(f"静态摄影分析失败：{exc}")
            transport_failed = isinstance(exc, CloudTransportError)
        if not transport_failed and time.monotonic() - started < self.settings.vlm_timeout_seconds:
            try:
                motion_raw = self._call(
                    self.helper._build_motion_prompt(context, ordered), ordered, "shot.motion",
                    timeout_seconds=max(1.0, self.settings.vlm_timeout_seconds - (time.monotonic() - started)),
                    shot_id=context.shot_id,
                )
                motion = self.helper._parse_motion(motion_raw)
                if self.helper._is_generic_action(motion):
                    warnings.append("动作描述过于笼统，需复核")
            except Exception as exc:
                motion_raw = motion_raw or f"ERROR: {exc}"
                warnings.append(f"时序运动分析失败：{exc}")
        else:
            warnings.append("网络失败或单镜总时限内未执行动态阶段")
        if static is None and motion is None:
            raise RuntimeError("双阶段视觉分析均失败：" + "；".join(warnings))
        observation = self.helper._merge_stages(static, motion, static_raw, motion_raw, warnings)
        if time.monotonic() - started > self.settings.vlm_timeout_seconds:
            observation.analysis_warnings.append("单镜云端分析超过时限")
            observation.confidence = min(observation.confidence, 0.3)
        return observation

    def summarize_video(self, frames: List[FrameInput]) -> str:
        ordered = sorted(frames, key=lambda frame: frame.timestamp)
        if not ordered:
            return ""
        with tempfile.TemporaryDirectory(prefix="storyboard-cloud-overview-") as directory:
            sheets = self.helper._contact_sheets(ordered[:12], Path(directory))
            sheet_frames = [FrameInput(f"contact-{i+1}", ordered[min(i * 6, len(ordered)-1)].timestamp, path)
                            for i, path in enumerate(sheets)]
            raw = self._call(
                "按联系表顺序，只根据可见内容输出 JSON 对象：characters、locations、story_progression、visual_style。"
                "区分直接可见事实和推断；不得编造姓名、因果或画外事件。",
                sheet_frames, "video.overview",
            )
            return json.dumps(self.helper._parse_payload(raw), ensure_ascii=False)

    def group_raw_shots(self, frames: List[FrameInput], protected_after: List[int]) -> List[List[int]]:
        ordered = sorted(frames, key=lambda frame: frame.timestamp)
        if not ordered:
            return []
        votes: Dict[tuple, List[bool]] = {}
        with tempfile.TemporaryDirectory(prefix="storyboard-cloud-cuts-") as directory:
            for start in range(0, len(ordered), 10):
                window = ordered[start:start + 12]
                if len(window) < 2:
                    break
                numbers = [int(frame.id.rsplit("-", 1)[-1]) for frame in window]
                sheets = self.helper._contact_sheets(window, Path(directory) / str(start))
                sheet_frames = [FrameInput(f"contact-{i+1}", window[min(i*6, len(window)-1)].timestamp, path)
                                for i, path in enumerate(sheets)]
                raw = self._call(
                    f"按联系表中的镜头编号 {numbers} 输出 JSON {{\"groups\":[[编号],[编号,编号]]}}。"
                    "每个编号按原顺序恰好出现一次。只合并主体、地点、道具和动作方向连续的相邻切段；"
                    "重要眼神、手部、道具、反应和结果特写必须独立；不确定保留边界。每组最多4个。",
                    sheet_frames, "cuts.group",
                )
                groups = self.helper._parse_payload(raw).get("groups")
                flattened = [n for group in groups for n in group] if isinstance(groups, list) and all(isinstance(g, list) for g in groups) else []
                valid = flattened == numbers and all(1 <= len(group) <= 4 and all(type(n) is int for n in group) for group in groups)
                if not valid:
                    raise ValueError("镜头合并计划必须按原顺序完整覆盖每个编号一次，每组1–4段")
                proposed = {(a, b) for group in groups for a, b in zip(group, group[1:])}
                for a, b in zip(numbers, numbers[1:]):
                    votes.setdefault((a, b), []).append(valid and (a, b) in proposed)
                if start + 12 >= len(ordered):
                    break
        protected = set(protected_after)
        groups = [[int(ordered[0].id.rsplit("-", 1)[-1])]]
        for frame in ordered[1:]:
            n = int(frame.id.rsplit("-", 1)[-1])
            decisions = votes.get((n-1, n), [False])
            if n-1 not in protected and decisions and all(decisions) and len(groups[-1]) < 4:
                groups[-1].append(n)
            else:
                groups.append([n])
        return groups

    def group_scenes(self, frames: List[FrameInput]) -> List[int]:
        ordered = sorted(frames, key=lambda frame: frame.timestamp)
        if not ordered:
            return []
        numbers = [int(frame.id.rsplit("-", 1)[-1]) for frame in ordered]
        starts = [numbers[0]]
        with tempfile.TemporaryDirectory(prefix="storyboard-cloud-scenes-") as directory:
            for start in range(0, len(ordered), 10):
                window = ordered[start:start + 12]
                if len(window) < 2:
                    break
                labels = [int(frame.id.rsplit("-", 1)[-1]) for frame in window]
                sheets = self.helper._contact_sheets(window, Path(directory) / str(start))
                sheet_frames = [FrameInput(f"contact-{i+1}", window[min(i*6, len(window)-1)].timestamp, path)
                                for i, path in enumerate(sheets)]
                raw = self._call(
                    f"按镜头 {labels} 的连续画面分析场次。仅地点、时间或连续事件发生变化时开启新场次；"
                    "正反打、特写、硬切不自动开场次。区分可见事实和叙事推断。"
                    "输出唯一 JSON 对象 scene_starts，值为本窗口内非首镜的编号数组；证据不足保留边界。",
                    sheet_frames, "scenes.group",
                )
                payload = self.helper._parse_payload(raw)
                proposed = payload.get("scene_starts")
                if not isinstance(proposed, list) or any(type(n) is not int or n not in labels[1:] for n in proposed):
                    raise ValueError("场次编号无效")
                starts.extend(proposed)
                if start + 12 >= len(ordered):
                    break
        return sorted(set(starts))

    def plan_script_director(self, prompt: str, *, schema, max_tokens: int) -> str:
        """Single text-only attempt; the bounded director owns all corrections."""
        return self._call(prompt, [], "script_director.plan", schema=schema,
                          max_tokens=max_tokens, max_attempts=1)

    def plan_script(self, beats: List[ScriptBeat], target_beat_ids: List[str]) -> ScriptPlanResult:
        prompt = self.helper._build_script_prompt(beats, target_beat_ids)
        responses: List[str] = []
        error = ""
        for attempt in range(2):
            request = prompt + (f"\n上次校验失败：{error[:500]}，请完整纠错。" if attempt else "")
            raw = self._call(request, [], "script.plan", max_tokens=self.settings.script_max_new_tokens, schema=ScriptPlan)
            responses.append(raw)
            try:
                payload = self.helper._normalize_script_payload(self.helper._parse_payload(raw))
                plan = ScriptPlan.model_validate(payload)
                flattened = [beat_id for shot in plan.shots for beat_id in shot.source_beat_ids]
                if flattened != target_beat_ids:
                    raise ValueError("source_beat_ids 未恰好按顺序覆盖目标节拍")
                plan, protected = reconcile_protected_boundaries(plan, beats, target_beat_ids)
                compacted = compact_oversplit_plan(plan, beats, target_beat_ids)
                result = validate_window_plan(compacted, beats, target_beat_ids)
                return ScriptPlanResult(plan=result, raw_responses=responses,
                                        rule_reconciled=protected or len(compacted.shots) != len(plan.shots))
            except Exception as exc:
                error = str(exc)
        raise ScriptPlanError(f"云端剧本规划 JSON 两次校验失败：{error}", responses)

    def analyze_montage(
        self, shots: List[Dict[str, Any]], frames_by_shot: Dict[str, List[FrameInput]],
    ) -> Dict[str, Any]:
        """Compare consecutive shots; visual facts stay separate from interpretation."""
        relations: List[MontageRelation] = []
        raw_responses: List[str] = []
        audits, rejected_pairs, unresolved_chains = [], [], []
        with tempfile.TemporaryDirectory(prefix="storyboard-cloud-montage-") as directory:
            for start in range(0, len(shots) - 1, 7):
                window = shots[start:start + 8]
                if len(window) < 2:
                    break
                eligible_pairs = [(left, right) for left, right in zip(window, window[1:])
                    if left.get("status", "ready") in ("ready", "warning")
                    and right.get("status", "ready") in ("ready", "warning")]
                if not eligible_pairs:
                    continue
                # Keep the existing bounded 8-shot windows. Failure gaps are
                # omitted within that same request, never bridged by a new pair.
                eligible_ids = {shot["id"] for pair in eligible_pairs for shot in pair}
                visible_window = [shot for shot in window if shot["id"] in eligible_ids]
                boundary_frames = []
                for shot in visible_window:
                    frames = sorted(frames_by_shot.get(shot["id"], []), key=lambda f: f.timestamp)
                    if not frames:
                        raise ValueError(f"跨镜分析缺少 {shot['id']} 的边界帧")
                    boundary_frames.append(frames[0])
                    if len(frames) > 1:
                        boundary_frames.append(frames[-1])
                sheets = self.helper._contact_sheets(boundary_frames, Path(directory) / str(start))
                inputs = [FrameInput(f"contact-{i+1}", boundary_frames[min(i*6, len(boundary_frames)-1)].timestamp, path)
                          for i, path in enumerate(sheets)]
                facts = []
                for shot in visible_window:
                    observation = shot.get("observation", {})
                    action = observation.get("action_details") or {}
                    visual = observation.get("visual_facts") or {}
                    # Preserve every saved endpoint verbatim; remove duplicate
                    # phase/style/spatial evidence instead of cutting JSON text.
                    facts.append({"shot_id": shot["id"], "number": shot["number"],
                        "start_time": shot["start_time"], "end_time": shot["end_time"],
                        "observation": {
                            "subject_action": str(observation.get("subject_action") or "")[:600],
                            "shot_size": observation.get("shot_size"),
                            "camera_movement": str(observation.get("camera_movement") or "")[:400],
                            "confidence": observation.get("confidence"),
                            "visual_facts": {key: visual.get(key) for key in ("people", "objects", "scene")},
                            "action_details": {**{key: action.get(key) for key in (
                                "primary_subject", "initial_state", "final_state", "interaction_target")},
                                "steps": [str(step)[:400] for step in (action.get("steps") or [])[:6]]},
                        }})
                prompt = (
                    "你是电影剪辑分析师。比较相邻镜头的边界帧与逐镜观察。"
                    "只输出 JSON 对象 relations，数组中每对指定相邻镜头恰好一个关系，按顺序排列。"
                    "每项严格包含 left_shot_id、right_shot_id、observed_edit_fact、relation_type、"
                    "possible_narrative_intent、evidence、confidence、scene_change、merge_eligible、event_goal、"
                    "boundary_role、reason、state_linkage、shared_subjects、shared_prop、shared_location。"
                    "relation_type 只能是 连续动作、反应镜头、视觉呼应、时间省略、场景转换、未知。"
                    "observed_edit_fact 只能写画面可见的剪辑事实；possible_narrative_intent 是可能解释，"
                    "不得把推测写成事实。evidence 引用镜头号及帧时间；没有证据时关系写未知。"
                    "scene_change 仅在地点、时间或连续事件改变时为 true，硬切和景别变化不自动开场次。"
                    "merge_eligible 只有同一明确动作目标、共同参与者/道具/地点且左镜结束状态能衔接右镜初始状态，"
                    "有明确边界帧证据且confidence>=0.8时为true；未知、时间省略、场景转换、状态冲突为false。"
                    "boundary_role 只能是 action-detail、continuation、independent-reaction、reveal、unknown；"
                    "手部/道具特写若在覆盖同一动作可为action-detail；独立反应、线索揭示或结果展示必须另立事件。"
                    "event_goal 必须是有证据支持的同一明确目标及连续因果链，不能只因人物或杯子相同就合并。"
                    "有逐镜状态与边界帧证据时，递给、接过特写、继而饮用可服务同一明确目标；"
                    "饮用若另起目标或缺少连续证据应独立；异常反应、独立情绪/线索揭示必须另立事件。"
                    "递杯中男孩转女人可为共同参与者和同一杯子状态转移，不能因动作主语改变而误判。"
                    "state_linkage={left_final_state,right_initial_state,compatible,evidence}，两状态逐字采用已保存逐镜观察；"
                    "只在可见状态匹配时compatible=true；shared_subjects为事件共同参与者数组，shared_prop/shared_location为可见共同道具/地点。"
                    "严格类型：evidence与state_linkage.evidence为字符串数组；event_goal/shared_prop/shared_location为字符串，未知用空字符串而非null。"
                    "同一事件的event_goal、shared_prop、shared_location统一称呼，不能逐对改写导致同一事件断开。"
                    "状态衔接不要求动作文字相同，接过后举杯是自然连续变化；两端状态逐字引用保存值，并用边界帧说明衔接。"
                    "手部特写看不到地点不等于换地点，可用两侧环境边界帧判断共同地点；画外参与者不得假称在该特写中可见。"
                    "context_evidence数组为共同人物/道具/地点提供已保存观察的逐字引用："
                    "{kind:subject/prop/location,value:与shared字段一致的统一称呼,shot_id,source_field,quote}；"
                    "source_field只能primary_subject/people/objects/scene/initial_state/final_state/interaction_target。"
                    "人物称呼可能是女子/女性/女孩，请统一当前事件称呼并引用真实来源；不要靠称呼字面相同判断身份。"
                    "shared_subjects使用简洁角色称呼，例如女子、男孩，不附发型服装；每个无法在两镜保存字段中逐字找到的称呼必须有对应context_evidence，"
                    "可引用initial_state或final_state里的实际参与者文字；不得漏掉shared_subjects中的画外共同参与者证据。"
                    "特写不见地点时只引用邻镜的地点，并在关系evidence说明特写属于同一连续交接；未知不要补造引用。"
                    "保留真实切点、景别和机位，不把动作连续解释为摄影机无剪辑连续。"
                    f"\n指定相邻镜头对：{json.dumps([[left['id'], right['id']] for left, right in eligible_pairs], ensure_ascii=False)}"
                    f"\n逐镜观察：{json.dumps(facts, ensure_ascii=False)}"
                )
                pending = {(a['id'], b['id']) for a, b in eligible_pairs}
                # A valid pair may use aliases inconsistent with its neighboring
                # pair. Correct the WHOLE affected chain once, not its semantics
                # by string guessing. Preserve valid fallback relations if the
                # correction fails; independent reactions are never reconsidered.
                coherence_fallbacks = {}
                contexts = {}
                window_deadline = time.monotonic() + 90
                failures = []
                for attempt in range(2):
                    # One bounded caller-owned correction, no hidden HTTP retries.
                    phase = 'montage.relations' if attempt == 0 else 'montage.correction'
                    request = prompt if attempt == 0 else (
                        prompt + '\n仅纠正下列未通过结构、引用或跨对称呼一致性校验的相邻关系，其他关系已保存，不要重复：'
                        + json.dumps(sorted(pending), ensure_ascii=False)
                        + '\n校验错误：' + json.dumps(failures, ensure_ascii=False))
                    try:
                        if time.monotonic() >= window_deadline:
                            raise TimeoutError('本窗口跨镜分析总预算90秒已耗尽')
                        raw = self._call(request, inputs, phase, max_tokens=4096,
                                         schema=MontageResponse, max_attempts=1,
                                         timeout_seconds=min(60, max(.001, window_deadline - time.monotonic())))
                        raw_responses.append(raw)  # Always retain even invalid model output.
                        from app.services.montage_wire import parse_montage_payload, repair_context_evidence
                        payload, root_repairs = parse_montage_payload(raw)
                        if root_repairs:
                            audits.append({'phase': phase, 'status': 'root-format-repaired', 'repairs': root_repairs})
                        items = payload.get('relations')
                        if not isinstance(items, list):
                            raise ValueError('relations必须为数组')
                        failures = []
                        seen = set()
                        for item in items:
                            pair = (item.get('left_shot_id'), item.get('right_shot_id')) if isinstance(item, dict) else (None, None)
                            try:
                                if pair not in pending or pair in seen:
                                    raise ValueError('关系重复、非指定相邻对或已保存，不能桥接/改写')
                                seen.add(pair)
                                normalized, repairs = normalize_relation(item)
                                relation = MontageRelation.model_validate(normalized)
                                if relation.merge_eligible and (relation.relation_type != '连续动作'
                                        or relation.boundary_role in ('independent-reaction', 'reveal') or relation.scene_change):
                                    relation.merge_eligible = False
                                    repairs.append('protected-boundary-disables-merge')
                                if relation.scene_change and not relation.evidence:
                                    raise ValueError('场次变化缺少帧证据')
                                if relation.merge_eligible:
                                    from app.schemas import Shot
                                    from app.services.event_sequences import _context
                                    pair_shots = []
                                    for identifier in pair:
                                        source = next(s for s in visible_window if s['id'] == identifier)
                                        observation = source.get('observation', {})
                                        pair_shots.append(Shot(id=identifier, number=source['number'],
                                            start_time=source['start_time'], end_time=source['end_time'],
                                            duration=source['end_time']-source['start_time'], time='',
                                            status=source.get('status', 'ready'), action=observation.get('subject_action', ''),
                                            action_details=observation.get('action_details') or {},
                                            visual_facts=observation.get('visual_facts') or {}))
                                    relation, reference_repairs = repair_context_evidence(relation, pair_shots)
                                    repairs.extend(reference_repairs)
                                    if _context(*pair_shots, relation) is None:
                                        raise ValueError('合并上下文引用未通过：两端状态须逐字保存；shared_subjects/shared_prop/shared_location '
                                            '必须在真实保存字段中可找到，别名或特写未知地点必须用context_evidence引用有效source_field和逐字quote。'
                                            '人物可引用interaction_target，地点必须引用scene。找不到时merge_eligible=false，不补造证据。')
                                relations.append(relation)
                                if relation.merge_eligible:
                                    contexts[pair] = _context(*pair_shots, relation)
                                pending.remove(pair)
                                audits.append({'pair': list(pair), 'phase': phase, 'repairs': repairs, 'status': 'valid'})
                            except (ValueError, TypeError) as exc:
                                failures.append({'pair': list(pair), 'error': str(exc)[:700]})
                        if failures:
                            audits.append({'phase': phase, 'status': 'discarded-invalid-items', 'errors': failures})
                        if attempt == 0:
                            adjacent = [(a['id'], b['id']) for a, b in eligible_pairs]
                            affected = set()
                            for first, second in zip(adjacent, adjacent[1:]):
                                if first[1] != second[0] or first not in contexts or second not in contexts:
                                    continue
                                from app.services.event_sequences import _chain_context_matches
                                a, b = contexts[first], contexts[second]
                                if not _chain_context_matches(a, b):
                                    affected.update((first, second))
                                    failures.append({'pairs': [list(first), list(second)],
                                        'error': '两对均有连续动作证据，但同一事件的目标、地点或人物称呼不同。'
                                            '结合整个事件与原始帧确认是否同一因果链；若是，统一两对的event_goal/shared_prop/shared_location/shared_subjects，'
                                            '每对使用自己的真实状态和context_evidence。若不是，明确保留边界。',
                                        'saved_contexts': [a, b]})
                            for pair in affected:
                                existing = next(r for r in relations if (r.left_shot_id, r.right_shot_id) == pair)
                                coherence_fallbacks[pair] = existing
                                relations.remove(existing)
                                pending.add(pair)
                            if affected:
                                audits.append({'phase': phase, 'status': 'coherence-correction-needed', 'pairs': sorted(affected)})
                        if not pending:
                            break
                    except Exception as exc:
                        failures = [{'error': str(exc)[:700]}]
                    audits.append({'phase': phase, 'status': 'partial', 'pending': sorted(pending), 'errors': failures})
                for pair in list(pending):
                    if pair in coherence_fallbacks:
                        relations.append(coherence_fallbacks[pair])
                        pending.remove(pair)
                        unresolved_chains.append(list(pair))
                        audits.append({'pair': list(pair), 'status': 'valid-pair-retained-unresolved-chain',
                            'reason': '单对证据有效，跨对称呼未修复；本地仍保守保留链边界'})
                rejected_pairs.extend(sorted(pending))
                if start + 8 >= len(shots):
                    break
        order = {shot['id']: i for i, shot in enumerate(shots)}
        relations.sort(key=lambda r: order[r.left_shot_id])
        return {"relations": [relation.model_dump() for relation in relations], "raw_responses": raw_responses,
                "validation_audit": audits, "rejected_pairs": rejected_pairs,
                "unresolved_chain_pairs": unresolved_chains,
                "status": "partial" if rejected_pairs or unresolved_chains else "completed", "version": "montage-relations.v4",
                "scene_starts": [shots[0]["number"], *[
                    next(shot["number"] for shot in shots if shot["id"] == relation.right_shot_id)
                    for relation in relations if relation.scene_change
                ]] if shots else []}

    def analyze_short_clip(self, clip_path: Path) -> Dict[str, Any]:
        """Explicit evaluation only; normal jobs never upload video files."""
        from app.services.video_processor import probe_video  # noqa: PLC0415

        if probe_video(clip_path)["duration"] >= 5.0:
            raise ValueError("只允许严格短于 5 秒的独立短片段")
        raw = self._call(
            "仅分析这段无音轨短视频的可见运动。输出唯一 JSON 对象，字段为主体、动作顺序、运镜、可见特效、逐秒证据、不确定点。不得推断声音或画外事件。",
            [], "video.short_clip_probe", max_tokens=1536, video_clip_path=clip_path,
        )
        return {"prediction": self.helper._parse_payload(raw), "raw_response": raw}

    def drain_usage(self) -> List[Dict[str, Any]]:
        records, self.usage = self.usage, []
        return records

    def unload(self) -> None:
        self._closed = True
        self.client.close()
