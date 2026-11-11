"""Freeze and reuse source plans independently from worker lifecycle."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from app.providers.base import FrameInput
from app.providers.bailian import CUT_GROUP_PROMPT_VERSION
from app.services import generation_plan
from app.services.video_processor import apply_semantic_groups, assign_scene_groups, extract_frames, extract_raw_contact_frames, split_long_shots


def stabilize_video_plan(service, row, result, job_config, provider_health, job_dir, *, semantic=True):
    """Freeze before perception. Style failures/resume never re-number existing shots."""
    job_id, video_path = row['id'], Path(row['video_path'])
    pinned = json.loads(row.get('generation_plan_json') or '{}')
    policy = json.loads(row['config_json']).get('source_grouping_policy', 'legacy-pre-analysis')
    if policy in ('post-perception-v2', 'raw-boundaries-v3'):
        # Freeze deterministic source segments first. Narrative merging must
        # use analyzed actions and endpoints, not midpoint contact sheets.
        semantic = False
    if pinned and pinned.get('video_sha256') and pinned['video_sha256'] != generation_plan.file_digest(video_path):
        raise ValueError('任务视频内容已改变，保留原计划；请创建新任务，不覆盖已有镜头')
    origin = 'task-frozen'
    if not pinned:
        # Preserve interrupted pre-cache jobs too; do not reinterpret their numbered shots.
        old_shots = service.db.fetchall('SELECT * FROM shots WHERE job_id=? ORDER BY number', (job_id,))
        old_raw = service.db.fetchall('SELECT * FROM raw_shots WHERE job_id=? ORDER BY number', (job_id,))
        if old_shots and old_raw:
            raw = [dict(number=r['number'], start_time=r['start_time'], end_time=r['end_time'],
                boundary_metrics=json.loads(r['boundary_metrics_json']), decision=r['decision'], reason=r['reason']) for r in old_raw]
            shots = [dict(number=s['number'], start_time=s['start_time'], end_time=s['end_time'],
                source_raw_numbers=[int(i.rsplit('-', 1)[-1]) for i in json.loads(s['source_raw_shot_ids_json'])],
                scene_group_id=s['scene_group_id'] or 'scene-001', split_reason=s['cut_reason'],
                source_edit_times=json.loads(s['source_edit_times_json']), scene_reason=s['scene_reason']) for s in old_shots]
            pinned = dict(version=generation_plan.VERSION, cache_key='', origin='legacy-task-frozen', raw_shots=raw, shots=shots)
    if not pinned and not semantic:
        key = generation_plan.plan_key(video_path, result, job_config, provider_health, [], CUT_GROUP_PROMPT_VERSION + ':local:' + policy)
        pinned = generation_plan.snapshot(result, key, 'local-deterministic')
        pinned['source_grouping_policy'] = policy
        origin = 'local-deterministic'
    if not pinned:
        contact_error = None
        try:
            contact_frames = extract_raw_contact_frames(video_path, result.raw_shots, job_dir / 'raw-contact', job_config)
            if len(contact_frames) != len(result.raw_shots):
                raise ValueError('联系表没有完整覆盖来源镜头，未请求模型')
            key = generation_plan.plan_key(video_path, result, job_config, provider_health, contact_frames, CUT_GROUP_PROMPT_VERSION)
        except Exception as exc:
            contact_error = str(exc)
            key = ''
            result.warnings.append(f'模型联系表准备失败，保留视觉指标分组：{exc}')
        force = bool(json.loads(row['config_json']).get('force_replan'))
        if contact_error:
            pinned = generation_plan.snapshot(result, key, 'local-fallback')
        else:
            pinned = None if force else generation_plan.load_shared(service.db, key, result, job_config)
        origin = 'local-fallback' if contact_error else 'shared-cache' if pinned else 'new-plan'
        if not pinned:
            try:
                protected = [r.number for r in result.raw_shots[:-1]
                    if r.boundary_metrics.get('fade_or_black') or r.boundary_metrics.get('scene_discontinuity')]
                with service._inference_slot():
                    groups = service.provider.group_raw_shots(
                        [FrameInput(f.id, f.timestamp, f.path) for f in contact_frames], protected)
                if not isinstance(groups, list) or not all(isinstance(g, list) and 1 <= len(g) <= job_config.max_raw_shots_per_narrative
                    and all(type(n) is int for n in g) for g in groups):
                    raise ValueError('模型合并组结构或来源数量无效')
                if [n for group in groups for n in group] != [r.number for r in result.raw_shots]:
                    raise ValueError('模型合并没有按顺序完整覆盖来源切段')
                candidate = copy.deepcopy(result)
                candidate.raw_shots = copy.deepcopy(result.raw_shots)
                candidate.shots = split_long_shots(
                    apply_semantic_groups(candidate.raw_shots, groups, job_config, allow_confirmed_angle_cuts=True),
                    raw_shots=candidate.raw_shots)
                assign_scene_groups(candidate.shots, candidate.raw_shots)
                pinned = generation_plan.snapshot(candidate, key, 'model-validated')
                pinned.update(model_groups=groups, provider=provider_health.provider, model=provider_health.model,
                    group_prompt_version=CUT_GROUP_PROMPT_VERSION,
                    video_sha256=generation_plan.file_digest(video_path))
                generation_plan.decode(pinned, result, job_config)
                generation_plan.save_shared(service.db, pinned, force=force, result=result, config=job_config)
                pinned = generation_plan.load_shared(service.db, key, result, job_config) or pinned
            except Exception as exc:
                result.warnings.append(f'模型联系表分组失败，保留视觉指标分组：{exc}')
                pinned = generation_plan.snapshot(result, key, 'local-fallback')
                origin = 'local-fallback'
    raw, shots = generation_plan.decode(pinned, result, job_config)
    if not pinned.get('video_sha256'):
        pinned['video_sha256'] = generation_plan.file_digest(video_path)
    if [(s.start_time, s.end_time, s.source_raw_numbers) for s in shots] != [(s.start_time, s.end_time, s.source_raw_numbers) for s in result.shots]:
        result.frames = extract_frames(video_path, shots, job_dir / 'frames', job_config)
    result.raw_shots, result.shots = raw, shots
    for r in raw:
        r.generation_numbers = [s.number for s in shots if r.number in s.source_raw_numbers]
        r.narrative_number = r.generation_numbers[0] if r.generation_numbers else 0
    pinned['raw_shots'] = generation_plan.snapshot(result, pinned['cache_key'], pinned['origin'])['raw_shots']
    service.db.update_job(job_id, generation_plan_json=json.dumps(pinned, ensure_ascii=False))
    service._emit(job_id, 'segmentation.plan_ready', {'origin': origin, 'version': pinned['version'],
        'cache_key': pinned['cache_key'], 'shot_count': len(shots), 'frozen': True})
    result.warnings.append(f'已固定分镜计划（{origin}）：{len(raw)} 个原始切段 → {len(shots)} 个叙事分镜')
