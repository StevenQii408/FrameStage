import io
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.generation.providers import GeneratedAsset, ProviderResult
from app.main import app
from app.media_paths import MediaPaths
from app.services.video_processor import DetectedShot, ExtractedFrame, VideoProcessResult
from app.style_direct.service import create_analysis
from test_generation_tasks import configured, image_bytes, request


def test_moving_the_data_directory_preserves_http_media_and_legacy_evidence(tmp_path, monkeypatch):
    original = tmp_path / "custom-store"
    relocated = tmp_path / "renamed-store"
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(original))
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(tmp_path / "private-settings.env"))
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "gold").save(buffer, "JPEG")
    frame_bytes = buffer.getvalue()

    def extract(video, directory, config):
        image = directory / "keyframe.jpg"
        image.write_bytes(frame_bytes)
        frame = ExtractedFrame("frame-1-1", "shot-001", .5, str(image), str(image), 32, 32)
        return VideoProcessResult({"duration": 1}, [DetectedShot(1, 0, 1)], [frame], [])

    monkeypatch.setattr("app.services.job_service.process_video", extract)
    with TestClient(app) as client:
        jobs = client.app.state.job_service
        monkeypatch.setattr(jobs.executor, "submit", lambda function, *args: function(*args))
        response = client.post("/api/jobs", files={"video": ("input.mp4", b"source-video", "video/mp4")})
        assert response.status_code == 202
        job = response.json()["job_id"]
        frame_id = client.get(f"/api/jobs/{job}/result").json()["frames"][0]["id"]
        style = create_analysis(jobs, [(frame_bytes, {})], synchronous=True)["analysis_id"]
        generation = client.post("/api/generation-assets", files={"image": ("ref.jpg", frame_bytes, "image/jpeg")})
        assert generation.status_code == 201
        reference_url = generation.json()["url"]
        reference_bytes = client.get(reference_url).content
        generator = client.app.state.generation_service
        generated = GeneratedAsset(image_bytes("blue", (1920, 1080)), "image/png")
        _, executor = configured(generator, [ProviderResult("succeeded", assets=[generated])])
        generation_job = generator.create(request())
        executor.run()
        generated_url = generator.get(generation_job["id"])["assets"][0]["url"]
        generated_bytes = client.get(generated_url).content
        assert generated_bytes.startswith(b"\x89PNG")
        style_url = f"/api/style/analyses/{style}/images/I1"
        style_bytes = client.get(style_url).content
        assert style_bytes.startswith(b"\xff\xd8") and reference_bytes.startswith(b"\x89PNG")
        assert str(tmp_path) not in client.get("/api/health").text
        assert "private_env_file" not in client.get("/api/health").json()["configuration"]

    database = original / "storyboard.sqlite3"
    with sqlite3.connect(database) as db:
        (video,) = db.execute("SELECT video_path FROM jobs WHERE id=?", (job,)).fetchone()
        keyframe, thumbnail = db.execute("SELECT path,thumbnail_path FROM frames WHERE id=?", (frame_id,)).fetchone()
        refs = db.execute("SELECT path FROM generation_assets").fetchall()
        assert len(refs) == 2
        source, result = db.execute("SELECT source_json,result_json FROM style_analyses WHERE id=?", (style,)).fetchone()
        for key in (video, keyframe, thumbnail, *(ref[0] for ref in refs), json.loads(source)["assets"][0]["path"],
                    json.loads(result)["assets"][0]["original_path"]):
            assert not Path(key).is_absolute() and (original / key).is_file()
        # Upgraded users can have any mixture of old absolute and new keys.
        db.execute("UPDATE frames SET path=?,thumbnail_path=? WHERE id=?", (str(original / keyframe), str(original / thumbnail), frame_id))
        legacy = json.loads(result)
        legacy["assets"][0]["path"] = str(original / legacy["assets"][0]["path"])
        db.execute("UPDATE style_analyses SET result_json=? WHERE id=?", (json.dumps(legacy), style))

    original.rename(relocated)
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(relocated))
    with TestClient(app) as client:
        for url, expected in ((f"/api/jobs/{job}/video", b"source-video"),
                              (f"/api/jobs/{job}/frames/{frame_id}", frame_bytes),
                              (style_url, style_bytes), (reference_url, reference_bytes),
                              (generated_url, generated_bytes)):
            response = client.get(url)
            assert response.status_code == 200 and response.content == expected
        results = client.get(f"/api/jobs/{job}/result")
        assert results.status_code == 200 and str(tmp_path) not in results.text
        assert str(tmp_path) not in client.get(f"/api/style/analyses/{style}").text
        assert not original.exists()


def test_invalid_or_symlinked_media_never_resolve_outside_data(tmp_path):
    root = tmp_path / "data"
    (root / "jobs").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (root / "jobs" / "link").symlink_to(outside)
    media = MediaPaths(root)
    for value in (outside, "jobs/link", "jobs/../outside.txt", "/old/data/jobs/link"):
        assert not media.open_path(value)
        with pytest.raises(ValueError):
            media.store(value)

