"""Streaming screenplay pipeline, isolated from video and legacy v2 jobs."""
from __future__ import annotations

import json
import re
from copy import deepcopy

from app.schemas import ActionDetails, SpatialEvidence, VisionObservation, VisualFacts
from app.script_director.document import parse_script_document
from app.script_director.engine import IMPLEMENTATION_VERSION, VERSION, WindowResult, director_windows, plan_window
from app.script_director.plan_models import DirectedEvent
from app.script_director.render import DEFAULT_STYLE
from app.script_director.source_support import VERSION as SOURCE_SUPPORT_VERSION
from app.services.prompt_service import build_prompts


def _source_style(document):
    instructions = []
    for span in document.spans:
        if span.role != "note":
            continue
        match = re.match(r"^\s*[【\[]?(?:整体画面风格|整体风格|视觉风格|画风|风格)[】\]]?\s*[:：]\s*(.+)$", span.text)
        if match:
            instructions.append(match.group(1).strip().rstrip("。；;"))
    return "整体画面风格：" + "；".join(instructions) + "。" if instructions else DEFAULT_STYLE


def by_scene(document, span_id):
    return next((span.scene_id for span in document.spans if span.id == span_id), None)


def run_script_director(service, row, config):
    job_id = row["id"]
    document = parse_script_document(row["script_text"] or "")
    windows = director_windows(document)
    if not windows:
        raise RuntimeError("当前文本只有人物设定或备注，没有剧情、对白或声音；请补充故事或对话内容")
    if len(document.content_spans) > config.max_shots * 4:
        raise RuntimeError("剧本剧情片段超过安全上限，请按场次分批输入；未截断原文")
    plan = {"schema_version": VERSION, "implementation_version": IMPLEMENTATION_VERSION,
        "source_support_version": SOURCE_SUPPORT_VERSION,
        "document": document.model_dump(), "events": [], "shots": [], "window_audit": [],
        "window_count": len(windows), "fallback_windows": [], "reconciled_windows": [],
        "tool_audit": [], "model_calls": 0, "normalization_notes": [], "timing_origin": "estimated-performance"}
    raw_responses, warnings, number, cursor, state, scene_id = [], list(document.warnings), 0, 0.0, {}, None
    restarting = bool(json.loads(row["config_json"]).get("restart_resume_requested"))
    completed_window = 0
    if restarting and row.get("script_plan_json"):
        saved = json.loads(row["script_plan_json"])
        if (saved.get("schema_version") != VERSION or saved.get("document") != document.model_dump()
                or saved.get("implementation_version") != IMPLEMENTATION_VERSION):
            raise RuntimeError("已保存剧本与当前文档/版本不符，拒绝覆盖历史镜头；请创建新任务")
        plan = saved
        raw_responses = json.loads(row.get("script_raw_response_json") or "[]")
        warnings = json.loads(row.get("planning_warnings_json") or "[]")
        checkpoint = plan.get("checkpoint")
        if not checkpoint:
            # Older plans only persisted at successful window boundaries.
            audit = plan.get("window_audit", [])
            completed_window = audit[-1]["window"] if audit else 0
            last_scene = audit[-1]["scene_id"] if audit else None
            legacy_state = {"axis": "A", "props": {}}
            for shot in plan.get("shots", []):
                if shot.get("source_span_ids") and by_scene(document, shot["source_span_ids"][0]) == last_scene:
                    for prop in shot.get("prop_states_after", []):
                        legacy_state["props"][prop["prop"]] = prop["holder"]
                    camera = shot.get("camera", {})
                    if camera.get("axis_side", "A") != "neutral":
                        legacy_state["axis"] = camera.get("axis_side", "A")
                    legacy_state["previous_shot"] = {"end_state": shot["end_state"],
                        "character_ids": shot.get("character_ids", []), "action_steps": shot.get("action_steps", []), "camera": camera}
            if audit and not audit[-1].get("fallback") and plan.get("events"):
                last = plan["events"][-1]
                legacy_state["open_event"] = {"id": last["id"], "window_number": completed_window,
                    "scene_id": last_scene, "title": last["title"], "goal": last["goal"],
                    "last_source_span_id": audit[-1]["source_span_ids"][-1], "successful": True}
            count = len(plan.get("shots", []))
            last_row = service.db.fetchone("SELECT end_time FROM shots WHERE job_id=? AND number=?", (job_id, count))
            checkpoint = {"window": completed_window, "shot_count": count,
                "cursor": last_row["end_time"] if last_row else 0.0, "state": legacy_state, "scene_id": last_scene}
        completed_window = checkpoint["window"]
        number, cursor, state, scene_id = checkpoint["shot_count"], checkpoint["cursor"], deepcopy(checkpoint["state"]), checkpoint["scene_id"]
        # A pre-checkpoint crash may have left a partially published legacy
        # window. Remove only that uncommitted tail, never completed windows.
        with service.db.connect() as conn:
            tail = [r["id"] for r in conn.execute("SELECT id FROM shots WHERE job_id=? AND number>?", (job_id, number))]
            for shot_id in tail:
                conn.execute("DELETE FROM prompts WHERE job_id=? AND shot_id=?", (job_id, shot_id))
                conn.execute("DELETE FROM shots WHERE job_id=? AND id=?", (job_id, shot_id))
    global_style = _source_style(document)

    def persist(conn=None):
        profile = "script-v3-rules" if len(plan["fallback_windows"]) == len(windows) else "script-v3-hybrid" if plan["fallback_windows"] else "script-v3-cloud"
        values = dict(segmentation_profile=profile, raw_shot_count=len(document.content_spans), narrative_shot_count=number,
            script_plan_json=json.dumps(plan, ensure_ascii=False), script_raw_response_json=json.dumps(raw_responses, ensure_ascii=False),
            planning_warnings_json=json.dumps(list(dict.fromkeys(warnings)), ensure_ascii=False))
        if conn is None:
            service.db.update_job(job_id, **values)
        else:
            conn.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", [*values.values(), job_id])

    service.db.update_job(job_id, status="processing", stage="segmenting", progress=10)
    persist()
    service._emit(job_id, "script.document_ready", {"character_count": len(document.characters),
        "content_span_count": len(document.content_spans), "scene_count": len(document.scenes), "schema_version": VERSION})
    by_id = {span.id: span for span in document.spans}
    characters = {character.id: character for character in document.characters}
    scenes = {scene.id: scene for scene in document.scenes}
    for window in windows:
        if window.number <= completed_window:
            continue
        if service._is_cancelled(job_id):
            return
        if scene_id != window.scene_id:
            state, scene_id = {"axis": "A", "props": {}}, window.scene_id
        service._emit(job_id, "script.window_started", {"window": window.number, "window_count": len(windows), "scene_id": scene_id})
        pending = plan.get("pending_window_result")
        if pending and pending["window"] == window.number:
            result = WindowResult(events=[DirectedEvent.model_validate(event) for event in pending["events"]],
                raw_responses=pending["raw_responses"], warnings=pending["warnings"], model_calls=pending["model_calls"],
                tool_calls=pending["tool_calls"], fallback=pending["fallback"], normalization_notes=pending["normalization_notes"])
        else:
            plan["in_flight_window"] = {"window": window.number, "cost_status_if_interrupted": "unknown"}
            persist()
            with service._inference_slot():
                result = plan_window(service.provider, document, window, state,
                                     cancelled=lambda: service._is_cancelled(job_id))
            plan["pending_window_result"] = {"window": window.number, "events": [event.model_dump() for event in result.events],
                "raw_responses": result.raw_responses, "warnings": result.warnings, "model_calls": result.model_calls,
                "tool_calls": result.tool_calls, "fallback": result.fallback, "normalization_notes": result.normalization_notes}
            plan.pop("in_flight_window", None)
            persist()
        # Cancelled or stale tasks cannot publish a late model response.
        if service._is_cancelled(job_id):
            return
        if number + sum(len(event.shots) for event in result.events) > config.max_shots:
            raise RuntimeError(f"导演规划超过{config.max_shots}镜上限；已完成窗口保留，没有截断后伪装成功")
        window_sql, window_events = [], []
        raw_responses.extend(result.raw_responses)
        warnings.extend(result.warnings)
        plan["normalization_notes"] = list(dict.fromkeys([*plan["normalization_notes"], *result.normalization_notes]))
        plan["model_calls"] += result.model_calls
        plan["tool_audit"].extend(result.tool_calls)
        for audit in result.tool_calls:
            service._emit(job_id, "script.tool_finished", {"window": window.number,
                "tool": audit["name"], "elapsed_ms": audit["elapsed_ms"], "error": audit["error"]})
        if result.fallback:
            plan["fallback_windows"].append(window.number)
        window_audit = {"window": window.number, "scene_id": scene_id,
            "source_span_ids": list(window.source_span_ids), "event_ids": [], "events": [],
            "fallback": result.fallback, "model_calls": result.model_calls,
            "normalization_notes": list(result.normalization_notes), "warnings": list(result.warnings)}
        plan["window_audit"].append(window_audit)
        for event in result.events:
            if event.continues_event_id:
                # Validation permits only the immediately adjacent successful
                # window's last event. Existing shots stay exactly as published.
                stored_event = plan["events"][-1]
                event_id = stored_event["id"]
                stored_event["source_span_ids"].extend(event.source_span_ids)
                stored_event["shots"].extend(shot.model_dump() for shot in event.shots)
                stored_event["window_numbers"].append(window.number)
            else:
                event_id = f"script-event-{len(plan['events']) + 1:03d}"
                stored_event = {"id": event_id, **event.model_dump(), "window_numbers": [window.number]}
                plan["events"].append(stored_event)
            window_audit["event_ids"].append(event_id)
            window_audit["events"].append({"id": event_id, "continues_event_id": event.continues_event_id,
                "title": event.title, "goal": event.goal, "source_span_ids": list(event.source_span_ids)})
            for directed in event.shots:
                if number >= config.max_shots:
                    raise RuntimeError(f"导演规划超过{config.max_shots}镜上限；已完成镜头保留，没有截断后伪装成功")
                number += 1
                shot_id = service._shot_id(job_id, number)
                duration = round(directed.duration_seconds, 3)
                spans = [by_id[sid] for sid in directed.source_span_ids]
                participants = [characters[cid] for cid in directed.character_ids]
                speaker_ids = {by_id[part.span_id].character_id for part in directed.dialogue_ranges}
                participants += [characters[cid] for cid in speaker_ids if cid and cid in characters and cid not in directed.character_ids]
                dialogues = [{"id": part.span_id, "speaker_id": by_id[part.span_id].character_id,
                    "speaker_name": by_id[part.span_id].speaker or "说话人未明确",
                    "text": by_id[part.span_id].text[part.start:part.end],
                    "parenthetical": by_id[part.span_id].parenthetical,
                    "source_start": part.start, "source_end": part.end,
                    "kind": by_id[part.span_id].role} for part in directed.dialogue_ranges]
                direction = {"schema_version": VERSION, "implementation_version": IMPLEMENTATION_VERSION,
                    "source_support_version": SOURCE_SUPPORT_VERSION,
                    "source_quotes": [quote.model_dump() for quote in directed.source_quotes],
                    "event_id": event_id, "event_title": stored_event["title"],
                    "event_goal": stored_event["goal"], "shot_function": directed.shot_function, "cut_reason": directed.cut_reason,
                    "camera_angle": directed.camera.angle, "source_span_ids": directed.source_span_ids,
                    "dialogue": dialogues, "sound_cues": [span.text for span in spans if span.role == "sound"],
                    "character_profiles": [character.model_dump() for character in participants],
                    "creative_assumptions": [*directed.creative_assumptions,
                        "机位与光线是导演创作方案，不是原片观察事实",
                        "未指定画风时使用可替换的写实电影创作基调" if global_style == DEFAULT_STYLE else "画风沿用原文的创作要求，不宣称参考影片识别结果"],
                    "continuity_notes": directed.continuity_notes, "duration_kind": "estimated-performance",
                    "duration_seconds": duration, "aspect_ratio": "9:16", "global_style_prompt": global_style,
                    "action_timeline": [phase.model_dump() for phase in directed.action_timeline],
                    "camera_plan": directed.camera.model_dump(), "audio_generated": False}
                camera = directed.camera
                actor_names = directed.primary_subject if directed.primary_subject != "原文主体" else ("、".join(character.name for character in participants) or "原文中的人物或环境")
                shot_warnings = [*result.warnings]
                if result.fallback:
                    shot_warnings.append("本窗口使用保守导演规划，原文与对白已保留，镜头表达需要复核")
                observation = VisionObservation(script_direction=direction, subject_action="；".join(directed.action_steps),
                    shot_size=camera.shot_size, lens=camera.lens, camera_movement=camera.movement,
                    lighting_vibe=camera.lighting, lighting_detail=camera.lighting, composition=camera.composition,
                    confidence=0.45 if result.fallback else 0.8, analysis_warnings=shot_warnings,
                    action_details=ActionDetails(primary_subject=actor_names, initial_state=directed.first_frame_state,
                        steps=directed.action_steps, final_state=directed.end_state,
                        evidence=[f"原文：{sid}" for sid in directed.source_span_ids]),
                    visual_facts=VisualFacts(people=[character.name for character in participants],
                        scene=scenes[scene_id].heading or "同一连续场景；具体环境按原文或创作设定建立",
                        evidence=[f"原文：{sid}" for sid in directed.source_span_ids]),
                    spatial_evidence=SpatialEvidence(observable=False))
                start, end = cursor, round(cursor + duration, 3)
                cursor = end
                window_sql.append(("""INSERT INTO shots
                    (id,job_id,number,start_time,end_time,duration,time,source_text,status,observation_json,scene_group_id,cut_reason,generation_duration_seconds,scene_reason)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (shot_id, job_id, number, start, end, duration, f"{start:06.2f} – {end:06.2f}",
                     "\n".join(span.text for span in spans), "warning" if shot_warnings else "ready",
                     observation.model_dump_json(), scene_id, directed.cut_reason, duration, "剧本原场景与导演事件规划")))
                plan["shots"].append({"shot_id": shot_id, "event_id": event_id, **directed.model_dump()})
                window_events.append(("shot.created", {"shot_id": shot_id, "number": number, "start_time": start, "end_time": end, "scene_group_id": scene_id}))
                window_events.append(("shot.description_ready", {"shot_id": shot_id, "description": observation.subject_action,
                    "script_direction": direction}))
                for prompt in build_prompts(shot_id, observation, generation_duration_seconds=duration, aspect_ratio="9:16"):
                    window_sql.append(("""INSERT INTO prompts (id,job_id,shot_id,prompt_type,platform,version,positive_prompt,negative_prompt,status,template_id,template_version,warnings_json,contract_json)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (prompt.id, job_id, shot_id, prompt.prompt_type, prompt.platform,
                        prompt.version, prompt.positive_prompt, prompt.negative_prompt, prompt.status, prompt.template_id,
                        prompt.template_version, json.dumps(prompt.warnings, ensure_ascii=False), prompt.contract.model_dump_json())))
                    window_events.append(("prompt.ready", {"prompt_id": prompt.id, "shot_id": shot_id, "prompt_type": prompt.prompt_type, "status": prompt.status}))
                for prop in directed.prop_states_after:
                    state.setdefault("props", {})[prop.prop] = prop.holder
                if camera.axis_side != "neutral":
                    state["axis"] = camera.axis_side
                state["previous_shot"] = {"event_goal": event.goal, "end_state": directed.end_state,
                    "character_ids": directed.character_ids, "action_steps": directed.action_steps,
                    "camera": camera.model_dump()}
        last_event = plan["events"][-1]
        if result.fallback:
            state.pop("open_event", None)
        else:
            state["open_event"] = {"id": last_event["id"], "window_number": window.number,
                "scene_id": scene_id, "title": last_event["title"], "goal": last_event["goal"],
                "source_span_ids": list(last_event["source_span_ids"]),
                "last_source_span_id": window.source_span_ids[-1], "successful": True}
        plan["checkpoint"] = {"window": window.number, "shot_count": number, "cursor": cursor,
            "state": deepcopy(state), "scene_id": scene_id}
        plan.pop("pending_window_result", None)
        with service.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "cancelled":
                return
            for query, arguments in window_sql:
                conn.execute(query, arguments)
            persist(conn)
        for event_type, payload in window_events:
            service._emit(job_id, event_type, payload)
        service._set_stage(job_id, "prompting", int(10 + window.number / len(windows) * 82))
        service._emit(job_id, "script.window_ready", {"window": window.number, "window_count": len(windows), "scene_id": scene_id,
            "shot_count": number, "model_calls": result.model_calls, "fallback": result.fallback})
        if window.number == len(windows) or windows[window.number].scene_id != scene_id:
            service._emit(job_id, "script.scene_ready", {"scene_id": scene_id, "shot_count": number})
    persist()
    for warning in list(dict.fromkeys(warnings)):
        service._emit(job_id, "job.warning", {"message": warning})
    service._build_event_sequences(job_id)
    if service._is_cancelled(job_id):
        return
    service.db.update_job(job_id, status="completed", stage="completed", progress=100, error=None)
    service._emit(job_id, "source.ready", {"input_type": "script", "segment_count": number,
        "beat_count": len(document.content_spans), "segmentation_profile": service.status(job_id)["segmentation_profile"]})
    service._emit(job_id, "job.completed", {"progress": 100, "input_type": "script"})
