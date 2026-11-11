"""Bounded local image ingestion; callers cannot submit a URL or filesystem path."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import subprocess
import tempfile
import threading
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.db import utc_now
from app.generation.models import GenerationError, VIDEO_DURATION_EXTENSION_MAX_SECONDS


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_SIDE = 12_000
ALLOWED = {"PNG", "JPEG", "WEBP", "GIF", "AVIF"}


def validate_output_contract(width, height, settings, *, duration=None, provider_spec=None):
    """Explicit image sizes and legacy adapters use short-edge pixels.

    Ratios permit 1% codec/alignment rounding; short edge permits two pixels.
    Video duration permits a quarter second of container/frame-time rounding.
    Wan3 additionally corroborates its reported tier against decoded geometry.
    Container size and duration always come from local decoders.
    """
    left, right = (float(value) for value in settings["aspect_ratio"].split(":"))
    expected_ratio = left / right
    expected_short_edge = int(settings["resolution"].removesuffix("p"))
    tier = duration is not None and settings.get("model_id") == "wan3.0-video" and provider_spec is not None
    if tier:
        # Wan exposes a resolution tier, not an exact width/height contract.
        # Corroborate authenticated usage with decoded geometry; never resize a
        # file to make it pass. These bounded tolerances are local policy, not
        # an invented vendor pixel-bucket table.
        if (type(provider_spec.get("SR")) is not int or provider_spec["SR"] != expected_short_edge
                or provider_spec.get("ratio") != settings["aspect_ratio"]):
            raise GenerationError(422, "云端计量档位或画幅与任务合同不一致", "output_contract_mismatch")
        ratio_error = abs(width / height / expected_ratio - 1)
        aligned = width % 8 == 0 and height % 8 == 0 and abs(width - height * expected_ratio) <= 32
        if ratio_error > 0.01 and (ratio_error > 0.03 or not aligned):
            raise GenerationError(422, "视频画幅超出有限编码对齐容差", "output_contract_mismatch")
        aspect = max(expected_ratio, 1 / expected_ratio)
        pixels = width * height
        if not expected_short_edge**2 * 0.9 <= pixels <= max(16 / 9, aspect) * expected_short_edge**2 * 1.1:
            raise GenerationError(422, "视频实际像素规模与云端档位不一致", "output_contract_mismatch")
    elif abs(width / height / expected_ratio - 1) > 0.01:
        raise GenerationError(422, "生成产物画幅与任务合同不一致", "output_contract_mismatch")
    if not tier and abs(min(width, height) - expected_short_edge) > 2:
        raise GenerationError(422, "生成产物分辨率与任务合同不一致", "output_contract_mismatch")
    if duration is not None and abs(duration - settings["duration_seconds"]) > 0.25:
        raise GenerationError(422, "生成产物时长与任务合同不一致", "output_contract_mismatch")


class GenerationAssets:
    def __init__(self, data_dir, repository):
        self.root = (Path(data_dir) / "generation-assets").resolve()
        self.repository = repository
        self._poster_gate = threading.BoundedSemaphore(2)

    def path(self, asset_id):
        asset = self.repository.asset(asset_id)
        if not asset:
            raise GenerationError(404, "图片资产不存在", "asset_missing")
        path = Path(asset["path"]).resolve()
        if self.root not in path.parents or not path.is_file():
            raise GenerationError(404, "图片资产文件不可用", "asset_missing")
        if asset["role"] == "output":
            job = self.repository.get(asset["job_id"])
            if not job or job["status"] != "succeeded" or not any(item["id"] == asset_id for item in job["assets"]):
                raise GenerationError(404, "生成产物尚未交付", "asset_unpublished")
        return asset, path

    def image(self, data, *, role="reference", job_id=None, settings=None):
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise GenerationError(413, "图片为空或超过 20MB 限制", "asset_size_limit")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as source:
                    if source.format not in ALLOWED:
                        raise ValueError("不支持此图片格式")
                    if source.width * source.height > MAX_PIXELS or max(source.size) > MAX_SIDE:
                        raise ValueError("图片超过 2400 万像素或单边 12000 像素限制")
                    source.seek(0)  # Animated imports are explicitly first-frame references.
                    image = ImageOps.exif_transpose(source).convert("RGBA")
                    width, height = image.size
                    output = io.BytesIO()
                    image.save(output, "PNG")  # Strip private EXIF and standardize decoder output.
                    normalized = output.getvalue()
        except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise GenerationError(422, "无法读取安全有效的参考图片：" + str(exc)[:120], "invalid_image") from exc
        if len(normalized) > MAX_UPLOAD_BYTES:
            raise GenerationError(413, "标准化图片超过 20MB 限制", "asset_size_limit")
        if role == "output":
            if settings is None:
                raise ValueError("生成产物必须绑定输出规格合同")
            validate_output_contract(width, height, settings)
        sha = hashlib.sha256(normalized).hexdigest()
        # Imported references are content-addressed. Generated output identity is
        # job-scoped so a reference can never masquerade as a generated asset.
        asset_id = "genasset-" + (hashlib.sha256((job_id + ":" + sha).encode()).hexdigest() if role == "output" else sha)
        existing = self.repository.asset(asset_id)
        if existing:
            if role != "output":
                self.path(asset_id)
            return {key: value for key, value in existing.items() if key not in {"path", "role", "job_id"}}
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (asset_id + ".png")
        fd, temporary = tempfile.mkstemp(prefix=".image-", dir=str(self.root))
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(normalized)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        metadata = {"id": asset_id, "sha256": sha, "mime_type": "image/png", "width": width,
                    "height": height, "size_bytes": len(normalized), "url": "/api/generation-assets/" + asset_id,
                    "created_at": utc_now()}
        return self.repository.save_asset(metadata, path, role=role, job_id=job_id)

    def video_poster(self, asset_id):
        """A cached JPEG also previews old videos without eager video decoding."""
        asset, source = self.path(asset_id)
        if asset["mime_type"] != "video/mp4":
            raise GenerationError(404, "该资产不是视频", "poster_unavailable")
        destination = self.root / (asset_id + ".poster.jpg")
        if destination.is_file() and destination.resolve().parent == self.root:
            return destination
        with self._poster_gate:
            if destination.is_file() and destination.resolve().parent == self.root:
                return destination
            fd, temporary = tempfile.mkstemp(prefix=".poster-", suffix=".jpg", dir=self.root)
            os.close(fd)
            try:
                subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-threads", "1",
                    "-i", str(source), "-ss", "0.04", "-frames:v", "1", "-vf",
                    "scale=640:640:force_original_aspect_ratio=decrease,setsar=1", "-q:v", "3", temporary],
                    check=True, capture_output=True, timeout=8)
                with Image.open(temporary) as poster:
                    if poster.format != "JPEG" or max(poster.size) > 640:
                        raise ValueError("invalid poster")
                    poster.verify()
                os.replace(temporary, destination)
                return destination
            except (OSError, subprocess.SubprocessError, ValueError):
                raise GenerationError(503, "视频封面暂时无法读取，可以双击节点播放原视频", "poster_unavailable") from None
            finally:
                Path(temporary).unlink(missing_ok=True)

    def video(self, data, *, job_id, settings, provider_spec=None):
        """Accept only a verified local MP4 artifact, never a provider URL."""
        incoming = data.resolve() if isinstance(data, Path) else None
        if incoming is not None:
            allowed = (self.root.parent / "generation-downloads").resolve()
            if allowed not in incoming.parents or not incoming.is_file():
                raise GenerationError(422, "视频临时文件不属于本机下载目录", "invalid_video_output")
            size = incoming.stat().st_size
        else:
            size = len(data) if data else 0
        if not 0 < size <= 256 * 1024 * 1024:
            raise GenerationError(413, "生成视频为空或超过 256MB 限制", "asset_size_limit")
        self.root.mkdir(parents=True, exist_ok=True)
        if incoming is None:
            fd, temporary = tempfile.mkstemp(prefix=".video-", suffix=".mp4", dir=str(self.root))
            with os.fdopen(fd, "wb") as target:
                target.write(data)
        else:
            temporary = str(incoming)
        try:
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", temporary],
                                   check=True, capture_output=True, text=True, timeout=8)
            details = json.loads(probe.stdout)
            stream = next(item for item in details.get("streams", []) if item.get("codec_type") == "video")
            width, height = int(stream["width"]), int(stream["height"])
            duration = float(details.get("format", {}).get("duration") or stream.get("duration") or 0)
            if (not math.isfinite(duration) or not 0 < duration <= VIDEO_DURATION_EXTENSION_MAX_SECONDS + 0.25
                    or min(width, height) <= 0 or max(width, height) > MAX_SIDE):
                raise ValueError("视频时长或尺寸不符合生成合同")
            if "mp4" not in details.get("format", {}).get("format_name", "").split(","):
                raise ValueError("产物不是有效 MP4")
            validate_output_contract(width, height, settings, duration=duration, provider_spec=provider_spec)
            digest = hashlib.sha256()
            with open(temporary, "rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            sha = digest.hexdigest()
            asset_id = "genasset-" + hashlib.sha256((job_id + ":" + sha).encode()).hexdigest()
            path = self.root / (asset_id + ".mp4")
            os.replace(temporary, path)
            metadata = {"id": asset_id, "sha256": sha, "mime_type": "video/mp4", "width": width,
                        "height": height, "duration_seconds": duration, "size_bytes": size,
                        "url": "/api/generation-assets/" + asset_id, "created_at": utc_now()}
            return self.repository.save_asset(metadata, path, role="output", job_id=job_id)
        except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration) as exc:
            raise GenerationError(422, "生成视频尚未通过本地媒体校验", "invalid_video_output") from exc
        finally:
            Path(temporary).unlink(missing_ok=True)
