import copy
import json
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.config import load_settings
from app.db import Database, utc_now
from app.events import EventBus
from app.schemas import JobConfig
from app.services import generation_plan as plans
from app.services.job_service import JobService
from app.services.video_processor import DetectedShot, RawDetectedShot, ExtractedFrame, VideoProcessResult


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = Database(tmp_path / 'test.sqlite3'); db.init()
    service = object.__new__(JobService)
    service.db, service.events = db, EventBus()
    service.settings = replace(load_settings(), data_dir=tmp_path)
    calls = []
    proposals = iter([[[1, 2], [3]], [[1], [2], [3]]])
    def grouping(*args):
        calls.append(args)
        return next(proposals)
    service.provider = SimpleNamespace(group_raw_shots=grouping)
    service._inference_slot = nullcontext
    video = tmp_path / 'v.mp4'; video.write_bytes(b'synthetic-video')
    frame = tmp_path / 'f.jpg'; frame.write_bytes(b'synthetic-frame')
    config = JobConfig()
    health = SimpleNamespace(provider='bailian', model='qwen3.8-flash')
    def result():
        return VideoProcessResult({'duration': 3.0},
            [DetectedShot(i, i-1., float(i), [i]) for i in (1, 2, 3)], [], [],
            [RawDetectedShot(i, i-1., float(i)) for i in (1, 2, 3)])
    frames = [ExtractedFrame(f'raw-{i}', '', float(i), str(frame), str(frame)) for i in (1, 2, 3)]
    monkeypatch.setattr('app.services.source_planning.extract_raw_contact_frames', lambda *args: frames)
    monkeypatch.setattr('app.services.source_planning.extract_frames', lambda *args: [])
    def job(identifier, force=False):
        db.insert_job(dict(id=identifier, status='queued', stage='validating', progress=0,
            video_path=str(video), config={**config.model_dump(), 'force_replan': force},
            created_at=utc_now(), updated_at=utc_now()))
        return dict(db.fetchone('SELECT * FROM jobs WHERE id=?', (identifier,)))
    return SimpleNamespace(service=service, db=db, calls=calls, result=result, job=job,
        config=config, health=health, frames=frames, video=video, directory=tmp_path)


def run(s, row):
    result = s.result()
    s.service._stabilize_video_plan(row, result, s.config, s.health, s.directory)
    return result


def test_identical_asset_uses_same_plan_without_new_model_request(setup):
    s = setup
    first, second = run(s, s.job('one')), run(s, s.job('two'))
    assert len(s.calls) == 1
    assert [x.source_raw_numbers for x in first.shots] == [[1, 2], [3]]
    assert first.shots == second.shots
    assert first.shots[0].source_edit_times == [1.0]
    assert json.loads(s.db.fetchone('SELECT generation_plan_json FROM jobs WHERE id=?', ('two',))[0])['origin'] == 'model-validated'
    assert s.db.events_since('two')[-1]['payload']['origin'] == 'shared-cache'
    saved = json.loads(s.db.fetchone('SELECT generation_plan_json FROM jobs WHERE id=?', ('one',))[0])
    assert [r['generation_numbers'] for r in saved['raw_shots']] == [[1], [1], [2]]


def test_new_source_policy_is_deterministic_and_merges_only_after_perception(setup):
    s = setup
    first = s.job('one')
    config = json.loads(first['config_json']); config['source_grouping_policy'] = 'post-perception-v2'
    first['config_json'] = json.dumps(config)
    before = run(s, first)
    second = s.job('two'); second['config_json'] = first['config_json']
    after = run(s, second)
    assert not s.calls
    assert before.shots == after.shots
    assert [shot.source_raw_numbers for shot in before.shots] == [[1], [2], [3]]
    saved = json.loads(s.db.fetchone('SELECT generation_plan_json FROM jobs WHERE id=?', ('one',))[0])
    assert saved['source_grouping_policy'] == 'post-perception-v2'
    assert saved['origin'] == 'local-deterministic'


def test_first_valid_shared_plan_wins_without_explicit_force(setup):
    s = setup; run(s, s.job('one'))
    saved = json.loads(s.db.fetchone('SELECT generation_plan_json FROM jobs WHERE id=?', ('one',))[0])
    candidate = s.result()
    candidate.shots = [DetectedShot(1, 0., 3., [1, 2, 3], source_edit_times=[1., 2.])]
    replacement = plans.snapshot(candidate, saved['cache_key'], 'model-validated')
    plans.save_shared(s.db, replacement, result=s.result(), config=s.config)
    assert plans.load_shared(s.db, saved['cache_key'], s.result(), s.config)['shots'] == saved['shots']


def test_force_is_explicit_and_existing_task_never_replanned(setup):
    s = setup
    first = run(s, s.job('one'))
    forced = run(s, s.job('two', True))
    assert len(first.shots) == 2 and len(forced.shots) == 3 and len(s.calls) == 2
    old = dict(s.db.fetchone('SELECT * FROM jobs WHERE id=?', ('one',)))
    assert run(s, old).shots == first.shots
    assert len(s.calls) == 2
    assert run(s, s.job('three')).shots == forced.shots
    assert len(s.calls) == 2


def test_failure_not_reused_across_jobs_but_frozen_on_resume(setup):
    s = setup
    def failure(*args):
        s.calls.append(args)
        raise ValueError('invalid model plan')
    s.service.provider.group_raw_shots = failure
    first = run(s, s.job('one'))
    assert len(first.shots) == 3
    assert not s.db.fetchall('SELECT * FROM generation_plan_cache')
    old = dict(s.db.fetchone('SELECT * FROM jobs WHERE id=?', ('one',)))
    assert run(s, old).shots == first.shots and len(s.calls) == 1
    run(s, s.job('two'))
    assert len(s.calls) == 2


def test_key_invalidates_on_asset_model_prompt_sampling_and_boundary_changes(setup):
    s = setup
    def key(**overrides):
        return plans.plan_key(overrides.get('video', s.video), overrides.get('result', s.result()),
            overrides.get('config', s.config), overrides.get('health', s.health), s.frames,
            overrides.get('prompt', 'v1'))
    base = key()
    assert key(config=s.config.model_copy(update={'vlm_timeout_seconds': 30})) == base
    assert key(prompt='v2') != base
    assert key(health=SimpleNamespace(provider='bailian', model='other-model')) != base
    assert key(config=s.config.model_copy(update={'jpeg_quality': 90})) != base
    r = s.result(); r.raw_shots[0].boundary_metrics['fade_or_black'] = True
    assert key(result=r) != base
    s.video.write_bytes(b'changed-video')
    assert key() != base


@pytest.mark.parametrize('damage', ['gap', 'source', 'protected', 'edits', 'number', 'duration'])
def test_invalid_cached_plan_never_applied(setup, damage):
    s = setup
    result = run(s, s.job('one'))
    payload = json.loads(s.db.fetchone('SELECT generation_plan_json FROM jobs WHERE id=?', ('one',))[0])
    if damage == 'gap': payload['shots'][1]['start_time'] += .1
    if damage == 'source': payload['shots'][0]['source_raw_numbers'] = [1]
    if damage == 'edits': payload['shots'][0]['source_edit_times'] = []
    if damage == 'number': payload['shots'][1]['number'] = 1
    if damage == 'duration': payload['shots'][0]['end_time'] = 6
    source = s.result()
    if damage == 'protected': source.raw_shots[0].boundary_metrics['fade_or_black'] = True
    with pytest.raises(ValueError): plans.decode(payload, source, s.config)


def test_database_migration_idempotent_and_preserves_jobs(setup):
    s = setup; s.job('old'); s.db.init(); s.db.init()
    assert s.db.fetchone('SELECT id FROM jobs WHERE id=?', ('old',))['id'] == 'old'
    assert 'generation_plan_json' in [r['name'] for r in s.db.fetchall('PRAGMA table_info(jobs)')]


def test_invalid_shared_cache_is_revalidated_before_use(setup):
    s = setup
    run(s, s.job('one'))
    cached = s.db.fetchone('SELECT * FROM generation_plan_cache')
    payload = json.loads(cached['plan_json']); payload['shots'][0]['source_raw_numbers'] = [1]
    s.db.execute('UPDATE generation_plan_cache SET plan_json=?', (json.dumps(payload),))
    # Rejected cached data must not silently become the authoritative plan.
    fresh = run(s, s.job('two'))
    assert len(s.calls) == 2 and len(fresh.shots) == 3
    assert fresh.shots[0].source_raw_numbers == [1]
    assert run(s, s.job('three')).shots == fresh.shots
    assert len(s.calls) == 2


def test_pinned_task_rejects_input_changed_in_place(setup):
    s = setup; run(s, s.job('one'))
    row = dict(s.db.fetchone('SELECT * FROM jobs WHERE id=?', ('one',)))
    s.video.write_bytes(b'other-content-with-same-boundaries')
    with pytest.raises(ValueError, match='视频内容已改变'):
        run(s, row)
    assert len(s.calls) == 1


@pytest.mark.parametrize('semantic', [True, False])
def test_legacy_interrupted_task_keeps_existing_shot_ids_without_model_call(setup, semantic):
    s = setup; row = s.job('legacy')
    for i in (1, 2, 3):
        s.db.execute('INSERT INTO raw_shots(id,job_id,number,start_time,end_time,duration) VALUES(?,?,?,?,?,?)',
                     (f'legacy-raw-{i:03d}', 'legacy', i, i-1., float(i), 1.))
    for i, start, end, sources, edits in ((1, 0., 2., [1, 2], [1.]), (2, 2., 3., [3], [])):
        s.db.execute('INSERT INTO shots(id,job_id,number,start_time,end_time,duration,time,status,source_raw_shot_ids_json,source_edit_times_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (f'legacy-shot-{i:03d}', 'legacy', i, start, end, end-start, '', 'ready',
             json.dumps([f'legacy-raw-{n:03d}' for n in sources]), json.dumps(edits)))
    frozen = s.result()
    s.service._stabilize_video_plan(row, frozen, s.config, s.health, s.directory, semantic=semantic)
    assert [shot.source_raw_numbers for shot in frozen.shots] == [[1, 2], [3]]
    assert not s.calls
    assert s.db.events_since('legacy')[-1]['payload']['origin'] == 'task-frozen'


def test_invalid_pinned_task_is_not_silently_replanned(setup):
    s = setup; run(s, s.job('one'))
    row = dict(s.db.fetchone('SELECT * FROM jobs WHERE id=?', ('one',)))
    payload = json.loads(row['generation_plan_json']); payload['shots'][1]['start_time'] = 2.1
    row['generation_plan_json'] = json.dumps(payload)
    with pytest.raises(ValueError, match='缺口'):
        run(s, row)
    assert len(s.calls) == 1


def test_local_plan_frozen_without_model_even_if_inference_later_available(setup):
    s = setup; row = s.job('local'); result = s.result()
    s.service._stabilize_video_plan(row, result, s.config, s.health, s.directory, semantic=False)
    assert not s.calls and len(result.shots) == 3
    resumed = dict(s.db.fetchone('SELECT * FROM jobs WHERE id=?', ('local',)))
    assert run(s, resumed).shots == result.shots and not s.calls


def test_contact_preparation_failure_does_not_lose_local_plan(setup, monkeypatch):
    s = setup
    monkeypatch.setattr('app.services.source_planning.extract_raw_contact_frames', lambda *args: [])
    result = run(s, s.job('one'))
    assert len(result.shots) == 3 and not s.calls
    assert not s.db.fetchall('SELECT * FROM generation_plan_cache')
    assert any('联系表准备失败' in warning for warning in result.warnings)


def test_new_source_policy_keeps_a_long_real_shot_without_contact_model(setup):
    s = setup
    row = s.job('uncut')
    config = json.loads(row['config_json']); config['source_grouping_policy'] = 'raw-boundaries-v3'
    row['config_json'] = json.dumps(config)
    result = VideoProcessResult({'duration': 5.483333},
        [DetectedShot(1, 0., 5.483333, [1])], [], [], [RawDetectedShot(1, 0., 5.483333)])
    s.service._stabilize_video_plan(row, result, s.config, s.health, s.directory)
    assert not s.calls
    assert [(shot.start_time, shot.end_time) for shot in result.shots] == [(0., 5.483333)]
    payload = json.loads(s.db.fetchone('SELECT generation_plan_json FROM jobs WHERE id=?', ('uncut',))[0])
    assert payload['version'] == 'generation-plan.v2'
    assert payload['source_grouping_policy'] == 'raw-boundaries-v3'


def test_v1_pinned_duration_splits_remain_unchanged_after_upgrade(setup):
    s = setup
    row = s.job('historical')
    result = VideoProcessResult({'duration': 5.483333},
        [DetectedShot(1, 0., 5.483333, [1])], [], [], [RawDetectedShot(1, 0., 5.483333)])
    payload = plans.snapshot(result, 'historic-key', 'local-deterministic')
    payload['version'] = 'generation-plan.v1'
    payload['shots'] = [
        dict(number=1, start_time=0., end_time=2.05, source_raw_numbers=[1]),
        dict(number=2, start_time=2.05, end_time=5.483333, source_raw_numbers=[1]),
    ]
    payload['video_sha256'] = plans.file_digest(s.video)
    row['generation_plan_json'] = json.dumps(payload)
    s.service._stabilize_video_plan(row, result, s.config, s.health, s.directory)
    assert not s.calls
    assert [(shot.start_time, shot.end_time) for shot in result.shots] == [(0., 2.05), (2.05, 5.483333)]
    assert s.db.events_since('historical')[-1]['payload']['version'] == 'generation-plan.v1'


def test_v1_shared_index_is_not_reused_as_a_new_policy_plan(setup):
    s = setup
    run(s, s.job('one'))
    cached = s.db.fetchone('SELECT * FROM generation_plan_cache')
    payload = json.loads(cached['plan_json']); payload['version'] = 'generation-plan.v1'
    s.db.execute('UPDATE generation_plan_cache SET plan_json=?', (json.dumps(payload),))
    assert plans.load_shared(s.db, cached['cache_key'], s.result(), s.config) is None
    fresh = run(s, s.job('two'))
    assert len(s.calls) == 2
    assert run(s, s.job('three')).shots == fresh.shots
    assert len(s.calls) == 2
