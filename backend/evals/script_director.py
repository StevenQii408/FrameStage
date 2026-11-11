"""Opt-in paid script regression against an existing, read-only source job."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from app.config import load_settings
from app.db import Database
from app.events import EventBus
from app.services.job_service import JobService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-job", required=True)
    parser.add_argument("--allow-paid", action="store_true", help="Explicitly authorize existing cloud key use")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error("真实云端验收需要显式 --allow-paid；不会自动产生费用")
    settings = load_settings(prefer_private_cloud=True)
    if not settings.cloud_api_key or settings.cloud_model != "qwen3.8-flash":
        parser.error("需要已配置的Flash云端Key；不会升级模型或调用本地模型")
    source_db = settings.data_dir / "storyboard.sqlite3"
    with sqlite3.connect(f"file:{source_db}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT script_text,input_type FROM jobs WHERE id=?", (args.source_job,)).fetchone()
    if row is None or row[1] != "script":
        parser.error("来源任务不存在或不是文字剧本")
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="script-director-eval-") as folder:
        private_settings = replace(settings, data_dir=Path(folder), script_strategy="director-v3", provider="bailian")
        database = Database(Path(folder) / "storyboard.sqlite3")
        database.init()
        service = JobService(private_settings, database, EventBus())
        try:
            job_id = service.create_script_job(row[0])
            last_stage = None
            while True:
                status = service.status(job_id)
                if status["stage"] != last_stage:
                    print(json.dumps({"stage": status["stage"], "progress": status["progress"],
                        "elapsed_seconds": round(time.perf_counter() - started, 2)}, ensure_ascii=False), flush=True)
                    last_stage = status["stage"]
                if status["status"] in ("completed", "failed", "cancelled"):
                    break
                if time.perf_counter() - started > 600:
                    service.cancel(job_id)
                    raise RuntimeError("隔离验收超过600秒，已取消；没有修改原任务")
                time.sleep(.1)
            # Wait for usage drain in the worker's finally block.
            service.executor.shutdown(wait=True)
            result = service.result(job_id).model_dump(mode="json")
            plan = result["metadata"]["script_planning"]
            report = {"source_job": args.source_job, "source_unchanged": True,
                "elapsed_seconds": round(time.perf_counter() - started, 2), "result": result}
            report["raw_responses"] = json.loads(service.status(job_id)["script_raw_response_json"] or "[]")
            if args.output:
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"status": result["job"]["status"], "profile": result["job"]["segmentation_profile"],
                "shots": len(result["shots"]), "events": len(result["narrative_events"] or []),
                "sequences": len(result["generation_sequences"] or []), "windows": plan["window_count"],
                "model_calls": plan["model_calls"], "fallback_windows": plan["fallback_windows"],
                "estimated_list_price_cny": result["metadata"]["estimated_list_price_cny"],
                "elapsed_seconds": report["elapsed_seconds"], "report": str(args.output) if args.output else None}, ensure_ascii=False), flush=True)
            return 0 if result["job"]["status"] == "completed" and not plan["fallback_windows"] else 1
        finally:
            service.executor.shutdown(wait=True)
            service.provider.unload()


if __name__ == "__main__":
    raise SystemExit(main())
