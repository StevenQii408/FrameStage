from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.main import app


def test_source_video_supports_range_head_missing_and_project_scope(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOARD_PROVIDER', 'mock')
    monkeypatch.setenv('STORYBOARD_DATA_DIR', str(tmp_path / 'data'))
    with TestClient(app) as client:
        service = client.app.state.job_service
        service.executor.submit = Mock()
        first = client.post('/api/projects', json={'name': '原片项目'}).json()['id']
        second = client.post('/api/projects', json={'name': '其他项目'}).json()['id']
        video = tmp_path / 'original.mp4'
        video.write_bytes(b'0123456789abcdef')
        job = service.create_job('original.mp4', '', video, project_id=first)
        url = f'/api/jobs/{job}/video?project_id={first}'
        response = client.get(url, headers={'Range': 'bytes=2-5'})
        assert response.status_code == 206
        assert response.content == b'2345'
        assert response.headers['content-range'] == 'bytes 2-5/16'
        assert str(tmp_path) not in response.headers.get('content-disposition', '')
        assert client.head(url).status_code == 200
        assert client.head(url).content == b''
        assert client.get(url, headers={'Range': 'bytes=99-100'}).status_code == 416
        assert client.get(f'/api/jobs/{job}/video?project_id={second}').status_code in (403, 404)
        Path(service.status(job)['video_path']).unlink()
        assert client.get(url).status_code == 404
