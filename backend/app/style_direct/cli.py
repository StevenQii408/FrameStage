"""Explicit local probe. Cloud calls require --live; default is Mock only."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from app.config import load_settings
from app.providers.bailian import BailianVisionProvider
from app.providers.mock import MockVisionProvider
from .pipeline import StylePipeline
from .service import prepare_image
from .diagnostics import attach_usage


def analyze_file(path, *, live=False, directory=None):
    settings = replace(load_settings(prefer_private_cloud=True), cloud_model="qwen3.8-flash")
    provider = BailianVisionProvider(settings) if live else MockVisionProvider(settings)
    directory = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="style-direct-probe-"))
    try:
        if not provider.health().ready:
            raise ValueError(provider.health().detail)
        asset = prepare_image(Path(path).read_bytes(), directory / "input", "I1")
        result = StylePipeline(provider, [asset], directory / "crops").run()
        usage = provider.drain_usage() if hasattr(provider, "drain_usage") else []
        result["usage"] = usage
        attach_usage(result, usage)
        complete = bool(usage) and all(u.get("estimated_list_price_cny") is not None for u in usage)
        result["estimated_list_price_cny"] = sum(u.get("estimated_list_price_cny") or 0 for u in usage) if usage else None
        result["cost_estimate_complete"] = complete
        result["probe_mode"] = "live" if live else "mock"
        return result
    finally:
        provider.unload()


def main(argv=None):
    parser = argparse.ArgumentParser(description="单图高密度画风验证；默认Mock，--live最多4次付费Flash请求")
    parser.add_argument("--image", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--report", help="完整JSON审计报告路径；不要提交原始评测产物")
    args = parser.parse_args(argv)
    result = analyze_file(args.image, live=args.live)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({"mode": result["probe_mode"], "profile": result["profile"], "budget": result["budget"],
                      "estimated_list_price_cny": result["estimated_list_price_cny"],
                      "cost_estimate_complete": result["cost_estimate_complete"], "report": args.report}, ensure_ascii=False, indent=2))
    return 0 if result["profile"]["descriptions"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
