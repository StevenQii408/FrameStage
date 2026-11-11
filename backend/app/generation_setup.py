"""Private generation configuration. No command in this module calls an API."""
from __future__ import annotations

import argparse
import getpass
import io
import json
import os
from pathlib import Path
import tempfile

from dotenv import dotenv_values, set_key

from app.config import private_env_file
from app.generation.config import (IMAGE_MODEL, VIDEO_MODEL, load_generation_settings,
                                   normalize_generation_base_url)


def status():
    config = load_generation_settings()
    return {
        "enabled": config.enabled,
        "image": {"model": config.image_model, "api_key_configured": bool(config.image_api_key),
                  "api_key_source": config.image_key_source, "base_url": config.image_base_url,
                  "transport": "async-task", "max_inflight": config.image_inflight,
                  "submit_rpm": config.image_submit_rpm},
        "video": {"model": config.video_model, "api_key_configured": bool(config.video_api_key),
                  "api_key_source": config.video_key_source, "base_url": config.video_base_url,
                  "transport": "async-task", "max_inflight": config.video_inflight,
                  "submit_rpm": config.video_submit_rpm},
        "max_inflight": config.global_inflight, "max_pending": config.max_pending,
        "poll_interval_seconds": config.poll_interval_seconds,
        "job_timeout_seconds": config.job_timeout_seconds,
        "frontend_transport": "project-sse",
        "config_notes": list(config.config_notes),
        "live_check": "not-performed",
        "note": "仅检查本机配置，不验证模型权限、额度或云端鉴权，不提交生成任务。",
    }


def save_generation_config(path: Path, *, image_base_url=None, image_api_key=None,
                           video_base_url=None, video_api_key=None):
    """Atomically update only selected generation fields in a mode-0600 file."""
    updates = {"STORYBOARD_IMAGE_MODEL": IMAGE_MODEL, "STORYBOARD_VIDEO_MODEL": VIDEO_MODEL}
    for name, value in (("STORYBOARD_IMAGE_BASE_URL", image_base_url), ("STORYBOARD_VIDEO_BASE_URL", video_base_url)):
        if value is not None:
            updates[name] = normalize_generation_base_url(value)
    for name, value in (("STORYBOARD_IMAGE_API_KEY", image_api_key), ("STORYBOARD_VIDEO_API_KEY", video_api_key)):
        if value is not None:
            value = value.strip()
            if not value or any(char.isspace() for char in value) or any(char in value for char in "<>\x00"):
                raise ValueError(f"{name} 为空或包含空白／占位符，未保存")
            updates[name] = value
    path = Path(path)
    if path.is_symlink():
        raise ValueError("私有配置文件不能是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    if "STORYBOARD_GENERATION_ENABLED" not in dotenv_values(stream=io.StringIO(existing)):
        updates["STORYBOARD_GENERATION_ENABLED"] = "0"
    fd, temporary = tempfile.mkstemp(prefix=".generation-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(existing)
        os.chmod(temporary, 0o600)
        for name, value in updates.items():
            set_key(temporary, name, value, quote_mode="always")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure():
    config = load_generation_settings()
    print("配置千问 Image 3.0 Pro 与万相 3.0；Key 隐藏输入，留空复用现有百炼配置。")
    print("此工具只保存本地配置，不调用 API；新增配置默认关闭生成。")
    image_host = input(f"生图 HTTPS 基础地址（回车保留 {config.image_base_url}）：").strip() or config.image_base_url
    image_key = getpass.getpass("生图独立 API Key（回车保留／复用现有百炼 Key）：").strip() or None
    video_host = input(f"视频 HTTPS 基础地址（回车保留 {config.video_base_url}）：").strip() or config.video_base_url
    video_key = getpass.getpass("视频独立 API Key（回车保留／复用现有百炼 Key）：").strip() or None
    path = private_env_file()
    save_generation_config(path, image_base_url=image_host, image_api_key=image_key,
                           video_base_url=video_host, video_api_key=video_key)
    print(f"已保存至 {path}（权限 600）。")
    print("确认模型权限及额度后，手动设置 STORYBOARD_GENERATION_ENABLED=1 并重启后端。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 API 私有配置（不发起网络请求、不打印密钥）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="输出脱敏配置状态")
    mode.add_argument("--configure", action="store_true", help="隐藏输入 Key，保留其他配置")
    args = parser.parse_args(argv)
    try:
        if args.configure:
            configure()
        else:
            print(json.dumps(status(), ensure_ascii=False, indent=2))
    except (ValueError, OSError, EOFError):
        # Configuration exceptions must not accidentally echo a secret or file
        # contents. Details are intentionally limited to parameter-independent text.
        print("配置未完成：请检查 HTTPS 基础地址、密钥格式、配置范围及文件写入权限。")
        return 2
    except KeyboardInterrupt:
        print("\n已取消，未发起 API 请求。")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
