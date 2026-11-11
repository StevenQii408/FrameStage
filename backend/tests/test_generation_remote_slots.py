"""Cloud capacity is held until the remote task, rather than local UI, ends."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time

import pytest

from app.db import Database, utc_now
from app.generation.config import GenerationSettingsConfig
from app.generation.queue import GenerationQueue
from app.generation.repository import GenerationRepository


@pytest.fixture
def state(tmp_path):
    db = Database(tmp_path / "slots.sqlite3")
    db.init()
    repository = GenerationRepository(db)
    return repository, GenerationQueue(db), replace(GenerationSettingsConfig(), global_inflight=1,
        image_inflight=1, video_inflight=1, image_submit_rpm=60000, video_submit_rpm=60000)


def create(state, number, kind="image"):
    repository, queue, _ = state
    identity = f"generation-slot-{number}"
    job = {"id": identity, "node_id": f"node-{number}", "kind": kind, "provider": "isolated-fake",
           "status": "queued", "stage": "queued", "revision": 1, "progress": None, "assets": [],
           "provider_task_id": None, "input_hash": str(number), "retry_of": None,
           "request": {"kind": kind}, "billing": {"status": "not_submitted"},
           "created_at": utc_now(), "updated_at": utc_now()}
    repository.create(job, f"request-slot-{number}")
    queue.enqueue(identity)
    return identity


def running(state, identity):
    repository, queue, _ = state
    repository.advance(identity, "submitting")
    repository.advance(identity, "running", updates={"provider_task_id": f"remote-{identity}"})
    queue.attach_remote(identity, f"remote-{identity}")


def test_local_cancellation_and_queue_cleanup_do_not_free_cloud_capacity(state):
    repository, queue, config = state
    first, second = create(state, 1), create(state, 2)
    now = time.time() + 10
    assert queue.claim("original", config, now=now)["job_id"] == first
    running(state, first)
    repository.advance(first, "cancelled")
    queue.finish(first)
    assert queue.claim("another", config, now=now + 1) is None
    assert queue.slot_counts()["total"] == 1
    claimed = queue.claim_reconciliation("reconciler", config, now=now + 2)
    assert claimed["job_id"] == first
    assert claimed["provider_task_id"] == f"remote-{first}"
    queue.defer_reconciliation(first, "reconciler", 60)
    assert queue.slot_counts()["total"] == 1
    assert queue.claim("another", config, now=now + 3) is None
    claimed = queue.claim_reconciliation("reconciler", config, now=time.time() + 61)
    assert claimed
    queue.finish_reconciliation(first, "reconciler")
    assert repository.get(first)["status"] == "cancelled"
    assert queue.claim("another", config, now=time.time() + 62)["job_id"] == second


def test_an_ambiguous_submit_without_handle_keeps_its_slot_until_manual_reconciliation(state):
    repository, queue, config = state
    first, _ = create(state, 1), create(state, 2)
    queue.claim("original", config, now=time.time() + 1)
    repository.advance(first, "submitting")
    repository.advance(first, "failed")
    queue.mark_remote_unknown(first)
    queue.finish(first)
    assert queue.slot_counts()["unknown"] == 1
    assert queue.claim_reconciliation("reconciler", config) is None
    assert queue.claim("another", config, now=time.time() + 5) is None
    queue.release_slot(first)  # Explicit external proof that it was not accepted.
    assert queue.slot_counts()["total"] == 0


def test_worker_lease_release_does_not_release_or_double_count_the_same_reserved_slot(state):
    _, queue, config = state
    identity = create(state, 1)
    queue.claim("first-owner", config, now=time.time() + 1)
    queue.release_owner("first-owner")
    assert queue.slot_counts()["reserved"] == 1
    claimed = queue.claim("new-owner", config, now=time.time() + 2)
    assert claimed["job_id"] == identity
    assert queue.slot_counts()["total"] == 1


def test_unknown_or_accepted_slots_cannot_be_reclaimed_as_new_submissions(state):
    _, queue, config = state
    identity = create(state, 1)
    queue.claim("original", config, now=time.time() + 1)
    queue.release_owner("original")
    queue.mark_remote_unknown(identity)
    assert queue.claim("new-owner", config, now=time.time() + 3) is None
    queue.attach_remote(identity, "remote-accepted")
    assert queue.claim("new-owner", config, now=time.time() + 4) is None


def test_reservations_apply_global_and_per_kind_limits_even_after_local_failures(state):
    repository, queue, _ = state
    config = replace(state[2], global_inflight=3, image_inflight=1, video_inflight=2)
    image = create(state, 1)
    video_a, video_b = create(state, 2, "video"), create(state, 3, "video")
    create(state, 4)
    create(state, 5, "video")
    now = time.time() + 10
    assert queue.claim("owner-a", config, now=now)["job_id"] == image
    repository.advance(image, "failed")
    queue.mark_remote_unknown(image)
    assert queue.claim("owner-b", config, now=now + 1)["job_id"] == video_a
    assert queue.claim("owner-c", config, now=now + 2)["job_id"] == video_b
    assert queue.claim("owner-d", config, now=now + 3) is None
    assert queue.slot_counts() == {"total": 3, "image": 1, "video": 2, "unknown": 1, "reserved": 2, "active": 0}


def test_concurrent_claimers_reserve_in_one_sqlite_transaction(state):
    _, queue, config = state
    for number in range(8):
        create(state, number)
    now = time.time() + 10
    with ThreadPoolExecutor(max_workers=8) as executor:
        claimed = list(executor.map(lambda index: queue.claim(f"owner-{index}", config, now=now + index), range(8)))
    assert sum(entry is not None for entry in claimed) == 1
    assert queue.slot_counts()["total"] == 1


def test_recovery_restores_the_existing_handle_without_inventing_a_second_slot(state):
    repository, queue, _ = state
    identity = create(state, 1)
    repository.advance(identity, "submitting")
    repository.advance(identity, "running", updates={"provider_task_id": "existing-remote-task"})
    queue.reserve_existing(repository.get(identity))
    queue.reserve_existing(repository.get(identity))
    assert queue.slot_counts()["active"] == 1
    assert queue.remote_slot(identity)["provider_task_id"] == "existing-remote-task"
    queue.mark_remote_unknown(identity)
    queue.attach_remote(identity, "existing-remote-task")
    assert queue.remote_slot(identity)["state"] == "active"
    with pytest.raises(ValueError):
        queue.attach_remote(identity, "unexpected-second-task")


def test_only_terminal_local_jobs_with_handles_are_reconciled_and_leases_are_fenced(state):
    repository, queue, config = state
    identity = create(state, 1)
    running(state, identity)
    assert queue.claim_reconciliation("owner-a", config, now=time.time() + 1) is None
    repository.advance(identity, "failed")
    now = time.time() + 10
    first = queue.claim_reconciliation("owner-a", config, now=now)
    assert first
    assert queue.claim_reconciliation("owner-b", config, now=now + 1) is None
    queue.finish_reconciliation(identity, "owner-b")
    assert queue.remote_slot(identity)
    second = queue.claim_reconciliation("owner-b", config, now=first["lease_until"] + 1)
    assert second
    queue.finish_reconciliation(identity, "owner-a")
    assert queue.remote_slot(identity)
    queue.finish_reconciliation(identity, "owner-b")
    assert queue.remote_slot(identity) is None


def test_due_time_includes_background_reconciliation_after_local_work_is_done(state):
    repository, queue, _ = state
    identity = create(state, 1)
    running(state, identity)
    repository.advance(identity, "cancelled")
    queue.finish(identity)
    due = time.time() + 90
    repository.db.execute("UPDATE generation_remote_slots SET next_check_at=? WHERE job_id=?", (due, identity))
    assert queue.next_due() == due
    queue.release_slot(identity)
    assert queue.next_due() is None
