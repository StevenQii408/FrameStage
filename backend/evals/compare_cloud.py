"""Key-gated Flash baseline; other comparison models require explicit selection."""
import argparse
import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from app.config import load_settings

from evals.run import render_markdown, run_benchmark


DEFAULT_MODELS = (
    "qwen3.8-flash",
)


def compare(manifest: Path, media_dir: Path, output_dir: Path, models: List[str]) -> dict:
    settings = load_settings()
    if not settings.cloud_api_key:
        raise RuntimeError("请在本机后端环境设置 STORYBOARD_BAILIAN_API_KEY，不要把 Key 写入 manifest")
    if not settings.cloud_base_url:
        raise RuntimeError("请设置北京区域 STORYBOARD_BAILIAN_BASE_URL")
    output_dir.mkdir(parents=True, exist_ok=True)
    anonymous = list(models)
    secrets.SystemRandom().shuffle(anonymous)
    codebook = {}
    blind = {"corpus_id": "", "rubric": [
        "物体与场景事实", "动作顺序", "艺术效果及帧证据",
        "重要特写", "场次边界", "跨镜关系", "无证据推断",
    ], "candidates": {}}
    summaries = []
    for index, model in enumerate(anonymous):
        code = chr(ord("A") + index)
        codebook[code] = model
        target = output_dir / code
        report = run_benchmark(manifest, media_dir, target, "bailian", model)
        blind["corpus_id"] = report["corpus_id"]
        (target / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (target / "report.md").write_text(render_markdown(report), encoding="utf-8")
        blind["candidates"][code] = [
            {"clip_id": clip["id"], "shots": [
                {"number": shot["number"], "prediction": shot.get("prediction"), "error": shot.get("error"),
                 "human_scores": {criterion: None for criterion in blind["rubric"]}, "reviewer_notes": ""}
                for shot in clip["model_metrics"]
            ], "montage": clip.get("montage_metrics", {}).get("relations", []),
             "montage_human_scores": {criterion: None for criterion in ("场次边界", "跨镜关系", "无证据推断")}}
            for clip in report["clips"]
        ]
        usage = report.get("model_usage", [])
        summaries.append({
            "blind_id": code, "model": model,
            "parse_rate": report["summary"]["model"]["parse_rate"],
            "action_non_unknown_rate": report["summary"]["model"]["action_non_unknown_rate"],
            "montage_relation_accuracy": report["summary"]["montage"]["relation_accuracy"],
            "scene_change_accuracy": report["summary"]["montage"]["scene_change_accuracy"],
            "total_tokens": sum(item["total_tokens"] for item in usage),
            "estimated_list_price_usd": round(sum(item.get("estimated_list_price_usd") or 0 for item in usage), 6),
            "estimated_list_price_cny": round(sum(item.get("estimated_list_price_cny") or 0 for item in usage), 6),
            "requests": len(usage),
            "cost_estimate_complete": bool(usage) and all(item.get("estimated_list_price_cny") is not None for item in usage),
        })
    (output_dir / "blind-review.json").write_text(json.dumps(blind, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "model-key.json").write_text(json.dumps(codebook, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {"created_at": datetime.now().isoformat(), "corpus_id": blind["corpus_id"], "models": summaries,
              "decision": "待人工盲评；先排除事实错误、无证据推断与结构不稳定，再比较质量，接近时比较费用"}
    (output_dir / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="默认运行 Flash 基准；其他模型须显式使用 --models")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, default=Path(os.getenv("STORYBOARD_EVAL_MEDIA_DIR", "")))
    parser.add_argument("--output-dir", type=Path, default=Path("eval-results/cloud-compare"))
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    args = parser.parse_args(argv)
    if not str(args.media_dir) or str(args.media_dir) == ".":
        parser.error("请设置 STORYBOARD_EVAL_MEDIA_DIR 或传入 --media-dir")
    result = compare(args.manifest, args.media_dir, args.output_dir, args.models)
    print(f"盲评表：{args.output_dir / 'blind-review.json'}")
    print(f"模型对照：{args.output_dir / 'comparison.json'}")
    print(result["decision"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
