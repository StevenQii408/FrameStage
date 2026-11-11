"""Official wire contracts via MockTransport; these tests never use a paid API."""
import asyncio
import base64
from dataclasses import replace
import io
import json
import socket
import subprocess
import time

import httpx
from PIL import Image
import pytest

from app.generation.cloud import CloudGenerationProvider
from app.generation.cloud.provider import image_size
from app.generation.cloud.transport import ArtifactDownloader, api_base, error_code
from app.generation.config import GenerationSettingsConfig
from app.generation.models import GenerationError
from app.generation.providers import ProviderResult


KEY = "fake-key-for-local-mock-only"
PUBLIC_IP = "93.184.216.34"


def resolver(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]


@pytest.fixture
def setup(tmp_path):
    config = GenerationSettingsConfig(enabled=True, image_api_key=KEY, video_api_key=KEY)
    assets = {}
    def put(number=1, transparent=False, size=(600, 600)):
        output = io.BytesIO()
        Image.new("RGBA", size, (20, 40, 60, 0 if transparent else 255)).save(output, "PNG")
        path = tmp_path / f"reference-{number}.png"
        path.write_bytes(output.getvalue())
        asset_id = "genasset-" + str(number) * 64
        assets[asset_id] = ({"mime_type": "image/png", "width": size[0], "height": size[1]}, path)
        return asset_id
    return config, assets.__getitem__, put, tmp_path


def request(kind="image", **updates):
    return {"kind": kind, "node_id": "local-node", "prompt": "主体：纸片机器人，构图：正侧面；整体画风：手工剪纸。",
        "settings": {"aspect_ratio": "16:9", "resolution": "1080p", "duration_seconds": 5 if kind == "video" else None},
        **updates}


def provider(setup, handler, **kwargs):
    config, reader, _, directory = setup
    return CloudGenerationProvider(config, reader, directory, transport=httpx.MockTransport(handler), resolver=resolver, **kwargs)


def test_factory_makes_no_network_calls_and_requires_explicit_enable(setup):
    config, reader, _, directory = setup
    calls = []
    service = CloudGenerationProvider(replace(config, enabled=False), reader, directory,
        transport=httpx.MockTransport(lambda req: calls.append(req)))
    capabilities = service.capabilities()
    assert not capabilities["configured"] and not capabilities["image"]["available"]
    assert capabilities["image"]["models"][0]["id"] == "qwen-image-3.0-pro"
    assert capabilities["video"]["models"][0]["id"] == "wan3.0-video"
    assert capabilities["upstream_sse"] is False
    with pytest.raises(GenerationError) as error:
        service.submit(request(), idempotency_key="local-id")
    assert error.value.code == "provider_unconfigured" and calls == []
    service.close()


def test_catalog_declares_actual_kind_specific_parameters_even_without_key(setup):
    config, reader, _, directory = setup
    service = CloudGenerationProvider(replace(config, enabled=False, image_api_key="", video_api_key=""), reader, directory)
    caps = service.capabilities()
    assert caps["video"]["default_model_id"] == "wan3.0-video"
    assert caps["video"]["resolutions"] == ["480p", "720p", "1080p"]
    assert caps["video"]["min_duration_seconds"] == 2 and caps["video"]["max_duration_seconds"] == 30
    assert "21:9" in caps["video"]["aspect_ratios"]
    assert "480p" not in caps["image"]["resolutions"] and "2160p" not in caps["image"]["resolutions"]
    assert caps["image"]["default_model_id"] == "qwen-image-3.0-pro"
    sizes = caps["image"]["resolution_options"]
    assert any(item["width"] == item["height"] == 2048 for item in sizes)
    assert all(512**2 <= item["width"] * item["height"] <= 2048**2 for item in sizes)
    assert all((item["width"], item["height"]) == image_size({"resolution": item["value"], "aspect_ratio": item["aspect_ratio"]}) for item in sizes)
    assert not any(item["value"] == "2048p" and item["aspect_ratio"] != "1:1" for item in sizes)
    service.close()


@pytest.mark.parametrize("resolution", ["480p", "720p", "1080p"])
@pytest.mark.parametrize("duration", [2, 15, 30])
def test_wan_submits_every_supported_resolution_and_duration_boundary(setup, resolution, duration):
    wire = []
    service = provider(setup, lambda req: (wire.append(json.loads(req.content)) or httpx.Response(200, json={"output": {"task_id": "video-parameters"}})))
    body = request("video", settings={"aspect_ratio": "21:9", "resolution": resolution, "duration_seconds": duration})
    assert not service.validate_request(body)
    service.submit(body, idempotency_key="local-parameters")
    assert wire[0]["parameters"]["resolution"] == resolution.upper()
    assert wire[0]["parameters"]["duration"] == duration
    assert wire[0]["parameters"]["ratio"] == "21:9"
    service.close()


def test_image_pixel_presets_do_not_silently_coerce_unsupported_combinations(setup):
    service = provider(setup, lambda req: pytest.fail("Invalid dimensions must not start a paid request"))
    assert not service.validate_request(request(settings={"aspect_ratio": "1:1", "resolution": "2048p", "duration_seconds": None}))
    invalid = request(settings={"aspect_ratio": "16:9", "resolution": "2048p", "duration_seconds": None})
    assert service.validate_request(invalid)
    with pytest.raises(GenerationError):
        service.submit(invalid, idempotency_key="invalid-dimensions")
    service.close()


def test_qwen_official_async_post_preserves_prompt_and_disables_rewrite(setup):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"output": {"task_id": "image-123", "task_status": "PENDING"}})
    service = provider(setup, handler)
    task_id = service.submit(request(), idempotency_key="persisted-local-id")
    assert task_id == "bailian:image:image-123" and len(calls) == 1
    wire = json.loads(calls[0].content)
    assert calls[0].url.path == "/api/v1/services/aigc/image-generation/generation"
    assert calls[0].headers["x-dashscope-async"] == "enable"
    assert calls[0].headers["authorization"] == "Bearer " + KEY
    assert "idempotency-key" not in calls[0].headers and "stream" not in wire
    assert wire["model"] == "qwen-image-3.0-pro"
    assert wire["parameters"] == {"n": 1, "size": "1920*1080", "prompt_extend": False, "enable_thinking": False, "watermark": False}
    assert wire["input"]["messages"] == [{"role": "user", "content": [{"text": request()["prompt"]}]}]
    service.close()


def test_reference_binding_order_and_noncontiguous_mentions(setup):
    config, reader, put, directory = setup
    one, two = put(1), put(2)
    wire = []
    service = provider(setup, lambda req: (wire.append(json.loads(req.content)) or httpx.Response(200, json={"output": {"task_id": "id"}})))
    body = request(prompt="@图片7与@图片2并排", reference_asset_id=one,
        reference_images=[{"asset_id": two, "mention": "图片2", "role": "reference"}, {"asset_id": one, "mention": "图片7", "role": "reference"}])
    service.submit(body, idempotency_key="local-id")
    content = wire[0]["input"]["messages"][0]["content"]
    assert content[-1] == {"text": "图片2与图片1并排"}
    for part, asset_id in zip(content, (two, one)):
        assert base64.b64decode(part["image"].split(",", 1)[1]) == reader(asset_id)[1].read_bytes()
    service.close()


def test_wan_exact_model_first_frame_and_user_settings(setup):
    _, _, put, _ = setup
    asset_id = put()
    wire = []
    service = provider(setup, lambda req: (wire.append((req, json.loads(req.content))) or httpx.Response(200, json={"output": {"task_id": "video-123"}})))
    assert service.submit(request("video", reference_asset_id=asset_id), idempotency_key="local-id") == "bailian:video:video-123"
    req, body = wire[0]
    assert req.url.path == "/api/v1/services/aigc/video-generation/video-synthesis"
    assert body["model"] == "wan3.0-video"
    assert body["input"]["media"][0]["type"] == "first_frame"
    frame = body["input"]["media"][0]["url"]
    with Image.open(io.BytesIO(base64.b64decode(frame.split(",", 1)[1]))) as encoded_image:
        assert encoded_image.mode == "RGB"
    assert body["parameters"] == {"resolution": "1080P", "ratio": "16:9", "duration": 5,
        "prompt_extend": False, "audio": False, "watermark": False}
    service.close()


def test_wan_reference_mode_keeps_all_images_and_maps_mentions(setup):
    _, _, put, _ = setup
    refs = [{"asset_id": put(i), "mention": f"图片{i+3}", "role": "reference"} for i in (1, 2)]
    wire = []
    service = provider(setup, lambda req: (wire.append(json.loads(req.content)) or httpx.Response(200, json={"output": {"task_id": "id"}})))
    service.submit(request("video", reference_images=refs, prompt="@图片4看向@图片5"), idempotency_key="local-id")
    assert wire[0]["input"]["prompt"] == "图1看向图2"
    assert [item["type"] for item in wire[0]["input"]["media"]] == ["reference_image", "reference_image"]
    service.close()


@pytest.mark.parametrize("case", ["transparent", "too-small", "mixed-mode", "too-many", "unsupported-model", "2160p"])
def test_input_contract_rejected_before_paid_submit(setup, case):
    config, reader, put, directory = setup
    calls = []
    service = provider(setup, lambda req: calls.append(req))
    body = request("video", reference_asset_id=put(1))
    if case == "transparent":
        body["reference_asset_id"] = put(1, transparent=True)
    elif case == "too-small":
        body["reference_asset_id"] = put(1, size=(200, 200))
    elif case == "mixed-mode":
        body["reference_images"] = [{"asset_id": put(1), "mention": "图片1", "role": "first_frame"}, {"asset_id": put(2), "mention": "图片2", "role": "reference"}]
    elif case == "too-many":
        body = request(reference_images=[{"asset_id": put(i), "mention": f"图片{i}", "role": "reference"} for i in range(1, 5)])
    elif case == "unsupported-model":
        body["settings"]["model_id"] = "wan2.7-i2v"
    elif case == "2160p":
        body = request()
        body["settings"]["resolution"] = "2160p"
    assert service.validate_request(body)
    with pytest.raises(GenerationError) as error:
        service.submit(body, idempotency_key="local-id")
    assert error.value.code == "invalid_generation_inputs" and calls == []
    service.close()


@pytest.mark.parametrize("state,expected", [("PENDING", "queued"), ("RUNNING", "running"), ("FAILED", "failed")])
def test_remote_states_do_not_fabricate_progress(setup, state, expected):
    service = provider(setup, lambda req: httpx.Response(200, json={"output": {"task_id": "id", "task_status": state}}))
    result = service.poll("bailian:video:id")
    assert result.state == expected and result.progress is None and not result.assets and not result.remote_assets
    service.close()


@pytest.mark.parametrize("kind", ["image", "video"])
def test_cloud_canceled_is_terminal_even_if_payload_contains_a_failure_code(setup, kind):
    output = {"task_id": "id", "task_status": "CANCELED", "code": "InputDataInspectionFailed",
              "message": "private provider detail"}
    service = provider(setup, lambda req: httpx.Response(200, json={"output": output}))
    result = service.poll(f"bailian:{kind}:id")
    assert result.state == "cancelled" and result.error_code == "provider_cancelled"
    assert result.retryable is False and result.progress is None
    assert not result.assets and not result.remote_assets
    assert output["message"] not in result.message
    service.close()


def test_unknown_status_requires_reconciliation_instead_of_releasing_remote_slot(setup):
    service = provider(setup, lambda req: httpx.Response(200, json={"output": {"task_id": "id", "task_status": "UNKNOWN"}}))
    result = service.poll("bailian:video:id")
    assert result.state == "unknown" and result.error_code == "remote_status_unknown"
    assert result.retryable is False and result.progress is None
    assert not result.assets and not result.remote_assets
    assert "未自动重新提交" in result.message
    service.close()


@pytest.mark.parametrize("kind", ["image", "video"])
def test_success_only_returns_private_result_reference_until_collect(setup, kind):
    output = {"task_id": "id", "task_status": "SUCCEEDED"}
    if kind == "image":
        output["choices"] = [{"message": {"content": [{"image": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/file.png"}]}}]
    else:
        output["video_url"] = "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/file.mp4"
    calls = []
    service = provider(setup, lambda req: (calls.append(req) or httpx.Response(200, json={"output": output})))
    result = service.poll(f"bailian:{kind}:id")
    assert result.state == "succeeded" and result.assets == [] and len(result.remote_assets) == 1
    assert len(calls) == 1
    service.close()


@pytest.mark.parametrize("value", ["http://dashscope.aliyuncs.com/api/v1", "https://evil.example/api/v1", "https://dashscope.aliyuncs.com/api/v1?token=secret", "https://secret@dashscope.aliyuncs.com/api/v1", "https://127.0.0.1/api/v1", "https://evil.maas.aliyuncs.com/api/v1"])
def test_base_urls_must_be_exact_official_https_api_roots(value):
    with pytest.raises(ValueError):
        api_base(value)


@pytest.mark.parametrize("value", ["https://dashscope.aliyuncs.com/api/v1", "https://workspace-abc.cn-beijing.maas.aliyuncs.com/api/v1/"])
def test_valid_region_bases(value):
    assert api_base(value) == value.rstrip("/")


@pytest.mark.parametrize("url", ["http://bucket.oss-cn-beijing.aliyuncs.com/f", "https://evil.example/f", "https://127.0.0.1/f", "https://bucket.oss-cn-beijing.aliyuncs.com:8080/f", "https://user:password@bucket.oss-cn-beijing.aliyuncs.com/f"])
def test_unsafe_artifact_addresses_never_make_request(tmp_path, url):
    calls = []
    loader = ArtifactDownloader(tmp_path, 60, resolver=resolver, transport=httpx.MockTransport(lambda req: calls.append(req)))
    with pytest.raises(GenerationError) as error:
        loader.fetch(url, "image")
    assert error.value.code == "unsafe_asset_url" and calls == []


def test_private_dns_answer_rejected_before_connect(tmp_path):
    calls = []
    def private(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
    loader = ArtifactDownloader(tmp_path, 60, resolver=private, transport=httpx.MockTransport(lambda req: calls.append(req)))
    with pytest.raises(GenerationError):
        loader.fetch("https://bucket.oss-cn-beijing.aliyuncs.com/file", "image")
    assert not calls


def test_download_pins_validated_ip_keeps_tls_sni_and_omits_credentials(setup):
    seen = []
    def download(req):
        seen.append(req)
        return httpx.Response(200, content=b"actual-bytes")
    service = provider(setup, lambda req: None, download_transport=httpx.MockTransport(download))
    result = ProviderResult("succeeded", remote_assets=[{"url": "https://bucket.oss-cn-beijing.aliyuncs.com/file?signature=temporary", "kind": "image", "mime_type": "image/png"}])
    assets = service.collect("bailian:image:id", result)
    assert assets[0].data == b"" and assets[0].path.read_bytes() == b"actual-bytes"
    assert seen[0].url.host == PUBLIC_IP and seen[0].headers["host"] == "bucket.oss-cn-beijing.aliyuncs.com"
    assert seen[0].extensions["sni_hostname"] == "bucket.oss-cn-beijing.aliyuncs.com"
    assert "authorization" not in seen[0].headers and "cookie" not in seen[0].headers
    assets[0].path.unlink()
    service.close()


def test_redirect_to_private_or_nonofficial_target_is_rejected(tmp_path):
    calls = []
    def download(req):
        calls.append(req)
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
    loader = ArtifactDownloader(tmp_path, 60, resolver=resolver, transport=httpx.MockTransport(download))
    with pytest.raises(GenerationError):
        loader.fetch("https://bucket.oss-cn-beijing.aliyuncs.com/file", "video")
    assert len(calls) == 1 and not list(tmp_path.iterdir())


def test_download_read_error_removes_partial_file(tmp_path):
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("secret temporary URL must not escape")
    loader = ArtifactDownloader(tmp_path, 60, resolver=resolver, transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Broken())))
    with pytest.raises(GenerationError) as error:
        loader.fetch("https://bucket.oss-cn-beijing.aliyuncs.com/file", "video")
    assert "secret" not in str(error.value) and not list(tmp_path.iterdir())


@pytest.mark.parametrize("status,code", [(429, "rate_limited"), (503, "provider_unavailable"), (401, "provider_authentication_failed"), (403, "provider_access_denied")])
def test_api_errors_are_sanitized_and_post_is_never_retried(setup, status, code):
    calls = []
    service = provider(setup, lambda req: (calls.append(req) or httpx.Response(status, headers={"retry-after": "12"}, json={"code": "bad", "message": KEY})))
    with pytest.raises(GenerationError) as error:
        service.submit(request(), idempotency_key="local-id")
    assert error.value.code == code and KEY not in str(error.value) and len(calls) == 1
    if status == 429:
        assert error.value.retry_after == 12
    service.close()


def test_api_timeout_is_sanitized_and_not_retried(setup):
    calls = []
    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout(KEY)
    service = provider(setup, handler)
    with pytest.raises(GenerationError) as error:
        service.submit(request(), idempotency_key="local-id")
    assert error.value.code == "provider_timeout" and KEY not in str(error.value) and len(calls) == 1
    service.close()


def test_content_rejection_is_not_retryable(setup):
    service = provider(setup, lambda req: httpx.Response(200, json={"output": {"task_status": "FAILED", "code": "DataInspectionFailed", "message": KEY}}))
    result = service.poll("bailian:image:id")
    assert result.error_code == "content_rejected" and not result.retryable and KEY not in result.message
    service.close()


@pytest.mark.parametrize("body", [{"output": {"task_id": "../../private"}}, {"output": {}}, ["not", "object"]])
def test_invalid_acceptance_response_is_never_treated_as_started(setup, body):
    service = provider(setup, lambda req: httpx.Response(200, json=body))
    with pytest.raises(GenerationError) as error:
        service.submit(request(), idempotency_key="local-id")
    assert error.value.code == "provider_response_invalid"
    service.close()


def test_remote_task_identity_mismatch_is_rejected(setup):
    service = provider(setup, lambda req: httpx.Response(200, json={"output": {"task_id": "other", "task_status": "RUNNING"}}))
    with pytest.raises(GenerationError):
        service.poll("bailian:video:id")
    service.close()


def test_cancel_uses_official_task_endpoint(setup):
    calls = []
    service = provider(setup, lambda req: (calls.append(req) or httpx.Response(200, json={"output": {"task_id": "id", "task_status": "CANCELED"}})))
    service.cancel("bailian:video:id")
    assert calls[0].method == "POST" and calls[0].url.path == "/api/v1/tasks/id/cancel"
    service.close()


def test_exact_size_preserves_portrait_and_square():
    assert image_size({"resolution": "1080p", "aspect_ratio": "9:16"}) == (1080, 1920)
    assert image_size({"resolution": "720p", "aspect_ratio": "1:1"}) == (720, 720)


class DripStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        while True:
            await asyncio.sleep(0.005)
            yield b" "  # Each read succeeds; only total deadline stops it.

    async def aclose(self):
        self.closed = True


def test_api_total_deadline_stops_continuous_drip_and_closes_stream(setup):
    config, reader, _, directory = setup
    stream = DripStream()
    service = CloudGenerationProvider(replace(config, request_timeout_seconds=0.05), reader, directory,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=stream)), resolver=resolver)
    started = time.monotonic()
    with pytest.raises(GenerationError) as error:
        service.submit(request(), idempotency_key="local-id")
    assert error.value.code == "provider_timeout" and time.monotonic() - started < 0.5
    assert stream.closed
    service.close()


def test_download_total_deadline_stops_sub_megabyte_drip_and_removes_file(tmp_path):
    stream = DripStream()
    loader = ArtifactDownloader(tmp_path, 1, resolver=resolver,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=stream)))
    loader.timeout = 0.05  # Test-only fast wall budget, no sleeping for a minute.
    started = time.monotonic()
    with pytest.raises(GenerationError) as error:
        loader.fetch("https://bucket.oss-cn-beijing.aliyuncs.com/file", "video")
    assert error.value.code == "provider_timeout" and time.monotonic() - started < 0.5
    assert stream.closed and not list(tmp_path.iterdir())


def test_resolver_subprocess_receives_no_keys_and_has_kill_timeout(tmp_path, monkeypatch):
    from app.generation.cloud.transport import PublicResolver
    observed = []
    def run(args, **kwargs):
        observed.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
    monkeypatch.setenv("STORYBOARD_BAILIAN_API_KEY", KEY)
    monkeypatch.setattr("app.generation.cloud.transport.subprocess.run", run)
    with pytest.raises(GenerationError) as error:
        PublicResolver().resolve("dashscope.aliyuncs.com", 0.1)
    assert error.value.code == "provider_timeout"
    args, kwargs = observed[0]
    assert args[-1] == "dashscope.aliyuncs.com" and "-I" in args
    assert kwargs["timeout"] == 0.1 and KEY not in str(kwargs["env"])
    assert set(kwargs["env"]) == {"PATH", "LANG"}


def test_cancel_accepts_legacy_request_id_string_only_for_cancel(setup):
    service = provider(setup, lambda req: httpx.Response(200, json="request-id"))
    service.cancel("bailian:image:id")
    with pytest.raises(GenerationError):
        service.poll("bailian:image:id")
    service.close()


@pytest.mark.parametrize("raw,expected", [
    ("Throttling.ServiceOverloaded", "provider_capacity_exhausted"),
    ("Throttling.ResourceExhausted", "provider_capacity_exhausted"),
    ("InsufficientResources", "provider_capacity_exhausted"),
    ("Arrearage", "provider_balance_insufficient"),
    ("InsufficientBalance", "provider_balance_insufficient"),
    ("PrepaidBillOverdue", "provider_balance_insufficient"),
    ("AllocationQuota.FreeTierOnly", "provider_quota_exceeded"),
    ("BudgetLimitExceeded", "provider_quota_exceeded"),
    ("Throttling.RateQuota", "rate_limited"),
    ("Throttling.AllocationQuota", "rate_limited"),
    ("Throttling.Concurrency", "rate_limited"),
    ("insufficient_quota", "rate_limited"),
    ("ResourceExhausted", "rate_limited"),
    ("InvalidApiKey", "provider_authentication_failed"),
    ("AccessDenied", "provider_access_denied"),
    ("CommodityNotPurchased", "provider_access_denied"),
    ("DataInspectionFailed", "content_rejected"),
    ("InvalidParameter.DataInspection", "content_rejected"),
    ("InvalidParameter", "invalid_generation_inputs"),
    ("InvalidParameter.UnsupportedFormat", "invalid_generation_inputs"),
    ("InvalidFile.Resolution", "invalid_generation_inputs"),
    ("InternalError.Algo.InvalidParameter", "invalid_generation_inputs"),
    ("GatewayTimeout.InputDownload", "provider_timeout"),
    ("ServiceUnavailable", "provider_unavailable"),
    ("ModelUnavailable", "provider_unavailable"),
    ("InternalError", "provider_unavailable"),
    ("unrecognized", "provider_failed"),
])
def test_official_codes_have_precise_categories_without_message_guessing(raw, expected):
    assert error_code({"code": raw, "message": "insufficient capacity " + KEY}) == expected


@pytest.mark.parametrize("payload", [{}, {"message": "InsufficientResources"}, {"code": None},
    {"code": ["InsufficientResources"]}, {"code": "InsufficientResources " + KEY}])
def test_unknown_codes_never_invent_capacity_shortage(payload):
    assert error_code(payload) == "provider_failed"


@pytest.mark.parametrize("status,raw,expected", [
    (403, "Arrearage", "provider_balance_insufficient"),
    (429, "AllocationQuota.FreeTierOnly", "provider_quota_exceeded"),
    (429, "Throttling.ServiceOverloaded", "provider_capacity_exhausted"),
    (503, "Throttling.ResourceExhausted", "provider_capacity_exhausted"),
    (400, "InvalidParameter", "invalid_generation_inputs"),
    (504, "GatewayTimeout.InputDownload", "provider_timeout"),
])
def test_structured_code_precedes_http_fallback_without_raw_message_leak(setup, status, raw, expected):
    calls = []
    service = provider(setup, lambda req: (calls.append(req) or httpx.Response(status,
        json={"code": raw, "message": KEY + " https://private.example?token=fixture"})))
    with pytest.raises(GenerationError) as error:
        service.submit(request(), idempotency_key="classified-error")
    assert error.value.code == expected and error.value.status_code == status
    assert KEY not in str(error.value) and "private.example" not in str(error.value)
    assert len(calls) == 1  # No automatic repeat of a possibly paid POST.
    service.close()


@pytest.mark.parametrize("raw,expected,retryable", [
    ("Throttling.ResourceExhausted", "provider_capacity_exhausted", True),
    ("Throttling.RateQuota", "rate_limited", True),
    ("Arrearage", "provider_balance_insufficient", False),
    ("AllocationQuota.FreeTierOnly", "provider_quota_exceeded", False),
    ("InvalidApiKey", "provider_authentication_failed", False),
    ("DataInspectionFailed", "content_rejected", False),
    ("InvalidParameter", "invalid_generation_inputs", False),
])
def test_failed_task_keeps_exact_public_reason_and_retry_policy(setup, raw, expected, retryable):
    service = provider(setup, lambda req: httpx.Response(200, json={"output": {
        "task_status": "FAILED", "code": raw, "message": KEY}}))
    result = service.poll("bailian:video:id")
    assert result.error_code == expected and result.retryable is retryable
    assert KEY not in result.message and result.progress is None and not result.assets
    service.close()


def test_failed_poll_can_read_top_level_structured_error_without_leaking_message(setup):
    service = provider(setup, lambda req: httpx.Response(200, json={"code": "Arrearage",
        "message": KEY, "output": {"task_status": "FAILED"}}))
    result = service.poll("bailian:image:id")
    assert result.error_code == "provider_balance_insufficient" and not result.retryable
    assert KEY not in result.message
    service.close()


@pytest.mark.parametrize("status,expected", [(403, "provider_access_denied"), (429, "rate_limited"), (503, "provider_unavailable")])
def test_non_json_error_uses_http_category_instead_of_claiming_bad_task_protocol(setup, status, expected):
    service = provider(setup, lambda req: httpx.Response(status, text=KEY))
    with pytest.raises(GenerationError) as error:
        service.submit(request(), idempotency_key="non-json-error")
    assert error.value.code == expected and KEY not in str(error.value)
    service.close()


def test_network_failure_is_distinct_from_cloud_service_rejection(setup):
    service = provider(setup, lambda req: (_ for _ in ()).throw(httpx.ConnectError(KEY)))
    with pytest.raises(GenerationError) as error:
        service.submit(request(), idempotency_key="network-error")
    assert error.value.code == "provider_network_error" and KEY not in str(error.value)
    service.close()
