"""Execution adapter for explicitly selected historical script-v2 tasks."""
from __future__ import annotations

import json
from app.schemas import ActionDetails, JobConfig, SpatialEvidence, VisionObservation
from app.services.prompt_service import build_prompts
from app.services.script_processor import extract_script_beats, fallback_window_plan, renumber_and_limit, script_windows


def run_legacy_script(service, row, job_config: JobConfig) -> None:
    job_id = row["id"]
    beats = extract_script_beats(row["script_text"] or "", 10_000)
    if not beats:
        raise RuntimeError("剧本文本为空，无法拆分分镜")
    if len(beats) > job_config.max_shots * 4:
        raise RuntimeError(f"剧本包含 {len(beats)} 个动作节拍，超过最多 {job_config.max_shots * 4} 个节拍的安全上限")
    service.db.update_job(job_id, status="processing", stage="segmenting", progress=10)
    windows = script_windows(beats, size=12, overlap=2)
    planned = []
    raw_responses = []
    fallback_windows = []
    reconciled_windows = []
    planning_warnings = []
    health = service.provider.health()
    use_model = health.provider == "bailian" and health.ready
    if health.provider == "bailian" and not health.ready:
        planning_warnings.append(f"模型剧本规划不可用，已使用规则兜底：{health.detail or 'Provider 未就绪'}")
    for window_index, window in enumerate(windows, start=1):
        plan = None
        if health.provider == "mock" and health.ready:
            try:
                result = service.provider.plan_script(window.beats, window.target_beat_ids)
                plan = result.plan
                raw_responses.extend(result.raw_responses)
                fallback_windows.append(window_index)
            except Exception as exc:
                raw_responses.extend(getattr(exc, "raw_responses", []))
                planning_warnings.append(f"Mock 剧本窗口 {window_index} 规划失败，已使用规则兜底：{exc}")
        if use_model and service._can_start_provider_inference(health):
            try:
                with service._inference_slot():
                    result = service.provider.plan_script(window.beats, window.target_beat_ids)
                plan = result.plan
                raw_responses.extend(result.raw_responses)
                if result.rule_reconciled:
                    reconciled_windows.append(window_index)
                    planning_warnings.append(f"剧本窗口 {window_index} 的模型分组已按规则保护边界校正")
            except Exception as exc:
                raw_responses.extend(getattr(exc, "raw_responses", []))
                planning_warnings.append(f"剧本窗口 {window_index} 的模型规划失败，已局部兜底：{exc}")
        elif use_model:
            planning_warnings.append(f"剧本窗口 {window_index} 可用内存不足，已局部使用规则兜底")
        if plan is None:
            plan = fallback_window_plan(window.beats, window.target_beat_ids, job_config.max_shots)
            fallback_windows.append(window_index)
        planned.extend(plan.shots)
        service._set_stage(job_id, "segmenting", int(10 + window_index / max(len(windows), 1) * 25))
        if service._is_cancelled(job_id):
            return
    try:
        segments = renumber_and_limit(planned, job_config.max_shots)
    except ValueError:
        planning_warnings.append(
            f"模型规划超过 {job_config.max_shots} 镜上限，已用最多四节拍一镜的规则重新编排全篇"
        )
        segments = fallback_window_plan(beats, [beat.id for beat in beats], job_config.max_shots).shots
        fallback_windows = list(range(1, len(windows) + 1))
    if not fallback_windows and not reconciled_windows:
        profile = "script-v2-cloud"
    elif len(fallback_windows) == len(windows) and not reconciled_windows:
        profile = "script-v2-rules"
    else:
        profile = "script-v2-hybrid"
    serialized_plan = {"beats": [beat.model_dump() for beat in beats], "shots": [shot.model_dump() for shot in segments], "window_count": len(windows), "fallback_windows": fallback_windows, "reconciled_windows": reconciled_windows}
    service.db.update_job(
        job_id, segmentation_profile=profile, raw_shot_count=len(beats), narrative_shot_count=len(segments),
        script_plan_json=json.dumps(serialized_plan, ensure_ascii=False),
        script_raw_response_json=json.dumps(raw_responses, ensure_ascii=False),
        planning_warnings_json=json.dumps(planning_warnings, ensure_ascii=False),
    )
    service._emit(job_id, "source.ready", {"input_type": "script", "segment_count": len(segments), "beat_count": len(beats), "segmentation_profile": profile})
    for warning in planning_warnings:
        service._emit(job_id, "job.warning", {"message": warning})
    total = len(segments)
    beat_by_id = {beat.id: beat for beat in beats}
    cursor = 0.0
    for index, segment in enumerate(segments):
        if service._is_cancelled(job_id):
            return
        number = index + 1
        shot_id = service._shot_id(job_id, number)
        source_text = "；".join(beat_by_id[beat_id].text for beat_id in segment.source_beat_ids if beat_id in beat_by_id)
        duration = float(segment.duration_seconds)
        start_time, end_time = cursor, cursor + duration
        cursor = end_time
        movement = segment.camera_movement_type
        if segment.camera_movement_detail and segment.camera_movement_detail != segment.camera_movement_type:
            movement = f"{movement}：{segment.camera_movement_detail}"
        lighting = segment.lighting_detail or segment.lighting_type
        warnings = list(dict.fromkeys([*segment.warnings, *(planning_warnings if index == 0 else [])]))
        observation = {
            "subject_action": "；".join(segment.action_steps),
            "shot_size": segment.shot_size,
            "lens": segment.lens,
            "camera_movement": movement,
            "lighting_vibe": lighting,
            "lighting_type": segment.lighting_type,
            "lighting_detail": segment.lighting_detail,
            "composition": segment.composition,
            "confidence": segment.confidence,
            "action_details": ActionDetails(primary_subject=segment.primary_subject, initial_state=segment.first_frame_state, steps=segment.action_steps, interaction_target=segment.interaction_target, final_state=segment.end_state, emotion_change=segment.emotion_change, evidence=[f"剧本节拍：{', '.join(segment.source_beat_ids)}"]).model_dump(),
            "spatial_evidence": SpatialEvidence(foreground=segment.foreground, midground=segment.midground, background=segment.background, relationships=segment.spatial_relationships, observable=False).model_dump(),
            "raw_response": None,
            "analysis_warnings": warnings,
        }
        shot_status = "warning" if warnings else "ready"
        service.db.execute(
            """INSERT INTO shots (id,job_id,number,start_time,end_time,duration,time,source_text,status,observation_json,scene_group_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET start_time=excluded.start_time,end_time=excluded.end_time,duration=excluded.duration,time=excluded.time,source_text=excluded.source_text,observation_json=excluded.observation_json,status=excluded.status,scene_group_id=excluded.scene_group_id""",
            (shot_id, job_id, number, start_time, end_time, duration, f"{start_time:06.2f} – {end_time:06.2f}", source_text, shot_status, json.dumps(observation, ensure_ascii=False), segment.scene_group_id),
        )
        service._emit(job_id, "shot.created", {"shot_id": shot_id, "number": number, "start_time": start_time, "end_time": end_time, "scene_group_id": segment.scene_group_id})
        service._emit(job_id, "shot.description_ready", {"shot_id": shot_id, "description": observation["subject_action"], "source_text": source_text})
        prompts = build_prompts(shot_id, VisionObservation.model_validate(observation), (row["script_text"] or "")[:3000], generation_duration_seconds=duration, style_strategy=json.loads(row["config_json"]).get("style_strategy", "legacy-v1"))
        for prompt in prompts:
            service.db.execute("INSERT OR REPLACE INTO prompts (id,job_id,shot_id,prompt_type,platform,version,positive_prompt,negative_prompt,status,template_id,template_version,warnings_json,contract_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (prompt.id, job_id, prompt.shot_id, prompt.prompt_type, prompt.platform, prompt.version, prompt.positive_prompt, prompt.negative_prompt, prompt.status, prompt.template_id, prompt.template_version, json.dumps(prompt.warnings, ensure_ascii=False), json.dumps(prompt.contract.model_dump(), ensure_ascii=False) if prompt.contract else None))
            service._emit(job_id, "prompt.ready", {"prompt_id": prompt.id, "shot_id": shot_id, "prompt_type": prompt.prompt_type, "status": prompt.status})
        service._set_stage(job_id, "prompting", int(35 + ((index + 1) / total) * 60))
    service._set_stage(job_id, "styling", 95)
    try:
        service._draft_style(job_id, json.loads(row["config_json"]).get("style_strategy"))
    except Exception as exc:
        service._emit(job_id, "job.warning", {"message": f"剧本画风草案未生成，分镜与 Prompt 已保留：{exc}"})
    service.db.update_job(job_id, status="completed", stage="completed", progress=100, error=None)
    service._build_event_sequences(job_id)
    service._emit(job_id, "job.completed", {"progress": 100, "input_type": "script"})
