"""Private, opt-in cloud generation settings, separate from vision analysis."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values

from app.config import private_env_file


IMAGE_MODEL = "qwen-image-3.0-pro"
VIDEO_MODEL = "wan3.0-video"
DEFAULT_GENERATION_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"


def normalize_generation_base_url(value: str) -> str:
    """Convert an official DashScope chat base to its native async API base.

    Credential-bearing URLs, arbitrary endpoints and insecure HTTP must not be
    persisted accidentally. This helper does not make a network request.
    """
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return DEFAULT_GENERATION_BASE_URL
    if any(char.isspace() for char in raw):
        raise ValueError("生成 API 地址不能包含空白")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise ValueError("生成 API 地址无效，请填写百炼 HTTPS 基础地址") from exc
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or port not in (None, 443)):
        raise ValueError("生成 API 地址须为不含凭据、查询参数或非标准端口的 HTTPS 基础地址")
    host = parts.hostname.lower()
    legacy = {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"}
    if host not in legacy and not host.endswith(".maas.aliyuncs.com"):
        raise ValueError("生成 API 地址须使用阿里云百炼官方域名")
    path = parts.path.rstrip("/")
    if path in ("", "/compatible-mode/v1", "/compatible-mode/v1/chat/completions", "/api/v1"):
        path = "/api/v1"
    else:
        raise ValueError("请填写百炼基础地址，不要填写具体生成或查询接口路径")
    return urlunsplit(("https", host, path, "", ""))


@dataclass(frozen=True)
class GenerationSettingsConfig:
    enabled: bool = False
    image_model: str = IMAGE_MODEL
    video_model: str = VIDEO_MODEL
    image_base_url: str = DEFAULT_GENERATION_BASE_URL
    video_base_url: str = DEFAULT_GENERATION_BASE_URL
    image_api_key: str = field(default="", repr=False)
    video_api_key: str = field(default="", repr=False)
    image_key_source: str = "none"
    video_key_source: str = "none"
    global_inflight: int = 16
    image_inflight: int = 10
    video_inflight: int = 16
    max_pending: int = 100
    io_workers: int = 4
    download_workers: int = 2
    image_finalize_workers: int = 2
    video_finalize_workers: int = 2
    poll_interval_seconds: float = 15.0
    job_timeout_seconds: float = 1800.0
    request_timeout_seconds: float = 60.0
    image_submit_rpm: int = 5
    video_submit_rpm: int = 5
    trust_env: bool = False
    config_notes: tuple[str, ...] = ()


def load_generation_settings() -> GenerationSettingsConfig:
    """Read dotenv + environment without exporting credentials to subprocesses."""
    path = private_env_file()
    saved = {key: value for key, value in dotenv_values(path).items() if value is not None} if path.is_file() else {}
    values = {**saved, **os.environ}

    def boolean(name, default=False):
        value = str(values.get(name, "1" if default else "0")).strip().lower()
        if value not in {"0", "1", "true", "false"}:
            raise ValueError(f"{name} 须为 0 或 1")
        return value in {"1", "true"}

    def number(name, default, low, high, *, integer=False):
        try:
            value = int(values.get(name, default)) if integer else float(values.get(name, default))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} 数值无效") from exc
        if isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name} 须在 {low}–{high} 范围内")
        return value

    def credential(primary):
        # Environment aliases must override saved aliases consistently with
        # analysis configuration; an old file must not shadow DASHSCOPE_API_KEY.
        # An explicit model-specific override takes priority over a shared Key.
        specific = str(values.get(primary) or "").strip()
        candidates = ([ ("environment" if os.environ.get(primary) else "private-env-file", {primary: specific}, (primary,)) ]
                      if specific else [])
        candidates.extend((source, mapping, ("STORYBOARD_BAILIAN_API_KEY", "DASHSCOPE_API_KEY"))
                          for source, mapping in (("environment", os.environ), ("private-env-file", saved)))
        for source, mapping, names in candidates:
            for name in names:
                value = str(mapping.get(name) or "").strip()
                if value:
                    if any(char.isspace() for char in value) or any(char in value for char in "<>\x00"):
                        raise ValueError(f"{name} 包含空白或占位符，请重新配置")
                    return value, source
        return "", "none"

    enabled = boolean("STORYBOARD_GENERATION_ENABLED")
    notes = []

    def base_url(*specific_names):
        for name in specific_names:
            if values.get(name):
                return normalize_generation_base_url(values[name])
        try:
            return normalize_generation_base_url(values.get("STORYBOARD_BAILIAN_BASE_URL") or "")
        except ValueError:
            if enabled:
                raise
            if not notes:
                notes.append("生成未启用：继承的分析地址不适用于生成接口，暂使用默认官方地址；开启前请配置生成端点。")
            return DEFAULT_GENERATION_BASE_URL

    image_key, image_source = credential("STORYBOARD_IMAGE_API_KEY")
    video_key, video_source = credential("STORYBOARD_VIDEO_API_KEY")
    image_base = base_url("STORYBOARD_IMAGE_BASE_URL")
    video_base = base_url("STORYBOARD_VIDEO_BASE_URL", "STORYBOARD_IMAGE_BASE_URL")
    if not video_key and video_base == image_base:
        video_key, video_source = image_key, image_source
    image_model = str(values.get("STORYBOARD_IMAGE_MODEL") or IMAGE_MODEL).strip()
    video_model = str(values.get("STORYBOARD_VIDEO_MODEL") or VIDEO_MODEL).strip()
    if image_model != IMAGE_MODEL or video_model != VIDEO_MODEL:
        raise ValueError("当前生成适配器仅支持 qwen-image-3.0-pro 与 wan3.0-video，不会自动替换模型")
    return GenerationSettingsConfig(
        enabled=enabled, image_model=image_model, video_model=video_model,
        image_base_url=image_base, video_base_url=video_base, image_api_key=image_key, video_api_key=video_key,
        image_key_source=image_source, video_key_source=video_source,
        global_inflight=number("STORYBOARD_GENERATION_MAX_INFLIGHT", 16, 1, 32, integer=True),
        image_inflight=number("STORYBOARD_IMAGE_MAX_INFLIGHT", 10, 1, 32, integer=True),
        video_inflight=number("STORYBOARD_VIDEO_MAX_INFLIGHT", 16, 1, 32, integer=True),
        max_pending=number("STORYBOARD_GENERATION_MAX_PENDING", 100, 1, 1000, integer=True),
        io_workers=number("STORYBOARD_GENERATION_IO_WORKERS", 4, 1, 16, integer=True),
        download_workers=number("STORYBOARD_GENERATION_DOWNLOAD_WORKERS", 2, 1, 8, integer=True),
        image_finalize_workers=number("STORYBOARD_GENERATION_IMAGE_FINALIZE_WORKERS", 2, 1, 4, integer=True),
        video_finalize_workers=number("STORYBOARD_GENERATION_VIDEO_FINALIZE_WORKERS", 2, 1, 4, integer=True),
        poll_interval_seconds=number("STORYBOARD_GENERATION_POLL_INTERVAL_SECONDS", 15.0, 1.0, 300.0),
        job_timeout_seconds=number("STORYBOARD_GENERATION_TIMEOUT_SECONDS", 1800.0, 60.0, 86400.0),
        request_timeout_seconds=number("STORYBOARD_GENERATION_REQUEST_TIMEOUT_SECONDS", 60.0, 1.0, 300.0),
        image_submit_rpm=number("STORYBOARD_IMAGE_SUBMIT_RPM", 5, 1, 600, integer=True),
        video_submit_rpm=number("STORYBOARD_VIDEO_SUBMIT_RPM", 5, 1, 600, integer=True),
        trust_env=boolean("STORYBOARD_GENERATION_TRUST_ENV"),
        config_notes=tuple(notes),
    )
