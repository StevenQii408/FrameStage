"""Video posters are lazy, cached, local and guarded by publication state."""
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.api.generations import router
from app.db import Database
from app.events import EventBus
from app.generation.models import GenerationError
from app.generation.providers import GeneratedAsset, ProviderResult
from app.generation.service import GenerationService
from app.generation.assets import validate_output_contract
from test_generation_tasks import configured, image_bytes, request


@pytest.fixture
def video_service(tmp_path):
    service = GenerationService(SimpleNamespace(data_dir=tmp_path), Database(tmp_path / "jobs.db"), EventBus())
    source = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
        "color=c=red:s=1280x720:r=25:d=5", "-vf",
        "drawbox=color=0x3167a2:t=fill:enable='eq(n,0)'", "-c:v", "libx264", "-threads", "1",
        "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source)], check=True, capture_output=True, timeout=15)
    reference = service.assets.image(image_bytes())
    _, executor = configured(service, [ProviderResult("succeeded", assets=[GeneratedAsset(source.read_bytes(), "video/mp4")])])
    job = service.create(request(kind="video", reference_asset_id=reference["id"],
        settings={"resolution": "720p", "aspect_ratio": "16:9", "duration_seconds": 5}))
    executor.run()
    assert service.get(job["id"])["status"] == "succeeded"
    yield service, service.get(job["id"])["assets"][0]
    service.close()


def test_generated_and_historical_video_poster_is_real_jpeg_and_cached(video_service, monkeypatch):
    service, asset = video_service
    # Previous releases cached an offset frame. The first source frame is blue;
    # the frame at 0.04 seconds and every subsequent frame are red.
    Image.new("RGB", (640, 360), "red").save(service.assets.root / (asset["id"] + ".poster.jpg"))
    path = service.assets.video_poster(asset["id"])
    with Image.open(path) as image:
        assert image.format == "JPEG" and image.size == (640, 360)
        red, green, blue = image.getpixel((300, 150))
        assert blue > green > red
    monkeypatch.setattr("app.generation.assets.subprocess.run", lambda *_args, **_kwargs: pytest.fail("cached poster must not rerun FFmpeg"))
    assert service.assets.video_poster(asset["id"]) == path


def test_video_poster_api_and_original_byte_ranges(video_service):
    service, asset = video_service
    app = FastAPI()
    app.state.generation_service = service
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        response = client.get(asset["url"] + "/poster")
        assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
        response = client.get(asset["url"], headers={"Range": "bytes=0-31"})
        assert response.status_code == 206 and len(response.content) == 32
        assert response.headers["content-type"] == "video/mp4"


def test_video_poster_unavailable_is_a_clear_error_not_a_fake_image(video_service, monkeypatch):
    service, asset = video_service
    monkeypatch.setattr("app.generation.assets.subprocess.run", lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("ffmpeg", 8)))
    with pytest.raises(GenerationError) as error:
        service.assets.video_poster(asset["id"])
    assert error.value.code == "poster_unavailable"
    assert not list(service.assets.root.glob(".poster-*"))


def test_poster_does_not_expose_reference_images_or_unpublished_outputs(video_service):
    service, _ = video_service
    image = service.assets.image(image_bytes())
    with pytest.raises(GenerationError):
        service.assets.video_poster(image["id"])


@pytest.mark.parametrize("dimensions,resolution,ratio", [
    ((832, 480), "480p", "16:9"), ((960, 960), "720p", "1:1"),
    ((1080, 1920), "1080p", "9:16"),
])
def test_wan_tier_validation_correlates_reported_tier_and_decoded_geometry(dimensions, resolution, ratio):
    # Synthetic geometry tests define our limited alignment policy; they do
    # not claim these are an official Wan3 pixel-bucket table.
    validate_output_contract(*dimensions, {"model_id": "wan3.0-video", "resolution": resolution,
        "aspect_ratio": ratio, "duration_seconds": 5}, duration=5,
        provider_spec={"SR": int(resolution[:-1]), "ratio": ratio})


@pytest.mark.parametrize("dimensions,spec", [
    ((832, 480), {"SR": 480, "ratio": "16:9"}),
    ((1280, 720), {"SR": 1080, "ratio": "16:9"}),
    ((720, 1280), {"SR": 720, "ratio": "16:9"}),
    ((1920, 1080), {"SR": 720, "ratio": "16:9"}),
])
def test_wan_tier_never_accepts_wrong_reported_tier_or_decoded_scale(dimensions, spec):
    with pytest.raises(GenerationError):
        validate_output_contract(*dimensions, {"model_id": "wan3.0-video", "resolution": "720p",
            "aspect_ratio": "16:9", "duration_seconds": 5}, duration=5, provider_spec=spec)
