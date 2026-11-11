"""Opt-in bounded real-cloud regression; production jobs remain read-only."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sqlite3
import time

from app.media_paths import MediaPaths
from app.config import load_settings
from app.db import Database
from app.events import EventBus
from app.services.job_service import JobService


def verify(database, source_job_id, output, *, allow_paid=False):
    if not allow_paid:
        raise ValueError('Explicit --allow-paid required')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = Path(database).resolve()
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = MediaPaths(database.parent).row
        source = conn.execute('SELECT video_path,video_filename FROM jobs WHERE id=?', (source_job_id,)).fetchone()
    if not source or not Path(source['video_path']).is_file():
        raise ValueError('Saved source video missing')
    settings = replace(load_settings(prefer_private_cloud=True), provider='bailian',
        cloud_model='qwen3.8-flash', data_dir=output / 'data', segmentation_profile='source-boundaries-v4')
    isolated = output / 'validation.sqlite3'
    if isolated == database:
        raise ValueError('Validation must use an isolated database')
    db = Database(isolated)
    db.init()
    service = JobService(settings, db, EventBus())
    original_call, calls = service.provider._call, []
    def bounded_call(prompt, frames, stage, **kwargs):
        if len(calls) >= 10:
            raise RuntimeError('Verification budget exhausted (10 calls)')
        call = {'stage': stage, 'image_count': len(frames), 'started_at': time.monotonic()}
        calls.append(call)
        kwargs['max_attempts'] = 1
        try:
            result = original_call(prompt, frames, stage, **kwargs)
            call['succeeded'] = True
            return result
        finally:
            call['elapsed_seconds'] = round(time.monotonic() - call.pop('started_at'), 3)
    service.provider._call = bounded_call
    temporary_video = output / 'source.mp4'
    shutil.copyfile(source['video_path'], temporary_video)
    started, job_id = time.monotonic(), None
    report = {'source_job_id': source_job_id, 'production_database_modified': False,
              'model': settings.cloud_model, 'output': str(output)}
    try:
        job_id = service.create_job(source['video_filename'], '', temporary_video)
        previous = None
        while time.monotonic() - started < 600:
            row = service.status(job_id)
            state = (row['status'], row['stage'], row['progress'])
            if state != previous:
                print(json.dumps({'job_id': job_id, 'status': state[0], 'stage': state[1], 'progress': state[2]}, ensure_ascii=False), flush=True)
                previous = state
            if row['status'] in ('completed', 'failed', 'cancelled', 'interrupted'):
                break
            time.sleep(.25)
        result = service.result(job_id).model_dump(mode='json')
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        report.update(job_id=job_id, status=row['status'], error=row['error'],
            raw_shots=len(result['raw_shots']), source_shots=len(result['shots']),
            sequences=[{'sources': sequence['source_shot_ids'], 'duration': sequence['generation_duration_seconds'],
                        'status': sequence['status'], 'prompt': sequence['seedance_prompt']['positive_prompt']}
                       for sequence in result.get('generation_sequences', [])],
            shots=[{'id': shot['id'], 'source_duration': shot['duration'],
                    'generation_duration': shot['generation_duration_seconds'],
                    'action': shot['action'], 'status': shot['status'], 'review': shot['analysis_review']}
                   for shot in result['shots']])
        usage = json.loads(service.status(job_id)['model_usage_json'] or '[]')
        report['usage'] = usage
        style = result.get('style_analysis') or {}
        report['style_usage'] = style.get('usage', [])
    finally:
        service.executor.shutdown(wait=True)
        report.update(calls=calls, elapsed_seconds=round(time.monotonic() - started, 3))
        report['cost_cny'] = round(sum(item.get('estimated_list_price_cny') or 0 for item in report.get('usage', [])), 6)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({key: value for key, value in report.items() if key not in ('usage', 'style_usage', 'sequences')}, ensure_ascii=False), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path('data/storyboard.sqlite3'))
    parser.add_argument('--source-job-id', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--allow-paid', action='store_true')
    args = parser.parse_args()
    verify(args.database, args.source_job_id, args.out, allow_paid=args.allow_paid)
