"""Stable cloud-only launch entry, even if a shell still exports mlx/mock."""
import argparse
import os
import socket
from contextlib import contextmanager

import uvicorn

from app.config import load_settings
from app.providers.bailian import BailianVisionProvider


@contextmanager
def cloud_environment():
    forced = {"STORYBOARD_PROVIDER": "bailian", "STORYBOARD_CLOUD_MODEL": "qwen3.8-flash",
              "STORYBOARD_PREFER_PRIVATE_CLOUD": "1"}
    previous = {name: os.environ.get(name) for name in forced}
    os.environ.update(forced)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def main(argv=None):
    parser = argparse.ArgumentParser(description="启动 FrameForge 云端 Flash 后端；不会加载本地模型")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    with cloud_environment():
        return run_cloud(args)


def run_cloud(args):
    settings = load_settings()
    provider = BailianVisionProvider(settings)
    try:
        health = provider.health()
        if not health.ready:
            print(f"无法启动云端分析：{health.detail}")
            print("请先运行：python -m app.cloud_setup（一次性保存私有 Key/Host）")
            return 2
    finally:
        provider.unload()
    with socket.socket() as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((args.host, args.port)) == 0:
            print(f"端口 {args.port} 已被占用；请先在原后端终端 Ctrl+C，避免继续连接旧服务。")
            return 2
    print(f"Provider=bailian | Model=qwen3.8-flash | Style={settings.style_strategy} | Key 来源={settings.cloud_key_source}")
    print("本地 MLX 推理已停用。此检查只确认配置，鉴权请运行 python -m app.cloud_setup --check。")
    uvicorn.run("app.main:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
