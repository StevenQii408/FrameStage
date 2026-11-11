"""Content-addressed, validated generation plans. Never caches model failures globally."""
import hashlib
import json
import math
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from app.db import utc_now
from app.services.video_processor import DetectedShot, RawDetectedShot, validate_generation_plan

VERSION = "generation-plan.v2"
READABLE_VERSIONS = {"generation-plan.v1", VERSION}


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def plan_key(video_path, result, config, health, frames, prompt_version):
    dependencies = {}
    for package in ('scenedetect', 'opencv-python', 'Pillow'):
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            dependencies[package] = 'unavailable'
    payload = {
        'version': VERSION, 'video_sha256': file_digest(video_path),
        'segmentation': result.segmentation_profile, 'prompt_version': prompt_version,
        'provider': health.provider, 'model': health.model, 'dependencies': dependencies,
        'config': {key: getattr(config, key) for key in (
            'segmentation_profile', 'narrative_min_seconds', 'narrative_max_seconds',
            'max_raw_shots_per_narrative', 'max_shots', 'frames_per_shot',
            'max_frames_per_shot', 'frame_max_long_edge', 'jpeg_quality')},
        'raw_boundaries': [(r.number, r.start_time, r.end_time, r.boundary_metrics) for r in result.raw_shots],
        'contact_frames': [(f.timestamp, file_digest(f.path)) for f in frames],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def snapshot(result, key, origin):
    raw = [asdict(r) for r in result.raw_shots]
    for r in raw:
        r['generation_numbers'] = [s.number for s in result.shots if r['number'] in s.source_raw_numbers]
        r['narrative_number'] = r['generation_numbers'][0] if r['generation_numbers'] else 0
    return {'version': VERSION, 'cache_key': key, 'origin': origin,
            'raw_shots': raw,
            'shots': [asdict(s) for s in result.shots]}


def decode(payload, result, config):
    # Old task snapshots remain pinned. Only newly built/shared plans use v2;
    # reading a v1 snapshot must never silently renumber existing source cards.
    if payload.get('version') not in READABLE_VERSIONS:
        raise ValueError('分镜计划版本不匹配')
    raw = [RawDetectedShot(**r) for r in payload['raw_shots']]
    shots = [DetectedShot(**s) for s in payload['shots']]
    boundaries = lambda rows: [(r.number, r.start_time, r.end_time) for r in rows]
    if boundaries(raw) != boundaries(result.raw_shots):
        raise ValueError('已保存计划与视频原始边界不匹配，未重新规划或覆盖旧镜头')
    if not shots or len(shots) > config.max_shots:
        raise ValueError('分镜计划为空或超过上限')
    if [r.number for r in raw] != list(range(1, len(raw) + 1)):
        raise ValueError('原始切段编号无效')
    for index, s in enumerate(shots, 1):
        if s.number != index:
            raise ValueError('分镜编号不连续或重复')
        if not math.isfinite(s.start_time) or not math.isfinite(s.end_time) or s.end_time <= s.start_time:
            raise ValueError('分镜时间无效')
    validate_generation_plan(raw, shots)
    for s in shots:
        if len(s.source_raw_numbers) > config.max_raw_shots_per_narrative:
            raise ValueError('镜头来源数量超过限制')
        for number in s.source_raw_numbers[:-1]:
            metrics = result.raw_shots[number - 1].boundary_metrics
            if metrics.get('fade_or_black') or metrics.get('scene_discontinuity'):
                raise ValueError('分镜计划跨越保护边界')
        expected_edits = [r.end_time for r in raw[:-1] if s.start_time + 1e-5 < r.end_time < s.end_time - 1e-5]
        if s.source_edit_times != expected_edits:
            raise ValueError('分镜计划遗漏内部原剪辑切点')
    return raw, shots


def load_shared(db, key, result, config):
    row = db.fetchone('SELECT plan_json FROM generation_plan_cache WHERE cache_key=?', (key,))
    if not row:
        return None
    try:
        payload = json.loads(row['plan_json'])
        if payload.get('version') != VERSION or payload.get('cache_key') != key or payload.get('origin') != 'model-validated':
            return None
        decode(payload, result, config)
        return payload
    except (ValueError, KeyError, TypeError):
        return None


def save_shared(db, payload, *, force=False, result=None, config=None):
    # A normal run never replaces an accepted plan with a fresh random proposal.
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT plan_json FROM generation_plan_cache WHERE cache_key=?', (payload['cache_key'],)).fetchone()
        invalid = False
        if existing and result is not None and config is not None:
            try:
                old = json.loads(existing['plan_json'])
                if old.get('version') != VERSION or old.get('cache_key') != payload['cache_key'] or old.get('origin') != 'model-validated':
                    raise ValueError('缓存元数据不一致')
                decode(old, result, config)
            except (ValueError, KeyError, TypeError):
                invalid = True
        sql = ('INSERT OR REPLACE' if force or invalid else 'INSERT OR IGNORE') + ' INTO generation_plan_cache VALUES(?,?,?)'
        conn.execute(sql, (payload['cache_key'], json.dumps(payload, ensure_ascii=False), utc_now()))
