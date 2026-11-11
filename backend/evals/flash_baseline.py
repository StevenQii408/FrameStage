"""Explicit paid Flash-only baseline. Default v1 measures pre-v2 behavior first."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path

from app.config import load_settings
from app.providers.bailian import BailianVisionProvider
from app.providers.base import ScriptPlanError
from app.services.script_processor import extract_script_beats, script_windows
from evals.perception_review import review_draft, technical_summary
from evals.run import render_markdown, run_benchmark


def run(manifest, media_dir, output_dir, version="v1", script_path=None):
    settings = replace(load_settings(), provider="bailian", cloud_model="qwen3.8-flash",
                       perception_v2_enabled=version == "v2")
    # Preflight before material extraction or any paid requests; never read/save keys.
    provider = BailianVisionProvider(settings)
    health = provider.health()
    if not health.ready:
        provider.unload()
        raise RuntimeError(health.detail)
    provider.unload()
    script = script_path.read_text(encoding="utf-8").strip() if script_path else None
    if script_path and (not script or len(script) > 10000):
        raise ValueError("故事须为 1–10,000 字纯文本")
    started = time.monotonic()
    report = run_benchmark(manifest, media_dir, output_dir, "bailian", "qwen3.8-flash", version)
    if script:
        provider = BailianVisionProvider(settings)
        beats = extract_script_beats(script)
        windows = []
        try:
            for index, window in enumerate(script_windows(beats), 1):
                entry = {"number": index, "source_beat_ids": window.target_beat_ids}
                window_started = time.monotonic()
                try:
                    result = provider.plan_script(window.beats, window.target_beat_ids)
                    entry.update(plan=result.plan.model_dump(), raw_responses=result.raw_responses,
                                 parsed=True, needs_review=result.rule_reconciled)
                except Exception as exc:
                    entry.update(error=str(exc), parsed=False, needs_review=True,
                                 raw_responses=getattr(exc, "raw_responses", []))
                entry["elapsed_seconds"] = round(time.monotonic() - window_started, 3)
                windows.append(entry)
        finally:
            report["model_usage"].extend(provider.drain_usage())
            provider.unload()
        report["script_baseline"] = {"beat_count": len(beats), "windows": windows,
                                     "input_sha256": hashlib.sha256(script.encode()).hexdigest(),
                                     "human_quality_status": "pending"}
    report["total_elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["baseline_version"] = version
    report["perception_summary"] = technical_summary(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "report.json"
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    path.write_text(encoded, encoding="utf-8")
    (output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    draft = review_draft(report, hashlib.sha256(encoded.encode()).hexdigest())
    (output_dir / "human-review.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="只调用 qwen3.8-flash 的显式付费基线")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--observation-version", choices=("v1", "v2"), default="v1")
    parser.add_argument("--script", type=Path, help="可选一段单段故事的 UTF-8 文件")
    args = parser.parse_args(argv)
    media_dir = args.media_dir or (Path(os.environ["STORYBOARD_EVAL_MEDIA_DIR"])
                                  if os.getenv("STORYBOARD_EVAL_MEDIA_DIR") else None)
    if media_dir is None:
        parser.error("请配置 STORYBOARD_EVAL_MEDIA_DIR 或 --media-dir")
    if (args.output_dir / "report.json").exists():
        parser.error("输出目录已有基线报告，请换新目录，避免覆盖原始对照")
    try:
        report = run(args.manifest, media_dir, args.output_dir, args.observation_version, args.script)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"Flash 基线未启动：{exc}\n")
    print(args.output_dir / "report.md")
    print(f"人民币目录价估算：{report['perception_summary']['estimated_list_price_cny']}；人工质量待核验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
