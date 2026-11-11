"""Explicit, backed-up repair of one completed job's network-only unknowns.

Stop the backend before running this command. It reuses the normal single-worker
pipeline and the frozen source plan. Other successful observations are preserved;
an audited source is rechecked only when its ID is explicitly supplied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

from app.config import load_settings
from app.db import Database
from app.events import EventBus
from app.services.job_service import JobService


def require_repairable(db, job_id):
    job = db.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or job["input_type"] != "video" or job["status"] != "completed":
        raise ValueError("Only an explicitly selected completed video job can be repaired")
    if job["provider"] != "bailian" or job["model"] != "qwen3.8-flash":
        raise ValueError("Repair must use the same configured cloud Flash model")
    if db.fetchone("SELECT 1 FROM jobs WHERE status IN ('queued','processing') LIMIT 1"):
        raise ValueError("Wait for all queued/running jobs before maintenance")
    if db.fetchone("SELECT 1 FROM style_analyses WHERE status IN ('queued','running') LIMIT 1"):
        raise ValueError("Wait for the running style analysis before maintenance")
    if db.fetchone("SELECT 1 FROM event_agent_runs WHERE status IN ('queued','running') LIMIT 1"):
        raise ValueError("Wait for the running event experiment before maintenance")
    shots = db.fetchall("SELECT * FROM shots WHERE job_id=? ORDER BY number", (job_id,))
    candidates = [row["id"] for row in shots if JobService._network_recovery_candidate(row)]
    if not candidates:
        raise ValueError("No unrecovered network-only unknown sources; no paid calls made")
    return dict(job), shots, candidates


def repair(job_id, *, allow_paid=False, database=None, host="127.0.0.1", port=8000, recheck_sources=()):
    if not allow_paid:
        raise ValueError("Paid repair requires --allow-paid and an explicit --job-id")
    with socket.socket() as probe:
        probe.settimeout(.5)
        if probe.connect_ex((host, port)) == 0:
            raise ValueError("Stop the backend before maintenance; its port is still occupied")
    settings = replace(load_settings(prefer_private_cloud=True), provider="bailian", cloud_model="qwen3.8-flash")
    path = Path(database or settings.data_dir / "storyboard.sqlite3").resolve()
    if not path.is_file():
        raise ValueError("Existing database required")
    settings = replace(settings, data_dir=path.parent)
    db = Database(path)
    job, shots, candidates = require_repairable(db, job_id)
    recheck_sources = list(dict.fromkeys(recheck_sources))
    if any(sid not in {row["id"] for row in shots} for sid in recheck_sources):
        raise ValueError("An explicitly rechecked source must belong to the selected job")
    repair_ids = set(candidates) | set(recheck_sources)
    # Do not use this maintenance path for a large or continuing outage.
    if len(candidates) > 2 or len(repair_ids) > 3:
        raise ValueError("Maintenance is limited to two network failures and three total sources")
    original_frames = [dict(row) for row in db.fetchall("SELECT * FROM frames WHERE job_id=? AND selected=1 ORDER BY id", (job_id,))]
    original_bounds = [(row["id"], row["start_time"], row["end_time"], row["source_raw_shot_ids_json"]) for row in shots]
    known = {row["id"]: (row["status"], row["observation_json"], row["perception_json"])
             for row in shots if row["id"] not in repair_ids}
    other_jobs = [dict(row) for row in db.fetchall("SELECT * FROM jobs WHERE id<>? ORDER BY id", (job_id,))]
    from app.services.generation_plan import file_digest
    video = Path(job["video_path"] or "")
    if not video.is_file() or any(not Path(frame["path"]).is_file() for frame in original_frames):
        raise ValueError("The original video and selected frames must still be readable")
    video_hash = file_digest(video)
    plan_hash = json.loads(job["generation_plan_json"] or "{}").get("video_sha256")
    if plan_hash and plan_hash != video_hash:
        raise ValueError("Video changed since the frozen source plan; refusing paid maintenance")
    frame_hashes = {frame["id"]: hashlib.sha256(Path(frame["path"]).read_bytes()).hexdigest()
                    for frame in original_frames}
    output = settings.data_dir / "repairs" / (job_id + "-" + str(time.time_ns()))
    output.mkdir(parents=True, exist_ok=False)
    backup = output / "before.sqlite3"
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    service = JobService(settings, db, EventBus())
    health = service.provider.health()
    if not health.ready or (health.provider, health.model) != (job["provider"], job["model"]):
        service.executor.shutdown(wait=True)
        service.provider.unload()
        raise ValueError("The same configured cloud provider/model is required; no paid calls made")
    original_call = service.provider._call
    attempts = 0
    def bounded_call(*args, **kwargs):
        nonlocal attempts
        if attempts >= 12:
            raise RuntimeError("Maintenance paid request budget exhausted (12)")
        attempts += 1
        kwargs["max_attempts"] = 1
        return original_call(*args, **kwargs)
    service.provider._call = bounded_call
    config = json.loads(job["config_json"])
    config.update(network_resume_requested=True, event_grouping_policy="event-agent-v2",
                  recoverable_network_failure=False, maintenance_backup=db.media.store(backup))
    # The command requires a stopped server; this is one synchronous worker.
    db.update_job(job_id, status="queued", error=None, config_json=json.dumps(config, ensure_ascii=False))
    for sid in recheck_sources:
        # Only an explicitly enumerated source is reanalysed. Its old evidence
        # and observations remain recoverable in the maintenance snapshot.
        db.execute("UPDATE shots SET status='queued' WHERE job_id=? AND id=?", (job_id, sid))
    started = time.monotonic()
    try:
        service._run(job_id)
        current = service.status(job_id)
        after_shots = db.fetchall("SELECT * FROM shots WHERE job_id=? ORDER BY number", (job_id,))
        assert original_bounds == [(row["id"], row["start_time"], row["end_time"], row["source_raw_shot_ids_json"]) for row in after_shots], "Source boundaries changed"
        assert original_frames == [dict(row) for row in db.fetchall("SELECT * FROM frames WHERE job_id=? AND selected=1 ORDER BY id", (job_id,))], "Keyframes changed"
        assert video_hash == file_digest(video), "Video file changed"
        assert all(frame_hashes[frame["id"]] == hashlib.sha256(Path(frame["path"]).read_bytes()).hexdigest()
                   for frame in original_frames), "Selected frame content changed"
        assert all(known[row["id"]] == (row["status"], row["observation_json"], row["perception_json"])
                   for row in after_shots if row["id"] in known), "Successful source observation changed"
        assert other_jobs == [dict(row) for row in db.fetchall("SELECT * FROM jobs WHERE id<>? ORDER BY id", (job_id,))], "Other job changed"
        old_usage = json.loads(job["model_usage_json"] or "[]")
        usage = json.loads(current["model_usage_json"] or "[]")[len(old_usage):]
        payload = json.loads(current["event_sequence_json"] or "{}")
        config = json.loads(current["config_json"])
        config["network_resume_requested"] = False
        db.update_job(job_id, config_json=json.dumps(config, ensure_ascii=False))
        report = {"job_id": job_id, "status": current["status"], "error": current["error"],
                  "backup": str(backup), "repaired_source_ids": candidates,
                  "explicitly_rechecked_source_ids": recheck_sources,
                  "elapsed_seconds": round(time.monotonic() - started, 3), "requests": len(usage),
                  "known_estimated_list_price_cny": round(sum(item.get("estimated_list_price_cny") or 0 for item in usage), 6),
                  "cost_estimate_complete": bool(usage) and all(item.get("estimated_list_price_cny") is not None for item in usage),
                  "sequence_metadata": payload.get("sequence_metadata", {}),
                  "sequence_source_groups": [item["source_shot_ids"] for item in payload.get("generation_sequences", [])],
                  "checks": {"source_boundaries_preserved": True, "selected_frames_preserved": True,
                             "video_and_frame_hashes_preserved": True,
                             "successful_observations_preserved": True, "other_jobs_preserved": True}}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if current["status"] != "completed" or payload.get("sequence_metadata", {}).get("agent_status") != "ready":
            raise RuntimeError("Repair not accepted; diagnostics and the prior database are retained")
        return report
    finally:
        service.executor.shutdown(wait=True)
        service.provider.unload()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--recheck-source", action="append", default=[], help="Explicit source ID to recheck after an evidence audit (maximum three total repairs)")
    args = parser.parse_args()
    repair(args.job_id, allow_paid=args.allow_paid, database=args.database, host=args.host, port=args.port,
           recheck_sources=args.recheck_source)


if __name__ == "__main__":
    main()
