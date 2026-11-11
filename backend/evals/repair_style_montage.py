"""Explicit paid replay of saved evidence; production history is read-only.

Uploads only this job's saved style images and shot boundary contact sheets.
Does not migrate history, change configuration, or create a production worker.
"""
import argparse
import json
import sqlite3
import time
import shutil
from dataclasses import replace
from pathlib import Path

from app.config import load_settings
from app.providers.bailian import BailianVisionProvider
from app.providers.base import FrameInput
from app.schemas import Shot
from app.media_paths import MediaPaths
from app.services.event_sequences import derive_sequences
from app.style_direct.pipeline import StylePipeline


def verify_delivery(database, job_id):
    """Exercise the actual HTTP read routes without worker startup or API calls."""
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import jobs, artifacts, style_direct
    from app.services.job_service import JobService

    class ReadOnlyDatabase:
        media = MediaPaths(Path(database).resolve().parent)
        def fetchall(self, query, params=()):
            with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as connection:
                connection.row_factory = self.media.row
                return connection.execute(query, params).fetchall()
        def fetchone(self, query, params=()):
            return next(iter(self.fetchall(query, params)), None)

    # Deliberately do not invoke JobService.__init__: it creates a worker and
    # performs interruption recovery writes, which are not read diagnostics.
    service = JobService.__new__(JobService)
    service.db = ReadOnlyDatabase()
    service.memory = SimpleNamespace(snapshot=lambda: {'mode': 'read-only-http-verification'})
    application = FastAPI()
    application.state.job_service = service
    application.state.db = service.db
    for router in (jobs.router, artifacts.router, style_direct.router):
        application.include_router(router, prefix='/api')
    with TestClient(application) as client:
        response = client.get(f'/api/jobs/{job_id}/result')
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['job']['status'] == 'completed'
        source = result['shots']
        sequences = result['generation_sequences']
        assert [identifier for seq in sequences for identifier in seq['source_shot_ids']] == [shot['id'] for shot in source]
        assert all(shot['duration'] < 5 for shot in source)
        assert all(seq['generation_duration_seconds'] <= 12 for seq in sequences)
        assert all(seq['first_frame_prompt'] and seq['seedance_prompt'] for seq in sequences)
        for frame in result['frames'] + result.get('evidence_frames', []):
            image = client.get(frame['path'])
            assert image.status_code == 200 and image.headers['content-type'].startswith('image/'), frame['id']
        style_id = result['style_analysis']['analysis_id']
        style = client.get(f'/api/style/analyses/{style_id}').json()
        for asset in style['result']['assets']:
            assert client.get(f'/api/style/analyses/{style_id}/images/{asset["alias"]}').status_code == 200
        assert client.get(f'/api/jobs/{job_id}/export').json()['generation_sequences'] == sequences
        assert client.get(f'/api/jobs/{job_id}/sequences').json()['generation_sequences'] == sequences
        prompt = style['result']['profile']['rendered_prompt']
        return {'job_id': job_id, 'source_count': len(source), 'sequence_count': len(sequences),
            'opened_frame_count': len(result['frames']) + len(result.get('evidence_frames', [])),
            'opened_style_image_count': len(style['result']['assets']), 'style_prompt': prompt,
            'style_in_source_prompts': sum(prompt in item['positive_prompt'] for item in result['prompts']),
            'source_prompt_count': len(result['prompts']),
            'style_in_sequence_prompts': sum(prompt in seq[key]['positive_prompt'] for seq in sequences
                for key in ('first_frame_prompt', 'seedance_prompt')),
            'sequence_prompt_count': len(sequences) * 2, 'database_access': 'read-only', 'cloud_calls': 0,
            'checks': ['result', 'frames', 'style-images', 'source-coverage', 'duration', 'both-prompts', 'export', 'sequence-route']}


def run_full_video(database, job_id, output):
    from app.db import Database
    from app.events import EventBus
    from app.services.job_service import JobService
    output = Path(output)
    if output.exists():
        raise ValueError('使用新的全链路报告目录')
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = MediaPaths(Path(database).resolve().parent).row
        saved = conn.execute('SELECT video_path FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not saved or not saved[0] or not Path(saved[0]).is_file():
        raise ValueError('任务原视频不存在')
    settings = load_settings(prefer_private_cloud=True)
    if settings.provider != 'bailian' or settings.cloud_model != 'qwen3.8-flash':
        raise ValueError('只允许已配置Flash云端模型')
    settings = replace(settings, data_dir=output / 'data')
    db = Database(settings.data_dir / 'storyboard.sqlite3')
    db.init()
    service = JobService(settings, db, EventBus())
    # Never move or overwrite the production source; create_job moves ONLY this copy.
    upload = output / 'regression-upload.mp4'
    shutil.copy2(saved[0], upload)
    started = time.monotonic()
    new_id = service.create_job('regression.mp4', '', upload)
    print('FULL JOB: ' + new_id, flush=True)
    previous = None
    try:
        while time.monotonic() - started < 1200:
            status = service.status(new_id)
            state = (status['stage'], status['progress'], status['status'])
            if state != previous:
                print(json.dumps({'stage': state[0], 'progress': state[1], 'status': state[2]}, ensure_ascii=False), flush=True)
                previous = state
            if state[2] in ('completed', 'failed', 'cancelled'):
                break
            time.sleep(1)
        service.executor.shutdown(wait=True)
        result = service.result(new_id).model_dump(mode='json')
        row = service.status(new_id)
        report = {'result': result, 'montage': json.loads(row['montage_json'] or '{}'),
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'source_database_access': 'read-only', 'production_job_modified': False}
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({'job_id': new_id, 'status': row['status'], 'error': row['error'],
            'shots': len(result['shots']), 'sequences': len(result.get('generation_sequences') or []),
            'sequence_metadata': result.get('sequence_metadata'), 'style': result.get('style_analysis'),
            'estimated_list_price_cny': result['metadata'].get('estimated_list_price_cny'),
            'elapsed_seconds': report['elapsed_seconds']}, ensure_ascii=False), flush=True)
        if row['status'] != 'completed':
            raise RuntimeError('全链路验收未完成，查看报告中的实际错误')
        return report
    finally:
        service.provider.unload()


def run(database, job_id, analysis_id, output):
    output = Path(output)
    if output.exists():
        raise ValueError('使用新的报告目录，不覆盖已有验证记录')
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        media = MediaPaths(Path(database).resolve().parent)
        conn.row_factory = media.row
        source_row = conn.execute('SELECT source_json FROM style_analyses WHERE id=?', (analysis_id,)).fetchone()
        rows = conn.execute('SELECT * FROM shots WHERE job_id=? ORDER BY number', (job_id,)).fetchall()
        frames = conn.execute('SELECT * FROM frames WHERE job_id=? AND selected=1 ORDER BY timestamp', (job_id,)).fetchall()
    if not source_row or not rows:
        raise ValueError('指定的保存证据不存在')
    source = json.loads(source_row['source_json'])
    source['assets'] = [media.record(asset, reading=True) for asset in source['assets']]
    settings = load_settings(prefer_private_cloud=True)
    if settings.provider != 'bailian' or settings.cloud_model != 'qwen3.8-flash':
        raise ValueError('付费回归仅允许已配置的云端qwen3.8-flash')
    provider = BailianVisionProvider(settings)
    if not provider.health().ready:
        raise ValueError(provider.health().detail)
    shots, montage_shots, by_shot = [], [], {}
    for row in rows:
        observation = json.loads(row['observation_json'] or '{}')
        shots.append(Shot(**{key: row[key] for key in ('id', 'number', 'start_time', 'end_time', 'duration', 'time', 'status', 'scene_group_id', 'scene_reason', 'generation_duration_seconds')},
            action=observation.get('subject_action', ''),
            **{key: observation.get(key, '') for key in ('shot_size', 'lens', 'camera_movement', 'lighting_vibe', 'composition')},
            confidence=observation.get('confidence', 0), action_details=observation.get('action_details', {}),
            visual_facts=observation.get('visual_facts', {}),
            source_raw_shot_ids=json.loads(row['source_raw_shot_ids_json'] or '[]'),
            source_edit_times=json.loads(row['source_edit_times_json'] or '[]')))
        montage_shots.append({key: row[key] for key in ('id', 'number', 'start_time', 'end_time', 'status')})
        montage_shots[-1]['observation'] = observation
        by_shot[row['id']] = [FrameInput(f['id'], f['timestamp'], Path(f['path'])) for f in frames if f['shot_id'] == row['id']]
    output.mkdir(parents=True)
    report = {'job_id': job_id, 'analysis_id': analysis_id, 'database_access': 'read-only', 'model': settings.cloud_model}
    started = time.monotonic()
    try:
        report['style'] = StylePipeline(provider, source['assets'], output / 'crops', scope=source['scope'],
            notify=lambda stage, detail: print(stage + ': ' + detail, flush=True)).run()
        (output / 'style.json').write_text(json.dumps(report['style'], ensure_ascii=False, indent=2))
        print('STYLE: ' + report['style']['profile']['rendered_prompt'], flush=True)
        report['montage'] = provider.analyze_montage(montage_shots, by_shot)
        report['sequences'] = derive_sequences(shots, montage=report['montage'])
    finally:
        report['model_usage'] = provider.drain_usage()
        provider.unload()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['estimated_list_price_cny'] = round(sum(u.get('estimated_list_price_cny') or 0 for u in report['model_usage']), 6)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    sequence = report['sequences']
    print(json.dumps({'style_prompt': report['style']['profile']['rendered_prompt'],
        'recommendations': [r['label'] for r in report['style']['recommendations']],
        'montage_status': report['montage']['status'], 'relations': len(report['montage']['relations']),
        'sequence_metadata': sequence['sequence_metadata'],
        'groups': [s['source_shot_ids'] for s in sequence['generation_sequences']],
        'elapsed_seconds': report['elapsed_seconds'], 'estimated_list_price_cny': report['estimated_list_price_cny']}, ensure_ascii=False, indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description='显式付费Flash验证，不改生产历史')
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--analysis-id')
    parser.add_argument('--full-video', action='store_true')
    parser.add_argument('--verify-only', action='store_true', help='只读HTTP交付检查，不调用模型、不启动Worker')
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    if args.verify_only:
        report = verify_delivery(args.database, args.job_id)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / 'delivery.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.full_video:
        run_full_video(args.database, args.job_id, args.output_dir)
    else:
        if not args.analysis_id:
            parser.error('只读证据回归需要--analysis-id')
        run(args.database, args.job_id, args.analysis_id, args.output_dir)


if __name__ == '__main__':
    main()
