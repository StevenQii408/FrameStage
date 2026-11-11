"""Publish derived script and video sequences without owning worker lifecycle."""
from __future__ import annotations

import json
from app.services.event_sequences import derive_sequences_safe, store_sequence_payload


def build_event_sequences(service, job_id: str, *, allow_agent=True, warning="") -> None:
    """Persist a new job's derived coverage without changing its source plan."""
    agent_warning = warning
    try:
        row = service.db.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
        config = json.loads(row["config_json"])
        if row["input_type"] == "script" and config.get("script_strategy") == "director-v3":
            from app.script_director.sequences import build_script_sequences
            result = service.result(job_id)
            payload = build_script_sequences(result.shots, result.prompts)
            with service.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "cancelled":
                    return
                payload = store_sequence_payload(conn, job_id, payload)
            service._emit_sequence_ready(job_id, payload)
            return
        use_agent = row["input_type"] == "video" and config.get("event_grouping_policy") == "event-agent-v2"
        if use_agent:
            from app.event_agent.models import EventAgentResult
            from app.event_agent.projection import project_event_agent_result
            from app.event_agent.service import EventAgentService
            result = service.result(job_id)
            from app.event_agent.engine import source_has_saved_observations
            from app.event_agent.models import EventAgentConfig
            agent_limits = EventAgentConfig()
            needs_span_analysis = any(shot.duration > min(agent_limits.window_seconds, agent_limits.generation_max_seconds)
                                      for shot in result.shots)
            if allow_agent and (needs_span_analysis or sum(shot.status in ("ready", "warning") and source_has_saved_observations(shot) for shot in result.shots) > 1):
                service._set_stage(job_id, "event_planning", 88)
                coordinator = getattr(service, "event_agent_service", None) or EventAgentService(service)
                try:
                    agent_result = EventAgentResult.model_validate(coordinator.run_inline(job_id))
                    if service._is_cancelled(job_id):
                        return
                    run = coordinator._row(job_id, agent_result.run_id)
                    source = service.db.media.snapshot(json.loads(run["source_snapshot_json"]), reading=True)
                    source_shots, source_frames = coordinator._inputs(source)
                    scenes = coordinator.scene_projection(agent_result, source, source_shots, source_frames)
                    scene_map = {item["shot_id"]: item for item in scenes}
                    source_shots = [shot.model_copy(update={key: scene_map[shot.id][key]
                        for key in ("scene_group_id", "scene_reason")}) for shot in source_shots]
                    source_prompts = [prompt for shot in source_shots for prompt in shot.prompts]
                    payload = project_event_agent_result(agent_result, source_shots, frames=source_frames, prompts=source_prompts)
                    payload["sequence_metadata"]["scene_projection"] = {"run_id": agent_result.run_id,
                        "decisions": scenes}
                    with service.db.connect() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        current = coordinator._source_rows(conn, job_id)
                        baseline = {key: value for key, value in source.items() if key != "file_fingerprints"}
                        if current["job"]["status"] == "cancelled":
                            return
                        if coordinator._source_signature(current, inline=True) != coordinator._source_signature(baseline, inline=True):
                            raise ValueError("事件分析后的来源已变化，未覆盖用户编辑")
                        for scene in scenes:
                            conn.execute("UPDATE shots SET scene_group_id=?,scene_reason=? WHERE job_id=? AND id=?",
                                (scene["scene_group_id"], scene["scene_reason"], job_id, scene["shot_id"]))
                        plan = json.loads(current["job"].get("generation_plan_json") or "{}")
                        if plan.get("shots"):
                            by_number = {item["number"]: item for item in scenes}
                            for planned in plan["shots"]:
                                scene = by_number.get(planned.get("number"))
                                if scene:
                                    planned.update({key: scene[key] for key in ("scene_group_id", "scene_reason")})
                            plan["scene_projection"] = {"run_id": agent_result.run_id, "decisions": scenes}
                            conn.execute("UPDATE jobs SET generation_plan_json=? WHERE id=?", (json.dumps(plan, ensure_ascii=False), job_id))
                        payload = store_sequence_payload(conn, job_id, payload)
                    service._emit_sequence_ready(job_id, payload)
                    service._emit(job_id, "scene.regrouped", {"scene_count": len({item["scene_group_id"] for item in scenes}),
                        "source": "event-agent-v2", "run_id": agent_result.run_id})
                    return
                except Exception as exc:
                    if service._is_cancelled(job_id):
                        return
                    agent_warning = "事件 Agent 未完成，已保留所有来源镜头，不回退到旧的强制反应拆分规则：" + str(exc)[:220]
        with service.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row["status"] == "cancelled":
                return
            result = service.result(job_id)
            payload = derive_sequences_safe(result.shots, result.frames, result.prompts,
                [] if use_agent else json.loads(row["montage_json"] or "{}"),
                strategy=json.loads(row["config_json"]).get("sequence_strategy"), mode="persisted")
            if use_agent:
                payload["sequence_metadata"].update(event_grouping_policy="event-agent-v2",
                    agent_status="failed" if agent_warning else "not-needed", degraded=bool(agent_warning))
                if agent_warning:
                    payload["sequence_metadata"]["warnings"] = [*payload["sequence_metadata"].get("warnings", []), agent_warning]
                    for sequence in payload["generation_sequences"]:
                        sequence["warnings"].append(agent_warning)
                        sequence["status"] = sequence["first_frame_prompt"]["status"] = sequence["seedance_prompt"]["status"] = "warning"
            payload = store_sequence_payload(conn, job_id, payload)
        service._emit_sequence_ready(job_id, payload)
    except Exception as exc:
        service._emit(job_id, "job.warning", {"message": f"生成序列派生失败，来源镜头和 Prompt 已保留：{str(exc)[:240]}"})


def emit_sequence_ready(service, job_id, payload):
    service._emit(job_id, "sequence.ready", {"sequence_count": len(payload["generation_sequences"]),
        "event_count": len(payload["narrative_events"]), "mode": "persisted",
        "version": payload["sequence_metadata"]["version_number"], "build_hash": payload["sequence_metadata"]["build_hash"],
        "event_grouping_policy": payload["sequence_metadata"].get("event_grouping_policy", "legacy-pairs-v1"),
        "run_id": payload["sequence_metadata"].get("agent_run_id")})
    for warning in payload["sequence_metadata"].get("warnings", []):
        service._emit(job_id, "job.warning", {"message": warning})
