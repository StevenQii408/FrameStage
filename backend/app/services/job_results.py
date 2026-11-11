"""Build public analysis results from saved observations without inference."""
from __future__ import annotations

import json
from typing import Optional
from app.perception_models import VisualObservationV2
from app.services.event_sequences import derive_sequences_safe, sequence_payload_view
from app.schemas import JobConfig, JobStatus, PromptBundle, Shot, StoryboardResult
from app.style_direct.models import StyleProfileV3
from app.services.scene_contract import build_action_atoms, build_scene_groups, validate_action_coverage
from app.services.script_processor import extract_script_beats, parse_script


def build_job_result(service, job_id: str) -> Optional[StoryboardResult]:
    row = service.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not row:
        return None
    config = JobConfig(**json.loads(row["config_json"]))
    job = JobStatus(id=row["id"], status=row["status"], stage=row["stage"], progress=row["progress"], video_filename=row["video_filename"], input_type=row["input_type"], script_text=row["script_text"], provider=row["provider"], model=row["model"], segmentation_profile=row["segmentation_profile"], style_strategy=json.loads(row["config_json"]).get("style_strategy", "legacy-v1"), raw_shot_count=row["raw_shot_count"], narrative_shot_count=row["narrative_shot_count"], error=row["error"], result_version=row["result_version"], created_at=row["created_at"], updated_at=row["updated_at"], config=config)
    shot_rows = service.db.fetchall("SELECT * FROM shots WHERE job_id = ? ORDER BY number", (job_id,))
    prompt_rows = service.db.fetchall("SELECT * FROM prompts WHERE job_id = ? ORDER BY shot_id, prompt_type", (job_id,))
    frame_rows = service.db.fetchall("SELECT * FROM frames WHERE job_id = ? ORDER BY shot_id, timestamp", (job_id,))
    raw_rows = service.db.fetchall("SELECT * FROM raw_shots WHERE job_id = ? ORDER BY number", (job_id,))
    prompts = [PromptBundle(id=r["id"], shot_id=r["shot_id"], prompt_type=r["prompt_type"], platform=r["platform"], version=r["version"], positive_prompt=r["positive_prompt"], negative_prompt=r["negative_prompt"], status=r["status"], template_id=r["template_id"], template_version=r["template_version"], warnings=json.loads(r["warnings_json"]), contract=json.loads(r["contract_json"]) if r["contract_json"] else None) for r in prompt_rows]
    frames = [{"id": r["id"], "shot_id": r["shot_id"], "timestamp": r["timestamp"], "path": f"/api/jobs/{job_id}/frames/{r['id']}", "thumbnail_path": f"/api/jobs/{job_id}/frames/{r['id']}", "width": r["width"], "height": r["height"], "blur_score": r["blur_score"], "brightness_score": r["brightness_score"], "selected": bool(r["selected"]), "artifact_kind": r["artifact_kind"], "parent_frame_id": r["parent_frame_id"], "bbox": json.loads(r["bbox_json"]) if r["bbox_json"] else None} for r in frame_rows]
    evidence_frames = [frame for frame in frames if not frame["selected"]]
    frames = [frame for frame in frames if frame["selected"]]
    shots = []
    if row["input_type"] == "script" and json.loads(row["config_json"]).get("script_strategy") != "director-v3":
        segments = parse_script(row["script_text"] or "", config.max_shots)
        recovery_beats = {beat.id: beat for beat in extract_script_beats(row["script_text"] or "", config.max_shots * 4)}
        prompt_shot_ids = list(dict.fromkeys(prompt.shot_id for prompt in prompts))
        existing_shot_ids = {shot["id"] for shot in shot_rows}
        missing_shot_ids = [shot_id for shot_id in prompt_shot_ids if shot_id not in existing_shot_ids]
        if not shot_rows and not missing_shot_ids:
            missing_shot_ids = [f"{job_id}-shot-{index + 1:03d}" for index in range(len(segments))]
        for index, shot_id in enumerate(missing_shot_ids):
            number_match = shot_id.rsplit("-", 1)[-1]
            number = int(number_match) if number_match.isdigit() else index + 1
            segment = segments[number - 1] if 0 < number <= len(segments) else (segments[index] if index < len(segments) else None)
            source_text = "；".join(recovery_beats[beat_id].text for beat_id in segment.source_beat_ids if beat_id in recovery_beats) if segment else ""
            action = "；".join(segment.action_steps) if segment else "等待分镜描述"
            start_time = float(number - 1) * 4.5
            duration = float(segment.duration_seconds) if segment else 4.5
            movement = f"{segment.camera_movement_type}：{segment.camera_movement_detail}" if segment and segment.camera_movement_detail else (segment.camera_movement_type if segment else "")
            shots.append(Shot(id=shot_id, number=number, start_time=start_time, end_time=start_time + duration, duration=duration, time=f"{start_time:06.2f} – {start_time + duration:06.2f}", source_text=source_text, action=action, shot_size=segment.shot_size if segment else "", lens=segment.lens if segment else "", camera_movement=movement, lighting_vibe=(segment.lighting_detail or segment.lighting_type) if segment else "", composition=segment.composition if segment else "", confidence=segment.confidence if segment else 0.45, status="warning", frame_ids=[], prompts=[prompt for prompt in prompts if prompt.shot_id == shot_id], analysis_warnings=["从旧任务数据恢复的剧本分镜，需要人工复核"]))
    for r in shot_rows:
        observation = json.loads(r["observation_json"] or "{}")
        shots.append(Shot(id=r["id"], script_direction=observation.get("script_direction"), timing_origin="script-performance" if observation.get("script_direction") else "source-video", number=r["number"], start_time=r["start_time"], end_time=r["end_time"], duration=r["duration"], time=r["time"], source_text=r["source_text"], action=observation.get("subject_action", ""), shot_size=observation.get("shot_size", ""), lens=observation.get("lens", ""), camera_movement=observation.get("camera_movement", ""), lighting_vibe=observation.get("lighting_vibe", ""), composition=observation.get("composition", ""), confidence=observation.get("confidence", 0), status=r["status"], frame_ids=[f["id"] for f in frames if f["shot_id"] == r["id"]], prompts=[p for p in prompts if p.shot_id == r["id"]], analysis_warnings=observation.get("analysis_warnings", []), action_details=observation.get("action_details", {}), spatial_evidence=observation.get("spatial_evidence", {}), visual_style_evidence=observation.get("visual_style_evidence", {}), visual_facts=observation.get("visual_facts", {}), source_raw_shot_ids=json.loads(r["source_raw_shot_ids_json"] or "[]"), scene_group_id=r["scene_group_id"], cut_reason=r["cut_reason"] or "", source_edit_times=json.loads(r["source_edit_times_json"] or "[]"), generation_duration_seconds=r["generation_duration_seconds"], scene_reason=r["scene_reason"] or ""))
    raw_shots = [{"id": r["id"], "number": r["number"], "start_time": r["start_time"], "end_time": r["end_time"], "duration": r["duration"], "boundary_metrics": json.loads(r["boundary_metrics_json"] or "{}"), "decision": r["decision"], "reason": r["reason"], "narrative_shot_id": r["narrative_shot_id"], "generation_shot_ids": json.loads(r["generation_shot_ids_json"] or "[]") or ([r["narrative_shot_id"]] if r["narrative_shot_id"] else [])} for r in raw_rows]
    planning_warnings = json.loads(row["planning_warnings_json"] or "[]") if "planning_warnings_json" in row.keys() else []
    plan_payload = json.loads(row["script_plan_json"] or "{}") if "script_plan_json" in row.keys() else {}
    saved_observations = {item['id']: json.loads(item['observation_json'] or '{}') for item in shot_rows}
    for shot in shots:
        shot.analysis_review = saved_observations.get(shot.id, {}).get('analysis_review', {})
        shot.analysis_span_observations = saved_observations.get(shot.id, {}).get('analysis_span_observations', [])
        if not shot.analysis_span_observations:
            stored = next((item for item in shot_rows if item['id'] == shot.id), None)
            if stored:
                shot.analysis_span_observations = json.loads(stored['perception_json'] or '{}').get('analysis_span_observations', [])
    action_atoms = build_action_atoms(shots, plan_payload if row["input_type"] == "script" else None)
    validate_action_coverage(shots, action_atoms)
    scene_groups = build_scene_groups(shots)
    montage = json.loads(row["montage_json"] or "{}")
    for shot, stored in zip(shots, shot_rows):
        # Video shots correspond to persisted rows; script recovery is legacy-only.
        if row["input_type"] == "video" and stored["perception_json"]:
            shot.visual_observation_v2 = VisualObservationV2.model_validate(json.loads(stored["perception_json"])["observation"])
    perception_runs = {
        stored["id"]: json.loads(stored["perception_json"])
        for stored in shot_rows if stored["perception_json"]
    }
    usage = json.loads(row["model_usage_json"] or "[]")
    measured_costs = [item.get("estimated_list_price_cny") for item in usage]
    cost = sum(item for item in measured_costs if item is not None)
    cost_complete = bool(usage) and all(item is not None for item in measured_costs)
    direct_state = json.loads(row["style_direct_json"]) if row["style_direct_json"] else None
    if direct_state and direct_state.get("analysis_id"):
        analysis_row = service.db.fetchone("SELECT result_json FROM style_analyses WHERE id=?", (direct_state["analysis_id"],))
        direct_usage = json.loads(analysis_row["result_json"] or "{}").get("usage", []) if analysis_row else []
        # Dedicated style runs keep separate audit rows but contribute to project totals.
        usage.extend(direct_usage)
        cost = sum(item.get("estimated_list_price_cny") or 0 for item in usage)
        cost_complete = bool(usage) and all(item.get("estimated_list_price_cny") is not None for item in usage)
    derived = json.loads(row["event_sequence_json"] or "{}")
    if derived:
        derived = sequence_payload_view(derived, shots, prompts, frames)
    elif row["status"] == "completed":
        if row["input_type"] == "script" and json.loads(row["config_json"]).get("script_strategy") == "director-v3":
            from app.script_director.sequences import build_script_sequences
            derived = build_script_sequences(shots, prompts)
        else:
            derived = derive_sequences_safe(shots, frames, prompts, montage,
                strategy=json.loads(row["config_json"]).get("sequence_strategy"), mode="preview")
    script_diagnostics = {"mode": row["segmentation_profile"], "window_count": plan_payload.get("window_count", 0),
        "implementation_version": plan_payload.get("implementation_version"),
        "window_audit": plan_payload.get("window_audit", []),
        "fallback_windows": plan_payload.get("fallback_windows", []), "reconciled_windows": plan_payload.get("reconciled_windows", []),
        "warnings": planning_warnings, "model_calls": plan_payload.get("model_calls", 0),
        "normalization_notes": plan_payload.get("normalization_notes", []),
        "tool_audit": plan_payload.get("tool_audit", []), "document": plan_payload.get("document"),
        "events": plan_payload.get("events", []), "timing_origin": plan_payload.get("timing_origin")}
    from app.services.delivery_quality import delivery_quality, review_requests_view
    sequence_meta = derived.get("sequence_metadata", {})
    if sequence_meta.get("fact_review_requests") and sequence_meta.get("agent_run_id"):
        run = service.db.fetchone("SELECT created_at FROM event_agent_runs WHERE id=? AND job_id=?", (sequence_meta["agent_run_id"], job_id))
        if run:
            operations = service.db.fetchall("SELECT * FROM perception_operations WHERE job_id=?", (job_id,))
            sequence_meta["fact_review_requests"] = review_requests_view(sequence_meta, operations, run["created_at"])
    quality = delivery_quality(row, shots, prompts, derived, direct_state, plan_payload)
    from app.style_direct.inheritance import build_scene_style_overrides
    try:
        baseline = StyleProfileV3.model_validate(direct_state["profile"]) if direct_state and direct_state.get("profile") else None
        scene_style_review = build_scene_style_overrides(shots, baseline)
    except (ValueError, TypeError, KeyError):
        scene_style_review = {"version": "scene-style-review.v1", "scene_overrides": [],
                              "pending_shot_changes": [], "extra_model_calls": 0, "status": "legacy-unavailable"}
    return StoryboardResult(job=job, shots=shots, frames=frames, evidence_frames=evidence_frames, prompts=prompts, raw_shots=raw_shots, action_atoms=action_atoms, scene_groups=scene_groups, montage_relations=montage.get("relations", []), narrative_events=derived.get("narrative_events"), generation_sequences=derived.get("generation_sequences"), sequence_metadata=derived.get("sequence_metadata"), style_profile=json.loads(row["style_profile_json"]) if row["style_profile_json"] else None, style_observation=json.loads(row["style_observation_json"]) if row["style_observation_json"] else None, visual_bible=json.loads(row["visual_bible_json"]) if row["visual_bible_json"] else None, style_analysis=direct_state, metadata={"provider": row["provider"], "model": row["model"], "model_usage": usage, "perception": perception_runs, "estimated_list_price_cny": cost if usage else None, "cost_estimate_complete": cost_complete, "memory": service.memory.snapshot(), "segmentation_profile": row["segmentation_profile"], "script_planning": script_diagnostics if row["input_type"] == "script" else None, "delivery_quality": quality, "capacity_preflight": json.loads(row["config_json"]).get("capacity_preflight"), "scene_style_review": scene_style_review})
