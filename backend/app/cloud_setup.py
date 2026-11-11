"""One-time private cloud configuration; credentials never go into CLI arguments."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from dataclasses import replace
from pathlib import Path

import httpx
from dotenv import set_key

from app.config import load_settings, normalize_cloud_base_url, private_env_file
from app.providers.bailian import BailianVisionProvider


def save_cloud_config(path: Path, host: str, key: str):
    host = normalize_cloud_base_url(host)
    key = key.strip()
    candidate = replace(load_settings(), provider="bailian", cloud_model="qwen3.8-flash",
                        cloud_base_url=host, cloud_api_key=key)
    provider = BailianVisionProvider(candidate)
    try:
        health = provider.health()
        if not health.ready:
            raise ValueError(health.detail)
        if any(char.isspace() for char in key) or any(word in key for word in ("你的", "私有Key", "<", ">")):
            raise ValueError("API Key 仍为占位符或包含空白，请输入控制台实际 Key")
    finally:
        provider.unload()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with restricted permissions before writing any secret. Keep all
    # unrelated existing settings when updating the four cloud fields.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    path.chmod(0o600)
    for name, value in {
        "STORYBOARD_PROVIDER": "bailian", "STORYBOARD_CLOUD_MODEL": "qwen3.8-flash",
        "STORYBOARD_BAILIAN_BASE_URL": host, "STORYBOARD_BAILIAN_API_KEY": key,
    }.items():
        set_key(str(path), name, value, quote_mode="always")
    path.chmod(0o600)


def diagnostics(settings=None, live=False, transport=None):
    settings = settings or replace(load_settings(prefer_private_cloud=True),
                                    provider="bailian", cloud_model="qwen3.8-flash")
    provider = BailianVisionProvider(settings, transport)
    try:
        health = provider.health()
        result = {
            "provider": settings.provider, "model": settings.cloud_model,
            "api_key_configured": bool(settings.cloud_api_key),
            "api_host_configured": bool(settings.cloud_base_url),
            "api_key_source": settings.cloud_key_source,
            "api_host_source": settings.cloud_host_source,
            "configuration_ready": health.ready, "detail": health.detail,
            "config_notes": list(settings.config_notes),
            "live_check": "not-requested", "authenticated": None,
        }
        if live and health.ready:
            try:
                provider._call('只返回 JSON 对象：{"ok": true}。这是接口连通性检查。',
                               [], "cloud.connectivity_check", max_tokens=128,
                               timeout_seconds=30, max_attempts=1)
                result["live_check"] = "success"
                result["authenticated"] = True
            except Exception as exc:
                result["live_check"] = "failed"
                result["authenticated"] = False
                result["detail"] = str(exc).replace(settings.cloud_api_key, "[REDACTED]")
            result["usage"] = provider.drain_usage()
        elif live:
            result["live_check"] = "skipped-missing-configuration"
        return result
    finally:
        provider.unload()


def configure_interactively():
    current = load_settings(prefer_private_cloud=True)
    print("一次性配置云端 Flash：Key 隐藏输入，保存到私有 .env（权限 600、Git 忽略）。")
    host = input("粘贴百炼北京 API Host（已保存则回车保留）：").strip() or current.cloud_base_url
    key = getpass.getpass("粘贴 API Key（隐藏输入；已保存则回车保留）：").strip() or current.cloud_api_key
    path = private_env_file()
    save_cloud_config(path, host, key)
    print(f"已保存到 {path}。后续无需重复 export；现有后端需要重启。")
    if current.cloud_key_source == "environment" or current.cloud_host_source == "environment":
        print("提示：app.start_cloud 和 --check 优先使用刚保存的私有配置，不受旧终端 Key/Host 干扰。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="云端私有配置与安全诊断（不会打印 Key）")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--diagnose", action="store_true", help="只检查配置，不请求云端")
    action.add_argument("--check", action="store_true", help="发起一次最多 128 输出 Token 的 Flash 连通性检查，可能计费")
    args = parser.parse_args(argv)
    if args.diagnose or args.check:
        result = diagnostics(live=args.check)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["configuration_ready"] and (not args.check or result["authenticated"]) else 2
    try:
        configure_interactively()
    except (ValueError, OSError, EOFError) as exc:
        print(f"配置未完成：{exc}")
        return 2
    except KeyboardInterrupt:
        print("\n已取消，未开始配置保存。")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
