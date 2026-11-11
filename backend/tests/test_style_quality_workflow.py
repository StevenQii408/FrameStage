import copy
import json
import sqlite3

import pytest
from pydantic import ValidationError

from evals.style_direct_gold import GoldSet, independence, prepare_draft, run, sample_video
from evals.style_direct_review import summarize
from evals.replay_style_direct import export_saved
from test_style_direct import image_bytes
from test_style_direct_gold import manifest


def v4(data, **changes):
    raw = manifest(data, reviewed_at='2026-10-03', annotation_signature='human-test', naming_expectation='descriptive')
    raw.update(schema_version='style-direct-gold.v4', corpus_id='development-01')
    raw['samples'][0]['features'][0]['evidence'] = [dict(frame_number=1, description='第1帧可观察轮廓')]
    raw.update(changes)
    return raw


def test_draft_has_twelve_images_three_videos_without_inventing_truth(tmp_path):
    (tmp_path / 'image.png').write_bytes(image_bytes())
    data = prepare_draft(tmp_path, images=['image.png'])
    assert len(data['samples']) == 15
    assert sum(s['media_type'] == 'image' for s in data['samples']) == 12
    assert sum(s['media_type'] == 'video' for s in data['samples']) == 3
    assert data['annotation_status'] == 'pending-human'
    assert data['samples'][0]['sha256'] and not data['samples'][0]['features']
    assert all(s['reviewer'] == '' and s['rights'] is None for s in data['samples'])
    with pytest.raises(ValidationError):
        GoldSet.model_validate(data)


def test_v4_requires_real_human_annotation_and_disjoint_acceptance():
    raw = v4(image_bytes()); gold = GoldSet.model_validate(raw)
    blank = copy.deepcopy(raw); blank['samples'][0]['annotation_signature'] = ''
    with pytest.raises(ValidationError, match='人工标注'):
        GoldSet.model_validate(blank)
    missing = copy.deepcopy(raw); missing['samples'][0]['features'][0]['evidence'] = []
    with pytest.raises(ValidationError, match='帧证据'):
        GoldSet.model_validate(missing)
    acceptance = GoldSet.model_validate(v4(image_bytes(), split='acceptance', corpus_id='acceptance-01'))
    assert not independence(acceptance)['verified']
    with pytest.raises(ValueError, match='相同素材'):
        independence(acceptance, gold)
    disjoint = GoldSet.model_validate(v4(image_bytes(color=(1, 2, 3)), split='acceptance', corpus_id='acceptance-01'))
    assert independence(disjoint, gold)['verified']


def test_blank_mock_and_hash_changed_reviews_cannot_be_real_accuracy(tmp_path):
    data = image_bytes(); (tmp_path / 'frame.png').write_bytes(data)
    output = tmp_path / 'report'; run(GoldSet.model_validate(v4(data)), tmp_path, output)
    report, review = output / 'report.json', output / 'human-review.json'
    assert summarize(report, review)['quality_scores'] is None
    raw = json.loads(review.read_text())
    raw.update(reviewer='human', reviewed_at='2026-10-03', annotation_signature='signed')
    raw['samples'][0].update(facts_accurate=True, representative=True, content_leakage=False,
                             unsupported_expansion=False, prompt_usable=True)
    review.write_text(json.dumps(raw))
    result = summarize(report, review)
    assert result['human_review_complete'] and result['quality_scores'] is None
    report.write_text(report.read_text() + '\n')
    with pytest.raises(ValueError, match='SHA'):
        summarize(report, review)


def test_saved_live_result_can_be_reviewed_with_zero_new_requests(tmp_path, monkeypatch):
    data = image_bytes(); (tmp_path / 'frame.png').write_bytes(data)
    source = tmp_path / 'source'; run(GoldSet.model_validate(v4(data)), tmp_path, source)
    report = json.loads((source / 'report.json').read_text())
    # Synthetic branch fixture only, not a production accuracy result.
    report['samples'][0]['prediction']['probe_mode'] = 'live'
    replay = tmp_path / 'saved-live-fixture.json'; replay.write_text(json.dumps(report))
    monkeypatch.setattr('evals.style_direct_gold.analyze_file', lambda *a, **k: pytest.fail('replay must not call provider'))
    output = tmp_path / 'replay'; summary = run(GoldSet.model_validate(v4(data)), tmp_path, output, replay=replay)
    assert summary['mode'] == 'replay' and summary['new_paid_requests'] == 0
    assert summary['quality_scores'] is None
    review = output / 'human-review.json'; raw = json.loads(review.read_text())
    raw.update(reviewer='synthetic-test', reviewed_at='2026-10-03', annotation_signature='fixture')
    raw['samples'][0].update(facts_accurate=False, representative=True, content_leakage=False,
                            unsupported_expansion=True, prompt_usable=False, comments='测试分支不是生产评分')
    review.write_text(json.dumps(raw))
    graded = summarize(output / 'report.json', review)
    assert graded['quality_scores']['facts_accurate_rate'] == 0
    assert graded['quality_scores']['unsupported_expansion_rate'] == 1
    assert not graded['independent_acceptance']
    assert 'unsupported_expansion' in graded['failures'][0]['issues']


def test_replay_media_hash_mismatch_stops_without_inference(tmp_path, monkeypatch):
    data = image_bytes(); (tmp_path / 'frame.png').write_bytes(data)
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'samples': [{'id': 'gold-01', 'media_sha256': '0' * 64, 'prediction': {'profile': {}}}]}))
    monkeypatch.setattr('evals.style_direct_gold.analyze_file', lambda *a, **k: pytest.fail('must not call'))
    with pytest.raises(ValueError, match='SHA'):
        run(GoldSet.model_validate(v4(data)), tmp_path, tmp_path / 'out', replay=bad)


def test_database_export_readonly_original_hash_and_no_paths(tmp_path):
    from evals.style_direct_gold import file_hash
    path = tmp_path / 'saved.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE style_analyses(id TEXT,source_json TEXT,result_json TEXT)')
        conn.execute('INSERT INTO style_analyses VALUES(?,?,?)', ('a', json.dumps({'assets': [{'source_sha256': '1' * 64, 'sha256': '2' * 64}]}),
            json.dumps({'profile': {}, 'assets': [{'path': '/private/image', 'original_path': '/private/original'}]})))
    before = file_hash(path)
    result = export_saved(path, {'samples': [{'id': 's', 'media_sha256': '1' * 64, 'analysis_id': 'a'}]})
    assert result['new_paid_requests'] == 0 and '/private/' not in json.dumps(result)
    assert file_hash(path) == before
    with pytest.raises(ValueError, match='原素材哈希'):
        export_saved(path, {'samples': [{'id': 's', 'media_sha256': '2' * 64, 'analysis_id': 'a'}]})


def test_video_uses_four_decoded_frames_with_absolute_timestamps(tmp_path):
    import cv2
    import numpy as np
    from evals.style_direct_gold import VideoWindow
    video = tmp_path / 'fixture.avi'
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), 20, (64, 64))
    if not writer.isOpened():
        pytest.skip('本机缺少MJPG写入器')
    for index in range(40):
        writer.write(np.full((64, 64, 3), index * 5, dtype=np.uint8))
    writer.release()
    assets = sample_video(video, VideoWindow(start_seconds=.2, end_seconds=1.8), tmp_path / 'frames')
    assert len(assets) == 4
    times = [asset['timestamp'] for asset in assets]
    assert times == sorted(set(times)) and all(.2 <= time <= 1.8 for time in times)
    assert all(asset['timestamp_basis'] and asset['source_sha256'] for asset in assets)
