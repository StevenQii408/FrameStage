"""Persist project style drafts and apply confirmed profiles without rerunning VLM shots."""

from __future__ import annotations

import json
from math import gcd
from typing import Any, Dict, List, Optional

from app.db import utc_now
from app.providers.base import FrameInput
from app.schemas import VisionObservation
from app.services.prompt_service import build_prompts
from app.services.style_service import ANCHORS, StyleObservation, StyleProfile, StyleProposalError, profile_hash, propose_style, render_style
from app.style_ontology.catalog import REFERENCE, VERSION, terms


def catalog_payload() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "reference": REFERENCE,
        "terms": [vars(term) for term in terms().values()],
        "anchors": ANCHORS,
    }


def _representative_frames(service, job_id: str) -> List[FrameInput]:
    rows = service.db.fetchall(
        "SELECT id,shot_id,timestamp,path,width,height FROM frames WHERE job_id=? AND selected=1 ORDER BY shot_id,timestamp",
        (job_id,),
    )
    first_by_shot = {}
    for row in rows:
        first_by_shot.setdefault(row["shot_id"], row)
    ordered = list(first_by_shot.values())
    if len(ordered) > 10:
        ordered = [ordered[round(index * (len(ordered) - 1) / 9)] for index in range(10)]
    return [FrameInput(row["id"], row["timestamp"], row["path"], row["width"], row["height"]) for row in ordered]


def _context(service, job_id: str, input_type: str, script_text: str) -> str:
    if input_type == "script":
        return script_text[:2500]
    rows = service.db.fetchall("SELECT observation_json FROM shots WHERE job_id=? ORDER BY number", (job_id,))
    summaries = []
    for row in rows[:40]:
        item = json.loads(row["observation_json"] or "{}")
        style = item.get("visual_style_evidence") or {}
        summaries.append("；".join(str(style.get(key, "")) for key in
                                  ("medium", "character_design", "rendering", "color_palette", "texture")))
    return "\n".join(dict.fromkeys(summaries))[:2500]


def draft_job_style(service, job_id: str, *, force: bool = False) -> Optional[StyleProfile]:
    row = service.db.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not row:
        raise ValueError("任务不存在")
    if row["style_profile_json"] and not force:
        return StyleProfile.model_validate_json(row["style_profile_json"])
    if row["status"] not in ("processing", "completed"):
        raise ValueError("需等待任务分析完成后才能创建画风草案")
    frames = _representative_frames(service, job_id) if row["input_type"] == "video" else []
    if row["input_type"] == "video" and not frames:
        raise ValueError("视频任务没有可用于画风判断的关键帧")
    mode = "observed" if frames else "creative"
    previous = StyleProfile.model_validate_json(row["style_profile_json"]) if row["style_profile_json"] else None
    try:
        observation, profile = propose_style(service.provider,
                                             _context(service, job_id, row["input_type"], row["script_text"] or ""),
                                             frames, mode)
    except StyleProposalError as exc:
        observation = StyleObservation(mode=mode, raw_tags_response=exc.raw_tags,
                                       raw_response=exc.raw_response, uncertainties=[str(exc)],
                                       model=service.provider.health().model)
        service.db.update_job(job_id, style_observation_json=observation.model_dump_json())
        service._emit(job_id, "style.review_needed", {"reason": "画风结构解析失败，原始响应已保留"})
        return previous
    if profile is not None:
        profile.version = previous.version + 1 if previous else 1
    now = utc_now()
    with service.db.connect() as conn:
        conn.execute("UPDATE jobs SET style_observation_json=?,style_profile_json=?,updated_at=? WHERE id=?",
                     (observation.model_dump_json(), profile.model_dump_json() if profile else row["style_profile_json"], now, job_id))
        if profile is not None:
            conn.execute("INSERT INTO style_profile_revisions(job_id,version,profile_json,created_at) VALUES(?,?,?,?)",
                         (job_id, profile.version, profile.model_dump_json(), now))
    if profile is not None:
        service._emit(job_id, "style.draft_ready", {"version": profile.version, "mode": mode})
    else:
        service._emit(job_id, "style.review_needed", {"reason": observation.uncertainties[-1]})
    return profile or previous


def save_style_draft(service, job_id: str, payload: dict) -> StyleProfile:
    row = service.db.fetchone("SELECT status,style_profile_json FROM jobs WHERE id=?", (job_id,))
    if not row:
        raise ValueError("任务不存在")
    if row["status"] != "completed":
        raise ValueError("任务完成后才能编辑画风")
    previous = StyleProfile.model_validate_json(row["style_profile_json"]) if row["style_profile_json"] else None
    values = dict(payload)
    values.update(status="draft", version=previous.version + 1 if previous else 1,
                  schema_version="style-profile.v1", ontology_version=VERSION)
    values.pop("profile_hash", None)
    values.pop("rendered_style_prompt", None)
    if any(anchor not in ANCHORS for anchor in values.get("optional_anchors", [])):
        raise ValueError("未知风格锚点")
    values["expanded_anchor_traits"] = list(dict.fromkeys(term for anchor in values.get("optional_anchors", [])
                                                            for term in ANCHORS[anchor]["traits"]))
    shot_ids = {shot["id"] for shot in service.db.fetchall("SELECT id FROM shots WHERE job_id=?", (job_id,))}
    overrides = values.get("per_shot_effect_overrides") or {}
    if any(shot_id not in shot_ids for shot_id in overrides):
        raise ValueError("单镜特效引用了当前任务以外的镜头")
    if any(len(effects) > 2 or len(set(effects)) != len(effects) for effects in overrides.values()):
        raise ValueError("每镜最多两个不重复的局部效果")
    profile = StyleProfile.model_validate(values)
    profile.rendered_style_prompt = render_style(profile)
    profile.profile_hash = profile_hash(profile)
    now = utc_now()
    with service.db.connect() as conn:
        conn.execute("UPDATE jobs SET style_profile_json=?,updated_at=? WHERE id=?",
                     (profile.model_dump_json(), now, job_id))
        conn.execute("INSERT INTO style_profile_revisions(job_id,version,profile_json,created_at) VALUES(?,?,?,?)",
                     (job_id, profile.version, profile.model_dump_json(), now))
    service._emit(job_id, "style.draft_ready", {"version": profile.version, "mode": profile.mode})
    return profile


def confirm_style(service, job_id: str) -> StyleProfile:
    job = service.db.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or not job["style_profile_json"]:
        raise ValueError("请先生成或保存画风草案")
    if job["status"] != "completed":
        raise ValueError("任务完成后才能应用全局画风")
    profile = StyleProfile.model_validate_json(job["style_profile_json"])
    if profile.status != "draft":
        raise ValueError("当前画风版本已经确认")
    profile.rendered_style_prompt = render_style(profile)
    profile.profile_hash = profile_hash(profile)
    profile.status = "confirmed"
    shots = service.db.fetchall("SELECT * FROM shots WHERE job_id=? ORDER BY number", (job_id,))
    frames = service.db.fetchall("SELECT shot_id,width,height FROM frames WHERE job_id=? AND selected=1 ORDER BY timestamp", (job_id,))
    legacy_templates = {item["shot_id"]: item["template_id"] for item in service.db.fetchall(
        "SELECT shot_id,template_id FROM prompts WHERE job_id=? AND template_id IN ('script-first-frame-v1','seedance-script-v1')", (job_id,))}
    cover_by_shot = {}
    for frame in frames:
        cover_by_shot.setdefault(frame["shot_id"], frame)
    prepared = []
    for shot in shots:
        if not shot["observation_json"]:
            continue
        observation = VisionObservation.model_validate_json(shot["observation_json"])
        frame = cover_by_shot.get(shot["id"])
        divisor = gcd(frame["width"], frame["height"]) if frame and frame["width"] and frame["height"] else 0
        ratio = f"{frame['width']//divisor}:{frame['height']//divisor}" if divisor else "待设定"
        prompts = build_prompts(shot["id"], observation, (job["script_text"] or "")[:3000],
                                source_edit_times=json.loads(shot["source_edit_times_json"] or "[]"),
                                generation_duration_seconds=shot["generation_duration_seconds"] or shot["duration"],
                                aspect_ratio=ratio, style_profile=profile, template_id=legacy_templates.get(shot["id"]))
        prepared.extend(prompts)
    if not prepared:
        raise ValueError("没有可重新渲染的镜头 Prompt")
    now = utc_now()
    with service.db.connect() as conn:
        for prompt in prepared:
            existing = conn.execute("SELECT * FROM prompts WHERE id=? AND job_id=?", (prompt.id, job_id)).fetchone()
            if not existing:
                existing = conn.execute("SELECT * FROM prompts WHERE shot_id=? AND job_id=? AND prompt_type=? AND platform=?",
                                        (prompt.shot_id, job_id, prompt.prompt_type, prompt.platform)).fetchone()
            if existing:
                old_contract = json.loads(existing["contract_json"] or "{}")
                old_version = old_contract.get("style_profile_version", "")
                old_version = int(old_version.rsplit(":", 1)[-1]) if ":" in old_version else 0
                conn.execute("INSERT OR REPLACE INTO prompt_revisions(job_id,prompt_id,style_version,payload_json,created_at) VALUES(?,?,?,?,?)",
                             (job_id, existing["id"], old_version, json.dumps(dict(existing), ensure_ascii=False), now))
                conn.execute("DELETE FROM prompts WHERE id=?", (existing["id"],))
            conn.execute("INSERT INTO prompts (id,job_id,shot_id,prompt_type,platform,version,positive_prompt,negative_prompt,status,template_id,template_version,warnings_json,contract_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (prompt.id, job_id, prompt.shot_id, prompt.prompt_type, prompt.platform, prompt.version,
                          prompt.positive_prompt, prompt.negative_prompt, prompt.status, prompt.template_id,
                          prompt.template_version, json.dumps(prompt.warnings, ensure_ascii=False),
                          prompt.contract.model_dump_json() if prompt.contract else None))
        conn.execute("UPDATE jobs SET style_profile_json=?,updated_at=? WHERE id=?",
                     (profile.model_dump_json(), now, job_id))
        conn.execute("UPDATE style_profile_revisions SET profile_json=? WHERE job_id=? AND version=?",
                     (profile.model_dump_json(), job_id, profile.version))
    service._emit(job_id, "style.confirmed", {"version": profile.version, "profile_hash": profile.profile_hash,
                                              "prompt_count": len(prepared)})
    return profile


def style_history(service, job_id: str) -> Dict[str, Any]:
    revisions = service.db.fetchall("SELECT version,profile_json,created_at FROM style_profile_revisions WHERE job_id=? ORDER BY version",
                                    (job_id,))
    prompts = service.db.fetchall("SELECT prompt_id,style_version,payload_json,created_at FROM prompt_revisions WHERE job_id=? ORDER BY style_version,prompt_id",
                                  (job_id,))
    return {"profiles": [{"version": row["version"], "profile": json.loads(row["profile_json"]), "created_at": row["created_at"]} for row in revisions],
            "prompt_revisions": [{"prompt_id": row["prompt_id"], "style_version": row["style_version"],
                                  "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]} for row in prompts]}
