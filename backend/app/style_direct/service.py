from __future__ import annotations

from hashlib import sha256
import io
import json
from math import gcd
from pathlib import Path
import uuid
import shutil

from PIL import Image, ImageOps, ImageFilter, ImageStat, UnidentifiedImageError

from app.db import utc_now
from app.schemas import PromptBundle, VisionObservation
from app.services.event_sequences import rerender_sequence_payload, store_sequence_payload
from app.services.prompt_service import build_prompts
from .models import StyleProfileV3, render_profile, safe_generation_text
from .pipeline import StylePipeline
from .prompts import VERSION
from .diagnostics import attach_usage
from .anchors import digest as anchor_digest
from .reliability import usable_baseline, resolution, VERSION as RELIABILITY_VERSION
from .models import RENDERER_VERSION
from .references import reference_digest
from .recommendations import historical_recommendations, selected_recipe, VERSION as RECOMMENDATION_VERSION

MAX_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 30_000_000


def prepare_image(data: bytes, directory: Path, alias: str, origin=None):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("参考图片为空或超过25MB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("PNG", "JPEG", "WEBP", "GIF", "AVIF"):
                raise ValueError("仅支持PNG/JPEG/WebP/GIF/AVIF参考图片")
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("参考图解码像素超过3000万")
            if getattr(image, "n_frames", 1) > 1:
                raise ValueError("画风分析只接受静态图片，请先选择动画的一帧")
            image.load()
            image = ImageOps.exif_transpose(image)
            transparent = image.mode in ("RGBA", "LA") or "transparency" in image.info
            if transparent:
                rgba = image.convert("RGBA")
                base = Image.new("RGBA", rgba.size, (128, 128, 128, 255))
                image = Image.alpha_composite(base, rgba).convert("RGB")
            else:
                image = image.convert("RGB")
            directory.mkdir(parents=True, exist_ok=True)
            original = directory / (alias + "-original.png")
            image.save(original)  # Re-encode without EXIF or arbitrary metadata.
            image.thumbnail((1536, 1536))
            path = directory / (alias + ".jpg")
            image.save(path, quality=92)
            payload = {"alias": alias, "asset_id": directory.parent.name + "-" + alias,
                       "path": str(path), "original_path": str(original), "width": image.width,
                       "height": image.height, "sha256": sha256(path.read_bytes()).hexdigest(),
                       "source_sha256": sha256(data).hexdigest(),
                       "preprocess_version": "style-image.v3.1", "transparent_composite": transparent,
                       "background_policy": "neutral-grey-exclude-from-palette" if transparent else "original"}
            payload.update(origin or {})
            return payload
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片格式无效、无法解码或像素超出安全限制") from exc


def get_analysis(service, analysis_id, *, private=False):
    row = service.db.fetchone("SELECT * FROM style_analyses WHERE id=?", (analysis_id,))
    if not row:
        raise ValueError("画风分析不存在")
    result = json.loads(row["result_json"] or "{}")
    if not private:
        result["assets"] = [{k: v for k, v in asset.items() if k not in ("path", "original_path")}
                            for asset in result.get("assets", json.loads(row["source_json"])["assets"])]
    recommendations, derived = historical_recommendations(result)
    return {"analysis_id": row["id"], "status": row["status"], "stage": row["stage"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "error": row["error"],
            "recommendations": recommendations, "recommendations_derived": derived,
            "recommendation_version": RECOMMENDATION_VERSION,
            "result": result}


def _matching_analysis(conn, cache_key, assets, project_id, *, synchronous, force):
    from app.projects.access import style_owner

    def origin_ids(items):
        return [(item.get("origin_job_id"), item.get("origin_frame_id")) for item in items]

    origins = origin_ids(assets)
    rows = conn.execute(
        "SELECT id, status, source_json FROM style_analyses WHERE cache_key=? "
        "AND ((status IN ('queued','running') AND ?) OR (status='completed' AND ?)) "
        "ORDER BY (status='completed'), created_at DESC", (cache_key, not synchronous, not force))
    for row in rows:
        if style_owner(conn, row["id"]) != project_id or origin_ids(json.loads(row["source_json"])["assets"]) != origins:
            continue
        if row["status"] == "completed":
            saved = conn.execute("SELECT result_json FROM style_analyses WHERE id=?", (row["id"],)).fetchone()
            if json.loads(saved["result_json"] or "{}").get("profile", {}).get("application_status") == "user-edited":
                return None
        return row
    return None


def create_analysis(service, sources, *, scope="reference", synchronous=False, force=False,
                    project_id=None, pending_job_id=None):
    from app.projects.access import active_project
    from app.projects.models import ProjectError
    with service.db.connect() as conn:
        origins = {origin.get("origin_job_id") for _, origin in sources if origin and origin.get("origin_job_id")}
        owners = {job["project_id"] for identity in origins for job in conn.execute("SELECT project_id FROM jobs WHERE id=?", (identity,))}
        if project_id is None and len(owners) == 1:
            project_id = next(iter(owners))
        if project_id is not None and owners - {project_id}:
            raise ProjectError(404, "参考帧不属于当前项目")
        active_project(conn, project_id)
    if not 1 <= len(sources) <= 4:
        raise ValueError("每次画风分析接受1–4张图片")
    identifier = "style-" + uuid.uuid4().hex[:12]
    directory = service.settings.data_dir / "style" / identifier
    try:
        assets = [prepare_image(data, directory / "input", f"I{i+1}", origin)
                  for i, (data, origin) in enumerate(sources)]
        cache_key = sha256(json.dumps({"hashes": [a["sha256"] for a in assets], "model": service.provider.health().model,
                                      "prompt": VERSION, "scope": scope,
                                      "reliability": RELIABILITY_VERSION, "renderer": RENDERER_VERSION,
                                      "anchor_catalog_hash": anchor_digest(),
                                      "reference_cards_hash": reference_digest(),
                                      "scenes": [a.get("scene_id") for a in assets], "preprocess": "style-image.v3.1"},
                                     sort_keys=True).encode()).hexdigest()
        with service.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_project(conn, project_id)
            if pending_job_id:
                job = conn.execute("SELECT status, input_type, style_direct_json, project_id FROM jobs WHERE id=?",
                                   (pending_job_id,)).fetchone()
                if not job or job["project_id"] != project_id:
                    raise ProjectError(404, "任务不属于当前项目")
                if job["status"] != "completed" or job["input_type"] != "video":
                    raise ProjectError(409, "视频任务完成后才能重分析画风")
            existing = _matching_analysis(conn, cache_key, assets, project_id, synchronous=synchronous, force=force)
            if existing is None:
                now = utc_now()
                conn.execute("INSERT INTO style_analyses VALUES(?,?,?,?,?,?,?,?,?)", (
                    identifier, "queued", "queued", cache_key, json.dumps({"assets": [service.db.media.record(asset) for asset in assets], "scope": scope}, ensure_ascii=False),
                    None, None, now, now))
                if project_id is not None:
                    conn.execute("INSERT INTO project_style_refs VALUES(?,?)", (identifier, project_id))
            if pending_job_id:
                state = json.loads(job["style_direct_json"] or "{}")
                state["pending_analysis_id"] = existing["id"] if existing else identifier
                conn.execute("UPDATE jobs SET style_direct_json=? WHERE id=?",
                             (json.dumps(state, ensure_ascii=False), pending_job_id))
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    if existing:
        shutil.rmtree(directory, ignore_errors=True)
        return {**get_analysis(service, existing["id"]), "cached": existing["status"] == "completed"}
    service._emit(identifier, "style.direct.queued", {"stage": "queued"})
    if synchronous:
        run_analysis(service, identifier)
    else:
        try:
            service.executor.submit(run_analysis, service, identifier)
        except RuntimeError:
            service.db.execute("UPDATE style_analyses SET status='failed',stage='failed',error=?,updated_at=? WHERE id=?",
                               ("分析队列已关闭，请重新提交", utc_now(), identifier))
            service._emit(identifier, "style.direct.finished", {"stage": "failed", "error": "分析队列已关闭，请重新提交"})
    return get_analysis(service, identifier)


def run_analysis(service, analysis_id):
    from app.projects.access import active_project, style_owner
    row = service.db.fetchone("SELECT * FROM style_analyses WHERE id=?", (analysis_id,))
    if not row or row["status"] not in {"queued", "running"}:
        return
    source = json.loads(row["source_json"])
    def cancelled():
        state = service.db.fetchone("SELECT status FROM style_analyses WHERE id=?", (analysis_id,))
        if not state or state["status"] not in {"queued", "running"}:
            return True
        return any(service._is_cancelled(parent) for parent in
                   {asset.get("origin_job_id") for asset in source["assets"] if asset.get("origin_job_id")})
    def notify(stage, detail):
        with service.db.connect() as conn:
            active_project(conn, style_owner(conn, analysis_id))
            state = conn.execute("SELECT status FROM style_analyses WHERE id=?", (analysis_id,)).fetchone()
            if state["status"] not in {"queued", "running"}:
                raise ValueError("画风分析已停止")
        service.db.execute("UPDATE style_analyses SET status='running',stage=?,updated_at=? WHERE id=?",
                           (stage, utc_now(), analysis_id))
        service._emit(analysis_id, "style.direct.stage", {"stage": stage, "detail": detail})
        for parent in {a.get("origin_job_id") for a in source["assets"] if a.get("origin_job_id")}:
            parent_job = service.status(parent)
            if parent_job and parent_job["status"] in ('running', 'processing') and source["scope"] == "project":
                service._emit(parent, "job.stage_changed", {"stage": "styling:" + stage, "progress": parent_job['progress'], "detail": detail})
    result = None
    error = None
    try:
        with service.db.connect() as conn:
            active_project(conn, style_owner(conn, analysis_id))
        runtime_assets = [service.db.media.record(asset, reading=True) for asset in source["assets"]]
        pipeline = StylePipeline(service.provider, runtime_assets, service.settings.data_dir / "style" / analysis_id / "crops",
                                 scope=source["scope"], notify=notify, cancelled=cancelled)
        result = pipeline.run()
        result["assets"] = [service.db.media.record(asset) for asset in result["assets"]]
    except Exception as exc:
        error = str(exc)[:300]
    finally:
        stopped = cancelled()
        if stopped:
            result, error = None, "任务已取消；未发布迟到画风结果，已发出请求费用可能未知"
        drain = getattr(service.provider, "drain_usage", None)
        usage = drain() if callable(drain) else []
        # A cancelled/failed style stage must still settle its charged attempts;
        # unrelated source-analysis usage remains owned by the parent job.
        own_usage = [u for u in usage if str(u.get("phase", "")).startswith("style.direct")]
        known_costs = [u["estimated_list_price_cny"] for u in own_usage if u.get("estimated_list_price_cny") is not None]
        estimated_cost = sum(known_costs) if known_costs else None
        other_usage = [u for u in usage if u not in own_usage]
        if other_usage and hasattr(service.provider, "usage"):
            service.provider.usage.extend(other_usage)
        latest = service.db.fetchone("SELECT status,result_json FROM style_analyses WHERE id=?", (analysis_id,))
        if latest and latest["status"] not in {"queued", "running"}:
            # Preserve late billing evidence without reviving a deleted task.
            saved = json.loads(latest["result_json"] or "{}")
            saved["usage"] = [*saved.get("usage", []), *own_usage]
            service.db.execute("UPDATE style_analyses SET result_json=? WHERE id=?", (json.dumps(saved, ensure_ascii=False), analysis_id))
            return
        if result is not None:
            result["usage"] = own_usage
            attach_usage(result, own_usage)
            result["estimated_list_price_cny"] = estimated_cost
            result["cost_estimate_complete"] = bool(own_usage) and all(u.get("usage_available") and u.get("estimated_list_price_cny") is not None for u in own_usage)
            profile = result["profile"]
            success = any(d.get("scope") in ("reference", "global") for d in profile["descriptions"])
            status = "completed" if success and profile["application_status"] == "ready" else "partial" if profile["descriptions"] else "failed"
            error = None if success else "未能确定统一画风，已知观察与场次特征已保留"
            now = utc_now()
            with service.db.connect() as conn:
                conn.execute("UPDATE style_analyses SET status=?,stage=?,result_json=?,error=?,updated_at=? WHERE id=?",
                             (status, status, json.dumps(result, ensure_ascii=False), error, now, analysis_id))
                conn.execute("INSERT OR REPLACE INTO style_analysis_revisions VALUES(?,?,?,?)", (analysis_id, 1, json.dumps(profile, ensure_ascii=False), now))
        else:
            terminal = "cancelled" if stopped else "failed"
            audit = {"usage": own_usage, "assets": source["assets"],
                     "cost_estimate_complete": bool(own_usage) and all(u.get("usage_available") and u.get("estimated_list_price_cny") is not None for u in own_usage),
                     "raw": pipeline.raw if 'pipeline' in locals() else [],
                     "estimated_list_price_cny": estimated_cost}
            service.db.execute("UPDATE style_analyses SET status=?,stage=?,error=?,result_json=?,updated_at=? WHERE id=?", (terminal, terminal, error, json.dumps(audit, ensure_ascii=False), utc_now(), analysis_id))
        service._emit(analysis_id, "style.direct.finished", {"stage": get_analysis(service, analysis_id)["stage"], "error": error})


def update_profile(service, analysis_id, text, expected_version, source=None):
    data = get_analysis(service, analysis_id, private=True)
    if data["status"] not in ("completed", "partial"):
        raise ValueError("分析完成后才能编辑")
    result = data["result"]
    profile = StyleProfileV3.model_validate(result["profile"])
    if profile.version != expected_version:
        raise ValueError("画风版本已更新，请刷新后编辑")
    recipe = selected_recipe(result, source)[0] if source is not None else None
    previous_choice = profile.provenance.get('creative_override')
    if source is None and previous_choice and previous_choice.get('kind') == 'user-selected-library':
        # Permit the already-chosen reference name during a later hand edit,
        # without borrowing/fabricating an observed StyleReference.
        recipe = selected_recipe(result, {'kind': 'library-recipe',
            'recipe_id': previous_choice.get('recipe_id'), 'source_hash': previous_choice.get('source_hash')})[0]
    if source is not None and text.strip() not in (recipe.compact_prompt, '整体画面风格：' + recipe.compact_prompt):
        raise ValueError('选择配方必须使用完整知识库原文；修改文字请按手动编辑保存')
    if not 4 <= len(text.strip()) <= 1200 or not safe_generation_text(text, profile.reference or profile.anchor, recipe=recipe):
        raise ValueError("编辑内容为空、过长或混入内容实体/未证实技术名称")
    profile.edited_prompt = text.strip()
    profile.application_status = "user-edited"
    profile.version += 1
    profile.provenance["edited_by"] = "user"
    if source is not None:
        profile.provenance['creative_override'] = {
            'kind': 'user-selected-library', 'recipe_id': recipe.id,
            'source_hash': source['source_hash'], 'label': recipe.label,
            'library_prompt': recipe.compact_prompt,
            'detailed_description': recipe.detailed_description,
            'selection_version': profile.version,
            'observed_fact': False,
        }
    elif profile.provenance.get('creative_override'):
        profile.provenance['creative_override']['edited_after_selection'] = True
    profile.provenance['resolution_status'] = 'resolved'
    if profile.provenance.get('quality_state'):
        profile.provenance['quality_state'] = {**profile.provenance['quality_state'],
            'style': 'resolved', 'interpretation': 'user-edited'}
    render_profile(profile)
    result["profile"] = profile.model_dump()
    now = utc_now()
    with service.db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute("SELECT result_json FROM style_analyses WHERE id=?", (analysis_id,)).fetchone()
        current_result = json.loads(current["result_json"]) if current else {}
        if current_result.get('profile', {}).get('version') != expected_version:
            raise ValueError("画风版本冲突，请刷新后编辑")
        if source is not None:
            # Membership and compiled source version are checked inside the same
            # write transaction as profile version/history, including old hydration.
            selected_recipe(current_result, source)
        conn.execute("UPDATE style_analyses SET result_json=?,updated_at=? WHERE id=?", (json.dumps(result, ensure_ascii=False), now, analysis_id))
        conn.execute("INSERT INTO style_analysis_revisions VALUES(?,?,?,?)", (analysis_id, profile.version, profile.model_dump_json(), now))
    return get_analysis(service, analysis_id)


def apply_style(service, job_id, analysis_id, expected_version=None, *, automatic=False):
    job = service.db.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or job["status"] != "completed":
        raise ValueError("目标任务不存在或尚未完成")
    data = get_analysis(service, analysis_id)
    if data["status"] not in ("completed", "partial"):
        raise ValueError("画风分析未完成")
    profile = StyleProfileV3.model_validate(data["result"]["profile"])
    if expected_version is not None and profile.version != expected_version:
        raise ValueError("画风版本已变化，请刷新后应用")
    if not profile.descriptions and not profile.edited_prompt:
        raise ValueError("没有可用画风，不应用未知占位")
    old_state = json.loads(job["style_direct_json"] or "{}")
    if automatic and old_state.get("profile", {}).get("application_status") == "user-edited":
        return old_state
    if not profile.edited_prompt and not any(d.scope in ("reference", "global") for d in profile.descriptions):
        raise ValueError("参考帧没有统一画风，场次特征已保留但不冒充全局")
    rows = service.db.fetchall("SELECT * FROM shots WHERE job_id=? ORDER BY number", (job_id,))
    legacy_templates = {item["shot_id"]: item["template_id"] for item in service.db.fetchall(
        "SELECT shot_id,template_id FROM prompts WHERE job_id=? AND template_id IN ('script-first-frame-v1','seedance-script-v1')", (job_id,))}
    prepared = []
    for row in rows:
        if not row["observation_json"]:
            continue
        frame = service.db.fetchone("SELECT width,height FROM frames WHERE shot_id=? ORDER BY blur_score DESC LIMIT 1", (row["id"],))
        divisor = gcd(frame["width"], frame["height"]) if frame and frame["width"] and frame["height"] else 0
        ratio = f"{frame['width']//divisor}:{frame['height']//divisor}" if divisor else "待设定"
        prepared.extend(build_prompts(row["id"], VisionObservation.model_validate_json(row["observation_json"]),
            (job["script_text"] or "")[:3000], source_edit_times=json.loads(row["source_edit_times_json"] or "[]"),
            generation_duration_seconds=row["generation_duration_seconds"] or row["duration"],
            aspect_ratio=ratio, direct_style=profile, scene_group_id=row["scene_group_id"] or "", style_strategy="qwen-direct-v3",
            template_id=legacy_templates.get(row["id"])))
    # Read source models before acquiring the write lock; only derived contracts
    # are replaced below. Source cuts and observations are never re-planned.
    source_result = service.result(job_id) if job["event_sequence_json"] else None
    source_shots = source_result.shots if source_result else []
    source_frames = source_result.frames if source_result else []
    now = utc_now()
    sequence_state = None
    with service.db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT result_json FROM style_analyses WHERE id=?", (analysis_id,)).fetchone()
        if not current or json.loads(current["result_json"])["profile"]["profile_hash"] != profile.profile_hash:
            raise ValueError("画风版本已变化，请刷新后应用")
        current_job = conn.execute("SELECT event_sequence_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not current_job or current_job["event_sequence_json"] != job["event_sequence_json"]:
            raise ValueError("生成片段版本已变化，请刷新后应用画风")
        revision = conn.execute("SELECT COALESCE(MAX(style_version),0)+1 FROM prompt_revisions WHERE job_id=?", (job_id,)).fetchone()[0]
        for prompt in prepared:
            old = conn.execute("SELECT * FROM prompts WHERE job_id=? AND id=?", (job_id, prompt.id)).fetchone()
            if old:
                old_contract = json.loads(old["contract_json"] or "{}")
                delivered = old_contract.get("delivery", {}).get("first_frame_prompt" if old["prompt_type"] == "first_frame" else "seedance_video_prompt")
                if delivered and delivered != old["positive_prompt"]:
                    raise ValueError("检测到手动修改的镜头Prompt，未覆盖；请先保存或导出当前编辑")
                # Archive the exact previous text/contract, not a reconstructed approximation.
                conn.execute("INSERT INTO prompt_revisions VALUES(?,?,?,?,?)", (job_id, old["id"], revision, json.dumps(dict(old), ensure_ascii=False), now))
            conn.execute("INSERT OR REPLACE INTO prompts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                prompt.id, job_id, prompt.shot_id, prompt.prompt_type, prompt.platform, prompt.version,
                prompt.positive_prompt, prompt.negative_prompt, prompt.status, prompt.template_id, prompt.template_version,
                json.dumps(prompt.warnings, ensure_ascii=False), prompt.contract.model_dump_json()))
        if current_job["event_sequence_json"]:
            previous_sequences = json.loads(current_job["event_sequence_json"])
            effective_prompts = []
            for stored in conn.execute("SELECT * FROM prompts WHERE job_id=?", (job_id,)).fetchall():
                value = dict(stored)
                value["warnings"] = json.loads(value.pop("warnings_json") or "[]")
                value["contract"] = json.loads(value.pop("contract_json") or "null")
                value.pop("job_id", None)
                effective_prompts.append(PromptBundle.model_validate(value))
            rebuilt = rerender_sequence_payload(previous_sequences, source_shots, effective_prompts, frames=source_frames)
            sequence_state = store_sequence_payload(conn, job_id, rebuilt,
                expected_version=previous_sequences.get("sequence_metadata", {}).get("version_number", 0))
        state = {"analysis_id": analysis_id, "profile": profile.model_dump(), "application_version": revision,
                 "automatic": automatic, "applied_at": now, "reference_scope": profile.reference_scope}
        if sequence_state:
            state["generation_sequence_version"] = sequence_state["sequence_metadata"]["version_number"]
        conn.execute("UPDATE jobs SET style_direct_json=?,updated_at=? WHERE id=?", (json.dumps(state, ensure_ascii=False), now, job_id))
    service._emit(job_id, "style.direct.applied", {"analysis_id": analysis_id, "version": revision})
    if sequence_state:
        service._emit(job_id, "sequence.ready", {"sequence_count": len(sequence_state["generation_sequences"]),
            "event_count": len(sequence_state["narrative_events"]), "mode": "persisted",
            "version": sequence_state["sequence_metadata"]["version_number"], "build_hash": sequence_state["sequence_metadata"]["build_hash"]})
    return state


def project_sources(service, job_id):
    shots = service.db.fetchall("SELECT id,scene_group_id FROM shots WHERE job_id=? ORDER BY number", (job_id,))
    candidates = []
    for shot in shots:
        frame = service.db.fetchone("SELECT * FROM frames WHERE shot_id=? AND selected=1 ORDER BY blur_score DESC,timestamp ASC LIMIT 1", (shot["id"],))
        if frame and Path(frame["path"]).is_file():
            candidates.append((frame, shot))
    if not candidates:
        return []
    # Time-spread representatives, preferring different scenes when available.
    indices = sorted({round(i * (len(candidates) - 1) / max(1, min(3, len(candidates)) - 1)) for i in range(min(3, len(candidates)))})
    chosen = [candidates[i] for i in indices]
    # Ensure sampling does not entirely miss a short, visually distinct scene.
    represented = {shot["scene_group_id"] for _, shot in chosen}
    scenes = list(dict.fromkeys(shot["scene_group_id"] for _, shot in candidates))
    for scene in scenes:
        if scene in represented or len(scenes) > 3:
            continue
        replacement = next(pair for pair in candidates if pair[1]["scene_group_id"] == scene)
        duplicate = next((i for i, (_, shot) in enumerate(chosen)
                          if sum(s["scene_group_id"] == shot["scene_group_id"] for _, s in chosen) > 1), None)
        if duplicate is not None:
            chosen[duplicate] = replacement
            represented.add(scene)
    chosen.sort(key=lambda pair: pair[0]["timestamp"])
    return [(Path(frame["path"]).read_bytes(), {"origin_job_id": job_id, "origin_frame_id": frame["id"],
                "scene_id": shot["scene_group_id"], "timestamp": frame["timestamp"]}) for frame, shot in chosen]


def draft_project_style(service, job_id):
    job = service.status(job_id)
    previous = json.loads(job["style_direct_json"] or "{}")
    if previous.get('analysis_id'):
        # Reuse the paid analysis even when partial/failed; refresh must not silently buy another run.
        return get_analysis(service, previous['analysis_id'])
    if previous.get("profile", {}).get("application_status") == "user-edited":
        return None
    if job["input_type"] == "script":
        service._emit(job_id, "style.direct.unavailable", {"detail": "剧本没有参考画面，请导入参考图分析或自行设定创作画风"})
        return None
    sources = project_sources(service, job_id)
    if not sources:
        return None
    analysis = create_analysis(service, sources, scope="project", synchronous=True)
    payload = analysis['result'].get('profile')
    profile = StyleProfileV3.model_validate(payload) if payload else None
    state = resolution(profile)
    service.db.update_job(job_id, style_direct_json=json.dumps({"analysis_id": analysis["analysis_id"],
        "profile": payload, "phase": "early-baseline", "frozen": state == 'resolved',
        "baseline_usable": usable_baseline(profile), "resolution_status": state}, ensure_ascii=False))
    service._emit(job_id, "style.direct.baseline_ready", {"analysis_id": analysis['analysis_id'],
        "status": analysis['status'], "resolution_status": state, "baseline_usable": usable_baseline(profile),
        "prompt": (analysis['result'].get('profile') or {}).get('rendered_prompt', '')})
    return analysis


def frame_quality(path):
    """Equal-width edge variance for frame ranking, not a semantic sharpness guarantee."""
    with Image.open(path) as source:
        image = source.convert('L')
        image.thumbnail((256, 256))
        edges = image.filter(ImageFilter.FIND_EDGES)
        return round(ImageStat.Stat(edges).var[0], 3), round(ImageStat.Stat(image).mean[0], 3)
