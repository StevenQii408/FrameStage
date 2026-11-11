"""Opt-in paid regression on a read-only backup, never rewrites the source job."""
from __future__ import annotations

import argparse
import json
import sqlite3
import shutil
import time
from dataclasses import replace
from pathlib import Path

from app.config import load_settings
from app.db import Database
from app.media_paths import MediaPaths
from app.events import EventBus
from app.providers.base import FrameInput, ShotContext
from app.services.job_service import JobService
from app.services.prompt_service import build_prompts
from evals.event_agent import event_agent_metrics


def verify(database, job_id, output, *, phase="all", allow_paid=False, recover_structural=False):
    if not allow_paid:
        raise ValueError("Real cloud calls require --allow-paid; never replay them implicitly")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_path = Path(database).resolve()
    isolated_path = output / "validation.sqlite3"
    if isolated_path == source_path:
        raise ValueError("Validation database must differ from the production database")
    if not isolated_path.exists():
        # SQLite backup includes a coherent committed snapshot, also with WAL.
        with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(isolated_path) as target:
                source.backup(target)
    db = Database(isolated_path)
    db.init()
    # The DB snapshot now stores relative media keys. Copy this job's inputs
    # only, keeping the validation worker completely isolated from the source.
    original = MediaPaths(source_path.parent)
    with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source:
        files = source.execute("SELECT video_path FROM jobs WHERE id=? UNION SELECT path FROM frames WHERE job_id=? UNION SELECT thumbnail_path FROM frames WHERE job_id=?", (job_id, job_id, job_id))
        for (stored,) in files:
            path = original.open_path(stored)
            if path and Path(path).is_file():
                destination = Path(db.media.open_path(original.store(path)))
                if not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
    settings = replace(load_settings(prefer_private_cloud=True), provider="bailian",
                       cloud_model="qwen3.8-flash", data_dir=output)
    service = JobService(settings, db, EventBus())
    provider = service.provider
    # One replay is intentionally small. The Agent can do at most two calls;
    # a recovered source takes two or three, not an unbounded paid rerun.
    original_call = provider._call
    call_count = 0
    def bounded_call(*args, **kwargs):
        nonlocal call_count
        if call_count >= 6:
            raise RuntimeError("Paid verification request budget exhausted (6)")
        call_count += 1
        kwargs["max_attempts"] = 1
        return original_call(*args, **kwargs)
    provider._call = bounded_call
    report = {"job_id": job_id, "model": settings.cloud_model, "phase": phase,
              "production_database_modified": False, "repaired_sources": [], "agent": None}
    start = time.monotonic()
    try:
        job = service.status(job_id)
        if not job or job["input_type"] != "video":
            raise ValueError("An existing video job is required")
        if phase in ("repair", "all"):
            for row in db.fetchall("SELECT * FROM shots WHERE job_id=? ORDER BY number", (job_id,)):
                perception = json.loads(row["perception_json"] or "{}")
                calls = perception.get("raw_responses", [])
                failed_network = perception.get("budgets", {}).get("network_failure") or any(
                    "网络请求失败" in str(call.get("error") or "") for call in calls)
                from app.services.perception import recoverable_stages
                schema_stages = recoverable_stages(perception) if recover_structural else []
                if not failed_network and not schema_stages:
                    continue
                frames = [FrameInput(frame["id"], frame["timestamp"], frame["path"],
                                     frame["width"], frame["height"])
                          for frame in db.fetchall("SELECT * FROM frames WHERE job_id=? AND shot_id=? AND selected=1 ORDER BY timestamp,id LIMIT 4", (job_id, row["id"]))]
                context = ShotContext(row["id"], row["number"], row["start_time"], row["end_time"],
                                      job["script_text"] or "", video_path=job["video_path"],
                                      artifact_dir=str(output / "evidence" / row["id"]))
                context.source_edit_times = json.loads(row["source_edit_times_json"] or "[]")
                if schema_stages and not failed_network:
                    from app.services.perception import PerceptionOrchestrator
                    observation = PerceptionOrchestrator(provider, context, frames,
                        seed_record=perception, selected_stages=schema_stages).run()
                else:
                    observation = provider.analyze_shot(frames, context)
                record = observation.perception_record
                if not record or record.budgets.get("network_failure"):
                    raise RuntimeError("Source recovery failed; stop paid verification instead of retrying")
                status = "warning" if observation.analysis_warnings else "ready"
                db.execute("UPDATE shots SET status=?,observation_json=?,perception_json=? WHERE id=?",
                           (status, observation.model_dump_json(), record.model_dump_json(), row["id"]))
                prompts = build_prompts(row["id"], observation, job["script_text"] or "",
                                        source_edit_times=context.source_edit_times,
                                        generation_duration_seconds=row["duration"])
                for prompt in prompts:
                    db.execute("UPDATE prompts SET positive_prompt=?,negative_prompt=?,status=?,contract_json=?,warnings_json=? WHERE job_id=? AND shot_id=? AND prompt_type=? AND platform=?",
                               (prompt.positive_prompt, prompt.negative_prompt, prompt.status,
                                prompt.contract.model_dump_json() if prompt.contract else None,
                                json.dumps(prompt.warnings, ensure_ascii=False), job_id, row["id"],
                                prompt.prompt_type, prompt.platform))
                report["repaired_sources"].append({"id": row["id"], "confidence": observation.confidence,
                    "subject_action": observation.subject_action,
                    "action_steps": observation.action_details.steps,
                    "elapsed_seconds": record.elapsed_seconds})
                print(json.dumps({"source_recovered": report["repaired_sources"][-1]}, ensure_ascii=False), flush=True)
        if phase in ("agent", "all"):
            config = json.loads(job["config_json"])
            config["event_grouping_policy"] = "event-agent-v2"
            db.update_job(job_id, status="processing", stage="event_planning", config_json=json.dumps(config),
                          montage_json="{}", event_sequence_json="{}")
            service.running_job = job_id
            service._build_event_sequences(job_id)
            run = db.fetchone("SELECT * FROM event_agent_runs WHERE job_id=? ORDER BY created_at DESC LIMIT 1", (job_id,))
            result = json.loads(run["result_json"] or "{}") if run else {}
            if result:
                (output / "event-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                sequences = json.loads(service.status(job_id)["event_sequence_json"] or "{}")
                (output / "sequences.json").write_text(json.dumps(sequences, ensure_ascii=False, indent=2), encoding="utf-8")
                report["agent"] = {"status": run["status"], "warnings": json.loads(run["warnings_json"]),
                    "metrics": event_agent_metrics(result),
                    "events": [{"title": event["title"], "source_shot_ids": event["source_shot_ids"],
                                "status": event["status"]} for event in result.get("events", [])],
                    "clips": [{"sources": clip["source_shot_ids"], "duration": clip["generation_duration_seconds"],
                               "mode": clip["generation_mode"], "timeline": clip["timeline"]}
                              for clip in result.get("generation_clips", [])]}
            else:
                raise RuntimeError("Agent did not produce a result; retained diagnostics in isolated database")
    finally:
        usage = provider.drain_usage()
        report.update(elapsed_seconds=round(time.monotonic() - start, 3), requests=len(usage), usage=usage,
                      requested_attempts=call_count,
                      known_estimated_list_price_cny=round(sum(item.get("estimated_list_price_cny") or 0 for item in usage), 6),
                      cost_estimate_complete=all(item.get("estimated_list_price_cny") is not None for item in usage))
        report["report_file"] = str(output / f"report-{phase}-{time.time_ns()}.json")
        Path(report["report_file"]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "usage"}, ensure_ascii=False), flush=True)
        provider.unload()
        service.executor.shutdown(wait=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/storyboard.sqlite3"))
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--phase", choices=("repair", "agent", "all"), default="all")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument('--recover-structural', action='store_true', help='Explicitly recover partial schema stages in the isolated copy')
    args = parser.parse_args()
    verify(args.database, args.job_id, args.out, phase=args.phase, allow_paid=args.allow_paid,
           recover_structural=args.recover_structural)


if __name__ == "__main__":
    main()
