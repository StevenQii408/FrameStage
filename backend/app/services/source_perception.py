"""Bounded perception spans retain one physical source shot and exact source PTS."""
from __future__ import annotations

import copy
import hashlib
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app.perception_models import (PerceptionRecord, PerceptionStageRecord, VisualObservationV2,
    merge_stages, observation_review, project_v1, unknown_field, validate_evidence)
from app.providers.base import FrameInput
from app.services.perception import PerceptionOrchestrator, safe_stage_error
from app.services.visual_tools import PerceptionBudget
from app.services.video_processor import _run, probe_frame_times, probe_video


VERSION = "source-span-perception.v1"


def _span_frames(provider, context, span, state):
    """Four distinct time-ordered source PTS; published as evidence, never cards."""
    if not context.video_path or not context.artifact_dir:
        raise ValueError("长来源镜头缺少原视频或持久化证据目录")
    video = str(Path(context.video_path))
    cache = state.setdefault("source_frame_times", {})
    if video not in cache:
        metadata = probe_video(Path(video))
        cache[video] = probe_frame_times(Path(video), metadata["duration"])
    start, end = span["start_time"], span["end_time"]
    pts = [value for value in cache[video] if start <= value < end]
    if not pts:
        raise ValueError("该分析区间没有可验证的帧时间戳，保留待分析")
    targets = [start + (end-start)*fraction for fraction in (.1, .35, .65, .9)]
    selected = []
    for target in targets:
        candidate = min(pts, key=lambda value: abs(value-target))
        if candidate not in selected:
            selected.append(candidate)
    for candidate in pts:
        if len(selected) >= min(4, len(pts)):
            break
        if candidate not in selected:
            selected.append(candidate)
    selected.sort()
    root = Path(context.artifact_dir) / "analysis"
    root.mkdir(parents=True, exist_ok=True)
    frames = []
    edge = provider.settings.frame_max_long_edge
    vf = f"scale=w='min({edge},iw)':h='min({edge},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuvj420p"
    for timestamp in selected:
        frame_id = f"{context.shot_id}-analysis-{uuid.uuid4().hex[:12]}"
        output = root / f"{frame_id}.jpg"
        remaining = state["deadline"]-time.monotonic()
        if remaining <= 0:
            raise TimeoutError("项目长镜头分析时间预算已耗尽")
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.9f}",
              "-i", video, "-frames:v", "1", "-vf", vf, "-q:v", "4", "-y", str(output)], timeout=remaining)
        with Image.open(output) as image:
            width, height = image.size
        frame = FrameInput(frame_id, timestamp, str(output), width, height, "analysis")
        if context.register_frame:
            context.register_frame(frame)
        frames.append(frame)
    return frames


def _aggregate(provider, frames, context, entries, *, used_calls, elapsed, previous=None):
    usable = [(entry, PerceptionRecord.model_validate(entry["perception_record"]))
              for entry in entries if entry.get("perception_record")]
    if usable:
        # A representative static observation remains local to its cited frame.
        _, best = max(usable, key=lambda pair: sum(field.observable for field in (
            pair[1].observation.facts.primary_subject, pair[1].observation.facts.scene)))
        observation = best.observation.model_copy(deep=True)
        actions = [record.observation.dynamics.subject_action for _, record in usable
                   if record.observation.dynamics.subject_action.observable]
        if actions:
            refs = {ref.frame_id: ref for field in actions for ref in field.evidence}
            observation.dynamics.subject_action = type(actions[0]).model_validate({
                "value": "；".join(field.value.rstrip("。；") for field in actions),
                "observable": True, "confidence": min(field.confidence for field in actions),
                "evidence": [ref.model_dump() for ref in sorted(refs.values(), key=lambda ref: ref.timestamp)],
                "uncertainties": []})
        first = usable[0][1].observation.dynamics
        last = usable[-1][1].observation.dynamics
        observation.dynamics.initial_state = first.initial_state.model_copy(deep=True) if entries[0].get("perception_record") else type(first.initial_state).model_validate(unknown_field(reason="最早区间尚未分析"))
        observation.dynamics.final_state = last.final_state.model_copy(deep=True) if entries[-1].get("perception_record") else type(last.final_state).model_validate(unknown_field(reason="最后区间尚未分析"))
        steps = [record.observation.dynamics.subject_action for _, record in usable
                 if record.observation.dynamics.subject_action.observable]
        if 0 < len(steps) <= 4:
            refs = {ref.frame_id: ref for field in steps for ref in field.evidence}
            observation.dynamics.action_steps = type(observation.dynamics.action_steps).model_validate(dict(
                value=[field.value for field in steps], observable=True, confidence=min(field.confidence for field in steps),
                evidence=[ref.model_dump() for ref in refs.values()], uncertainties=[]))
        else:
            observation.dynamics.action_steps = type(observation.dynamics.action_steps).model_validate(unknown_field([], "有序动作详见各分析区间，未截断动作列表"))
        observation.dynamics.action_phases = []
        observation.dynamics.step_evidence = []
        observation.dynamics.accompanying_states = type(observation.dynamics.accompanying_states).model_validate(unknown_field([], "区间状态不推定为全来源持续状态"))
    else:
        observation = merge_stages(None, None)
    raw, tools, catalogue = [], [], {frame.id: frame for frame in frames}
    for entry, record in usable:
        raw.extend(copy.deepcopy(record.raw_responses))
        tools.extend(copy.deepcopy(record.tools))
        for row in record.input_frames:
            catalogue.setdefault(row["frame_id"], FrameInput(row["frame_id"], row["timestamp"], "",
                row.get("width", 0), row.get("height", 0), row.get("artifact_kind", "analysis"), row.get("parent_frame_id")))
    if previous and not previous.analysis_span_observations:
        raw = [*copy.deepcopy(previous.raw_responses), *raw]
    pending = any(entry["status"] != "ready" for entry in entries)
    static_ready = observation.facts.primary_subject.observable and observation.facts.scene.observable
    record = PerceptionRecord(observation=observation, raw_responses=raw, tools=tools,
        stages={"static": PerceptionStageRecord(state="succeeded" if static_ready else "partial"),
                "motion": PerceptionStageRecord(state="partial" if pending else "succeeded")},
        analysis_span_observations=copy.deepcopy(entries),
        budgets={"strategy": VERSION, "used_model_calls": used_calls, "analysis_span_count": len(entries),
                 "total_record_model_calls": len(raw),
                 "pending_span_count": sum(entry["status"] != "ready" for entry in entries),
                 "source_start_time": context.start_time, "source_end_time": context.end_time},
        trace=[{"state": "bounded_source_perception", "span_count": len(entries)}],
        input_frames=[{"frame_id": frame.id, "timestamp": frame.timestamp, "width": frame.width,
                       "height": frame.height, "artifact_kind": frame.artifact_kind} for frame in catalogue.values()],
        elapsed_seconds=elapsed)
    record.budgets["review"] = observation_review(observation, record)
    if pending:
        observation.warnings = ["长来源镜头存在待分析区间，已保留全部来源时间及有效静态、动态结果"]
    validate_evidence(observation, list(catalogue.values()))
    projected = project_v1(observation, partial_failure=pending)
    projected.perception_record = record
    projected.analysis_review = record.budgets["review"]
    if "analysis_span_observations" in type(projected).model_fields:
        projected.analysis_span_observations = copy.deepcopy(entries)
    return projected


def analyze_source(provider, frames, context, *, event_config=None, shared_budget=None,
                   max_windows=12, max_model_calls=36, timeout_seconds=600, seed_record=None, cancelled=None,
                   selected_stages=None, only_unexecuted=False):
    """Analyze each bounded span; exhausted intervals remain explicit pending.

    Shared counters can be held for the whole job. No source shot/card/cut is
    created, and no model call is made for a ready persisted span on resume.
    """
    from app.event_agent.models import EventAgentConfig
    from app.event_agent.spans import span_plan
    config = event_config or EventAgentConfig()
    source = SimpleNamespace(id=context.shot_id, number=context.number, start_time=context.start_time,
                             end_time=context.end_time, source_raw_shot_ids=[])
    plan, spans = span_plan([source], config)
    if len(plan) == 1:
        return provider.analyze_shot(frames, context)
    state = shared_budget if shared_budget is not None else {}
    started = time.monotonic()
    state.setdefault("started_at", started)
    state.setdefault("deadline", state["started_at"] + timeout_seconds)
    state.setdefault("used_model_calls", 0)
    state.setdefault("used_windows", 0)
    previous = PerceptionRecord.model_validate(seed_record) if isinstance(seed_record, dict) else seed_record
    saved = {(entry["start_time"], entry["end_time"]): entry for entry in previous.analysis_span_observations} if previous else {}
    entries = []
    for view in plan:
        span = spans[view.id]
        existing = saved.get((span["start_time"], span["end_time"]))
        entries.append(copy.deepcopy(existing) if existing else {
            **span, "id": view.id, "status": "pending_review", "observation": None,
            "perception_record": None, "frame_ids": []})
    calls_before = state["used_model_calls"]
    for entry in entries:
        if entry["status"] == "ready":
            continue
        if only_unexecuted and entry.get("perception_record"):
            continue
        if entry.get("perception_record"):
            from app.services.perception import recoverable_stages
            recoverable = recoverable_stages(entry["perception_record"])
            if not recoverable or (selected_stages is not None and not set(selected_stages) & set(recoverable)):
                continue
        remaining_calls = max_model_calls-state["used_model_calls"]
        if state["used_windows"] >= max_windows or remaining_calls < 2 or time.monotonic() >= state["deadline"] or (cancelled and cancelled()):
            entry["pending_reason"] = "项目分析预算耗尽或任务取消；来源区间已保留，可明确恢复"
            continue
        state["used_windows"] += 1
        report = getattr(context, "report_perception_progress", None)
        if callable(report):
            report("shot.span", "started")
        orchestrator = None
        accounted = False
        try:
            images = _span_frames(provider, context, entry, state)
            remaining = state["deadline"]-time.monotonic()
            # PerceptionOrchestrator already bounds each stage and one extra.
            limited = copy.copy(provider)
            limited.settings = replace(provider.settings, vlm_timeout_seconds=min(provider.settings.vlm_timeout_seconds, remaining))
            subcontext = replace(context, start_time=entry["start_time"], end_time=entry["end_time"],
                artifact_dir=str(Path(context.artifact_dir) / "span-tools" / hashlib.sha256(entry["id"].encode()).hexdigest()[:12]),
                source_edit_times=[point for point in context.source_edit_times if entry["start_time"] < point < entry["end_time"]])
            subcontext.checkpoint_perception = None
            budget = PerceptionBudget(max_corrections=int(remaining_calls >= 3), max_supplemental_calls=int(remaining_calls >= 3))
            window_seed = entry.get("perception_record")
            if selected_stages is not None and window_seed is None:
                window_seed = PerceptionRecord(observation=merge_stages(None, None),
                    stages={name: PerceptionStageRecord(state="deferred") for name in ("static", "motion")})
            orchestrator = PerceptionOrchestrator(limited, subcontext, images, budget, cancelled=cancelled,
                seed_record=window_seed, selected_stages=selected_stages)
            if window_seed:
                saved_record = PerceptionRecord.model_validate(window_seed) if isinstance(window_seed, dict) else window_seed
                known = {frame.id: frame for frame in frames}
                for row in saved_record.input_frames:
                    frame_id = row["frame_id"]
                    orchestrator.tools.frames.setdefault(frame_id, known.get(frame_id) or FrameInput(
                        frame_id, row["timestamp"], "", row.get("width", 0), row.get("height", 0),
                        row.get("artifact_kind", "analysis"), row.get("parent_frame_id")))
            result = orchestrator.run()
            record = result.perception_record
            state["used_model_calls"] += record.budgets["used_model_calls"]
            accounted = True
            quality = observation_review(record.observation, record)
            entry.update(status="ready" if all(quality[name]["status"] == "ready" for name in ("static", "dynamic")) else "pending_review",
                observation=record.observation.model_dump(mode="json"), perception_record=record.model_dump(mode="json"),
                frame_ids=[frame.id for frame in images])
        except Exception as exc:
            if orchestrator is not None and not accounted:
                state["used_model_calls"] += orchestrator.operation_calls
            entry["pending_reason"] = safe_stage_error(exc)["message"]
        checkpoint = getattr(context, "checkpoint_perception", None)
        if callable(checkpoint):
            partial = _aggregate(provider, frames, context, entries, used_calls=state["used_model_calls"]-calls_before,
                                 elapsed=time.monotonic()-started, previous=previous)
            checkpoint(partial.perception_record)
    return _aggregate(provider, frames, context, entries, used_calls=state["used_model_calls"]-calls_before,
                      elapsed=time.monotonic()-started, previous=previous)
