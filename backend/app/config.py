from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import re
from typing import Tuple

from dotenv import dotenv_values


DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:5174", "http://localhost:5174",
    "http://127.0.0.1:5173", "http://localhost:5173",
    "http://[::1]:5174", "http://[::1]:5173",
)


def private_env_file() -> Path:
    return Path(os.environ.get("STORYBOARD_ENV_FILE") or DEFAULT_ENV_FILE).expanduser().resolve()


def load_cors_origins() -> Tuple[str, ...]:
    """Use the same private-file/environment precedence as backend settings.

    The workspace has its own strict development port (5174); 5173 remains
    compatible with earlier installs. Do not allow arbitrary localhost ports.
    """
    path = private_env_file()
    saved = dotenv_values(path) if path.is_file() else {}
    raw = os.environ.get("STORYBOARD_CORS_ORIGINS", saved.get("STORYBOARD_CORS_ORIGINS"))
    if raw is None:
        return DEFAULT_CORS_ORIGINS
    return tuple(dict.fromkeys(origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()))


@dataclass(frozen=True)
class LocalSecuritySettings:
    enabled: bool = True
    allowed_hosts: Tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
    api_token: str = field(default="", repr=False)


def load_local_security() -> LocalSecuritySettings:
    """Explicit local API protection; never infer a bypass from provider or Host."""
    path = private_env_file()
    saved = {key: value for key, value in dotenv_values(path).items() if value is not None} if path.is_file() else {}
    values = {**saved, **os.environ}
    enabled = str(values.get("STORYBOARD_LOCAL_AUTH", "1")).strip().lower() not in ("0", "false")
    hosts = tuple(dict.fromkeys(host.strip().lower().strip("[]") for host in
        str(values.get("STORYBOARD_ALLOWED_HOSTS", "127.0.0.1,localhost,::1")).split(",") if host.strip()))
    def valid_host(host):
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host))
    if not hosts or not all(valid_host(host) for host in hosts):
        raise ValueError("STORYBOARD_ALLOWED_HOSTS 必须是明确主机名，不能使用通配符、端口或路径")
    token = str(values.get("STORYBOARD_LOCAL_API_TOKEN", "")).strip()
    if token and (len(token) < 32 or any(char.isspace() for char in token)):
        raise ValueError("STORYBOARD_LOCAL_API_TOKEN 必须至少32个字符且不含空白；也可留空使用本次启动随机令牌")
    return LocalSecuritySettings(enabled=enabled, allowed_hosts=hosts, api_token=token)


def _int_env(name: str, default: int, values=None) -> int:
    try:
        return int((values if values is not None else os.environ).get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _float_env(name: str, default: float, values=None) -> float:
    try:
        return float((values if values is not None else os.environ).get(name, str(default)))
    except (ValueError, TypeError):
        return default


def normalize_cloud_base_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        normalized = normalized[:-len("/chat/completions")].rstrip("/")
    return normalized


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    provider: str
    max_video_size_mb: int
    max_video_duration_seconds: int
    min_available_memory_gb: float
    target_available_memory_gb: float
    vlm_max_context_tokens: int
    vlm_max_new_tokens: int
    script_max_new_tokens: int
    vlm_timeout_seconds: int = 180
    max_shots: int = 120
    frames_per_shot: int = 4
    max_frames_per_shot: int = 4
    frame_max_long_edge: int = 768
    jpeg_quality: int = 80
    provider_concurrency: int = 1
    segmentation_profile: str = "source-boundaries-v4"
    narrative_min_seconds: float = 1.2
    narrative_max_seconds: float = 5.0
    max_raw_shots_per_narrative: int = 4
    cloud_model: str = "qwen3.8-flash"
    cloud_connect_timeout_seconds: float = 8.0
    cloud_read_timeout_seconds: float = 75.0
    cloud_trust_env: bool = False
    style_strategy: str = "qwen-direct-v3"
    style_text_embed_model: str = "text-embedding-v4"
    style_vision_embed_model: str = "qwen3-vl-embedding"
    style_embed_dimension: int = 1024
    style_embed_concurrency: int = 4
    style_allow_api_rebuild: bool = False
    cloud_base_url: str = ""
    cloud_api_key: str = field(default="", repr=False)
    perception_v2_enabled: bool = True
    env_file: str = ""
    cloud_key_source: str = "none"
    cloud_host_source: str = "none"
    config_notes: Tuple[str, ...] = ()
    script_strategy: str = "director-v3"
    job_max_model_calls: int = 424
    job_timeout_seconds: int = 3600
    max_pending_jobs: int = 8


def load_settings(prefer_private_cloud=False) -> Settings:
    # Merge without mutating os.environ: config changes and tests cannot leak
    # keys into child processes or leave stale dotenv values after a reload.
    path = private_env_file()
    saved = {key: value for key, value in dotenv_values(path).items() if value is not None} if path.is_file() else {}
    values = {**saved, **os.environ}
    provider = str(values.get("STORYBOARD_PROVIDER") or "bailian").strip().lower()
    notes = []
    if provider == "mlx":
        # The user retired local inference. An old exported mlx value must not
        # accidentally allocate a 7B model again.
        provider = "bailian"
        notes.append("忽略旧的 STORYBOARD_PROVIDER=mlx；本地推理已停用，使用云端 Flash")
    if provider not in ("bailian", "mock"):
        raise ValueError("STORYBOARD_PROVIDER 仅支持 bailian（默认云端）或 mock（显式测试）")
    env_key = (os.environ.get("STORYBOARD_BAILIAN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    file_key = (saved.get("STORYBOARD_BAILIAN_API_KEY") or saved.get("DASHSCOPE_API_KEY") or "").strip()
    env_host = (os.environ.get("STORYBOARD_BAILIAN_BASE_URL") or "").strip()
    file_host = (saved.get("STORYBOARD_BAILIAN_BASE_URL") or "").strip()
    raw_host = env_host or file_host
    prefer_private_cloud = prefer_private_cloud or os.environ.get("STORYBOARD_PREFER_PRIVATE_CLOUD") == "1"
    key = env_key or file_key
    key_source = "environment" if env_key else "private-env-file" if file_key else "none"
    host_source = "environment" if env_host else "private-env-file" if file_host else "none"
    if prefer_private_cloud:
        if file_key:
            key, key_source = file_key, "private-env-file"
        if file_host:
            raw_host, host_source = file_host, "private-env-file"
        if (file_key and env_key) or (file_host and env_host):
            notes.append("云端专用入口优先使用私有文件的 Key/Host，避免旧终端值覆盖已保存配置")
    host = normalize_cloud_base_url(raw_host)
    if raw_host and host != raw_host:
        notes.append("API Host 已去掉末尾斜杠或 /chat/completions，使用标准基础地址")
    data_path = Path(values.get("STORYBOARD_DATA_DIR") or str(DEFAULT_ENV_FILE.parent / "data"))
    if not data_path.is_absolute():
        data_path = DEFAULT_ENV_FILE.parent / data_path
    style_strategy = str(values.get("STORYBOARD_STYLE_STRATEGY") or "qwen-direct-v3").strip()
    if style_strategy not in ("qwen-direct-v3", "vector-v2", "legacy-v1"):
        raise ValueError("STORYBOARD_STYLE_STRATEGY 仅支持 qwen-direct-v3、vector-v2 或 legacy-v1")
    if prefer_private_cloud and style_strategy != "qwen-direct-v3":
        notes.append("云端专用启动使用qwen-direct-v3；旧画风配置保留用于显式历史回归，不再依赖Embedding")
        style_strategy = "qwen-direct-v3"
    script_strategy = str(values.get("STORYBOARD_SCRIPT_STRATEGY") or "director-v3").strip()
    if script_strategy not in ("director-v3", "legacy-v2"):
        raise ValueError("STORYBOARD_SCRIPT_STRATEGY 仅支持 director-v3 或 legacy-v2")
    return Settings(
        job_max_model_calls=max(1, _int_env("STORYBOARD_JOB_MAX_MODEL_CALLS", 424, values)),
        job_timeout_seconds=max(1, _int_env("STORYBOARD_JOB_TIMEOUT_SECONDS", 3600, values)),
        max_pending_jobs=max(1, _int_env("STORYBOARD_MAX_PENDING_JOBS", 8, values)),
        script_strategy=script_strategy,
        data_dir=data_path.resolve(), provider=provider,
        max_video_size_mb=_int_env("STORYBOARD_MAX_VIDEO_SIZE_MB", 2048, values),
        max_video_duration_seconds=_int_env("STORYBOARD_MAX_VIDEO_DURATION_SECONDS", 600, values),
        min_available_memory_gb=_float_env("STORYBOARD_MIN_AVAILABLE_MEMORY_GB", 1, values),
        target_available_memory_gb=_float_env("STORYBOARD_TARGET_AVAILABLE_MEMORY_GB", 2, values),
        vlm_max_context_tokens=_int_env("STORYBOARD_VLM_MAX_CONTEXT_TOKENS", 4096, values),
        vlm_max_new_tokens=_int_env("STORYBOARD_VLM_MAX_NEW_TOKENS", 512, values),
        script_max_new_tokens=_int_env("STORYBOARD_SCRIPT_MAX_NEW_TOKENS", 2048, values),
        vlm_timeout_seconds=_int_env("STORYBOARD_VLM_TIMEOUT_SECONDS", 180, values),
        segmentation_profile=values.get("STORYBOARD_SEGMENTATION_PROFILE", "source-boundaries-v4"),
        narrative_min_seconds=_float_env("STORYBOARD_NARRATIVE_MIN_SECONDS", 1.2, values),
        narrative_max_seconds=min(5.0, _float_env("STORYBOARD_NARRATIVE_MAX_SECONDS", 5.0, values)),
        max_raw_shots_per_narrative=_int_env("STORYBOARD_MAX_RAW_SHOTS_PER_NARRATIVE", 4, values),
        cloud_model=str(values.get("STORYBOARD_CLOUD_MODEL") or "qwen3.8-flash").strip(),
        cloud_connect_timeout_seconds=max(0.1, _float_env("STORYBOARD_CLOUD_CONNECT_TIMEOUT_SECONDS", 8.0, values)),
        cloud_read_timeout_seconds=max(0.1, _float_env("STORYBOARD_CLOUD_READ_TIMEOUT_SECONDS", 75.0, values)),
        cloud_trust_env=str(values.get("STORYBOARD_CLOUD_TRUST_ENV", "0")).lower() in ("1", "true"),
        style_strategy=style_strategy,
        style_text_embed_model=str(values.get("STORYBOARD_STYLE_TEXT_EMBED_MODEL") or "text-embedding-v4").strip(),
        style_vision_embed_model=str(values.get("STORYBOARD_STYLE_VISION_EMBED_MODEL") or "qwen3-vl-embedding").strip(),
        style_embed_dimension=_int_env("STORYBOARD_STYLE_EMBED_DIMENSION", 1024, values),
        style_embed_concurrency=_int_env("STORYBOARD_STYLE_EMBED_CONCURRENCY", 4, values),
        style_allow_api_rebuild=str(values.get("STORYBOARD_STYLE_ALLOW_API_REBUILD", "0")).lower() in ("1", "true"),
        cloud_base_url=host, cloud_api_key=key,
        perception_v2_enabled=str(values.get("STORYBOARD_PERCEPTION_V2", "true")).lower() not in ("0", "false"),
        env_file=str(path), cloud_key_source=key_source,
        cloud_host_source=host_source,
        config_notes=tuple(notes),
    )
