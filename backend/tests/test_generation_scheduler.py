"""Durable scheduling invariants with isolated files and no external calls."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
import os
import time
from types import SimpleNamespace

import pytest

from app.api.generations import project_events
from app.db import Database
from app.events import EventBus
from app.generation.config import GenerationSettingsConfig
from app.generation.models import GenerationError
from app.generation.providers import ProviderResult, GeneratedAsset
from app.generation.service import GenerationService
from app.generation.scheduler import GenerationScheduler
from test_generation_tasks import image_bytes, request


class Provider:
    name = "durable-test"
    configured = True

    def __init__(self):
        self.submitted, self.polled, self.cancelled = [], [], []
        self.result = ProviderResult("running")
        self.after_submit = None

    def capabilities(self):
        return {"configured": True, **{kind: {"available": True, "aspect_ratios": ["16:9"],
            "resolutions": ["1080p"], "supports_reference": True, "max_reference_images": 8,
            "models": [{"id": model}], "min_duration_seconds": 5, "max_duration_seconds": 15}
            for kind, model in (("image", "qwen-image-3.0-pro"), ("video", "wan3.0-video"))}}

    def submit(self, body, *, idempotency_key):
        self.submitted.append(idempotency_key)
        if self.after_submit:
            self.after_submit()
        return "remote-" + idempotency_key

    def poll(self, task_id):
        self.polled.append(task_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def cancel(self, task_id):
        self.cancelled.append(task_id)


@pytest.fixture
def durable(tmp_path, monkeypatch):
    monkeypatch.setattr(GenerationService, "_start_scheduler", lambda self: None)
    config = replace(GenerationSettingsConfig(), enabled=True, global_inflight=2,
        image_inflight=2, video_inflight=2, poll_interval_seconds=0.001, image_submit_rpm=60000, video_submit_rpm=60000)
    service = GenerationService(SimpleNamespace(data_dir=tmp_path), Database(tmp_path / "jobs.db"),
        EventBus(), provider=Provider(), generation_config=config)
    yield service
    service.close()


def new(service, number, **values):
    return service.create(request(node_id=f"gen-{number}", idempotency_key=f"request-{number:04}", **values))


def step(service, *, finalizing=False, now=None):
    entry = service.queue.claim(f"{os.getpid()}:test", service.config, finalizing=finalizing,
        now=now if now is not None else time.time() + 1)
    assert entry
    service._step(entry, f"{os.getpid()}:test")
    return entry


def test_queue_admission_is_atomic_and_replay_does_not_consume_capacity(durable):
    durable.config = replace(durable.config, max_pending=1)
    first = new(durable, 1)
    assert new(durable, 1)["id"] == first["id"]
    with pytest.raises(GenerationError) as error:
        new(durable, 2)
    assert error.value.status_code == 429
    assert len(durable.repository.pending()) == 1


def test_lease_reserves_total_and_kind_slots_before_public_submitting(durable):
    for number in range(3):
        new(durable, number)
    now = time.time() + 10
    first = durable.queue.claim("one", durable.config, now=now)
    second = durable.queue.claim("two", durable.config, now=now + 1)
    assert first and second and first["job_id"] != second["job_id"]
    rate_before = dict(durable.repository.db.fetchone("SELECT * FROM generation_submit_limits WHERE kind='image'"))
    assert durable.queue.claim("three", durable.config, now=now + 2) is None
    assert dict(durable.repository.db.fetchone("SELECT * FROM generation_submit_limits WHERE kind='image'")) == rate_before
    durable.queue.release_owner("one")
    durable.queue.release_slot(first["job_id"])
    assert durable.queue.claim("three", durable.config, now=now + 3)


def test_submit_rate_survives_multiple_claimers(durable):
    durable.config = replace(durable.config, image_submit_rpm=5)
    new(durable, 1)
    new(durable, 2)
    now = time.time() + 1
    assert durable.queue.claim("one", durable.config, now=now)
    assert durable.queue.claim("two", durable.config, now=now + 5) is None
    assert durable.queue.claim("two", durable.config, now=now + 12)


def test_waiting_cloud_job_releases_network_lease_and_persists_remote_id(durable):
    job = new(durable, 1)
    step(durable)
    checkpoint = durable.queue.peek(job["id"])
    assert checkpoint["phase"] == "poll" and checkpoint["lease_owner"] is None
    assert durable.get(job["id"])["status"] == "running"
    assert durable.repository.get(job["id"])["provider_task_id"]
    step(durable)
    assert len(durable.provider.submitted) == 1 and len(durable.provider.polled) == 1
    assert durable.queue.peek(job["id"])["lease_owner"] is None
    assert durable.get(job["id"])["progress"] is None


def test_terminal_status_and_queue_retirement_commit_together(durable, monkeypatch):
    job_id = new(durable, 1)["id"]
    step(durable)
    assert durable.queue.peek(job_id)["phase"] == "poll"
    remote_slot = durable.queue.remote_slot(job_id)
    assert remote_slot

    connect = durable.repository.db.connect

    @contextmanager
    def observe_commit():
        with connect() as writer:
            yield writer
        # Inspect each committed state before advance can open another transaction.
        with connect() as reader:
            row = reader.execute(
                "SELECT j.status, q.job_id FROM generation_jobs j "
                "LEFT JOIN generation_queue q ON q.job_id=j.id WHERE j.id=?", (job_id,)
            ).fetchone()
            if row["status"] == "failed":
                assert row["job_id"] is None

    with monkeypatch.context() as patch:
        patch.setattr(durable.repository.db, "connect", observe_commit)
        assert durable.repository.advance(job_id, "failed")["status"] == "failed"

    durable.queue.enqueue(job_id, "poll")
    assert durable.queue.peek(job_id) is None
    assert durable.queue.remote_slot(job_id) == remote_slot


def test_recovery_queries_existing_remote_task_without_resubmitting(durable):
    job = new(durable, 1)
    step(durable)
    provider = Provider()
    recovered = GenerationService(SimpleNamespace(data_dir=durable.assets.root.parent), durable.repository.db,
        EventBus(), provider=provider, generation_config=durable.config)
    try:
        step(recovered)
        assert provider.polled == [durable.repository.get(job["id"])["provider_task_id"]]
        assert provider.submitted == []
    finally:
        recovered.close()


def test_recovery_preserves_a_live_submission_lease(durable):
    job = new(durable, 1)
    lease = durable.queue.claim(f"{os.getpid()}:live", durable.config, now=time.time() + 1)
    assert lease
    durable.repository.advance(job["id"], "submitting")
    durable._recover()
    assert durable.get(job["id"])["status"] == "submitting"
    assert durable.queue.peek(job["id"])["lease_owner"]


def test_unknown_submission_after_crash_is_not_automatically_paid_again(durable):
    job = new(durable, 1)
    durable.repository.advance(job["id"], "submitting", updates={"billing": {"status": "unknown"}})
    durable._recover()
    assert durable.get(job["id"])["status"] == "interrupted"
    assert not durable.provider.submitted and durable.queue.peek(job["id"]) is None


@pytest.mark.parametrize("remote_terminal", ["succeeded", "cancelled"])
def test_cancel_racing_submit_retains_private_handle_and_no_asset(durable, remote_terminal):
    job = new(durable, 1)
    durable.provider.after_submit = lambda: durable.cancel(job["id"])
    step(durable)
    actual = durable.repository.get(job["id"])
    assert actual["status"] == "cancelled" and actual["provider_task_id"]
    assert durable.provider.cancelled == [actual["provider_task_id"]]
    assert "provider_task_id" not in durable.get(job["id"])
    assert not actual["assets"] and durable.queue.peek(job["id"]) is None
    assert durable.queue.remote_slot(job["id"])["provider_task_id"] == actual["provider_task_id"]
    durable.provider.result = ProviderResult(remote_terminal)
    entry = durable.queue.claim_reconciliation("reconcile", durable.config)
    durable._reconcile_remote(entry, "reconcile")
    assert durable.queue.remote_slot(job["id"]) is None
    assert durable.get(job["id"])["status"] == "cancelled" and not durable.get(job["id"])["assets"]
    assert durable.get(job["id"])["error"]["code"] == "user_cancelled"


def test_shutdown_during_poll_error_preserves_checkpoint(durable):
    job = new(durable, 1)
    step(durable)

    def interrupted_poll(_):
        durable._closed.set()
        raise GenerationError(503, "network stopped", "provider_unavailable")

    durable.provider.poll = interrupted_poll
    step(durable)
    assert durable.get(job["id"])["status"] == "running"
    assert durable.queue.peek(job["id"])["lease_owner"] is None


def test_provider_cancellation_releases_slot_and_replays_project_event(durable):
    job = new(durable, 1)
    step(durable)
    assert durable.queue.remote_slot(job["id"])
    cursor = durable.repository.project_events_since(None)[-1]["sequence"]
    durable.provider.result = ProviderResult("cancelled", error_code="provider_cancelled")
    step(durable)
    final = durable.get(job["id"])
    assert final["status"] == "cancelled" and final["error"]["code"] == "provider_cancelled"
    assert final["error"]["retryable"] is False and not final["assets"]
    assert durable.provider.submitted == [job["id"]] and not durable.provider.cancelled
    assert durable.queue.peek(job["id"]) is None and durable.queue.remote_slot(job["id"]) is None
    replay = durable.repository.project_events_since(None, cursor)
    assert [event["type"] for event in replay] == ["generation.cancelled"]
    assert replay[0]["payload"]["job"] == final


def test_transient_query_retries_same_id_respecting_retry_after(durable):
    job = new(durable, 1)
    step(durable)
    error = GenerationError(429, "limited", "rate_limited")
    error.retry_after = 45
    durable.provider.result = error
    before = time.time()
    step(durable)
    checkpoint = durable.queue.peek(job["id"])
    assert checkpoint["failures"] == 1 and checkpoint["next_run_at"] >= before + 45
    assert durable.get(job["id"])["status"] == "running" and len(durable.provider.submitted) == 1


@pytest.mark.parametrize("code,retryable", [
    ("provider_capacity_exhausted", True), ("provider_balance_insufficient", False),
    ("provider_quota_exceeded", False), ("provider_authentication_failed", False),
    ("content_rejected", False), ("invalid_generation_inputs", False),
])
def test_failed_task_streams_public_reason_and_releases_only_confirmed_terminal_slot(durable, code, retryable):
    job = new(durable, 1)
    step(durable)
    assert durable.queue.remote_slot(job["id"])
    # A careless adapter flag cannot enable retry of known configuration errors.
    durable.provider.result = ProviderResult("failed", error_code=code, message="fixture-secret /private/path", retryable=True)
    step(durable)
    final = durable.get(job["id"])
    assert final["status"] == "failed" and final["error"]["code"] == code
    assert final["error"]["retryable"] is retryable
    assert not final["assets"] and durable.queue.remote_slot(job["id"]) is None
    assert len(durable.provider.submitted) == 1
    events = durable.repository.events_since(job["id"])
    assert events[-1]["type"] == "generation.failed"
    assert events[-1]["payload"]["job"]["error"] == final["error"]
    assert "fixture-secret" not in json.dumps(events)


@pytest.mark.parametrize("code,retained,retryable", [
    ("provider_capacity_exhausted", False, True), ("provider_balance_insufficient", False, False),
    ("provider_quota_exceeded", False, False), ("provider_network_error", True, True),
    ("provider_timeout", True, True), ("provider_unavailable", True, True),
])
def test_submit_failure_preserves_ambiguous_cloud_capacity_without_resubmitting(durable, code, retained, retryable):
    job = new(durable, 1)
    def rejected(*args, **kwargs):
        raise GenerationError(503, "fixture-secret", code)
    durable.provider.submit = rejected
    step(durable)
    final = durable.get(job["id"])
    assert final["status"] == "failed" and final["error"]["code"] == code
    assert final["error"]["retryable"] is retryable
    assert bool(durable.queue.remote_slot(job["id"])) is retained
    assert durable.queue.peek(job["id"]) is None
    if retained:
        assert durable.queue.remote_slot(job["id"])["state"] == "unknown"
    assert "fixture-secret" not in json.dumps(final)


def test_transient_network_query_keeps_same_remote_task_until_budget_exhausted(durable):
    job = new(durable, 1)
    step(durable)
    durable.provider.result = GenerationError(503, "fixture-private-transport", "provider_network_error")
    for attempt in range(6):
        step(durable, now=time.time() + 301)
        if attempt < 5:
            assert durable.get(job["id"])["status"] == "running"
            assert durable.queue.peek(job["id"])["failures"] == attempt + 1
    final = durable.get(job["id"])
    assert final["status"] == "failed" and final["error"]["code"] == "provider_network_error"
    assert len(durable.provider.submitted) == 1
    assert durable.queue.remote_slot(job["id"])["state"] == "unknown"


def test_timeout_uses_deadline_and_never_creates_second_remote_task(durable):
    job = new(durable, 1)
    step(durable)
    durable.repository.db.execute("UPDATE generation_queue SET deadline=0.1 WHERE job_id=?", (job["id"],))
    step(durable)
    assert durable.get(job["id"])["error"]["code"] == "generation_timeout"
    assert len(durable.provider.submitted) == 1 and not durable.provider.polled
    assert durable.queue.remote_slot(job["id"])["state"] == "unknown"


def test_unknown_remote_status_reserves_capacity_until_explicit_operator_confirmation(durable):
    job = new(durable, 1)
    step(durable)
    with pytest.raises(GenerationError):
        durable.confirm_remote_stopped(job["id"])
    durable.provider.result = ProviderResult("unknown")
    step(durable)
    assert durable.get(job["id"])["error"]["code"] == "remote_status_unknown"
    assert durable.queue.remote_slot(job["id"])["state"] == "unknown"
    entry = durable.queue.claim_reconciliation("reconcile", durable.config)
    durable._reconcile_remote(entry, "reconcile")
    assert durable.queue.remote_slot(job["id"])
    assert durable.confirm_remote_stopped(job["id"])["cloud_slot_released"]
    assert durable.queue.remote_slot(job["id"]) is None
    assert durable.repository.events_since(job["id"])[-1]["type"] == "generation.remote_stop_confirmed"


def test_delivery_urls_are_private_and_processing_is_a_separate_checkpoint(durable):
    job = new(durable, 1)
    step(durable)
    private = "https://example.oss-cn-beijing.aliyuncs.com/result.png?secret=fixture"
    durable.provider.result = ProviderResult("succeeded", remote_assets=[{"url": private}])
    step(durable)
    assert durable.get(job["id"])["status"] == "finalizing"
    assert private in durable.queue.peek(job["id"])["output_json"]
    assert "fixture" not in json.dumps(durable.repository.events_since(job["id"]))
    durable.provider.collect = lambda *_: [GeneratedAsset(image_bytes("blue", (1920, 1080)), "image/png")]
    step(durable, finalizing=True)
    assert durable.get(job["id"])["status"] == "succeeded" and durable.get(job["id"])["assets"]
    assert durable.queue.peek(job["id"]) is None


@pytest.mark.parametrize("state,expected", [
    ("running", "finalizing"), ("queued", "finalizing"), ("failed", "failed"), ("cancelled", "cancelled"),
])
def test_recovery_without_delivery_metadata_honors_actual_remote_state(durable, state, expected):
    job = new(durable, 1)
    step(durable)
    durable.repository.advance(job["id"], "finalizing")
    durable._recover()
    durable.provider.result = ProviderResult(state)
    step(durable, finalizing=True)
    assert durable.get(job["id"])["status"] == expected
    assert bool(durable.queue.peek(job["id"])) == (expected == "finalizing")


def test_project_event_replay_is_scoped_and_resumable(durable):
    db = durable.repository.db
    with db.connect() as conn:
        for identity in ("project-a", "project-b"):
            conn.execute("INSERT INTO projects VALUES(?,?,datetime('now'),datetime('now'),NULL)", (identity, identity))
    a = new(durable, 1, project_id="project-a")
    new(durable, 2, project_id="project-b")
    new(durable, 3)
    stream = durable.repository.project_events_since("project-a")
    assert len(stream) == 2 and {item["job_id"] for item in stream} == {a["id"]}
    assert len(durable.repository.project_events_since("project-a", stream[0]["sequence"])) == 1
    assert len(durable.repository.project_events_since(None)) == 2


def test_project_sse_replays_multiple_jobs_without_waiting_threads(durable):
    a, b = new(durable, 1), new(durable, 2)

    async def check():
        async def connected():
            return False
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(generation_service=durable)),
            headers={}, query_params={"scope": "legacy"}, is_disconnected=connected)
        response = await project_events(req)
        chunks = [await anext(response.body_iterator) for _ in range(4)]
        await response.body_iterator.aclose()
        identities = {json.loads(chunk.split("data: ")[1])["job_id"] for chunk in chunks}
        assert identities == {a["id"], b["id"]}
    asyncio.run(check())


def test_event_bus_thread_notification_uses_async_waiter_and_cleans_up():
    bus = EventBus()

    async def check():
        task = asyncio.create_task(bus.wait("project"))
        await asyncio.sleep(0)
        bus.notify("project")
        await task
        assert not bus._waiters
        await bus.wait("timeout", timeout=0.001)
        assert not bus._waiters
        task = asyncio.create_task(bus.wait("cancel"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not bus._waiters
    asyncio.run(check())


def test_real_scheduler_mixes_image_video_16_inflight_and_drains_32_jobs(tmp_path):
    """Exercise actual worker threads; all remote operations are local fakes."""
    config = replace(GenerationSettingsConfig(), enabled=True, global_inflight=16,
        image_inflight=10, video_inflight=16, poll_interval_seconds=600,
        image_submit_rpm=60000, video_submit_rpm=60000)
    provider = Provider()
    service = GenerationService(SimpleNamespace(data_dir=tmp_path), Database(tmp_path / "parallel.db"),
        EventBus(), provider=provider, generation_config=config)
    service.scheduler = GenerationScheduler(service, config, dispatch_interval=0.001)
    try:
        reference = service.assets.image(image_bytes())
        jobs = [new(service, number, kind="image" if number % 2 == 0 else "video",
            reference_asset_id=None if number % 2 == 0 else reference["id"]) for number in range(32)]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            active = [service.get(item["id"]) for item in jobs]
            if sum(item["status"] == "running" for item in active) == 16:
                break
            time.sleep(0.01)
        else:
            pytest.fail("16 local fake tasks did not enter cloud-waiting state")
        assert len(provider.submitted) == 16
        assert sum(item["status"] == "running" and item["kind"] == "image" for item in active) <= 10
        assert sum(item["status"] == "queued" for item in active) == 16
        # Waiting remote tasks leave all I/O leases available.
        assert not service.repository.db.fetchall("SELECT job_id FROM generation_queue WHERE lease_owner IS NOT NULL")
        provider.result = ProviderResult("failed", error_code="provider_failed")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            service.repository.db.execute("UPDATE generation_queue SET next_run_at=0 WHERE phase='poll' AND lease_owner IS NULL")
            service.scheduler.wake()
            if all(service.get(item["id"])["status"] == "failed" for item in jobs):
                break
            time.sleep(0.02)
        else:
            pytest.fail("Local fake queue did not drain")
        assert len(provider.submitted) == len(set(provider.submitted)) == 32
        assert not service.repository.db.fetchall("SELECT job_id FROM generation_queue")
    finally:
        service.close()
