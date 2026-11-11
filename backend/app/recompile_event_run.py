"""Explicit zero-network recompilation of a saved, paid event response.

Creates a new audit run and sequence revision; never replaces the original run.
The backend must be stopped and the source observations/files must be unchanged.
"""
from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

from app.config import load_settings
from app.db import Database
from app.event_agent.models import EventAgentConfig, EventAgentResult
from app.event_agent.service import EventAgentService
from app.events import EventBus
from app.services.job_service import JobService


def assert_same_evidence(saved, current, *, published_scene_ids=None):
    if saved["file_fingerprints"] != current["file_fingerprints"]:
        raise ValueError("The source video or evidence files changed")
    fields = ("id", "number", "start_time", "end_time", "duration", "observation_json", "perception_json",
              "source_text", "source_raw_shot_ids_json", "source_edit_times_json", "cut_reason",
              "generation_duration_seconds")
    if [[row.get(key) for key in fields] for row in saved["shots"]] != [[row.get(key) for key in fields] for row in current["shots"]]:
        raise ValueError("Source observations/boundaries changed; do not replay a stale model response")
    if saved.get("raw_shots") != current.get("raw_shots") or saved.get("frames") != current.get("frames"):
        raise ValueError("Raw source boundaries or saved frame metadata changed; do not replay stale evidence")
    for before, after in zip(saved["shots"], current["shots"]):
        if before.get("scene_group_id") != after.get("scene_group_id") and (
                not published_scene_ids or published_scene_ids.get(before["id"]) != after.get("scene_group_id")):
            raise ValueError("Source observations/boundaries changed; do not replay a stale model response")


def verified_published_scene_ids(run, saved, current):
    """Recognize only this saved run's own already-published scene projection."""
    metadata = json.loads(current.get("job", {}).get("event_sequence_json") or "{}").get("sequence_metadata", {})
    published = metadata.get("scene_projection") or {}
    if metadata.get("agent_run_id") != run["id"] or published.get("run_id") != run["id"]:
        return {}
    result = EventAgentResult.model_validate_json(run["result_json"])
    if result.job_id != run["job_id"] or result.run_id != run["id"]:
        raise ValueError("Published scene projection belongs to a different saved run")
    shots, frames = EventAgentService._inputs(saved)
    expected = EventAgentService.scene_projection(result, saved, shots, frames)
    if published.get("decisions") != expected:
        raise ValueError("Published scene projection does not match the saved run's verified frame evidence")
    current_map = {row["id"]: row for row in current["shots"]}
    if set(current_map) != {decision["shot_id"] for decision in expected} or any(
            current_map[decision["shot_id"]].get("scene_group_id") != decision["scene_group_id"]
            or current_map[decision["shot_id"]].get("scene_reason") != decision["scene_reason"]
            for decision in expected):
        raise ValueError("Current scenes changed after the saved run's published projection")
    return {decision["shot_id"]: decision["scene_group_id"] for decision in expected}


def read_saved_run(database, job_id, source_run_id):
    """Read an explicitly chosen validation run without changing that database."""
    path = Path(database).resolve(strict=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM event_agent_runs WHERE job_id=? AND id=?",
                           (job_id, source_run_id)).fetchone()
    if not row or row["status"] not in ("ready", "degraded"):
        raise ValueError("A saved ready/degraded run for this exact job is required")
    return dict(row)


def assert_verified_recompilation(result):
    """Audit warnings may survive a verified merge; fallback is not publishable."""
    events, clips = result.get("events", []), result.get("generation_clips", [])
    if (result.get("status") not in ("ready", "degraded") or not events or not clips
            or any(event.get("status") != "ready" for event in events)
            or any(clip.get("status") != "ready" for clip in clips)
            or any(audit.get("validation_errors") for audit in result.get("audit", []))):
        raise RuntimeError("Saved response still needs review; original run and backup retained")


def recompile(job_id, source_run_id, *, confirm=False, response_database=None):
    if not confirm:
        raise ValueError("Explicit --confirm is required to publish a new sequence revision")
    with socket.socket() as probe:
        probe.settimeout(.5)
        if probe.connect_ex(("127.0.0.1", 8000)) == 0:
            raise ValueError("Stop the backend before recompilation")
    settings = replace(load_settings(prefer_private_cloud=True), provider="bailian", cloud_model="qwen3.8-flash")
    db = Database(settings.data_dir / "storyboard.sqlite3")
    job = db.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
    run = (read_saved_run(response_database, job_id, source_run_id) if response_database else
           db.fetchone("SELECT * FROM event_agent_runs WHERE job_id=? AND id=?", (job_id, source_run_id)))
    if not job or job["status"] != "completed" or not run or run["status"] not in ("ready", "degraded"):
        raise ValueError("A completed source job and saved ready/degraded run are required")
    if db.fetchone("SELECT 1 FROM jobs WHERE status IN ('queued','processing') LIMIT 1") or db.fetchone("SELECT 1 FROM style_analyses WHERE status IN ('queued','running') LIMIT 1") or db.fetchone("SELECT 1 FROM event_agent_runs WHERE status IN ('queued','running') LIMIT 1"):
        raise ValueError("Wait for active jobs/analyses before maintenance")
    raw = json.loads(run["raw_responses_json"] or "[]")
    if not raw or any(json.loads(item["response"]).get("action") != "finalize" for item in raw):
        raise ValueError("This command only recompiles saved finalize responses without new tool evidence")
    jobs = JobService(settings, db, EventBus())
    coordinator = EventAgentService(jobs)
    try:
        saved = db.media.snapshot(json.loads(run["source_snapshot_json"]), reading=True)
        current = coordinator._snapshot(job_id)
        published_scene_ids = verified_published_scene_ids(run, saved, current)
        assert_same_evidence(saved, current, published_scene_ids=published_scene_ids)
    except Exception:
        jobs.provider.unload()
        jobs.executor.shutdown(wait=True)
        raise
    output = settings.data_dir / "repairs" / (job_id + "-recompile-" + str(time.time_ns()))
    output.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(db.path) as source, sqlite3.connect(output / "before.sqlite3") as target:
        source.backup(target)
    replies = iter(raw)
    replay_count = 0
    def replay(prompt, frames, **kwargs):
        nonlocal replay_count
        try:
            item = next(replies)
        except StopIteration as exc:
            raise RuntimeError("No saved response left; network calls are forbidden") from exc
        replay_count += 1
        jobs.provider.usage.append({"phase": "event_agent.offline_recompile", "status": "replayed",
            "source_run_id": source_run_id, "network_requests": 0, "model": run["model"],
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "estimated_list_price_cny": 0, "elapsed_seconds": 0})
        return item["response"]
    def forbidden(*args, **kwargs):
        raise RuntimeError("Offline recompilation forbids all network/model requests")
    jobs.provider.analyze_event_window = replay
    jobs.provider._call = forbidden
    old_config = json.loads(job["config_json"])
    config = {**old_config, "event_grouping_policy": "event-agent-v2"}
    db.update_job(job_id, status="processing", config_json=json.dumps(config, ensure_ascii=False))
    jobs.running_job = job_id
    try:
        jobs._build_event_sequences(job_id)
        latest = db.fetchone("SELECT * FROM event_agent_runs WHERE job_id=? ORDER BY created_at DESC LIMIT 1", (job_id,))
        payload = json.loads(jobs.status(job_id)["event_sequence_json"] or "{}")
        provenance = {"mode": "offline-recompile", "source_run_id": source_run_id,
                      "network_requests": 0, "new_paid_cost_cny": 0, "saved_responses_consumed": replay_count}
        if published_scene_ids:
            provenance["verified_published_scene_ids"] = published_scene_ids
        if response_database:
            provenance["source_response_database"] = str(Path(response_database).resolve())
        result = json.loads(latest["result_json"] or "{}")
        result.setdefault("usage_summary", {}).update(provenance)
        db.execute("UPDATE event_agent_runs SET result_json=?,usage_summary_json=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), json.dumps(result["usage_summary"], ensure_ascii=False), latest["id"]))
        payload.setdefault("sequence_metadata", {})["recompilation"] = provenance
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET event_sequence_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), job_id))
            version = payload["sequence_metadata"]["version_number"]
            conn.execute("UPDATE event_sequence_revisions SET payload_json=? WHERE job_id=? AND version=?",
                         (json.dumps(payload, ensure_ascii=False), job_id, version))
        jobs._emit(job_id, "sequence.ready", {"sequence_count": len(payload.get("generation_sequences", [])),
            "version": version, "run_id": latest["id"], "recompilation": provenance})
        report = {"job_id": job_id, "new_run_id": latest["id"], "status": latest["status"],
            "backup": str(output / "before.sqlite3"), **provenance,
            "groups": [sequence["source_shot_ids"] for sequence in payload.get("generation_sequences", [])]}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        assert_verified_recompilation(result)
        return report
    finally:
        db.update_job(job_id, status="completed", stage="completed", progress=100)
        jobs._emit(job_id, "job.completed", {"progress": 100})
        jobs.provider.unload()
        jobs.executor.shutdown(wait=True)
        jobs.running_job = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--response-database", type=Path,
                        help="Optional read-only validation SQLite; exact source evidence must still match")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    recompile(args.job_id, args.source_run_id, confirm=args.confirm,
              response_database=args.response_database)


if __name__ == "__main__":
    main()
