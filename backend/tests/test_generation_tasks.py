"""Generation lifecycle uses local fakes only; never calls a paid service."""
import copy
import io
import json
import hashlib
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from app.api.generations import router
from app.db import Database
from app.events import EventBus
from app.generation.models import GenerationCreate, GenerationError, GenerationRequest
from app.generation.providers import GeneratedAsset, ProviderResult
from app.generation.service import GenerationService


def image_bytes(color="orange", size=(32, 18)):
    output = io.BytesIO()
    Image.new("RGB", size, color=color).save(output, "PNG")
    return output.getvalue()


class ManualExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, method, *args):
        self.calls.append((method, args))
        return Future()

    def run(self):
        method, args = self.calls.pop(0)
        method(*args)


class FakeProvider:
    name = "test-only"
    configured = True

    def __init__(self, results=()):
        self.results = list(results)
        self.submissions, self.cancellations, self.polls = [], [], []
        self.on_poll = None

    def capabilities(self):
        kinds = {kind: {"available": True, "aspect_ratios": ["16:9", "9:16", "1:1", "4:3", "3:4"],
                        "resolutions": ["720p", "1080p", "2160p"], "supports_reference": True,
                        "min_duration_seconds": 1, "max_duration_seconds": 12} for kind in ("image", "video")}
        return {"configured": True, "provider": self.name, **kinds, "reason": ""}

    def submit(self, request, *, idempotency_key):
        self.submissions.append((copy.deepcopy(request), idempotency_key))
        return "remote-task"

    def poll(self, task_id):
        self.polls.append(task_id)
        if self.on_poll:
            self.on_poll()
        return self.results.pop(0)

    def cancel(self, task_id):
        self.cancellations.append(task_id)


@pytest.fixture
def service(tmp_path):
    db = Database(tmp_path / "generation.sqlite3")
    result = GenerationService(SimpleNamespace(data_dir=tmp_path), db, EventBus())
    yield result
    result.close()


def request(**updates):
    return {"node_id": "gen-image-node", "kind": "image", "prompt": "纸片人物在窗边", "settings": {"aspect_ratio": "16:9", "resolution": "1080p"},
            "idempotency_key": "request-key-one", **updates}


def configured(service, results):
    service.provider = FakeProvider(results)
    service.executor = ManualExecutor()
    service._owns_executor = False
    return service.provider, service.executor


def test_disabled_provider_saves_no_fake_progress_result_or_charge(service):
    preflight = service.preflight({key: value for key, value in request().items() if key != "idempotency_key"})
    assert preflight["valid"] and not preflight["can_submit"] and not preflight["configured"]
    job = service.create(request())
    assert job["status"] == "blocked" and job["stage"] == "awaiting_provider"
    assert job["progress"] is None and not job["assets"] and service.executor is None
    assert job["billing"] == {"status": "not_submitted", "estimated_cost_cny": None}
    assert job["error"]["code"] == "provider_unconfigured"
    events = service.repository.events_since(job["id"])
    assert [event["type"] for event in events] == ["generation.created", "generation.blocked"]
    assert all(event["payload"]["job"] == job for event in events)


def test_idempotency_conflict_dedup_and_input_snapshot(service):
    body = request()
    job = service.create(body)
    body["prompt"] = "用户后来编辑的文字"
    assert service.get(job["id"])["request"]["prompt"] == "纸片人物在窗边"
    assert service.create(request())["id"] == job["id"]
    assert len(service.repository.events_since(job["id"])) == 2
    with pytest.raises(GenerationError, match="幂等键") as error:
        service.create(body)
    assert error.value.status_code == 409
    assert service.preflight({k: v for k, v in request().items() if k != "idempotency_key"})["input_hash"] == job["input_hash"]


def test_assets_are_validated_deduplicated_and_separate_from_outputs(service):
    first = service.assets.image(image_bytes())
    assert service.assets.image(image_bytes())["id"] == first["id"]
    assert first["mime_type"] == "image/png" and first["width"] == 32
    assert "path" not in first and first["url"].startswith("/api/generation-assets/")
    job = service.create(request(reference_asset_id=first["id"]))
    assert job["request"]["reference_asset"]["sha256"] == first["sha256"]
    assert not job["assets"]
    with pytest.raises(GenerationError):
        service.assets.image(b"not an image")
    with pytest.raises(GenerationError):
        service.assets.path("../../private-file")


def test_video_requires_first_frame_and_image_has_no_duration(service):
    body = request(kind="video")
    check = service.preflight({k: v for k, v in body.items() if k != "idempotency_key"})
    assert not check["valid"] and "首帧" in check["issues"][0]
    with pytest.raises(GenerationError):
        service.create(body)
    asset = service.assets.image(image_bytes())
    job = service.create({**body, "reference_asset_id": asset["id"]})
    assert job["kind"] == "video" and job["request"]["settings"]["duration_seconds"] == 5
    assert not job["assets"]
    with pytest.raises(ValidationError):
        GenerationRequest(node_id="gen-a", kind="image", prompt="image", settings={"duration_seconds": 4})


@pytest.mark.parametrize("duration", range(2, 31))
def test_new_video_duration_accepts_only_current_integer_window(duration):
    parsed = GenerationRequest(node_id="gen-a", kind="video", prompt="人物转身", settings={"duration_seconds": duration})
    assert parsed.settings.duration_seconds == duration
    assert type(parsed.settings.duration_seconds) is int


@pytest.mark.parametrize("duration", [0, 1, 31, 5.0, 5.5, True, False, "5"])
def test_new_video_duration_rejects_outside_fractional_boolean_or_coerced_values(duration):
    with pytest.raises(ValidationError):
        GenerationRequest(node_id="gen-a", kind="video", prompt="人物转身", settings={"duration_seconds": duration})


def historical_video(service, duration):
    """Reproduce a stored old attempt, never pass legacy values through new API."""
    from app.generation.repository import encoded
    asset = service.assets.image(image_bytes())
    saved = service.create(request(kind="video", reference_asset_id=asset["id"]))
    historical = copy.deepcopy(saved)
    historical["request"]["settings"]["duration_seconds"] = duration
    historical["input_hash"] = hashlib.sha256(encoded(historical["request"]).encode()).hexdigest()
    service.repository.db.execute("UPDATE generation_jobs SET input_hash=?,payload_json=? WHERE id=?", (
        historical["input_hash"], encoded(historical), saved["id"]))
    return historical


@pytest.mark.parametrize("duration", [.75, 1, 1.0, 5.5, 12.5])
def test_legacy_video_is_readable_but_out_of_policy_retry_requires_a_new_request(service, duration):
    old = historical_video(service, duration)
    assert service.get(old["id"])["request"]["settings"]["duration_seconds"] == duration
    before = dict(service.repository.db.fetchone("SELECT * FROM generation_jobs WHERE id=?", (old["id"],)))
    with pytest.raises(GenerationError, match="历史任务时长") as error:
        service.retry(old["id"], "legacy-explicit-retry")
    assert error.value.status_code == 409 and error.value.code == "legacy_duration_policy"
    assert dict(service.repository.db.fetchone("SELECT * FROM generation_jobs WHERE id=?", (old["id"],))) == before
    assert len(service.repository.db.fetchall("SELECT id FROM generation_jobs")) == 1


def test_whole_float_legacy_retry_retains_exact_snapshot_and_hash(service):
    old = historical_video(service, 5.0)
    retry = service.retry(old["id"], "legacy-five-second-retry")
    assert retry["retry_of"] == old["id"]
    assert retry["request"] == old["request"] and retry["input_hash"] == old["input_hash"]
    assert type(retry["request"]["settings"]["duration_seconds"]) is float
    assert service.get(old["id"])["request"] == old["request"]


def test_current_wan_contract_opens_thirty_seconds_but_provider_can_be_narrower(service):
    limits = service.capabilities()["request_limits"]["video"]
    assert limits == {"min_duration_seconds": 2, "max_duration_seconds": 30, "duration_step_seconds": 1,
                      "extensible_max_duration_seconds": 30}
    asset = service.assets.image(image_bytes())
    provider, _ = configured(service, [])
    cap = provider.capabilities()
    cap["video"]["max_duration_seconds"] = 30
    provider.capabilities = lambda: cap
    body = {k: v for k, v in request(kind="video", reference_asset_id=asset["id"], settings={"duration_seconds": 15}).items() if k != "idempotency_key"}
    assert service.preflight(body)["can_submit"]
    assert service.preflight({**body, "settings": {"duration_seconds": 30}})["can_submit"]
    cap["video"]["models"] = [{"id": "narrow-model", "min_duration_seconds": 6, "max_duration_seconds": 10}]
    assert not service.preflight({**body, "settings": {"model_id": "narrow-model", "duration_seconds": 15}})["valid"]
    assert service.preflight({**body, "settings": {"model_id": "narrow-model", "duration_seconds": 8}})["can_submit"]


@pytest.mark.parametrize("actual_duration,success", [(15.0, True), (15.2, True), (15.3, False), (30.0, False)])
def test_fifteen_second_video_output_keeps_decoded_duration_contract(service, monkeypatch, actual_duration, success):
    asset = service.assets.image(image_bytes())
    provider, executor = configured(service, [ProviderResult("succeeded", assets=[GeneratedAsset(b"verified local test container", "video/mp4")])])
    cap = provider.capabilities()
    cap["video"]["max_duration_seconds"] = 15
    provider.capabilities = lambda: cap
    metadata = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": actual_duration},
                "streams": [{"codec_type": "video", "width": 1920, "height": 1080}]}
    monkeypatch.setattr("app.generation.assets.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(metadata)))
    saved = service.create(request(kind="video", reference_asset_id=asset["id"], settings={"duration_seconds": 15}))
    executor.run()
    final = service.get(saved["id"])
    assert (final["status"] == "succeeded") is success
    assert bool(final["assets"]) is success


@pytest.mark.parametrize("updates", [
    {"prompt": " "}, {"prompt": "a" * 10001}, {"kind": "audio"}, {"node_id": "../a"},
    {"reference_asset_id": "https://example.com/p.png"}, {"settings": {"aspect_ratio": "2:1"}},
    {"settings": {"resolution": "8k"}}, {"kind": "video", "settings": {"duration_seconds": 31}},
    {"kind": "video", "settings": {"duration_seconds": True}}, {"model": "unauthorized-service"},
])
def test_strict_input_contract_rejects_invalid_or_provider_injection(updates):
    with pytest.raises(ValidationError):
        GenerationCreate.model_validate(request(**updates))


def test_real_lifecycle_seam_uses_provider_progress_and_verified_own_output(service):
    reference = service.assets.image(image_bytes())
    provider, executor = configured(service, [ProviderResult("running", progress=12), ProviderResult("running", progress=8),
        ProviderResult("running", progress=None), ProviderResult("succeeded", assets=[GeneratedAsset(image_bytes("blue", (1920, 1080)), "image/png")])])
    service.poll_interval = 0
    saved = service.create(request(reference_asset_id=reference["id"]))
    assert saved["status"] == "queued" and saved["progress"] is None
    executor.run()
    job = service.get(saved["id"])
    assert job["status"] == "succeeded" and job["progress"] == 100
    assert len(provider.submissions) == 1
    assert job["assets"][0]["id"] != reference["id"]
    assert service.repository.asset(job["assets"][0]["id"])["role"] == "output"
    events = service.repository.events_since(job["id"])
    progresses = [event["payload"]["job"]["progress"] for event in events if event["type"] == "generation.progress"]
    assert progresses == [12]
    assert [e["payload"]["job"]["revision"] for e in events][2:] == sorted(e["payload"]["job"]["revision"] for e in events[2:])
    assert job["billing"]["status"] == "unknown"  # Success is not a verified invoice.


def test_cancel_before_submission_and_late_provider_result_cannot_win(service):
    provider, executor = configured(service, [ProviderResult("succeeded", assets=[GeneratedAsset(image_bytes(), "image/png")])])
    saved = service.create(request())
    cancelled = service.cancel(saved["id"])
    executor.run()
    assert not provider.submissions and cancelled["status"] == "cancelled"
    assert service.cancel(saved["id"])["revision"] == cancelled["revision"]
    next_job = service.create(request(idempotency_key="second-request-key"))
    provider.on_poll = lambda: service.cancel(next_job["id"])
    executor.run()
    final = service.get(next_job["id"])
    assert final["status"] == "cancelled" and not final["assets"]
    assert provider.cancellations == ["remote-task"]
    assert service.repository.advance(final["id"], "succeeded", progress=100) is None


def test_restart_does_not_replay_submission_and_retry_is_a_new_attempt(service):
    provider, _ = configured(service, [])
    saved = service.create(request())
    replacement = GenerationService(SimpleNamespace(data_dir=service.assets.root.parent), service.repository.db, EventBus())
    try:
        assert replacement.get(saved["id"])["status"] == "interrupted" and not provider.submissions
        assert replacement.get(saved["id"])["billing"]["status"] == "not_submitted"
        retry = replacement.retry(saved["id"], "explicit-retry-key")
        assert retry["id"] != saved["id"] and retry["retry_of"] == saved["id"]
        assert retry["request"] == saved["request"] and retry["status"] == "blocked"
        assert replacement.retry(saved["id"], "explicit-retry-key")["id"] == retry["id"]
        with pytest.raises(GenerationError):
            replacement.retry(saved["id"], "request-key-one")
    finally:
        replacement.close()


def test_active_node_cannot_create_parallel_attempt_and_snapshot_cannot_mutate(service):
    _, _ = configured(service, [])
    job = service.create(request())
    with pytest.raises(GenerationError, match="进行中"):
        service.create(request(idempotency_key="other-request-key"))
    with pytest.raises(ValueError, match="不可变"):
        service.repository.advance(job["id"], "submitting", updates={"request": {"prompt": "changed"}})


@pytest.mark.parametrize("results", [
    [ProviderResult("succeeded")], [ProviderResult("succeeded", assets=[GeneratedAsset(b"broken", "image/png")])],
    [ProviderResult("running", progress=float("nan"))], [ProviderResult("nonsense")],
    [ProviderResult("succeeded", assets=[GeneratedAsset(b"video", "video/mp4")])],
])
def test_invalid_provider_completion_never_becomes_success(service, results):
    _, executor = configured(service, results)
    job = service.create(request())
    executor.run()
    final = service.get(job["id"])
    assert final["status"] == "failed" and not final["assets"] and final["error"]


def test_unknown_progress_is_indeterminate_and_polling_has_a_budget(service):
    _, executor = configured(service, [ProviderResult("running"), ProviderResult("running")])
    service.poll_interval, service.max_polls = 0, 2
    job = service.create(request())
    executor.run()
    final = service.get(job["id"])
    assert final["status"] == "failed" and final["error"]["code"] == "generation_timeout"
    assert final["progress"] is None and not final["assets"]


def test_provider_capability_contract_is_checked_before_submission(service):
    provider, executor = configured(service, [])
    cap = provider.capabilities()
    cap["image"]["aspect_ratios"] = ["1:1"]
    cap["image"]["resolutions"] = ["720p"]
    provider.capabilities = lambda: cap
    payload = {k: v for k, v in request().items() if k != "idempotency_key"}
    check = service.preflight(payload)
    assert not check["valid"] and len(check["issues"]) == 2
    with pytest.raises(GenerationError, match="不支持"):
        service.create(request())
    assert not executor.calls and not provider.submissions
    cap["image"].pop("aspect_ratios")
    assert "尚未声明" in service.preflight(payload)["issues"][0]
    cap["video"]["max_duration_seconds"] = 4
    asset = service.assets.image(image_bytes())
    video = {**payload, "kind": "video", "reference_asset_id": asset["id"]}
    assert any("时长" in issue for issue in service.preflight(video)["issues"])


def test_restart_during_provider_submission_retains_unknown_charge(service):
    provider, executor = configured(service, [])
    job = service.create(request())
    service.repository.advance(job["id"], "submitting", updates={"billing": {"status": "unknown", "estimated_cost_cny": None}})
    replacement = GenerationService(SimpleNamespace(data_dir=service.assets.root.parent), service.repository.db, EventBus())
    try:
        after = replacement.get(job["id"])
        assert after["status"] == "interrupted" and after["billing"]["status"] == "unknown"
        executor.run()
        assert not provider.submissions
    finally:
        replacement.close()


def test_job_event_streams_and_assets_do_not_bleed_between_nodes(service):
    a = service.create(request())
    b = service.create(request(node_id="gen-second", idempotency_key="second-node-key"))
    service.cancel(a["id"])
    assert service.get(b["id"])["status"] == "blocked"
    assert all(event["payload"]["job"]["node_id"] == "gen-second" for event in service.repository.events_since(b["id"]))
    assert not any(event["type"] == "generation.cancelled" for event in service.repository.events_since(b["id"]))


def test_asset_record_cannot_escape_private_asset_directory(service, tmp_path):
    asset = service.assets.image(image_bytes())
    outside = tmp_path / "outside.png"
    outside.write_bytes(image_bytes())
    service.repository.db.execute("UPDATE generation_assets SET path=? WHERE id=?", (str(outside), asset["id"]))
    with pytest.raises(GenerationError):
        service.assets.path(asset["id"])


def test_video_provider_output_is_verified_before_success(service, monkeypatch):
    asset = service.assets.image(image_bytes())
    provider, executor = configured(service, [ProviderResult("succeeded", assets=[GeneratedAsset(b"invalid mp4", "video/mp4")])])
    monkeypatch.setattr("app.generation.assets.subprocess.run", lambda *a, **k: SimpleNamespace(stdout='{"streams": []}'))
    job = service.create(request(kind="video", reference_asset_id=asset["id"]))
    executor.run()
    assert service.get(job["id"])["status"] == "failed" and not service.get(job["id"])["assets"]


@pytest.mark.parametrize("size", [(64, 64), (1280, 720), (1080, 1920)])
def test_decoded_output_must_match_saved_ratio_and_resolution_before_publication(service, size):
    _, executor = configured(service, [ProviderResult("succeeded", assets=[GeneratedAsset(image_bytes(size=size), "image/png")])])
    job = service.create(request())
    executor.run()
    failed = service.get(job["id"])
    assert failed["status"] == "failed" and failed["error"]["code"] == "output_contract_mismatch"
    assert not failed["assets"]
    assert not service.repository.db.fetchall("SELECT * FROM generation_assets WHERE role='output'")


@pytest.mark.parametrize("dimensions,duration", [((1920, 1080), 0.1), ((1080, 1920), 5.0), ((1280, 720), 5.0)])
def test_video_decoded_metadata_must_match_duration_ratio_and_resolution(service, monkeypatch, dimensions, duration):
    asset = service.assets.image(image_bytes())
    _, executor = configured(service, [ProviderResult("succeeded", assets=[GeneratedAsset(b"local test container", "video/mp4")])])
    width, height = dimensions
    metadata = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": duration},
                "streams": [{"codec_type": "video", "width": width, "height": height}]}
    monkeypatch.setattr("app.generation.assets.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(metadata)))
    job = service.create(request(kind="video", reference_asset_id=asset["id"]))
    executor.run()
    final = service.get(job["id"])
    assert final["status"] == "failed" and not final["assets"]


@pytest.mark.parametrize("mode", ["exception", "provider-message"])
def test_provider_secrets_and_local_paths_are_never_persisted_or_streamed(service, mode):
    secret = "api_key=fixture-secret /private/internal/path?token=other-secret"
    provider, executor = configured(service, [ProviderResult("failed", message=secret, error_code=secret)])
    if mode == "exception":
        def fail(*args, **kwargs):
            raise RuntimeError(secret)
        provider.submit = fail
    job = service.create(request())
    executor.run()
    final = service.get(job["id"])
    assert final["status"] == "failed" and final["billing"]["status"] == "unknown"
    serialized = json.dumps([final, service.repository.events_since(job["id"])])
    assert "fixture-secret" not in serialized and "/private/internal" not in serialized and "other-secret" not in serialized


@pytest.mark.parametrize("mode", ["exception", "provider-result"])
@pytest.mark.parametrize("code,retryable", [("provider_capacity_exhausted", True),
    ("provider_balance_insufficient", False), ("provider_quota_exceeded", False),
    ("provider_authentication_failed", False), ("content_rejected", False)])
def test_legacy_execution_obeys_same_failure_categories_and_retry_policy(service, mode, code, retryable):
    provider, executor = configured(service, [ProviderResult("failed", error_code=code, retryable=True)])
    if mode == "exception":
        def fail(*args, **kwargs):
            raise GenerationError(503, "fixture-secret", code)
        provider.submit = fail
    job = service.create(request())
    executor.run()
    final = service.get(job["id"])
    assert final["status"] == "failed" and final["error"]["code"] == code
    assert final["error"]["retryable"] is retryable
    assert "fixture-secret" not in json.dumps(service.repository.events_since(job["id"]))


def test_cancel_uses_committed_provider_handle_and_does_not_publish_it(service, monkeypatch):
    provider, _ = configured(service, [])
    job = service.create(request())
    original = service.repository.advance

    def advances(job_id, status, **values):
        if status == "cancelled":
            original(job_id, "submitting")
            original(job_id, "running", updates={"provider_task_id": "internal-provider-secret-handle"})
        return original(job_id, status, **values)

    monkeypatch.setattr(service.repository, "advance", advances)
    cancelled = service.cancel(job["id"])
    assert provider.cancellations == ["internal-provider-secret-handle"]
    assert "provider_task_id" not in cancelled and "provider_task_id" not in service.get(job["id"])
    assert "internal-provider-secret-handle" not in json.dumps(service.repository.events_since(job["id"]))


def test_interrupt_pending_preserves_billing_under_queued_to_submitting_race(service, monkeypatch):
    _, _ = configured(service, [])
    job = service.create(request())
    original = service.repository.advance

    def advances(job_id, status, **values):
        if status == "interrupted":
            original(job_id, "submitting", updates={"billing": {"status": "unknown", "estimated_cost_cny": None}})
        return original(job_id, status, **values)

    monkeypatch.setattr(service.repository, "advance", advances)
    service.repository.interrupt_pending()
    assert service.get(job["id"])["status"] == "interrupted"
    assert service.get(job["id"])["billing"]["status"] == "unknown"


def test_unpublished_output_is_not_accessible_as_a_reference(service):
    job = service.create(request())
    output = service.assets.image(image_bytes(size=(1920, 1080)), role="output", job_id=job["id"], settings=job["request"]["settings"])
    with pytest.raises(GenerationError, match="尚未交付"):
        service.assets.path(output["id"])
    check = service.preflight({k: v for k, v in request(reference_asset_id=output["id"]).items() if k != "idempotency_key"})
    assert not check["valid"]


def test_nonretryable_provider_rejection_requires_editing_a_new_request(service):
    provider, executor = configured(service, [ProviderResult("failed", error_code="content_rejected", retryable=False)])
    job = service.create(request())
    executor.run()
    with pytest.raises(GenerationError, match="修改输入"):
        service.retry(job["id"], "rejected-retry-key")
    assert len(provider.submissions) == 1


@pytest.fixture
def client(service):
    app = FastAPI()
    app.state.generation_service = service
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        yield client


def test_http_disabled_generation_end_to_end_and_sse_cursor_replay(client):
    assert client.get("/api/generations/capabilities").json()["configured"] is False
    job = client.post("/api/generations", json=request()).json()
    assert client.get("/api/generations/" + job["id"]).json() == job
    response = client.get(f"/api/generations/{job['id']}/events")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    assert "generation.created" in response.text and "generation.blocked" in response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1]["payload"]["job"] == job
    after = client.get(f"/api/generations/{job['id']}/events", headers={"Last-Event-ID": str(events[0]["sequence"])})
    assert "generation.created" not in after.text and "generation.blocked" in after.text
    assert client.get(f"/api/generations/{job['id']}/events?after={events[-1]['sequence']}").text == ""
    assert client.get(f"/api/generations/{job['id']}/events?after=-1").status_code == 422
    assert client.get(f"/api/generations/{job['id']}/events?after={2**64}").status_code == 422
    assert client.get(f"/api/generations/{job['id']}/events", headers={"Last-Event-ID": "bad"}).status_code == 422
    cancelled = client.post(f"/api/generations/{job['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"
    assert client.post(f"/api/generations/{job['id']}/retry", json={"idempotency_key": "http-retry-key"}).json()["retry_of"] == job["id"]


def test_http_cloud_cancelled_replays_cancellation_and_disables_retry(service, client):
    provider, executor = configured(service, [ProviderResult("cancelled", error_code="provider_cancelled")])
    job = client.post("/api/generations", json=request()).json()
    executor.run()
    url = f"/api/generations/{job['id']}"
    final = client.get(url).json()
    assert final["status"] == "cancelled" and final["error"]["code"] == "provider_cancelled"
    assert final["error"]["retryable"] is False
    events = [json.loads(line[6:]) for line in client.get(url + "/events").text.splitlines() if line.startswith("data: ")]
    assert events[-1]["type"] == "generation.cancelled" and events[-1]["payload"]["job"] == final
    assert all(event["type"] != "generation.failed" for event in events)
    assert client.post(url + "/retry", json={"idempotency_key": "retry-cloud-cancelled"}).status_code == 409
    assert len(provider.submissions) == 1 and not provider.cancellations


@pytest.mark.parametrize("duration", [.75, 1, 5.0, 5.5])
def test_http_lost_response_replays_existing_legacy_snapshot_without_new_work(service, client, duration):
    previous = historical_video(service, duration)
    payload = {key: value for key, value in previous["request"].items() if key != "reference_asset"}
    payload["idempotency_key"] = "request-key-one"
    before = [dict(row) for row in service.repository.db.fetchall("SELECT * FROM generation_events WHERE job_id=?", (previous["id"],))]
    replay = client.post("/api/generations", json=payload)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == previous["id"] and replay.json()["input_hash"] == previous["input_hash"]
    assert replay.json()["request"] == previous["request"]
    assert len(service.repository.db.fetchall("SELECT id FROM generation_jobs")) == 1
    assert [dict(row) for row in service.repository.db.fetchall("SELECT * FROM generation_events WHERE job_id=?", (previous["id"],))] == before
    # A distinct key is a genuinely new request, not a replay exemption.
    assert client.post("/api/generations", json={**payload, "idempotency_key": "new-invalid-legacy-key"}).status_code == 422


def test_same_key_replays_legacy_whole_float_as_javascript_integer_without_rewriting_snapshot(service, client):
    previous = historical_video(service, 5.0)
    payload = {key: value for key, value in previous["request"].items() if key != "reference_asset"}
    payload["idempotency_key"] = "request-key-one"
    payload["settings"] = {**payload["settings"], "duration_seconds": 5}
    replay = client.post("/api/generations", json=payload)
    assert replay.status_code == 201
    assert replay.json()["id"] == previous["id"]
    assert replay.json()["input_hash"] == previous["input_hash"]
    assert type(replay.json()["request"]["settings"]["duration_seconds"]) is float
    assert service.get(previous["id"])["request"] == previous["request"]
    assert client.post("/api/generations", json={**payload, "settings": {**payload["settings"], "duration_seconds": 6}}).status_code == 409
    assert client.post("/api/generations", json={**payload, "prompt": "真正修改了提示词"}).status_code == 409


def test_json_parse_stringify_transport_replays_original_float_snapshot(service, client):
    previous = historical_video(service, 4.0)
    payload = {key: value for key, value in previous["request"].items() if key != "reference_asset"}
    payload["idempotency_key"] = "request-key-one"
    # JavaScript JSON.parse followed by JSON.stringify emits 4 instead of 4.0.
    wire = json.dumps(payload, ensure_ascii=False).replace('"duration_seconds": 4.0', '"duration_seconds": 4')
    assert type(json.loads(wire)["settings"]["duration_seconds"]) is int
    before = dict(service.repository.db.fetchone("SELECT payload_json,input_hash FROM generation_jobs WHERE id=?", (previous["id"],)))
    replay = client.post("/api/generations", content=wire.encode(), headers={"Content-Type": "application/json"})
    assert replay.status_code == 201
    assert replay.json()["request"] == previous["request"] and replay.json()["input_hash"] == previous["input_hash"]
    assert dict(service.repository.db.fetchone("SELECT payload_json,input_hash FROM generation_jobs WHERE id=?", (previous["id"],))) == before
    assert len(service.repository.db.fetchall("SELECT id FROM generation_jobs")) == 1


def test_current_omitted_default_video_replay_remains_idempotent(service, client):
    asset = service.assets.image(image_bytes())
    payload = request(kind="video", reference_asset_id=asset["id"])
    first = client.post("/api/generations", json=payload)
    repeat = client.post("/api/generations", json=payload)
    assert first.status_code == repeat.status_code == 201
    assert first.json()["id"] == repeat.json()["id"]


@pytest.mark.parametrize("duration", [1, 31, 5.5, True])
def test_http_new_video_requests_and_preflight_enforce_integer_two_to_thirty(service, client, duration):
    asset = service.assets.image(image_bytes())
    payload = request(kind="video", reference_asset_id=asset["id"], settings={"duration_seconds": duration})
    assert client.post("/api/generations", json=payload).status_code == 422
    assert client.post("/api/generations/preflight", json={key: value for key, value in payload.items() if key != "idempotency_key"}).status_code == 422
    assert not service.repository.db.fetchall("SELECT id FROM generation_jobs")


def test_http_reference_upload_preflight_and_asset_integrity(client):
    invalid = client.post("/api/generation-assets", files={"image": ("fake.png", b"not an image", "image/png")})
    assert invalid.status_code == 422
    uploaded = client.post("/api/generation-assets", files={"image": ("reference.png", image_bytes(), "image/png")})
    assert uploaded.status_code == 201
    asset = uploaded.json()
    retrieved = client.get(asset["url"])
    assert retrieved.status_code == 200 and retrieved.headers["content-type"] == "image/png"
    payload = {k: v for k, v in request(kind="video", reference_asset_id=asset["id"]).items() if k != "idempotency_key"}
    check = client.post("/api/generations/preflight", json=payload).json()
    assert check["valid"] and not check["can_submit"]
    assert client.get("/api/generations/missing").status_code == 404
    assert client.post("/api/generations", json=request(prompt="")).status_code == 422


def test_sse_does_not_lose_final_event_committed_between_event_and_status_reads(service, client, monkeypatch):
    _, _ = configured(service, [])
    job = service.create(request())
    original = service.repository.events_since
    queried = False

    def races(job_id, after):
        nonlocal queried
        result = original(job_id, after)
        if not queried:
            queried = True
            service.cancel(job_id)
        return result

    monkeypatch.setattr(service.repository, "events_since", races)
    response = client.get(f"/api/generations/{job['id']}/events")
    assert response.status_code == 200
    assert "generation.cancelled" in response.text
    assert response.text.count("event: generation.cancelled") == 1
