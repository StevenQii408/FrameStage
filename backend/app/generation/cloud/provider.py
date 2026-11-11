"""Qwen Image 3.0 Pro and Wan3.0 through official asynchronous DashScope APIs.

Contracts checked against the official references on 2026-11-11:
https://help.aliyun.com/en/model-studio/qwen-image-generation-and-editing-api-reference
https://help.aliyun.com/en/model-studio/wan3-video-generation-api-reference
Neither endpoint exposes upstream SSE; project SSE is our downstream transport.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
import re

from PIL import Image

from app.generation.cloud.transport import ApiTransport, ArtifactDownloader, api_base, error_code, failure
from app.generation.providers import CLOUD_CANCELLED_MESSAGE, GeneratedAsset, ProviderResult
from app.generation.failures import public_failure


IMAGE_MODEL = "qwen-image-3.0-pro"
VIDEO_MODEL = "wan3.0-video"
RATIOS = ["21:9", "16:9", "9:16", "1:1", "4:3", "3:4"]
VIDEO_RESOLUTIONS = ["480p", "720p", "1080p"]
IMAGE_RESOLUTIONS = ["512p", "720p", "1080p", "1536p", "2048p"]
TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,160}$")


def image_size(settings):
    """Use the workbench's short-edge resolution contract, without resizing output."""
    short = int(settings["resolution"].removesuffix("p"))
    left, right = (int(part) for part in settings["aspect_ratio"].split(":"))
    if left >= right:
        width, height = round(short * left / right), short
    else:
        width, height = short, round(short * right / left)
    if not 512 * 512 <= width * height <= 2048 * 2048:
        raise ValueError("千问 Image 3.0 Pro 输出总像素须为 512²–2048²，请选择所列图片尺寸")
    return width, height


def image_resolution_options():
    options = []
    for ratio in RATIOS:
        for resolution in IMAGE_RESOLUTIONS:
            try:
                width, height = image_size({"aspect_ratio": ratio, "resolution": resolution})
            except ValueError:
                continue
            options.append({"value": resolution, "aspect_ratio": ratio, "width": width,
                "height": height, "label": f"{width} × {height}"})
    return options


class CloudGenerationProvider:
    name = "bailian-generation"

    def __init__(self, config, asset_reader, data_dir, *, transport=None, download_transport=None, resolver=None):
        self.config = config
        self.asset_reader = asset_reader
        self.bases = {"image": api_base(config.image_base_url), "video": api_base(config.video_base_url)}
        self.api = ApiTransport(config.request_timeout_seconds, trust_env=config.trust_env, transport=transport, resolver=resolver)
        self.downloader = ArtifactDownloader(Path(data_dir) / "generation-downloads", config.request_timeout_seconds,
            transport=download_transport, resolver=resolver)

    @property
    def configured(self):
        return self._available("image") or self._available("video")

    def _available(self, kind):
        expected = IMAGE_MODEL if kind == "image" else VIDEO_MODEL
        return bool(self.config.enabled and getattr(self.config, kind + "_api_key")
            and getattr(self.config, kind + "_model") == expected)

    def capabilities(self):
        kinds = {}
        for kind, model in (("image", IMAGE_MODEL), ("video", VIDEO_MODEL)):
            available = self._available(kind)
            reason = "" if available else ("生成 API 尚未显式启用。" if not self.config.enabled
                else "尚未配置该模型的百炼 API Key，或模型配置与首期选型不一致。")
            item = {"available": available, "aspect_ratios": RATIOS,
                "resolutions": IMAGE_RESOLUTIONS if kind == "image" else VIDEO_RESOLUTIONS,
                "supports_reference": True, "max_reference_images": 3 if kind == "image" else 8,
                "progress_mode": "stages", "cloud_transport": "async-poll", "upstream_sse": False,
                "reason": reason}
            if kind == "video":
                item.update(min_duration_seconds=2, max_duration_seconds=30, duration_step_seconds=1)
            else:
                item.update(resolution_options=image_resolution_options(), resolution_mode="pixel-size")
            kinds[kind] = {**item, "default_model_id": model, "models": [{"id": model, "name": "千问 Image 3.0 Pro" if kind == "image" else "万相 Wan 3.0",
                "kind": kind, **item}]}
        return {"configured": self.configured, "provider": self.name, **kinds,
            "reason": "" if self.configured else "生成 API 尚未启用或百炼密钥尚未配置。",
            "progress_mode": "stages", "billing_mode": "provider_actual",
            "upstream_sse": False, "frontend_transport": "project-sse"}

    @staticmethod
    def _references(request):
        bindings = list(request.get("reference_images", []))
        if not bindings and request.get("reference_asset_id"):
            bindings = [{"asset_id": request["reference_asset_id"], "mention": "图片1", "role": "first_frame" if request["kind"] == "video" else "reference"}]
        return bindings

    def validate_request(self, request):
        kind = request["kind"]
        settings = request["settings"]
        model = settings.get("model_id") or getattr(self.config, kind + "_model")
        issues = []
        if model != (IMAGE_MODEL if kind == "image" else VIDEO_MODEL):
            issues.append("首期图片仅支持qwen-image-3.0-pro，视频仅支持wan3.0-video")
        resolutions = IMAGE_RESOLUTIONS if kind == "image" else VIDEO_RESOLUTIONS
        if settings["aspect_ratio"] not in RATIOS or settings["resolution"] not in resolutions:
            issues.append("所选画幅或输出尺寸未被当前生成模型支持")
        if kind == "video" and (type(settings.get("duration_seconds")) is not int or not 2 <= settings["duration_seconds"] <= 30):
            issues.append("Wan 3.0 视频时长须为2–30秒整数")
        if kind == "image":
            try:
                image_size(settings)
            except (ValueError, KeyError):
                issues.append("千问图片输出超过总像素规格，请选择该画幅下列出的图片尺寸")
        refs = self._references(request)
        if len(refs) > (3 if kind == "image" else 8):
            issues.append("参考图片数量超出当前模型接入范围")
        if kind == "video" and any(ref.get("role") == "first_frame" for ref in refs) and len(refs) > 1:
            issues.append("Wan3.0首帧模式不能混用其他参考图；请选择单首帧，或将全部图片作为普通参考")
        for ref in refs:
            try:
                metadata, path = self.asset_reader(ref["asset_id"])
                path = Path(path)
                width, height = metadata["width"], metadata["height"]
                minimum, maximum_bytes = (1, 10 * 1024 * 1024) if kind == "image" else (240, 20 * 1024 * 1024)
                if not metadata["mime_type"].startswith("image/") or path.stat().st_size > maximum_bytes:
                    issues.append("参考图片超过模型允许的文件大小")
                if min(width, height) < minimum or max(width, height) > 8000 or max(width, height) / min(width, height) > (10 if kind == "image" else 8):
                    issues.append("参考图片尺寸或长宽比超出模型允许范围")
                if kind == "video":
                    with Image.open(path) as image:
                        if "A" in image.getbands() and image.getchannel("A").getextrema()[0] < 255:
                            issues.append("Wan3.0参考图片不支持透明区域，请先导入不透明图片")
            except (KeyError, OSError, ValueError):
                issues.append("参考图片不可读取或未通过模型输入检查")
        return list(dict.fromkeys(issues))

    def _data_url(self, asset_id, *, kind="image"):
        metadata, path = self.asset_reader(asset_id)
        if kind == "video":
            # Local asset normalization uses RGBA even for opaque imports.
            # Wan forbids transparent channels, so strip the redundant opaque
            # channel. Actual transparent pixels were rejected in preflight.
            with Image.open(path) as image:
                if "A" in image.getbands() and image.getchannel("A").getextrema()[0] < 255:
                    raise ValueError("Wan3.0参考图必须不透明")
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, "PNG")
                data = buffer.getvalue()
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("Wan3.0参考图片超过20MB")
            return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        return "data:" + metadata["mime_type"] + ";base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")

    def _payload(self, request):
        kind = request["kind"]
        settings = request["settings"]
        refs = self._references(request)
        # Resolve aliases from the immutable binding array, not from potentially
        # non-contiguous mention numbers. Model media numbering is positional.
        mapping = {ref["mention"]: ("图片" if kind == "image" else "图") + str(index + 1) for index, ref in enumerate(refs)}
        prompt = re.sub(r"@图片[1-9][0-9]*", lambda match: mapping.get(match.group()[1:], match.group()), request["prompt"])
        model = settings.get("model_id") or getattr(self.config, kind + "_model")
        if kind == "image":
            width, height = image_size(settings)
            content = [{"image": self._data_url(ref["asset_id"])} for ref in refs] + [{"text": prompt}]
            return {"model": model, "input": {"messages": [{"role": "user", "content": content}]},
                "parameters": {"n": 1, "size": f"{width}*{height}", "prompt_extend": False, "enable_thinking": False, "watermark": False}}
        input_data = {"prompt": prompt}
        if refs:
            input_data["media"] = [{"type": "first_frame" if ref.get("role") == "first_frame" else "reference_image",
                "url": self._data_url(ref["asset_id"], kind="video")} for ref in refs]
        return {"model": model, "input": input_data,
            "parameters": {"resolution": settings["resolution"].upper(), "ratio": settings["aspect_ratio"],
                "duration": settings["duration_seconds"], "prompt_extend": False, "audio": False, "watermark": False}}

    @staticmethod
    def _task_id(value):
        if not isinstance(value, str) or not TASK_ID.fullmatch(value):
            raise failure("provider_response_invalid")
        return value

    @classmethod
    def _route(cls, value):
        if not isinstance(value, str):
            raise failure("provider_response_invalid")
        parts = value.split(":", 2)
        if len(parts) != 3 or parts[0] != "bailian" or parts[1] not in ("image", "video"):
            raise failure("provider_response_invalid")
        return parts[1], cls._task_id(parts[2])

    def submit(self, request, *, idempotency_key):
        kind = request["kind"]
        if not self._available(kind):
            raise failure("provider_unconfigured", 503)
        issues = self.validate_request(request)
        if issues:
            from app.generation.models import GenerationError
            raise GenerationError(422, "；".join(issues), "invalid_generation_inputs")
        path = "/services/aigc/" + ("image-generation/generation" if kind == "image" else "video-generation/video-synthesis")
        # No automatic POST retries and no invented upstream idempotency header.
        # The persistent scheduler owns submission intent and ambiguous failures.
        response = self.api.call("POST", self.bases[kind] + path, getattr(self.config, kind + "_api_key"),
            payload=self._payload(request), asynchronous=True)
        remote_id = self._task_id((response.get("output") or {}).get("task_id"))
        return f"bailian:{kind}:{remote_id}"

    def poll(self, task_id):
        kind, remote_id = self._route(task_id)
        response = self.api.call("GET", self.bases[kind] + "/tasks/" + remote_id, getattr(self.config, kind + "_api_key"))
        output = response.get("output")
        if not isinstance(output, dict) or (output.get("task_id") is not None and output["task_id"] != remote_id):
            raise failure("provider_response_invalid")
        state = output.get("task_status")
        if state in ("PENDING", "RUNNING"):
            return ProviderResult("queued" if state == "PENDING" else "running")
        if state == "UNKNOWN":
            # Expiration or a temporarily missing remote record does not prove
            # that generation stopped. The scheduler must keep its remote slot
            # until reconciliation, never resubmit the paid task automatically.
            return ProviderResult("unknown", error_code="remote_status_unknown", retryable=False,
                message="云端任务状态暂时未知，需继续核对；未自动重新提交。")
        if state == "CANCELED":
            return ProviderResult("cancelled", error_code="provider_cancelled", message=CLOUD_CANCELLED_MESSAGE, retryable=False)
        if state == "FAILED":
            code = error_code(output)
            if code == "provider_failed":
                code = error_code(response)
            details = public_failure(code, fallback="provider_failed")
            return ProviderResult("failed", error_code=details["code"], message=details["message"], retryable=details["retryable"])
        if state != "SUCCEEDED":
            raise failure("provider_response_invalid")
        if kind == "video":
            urls = [output.get("video_url")]
        else:
            urls = [item.get("image") for choice in output.get("choices", []) if isinstance(choice, dict)
                for item in (choice.get("message") or {}).get("content", []) if isinstance(item, dict) and item.get("image")]
        if len(urls) != 1 or not isinstance(urls[0], str) or not urls[0]:
            raise failure("provider_response_invalid")
        asset = {"url": urls[0], "kind": kind, "mime_type": "image/png" if kind == "image" else "video/mp4"}
        usage = response.get("usage")
        if kind == "video" and isinstance(usage, dict):
            asset["provider_spec"] = {key: usage[key] for key in ("SR", "ratio", "output_video_duration") if key in usage}
        return ProviderResult("succeeded", remote_assets=[asset])

    def collect(self, task_id, result):
        kind, _ = self._route(task_id)
        if len(result.remote_assets) != 1:
            raise failure("provider_response_invalid")
        item = result.remote_assets[0]
        expected = "image/png" if kind == "image" else "video/mp4"
        if item.get("kind") != kind or item.get("mime_type") != expected:
            raise failure("provider_response_invalid")
        path = self.downloader.fetch(item["url"], kind)
        return [GeneratedAsset(mime_type=expected, path=path, provider_spec=item.get("provider_spec"))]

    def cancel(self, task_id):
        kind, remote_id = self._route(task_id)
        self.api.call("POST", self.bases[kind] + "/tasks/" + remote_id + "/cancel", getattr(self.config, kind + "_api_key"), allow_request_id_string=True)

    def close(self):
        self.api.close()
