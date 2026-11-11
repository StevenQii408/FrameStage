import json
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from app.config import load_settings
from app.db import Database
from app.events import EventBus
from app.providers.bailian import BailianVisionProvider, estimate_list_price
from app.providers.base import FrameInput, ShotContext
from app.providers.cloud_errors import CloudTransportError
from app.services.job_service import JobService
from app.style_direct.models import StyleEvidenceV3


def test_multimodal_schema_is_guidance_and_locally_enforced(tmp_path):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return response({'claims': [], 'gaps': [], 'uncertainties': ['图像不可观察']})
    provider = BailianVisionProvider(settings(tmp_path), httpx.MockTransport(handler))
    provider.analyze_style('返回 JSON 对象', [frame(tmp_path, 1, 1)], phase='style.direct.evidence',
                           schema=StyleEvidenceV3, timeout_seconds=5, max_tokens=100)
    assert requests[0]['response_format'] == {'type': 'json_object'}
    assert '输出 JSON Schema' in requests[0]['messages'][0]['content'][0]['text']
    assert provider.usage[0]['schema_enforcement'] == 'local-multimodal'
    provider._call('返回 JSON 对象', [], 'text.schema', schema=StyleEvidenceV3)
    assert requests[1]['response_format']['type'] == 'json_schema'


def test_invalid_cloud_cut_group_rejected_not_cached_as_singletons(tmp_path, monkeypatch):
    provider = BailianVisionProvider(settings(tmp_path), httpx.MockTransport(lambda request: response({'groups': [[1, 1]]})))
    frames = [frame(tmp_path, i, i) for i in (1, 2)]
    monkeypatch.setattr(provider.helper, '_contact_sheets', lambda window, path: [frames[0].path])
    import pytest
    with pytest.raises(ValueError, match='完整覆盖'):
        provider.group_raw_shots(frames, [])


def settings(tmp_path):
    return replace(
        load_settings(), provider="bailian", data_dir=tmp_path / "data",
        cloud_model="qwen3.8-flash", cloud_api_key="test-secret", perception_v2_enabled=False,
        cloud_base_url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )


def frame(tmp_path, number, timestamp):
    path = tmp_path / f"frame-{number}.jpg"
    Image.new("RGB", (48, 36), (number * 20, 30, 40)).save(path)
    return FrameInput(f"shot-001-frame-{number}", timestamp, str(path))


def response(payload):
    return httpx.Response(
        200, json={"id": "req-1", "model": "qwen3.8-flash",
                   "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
                   "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}},
    )


def test_cloud_uses_separate_short_connect_timeout(tmp_path):
    captured = []
    def handler(request):
        captured.append(request.extensions["timeout"])
        return response({"ok": True})
    cfg = replace(settings(tmp_path), cloud_connect_timeout_seconds=8,
                  cloud_read_timeout_seconds=75)
    provider = BailianVisionProvider(cfg, httpx.MockTransport(handler))
    try:
        provider._call("JSON", [], "shot.static", timeout_seconds=75, max_attempts=1)
        assert captured[0]["connect"] == 8
        assert captured[0]["read"] <= 75
        assert captured[0]["write"] == 20
        assert captured[0]["pool"] == 5
        provider._call("JSON", [], "event_agent.initial", timeout_seconds=2, max_attempts=1)
        assert all(value <= 2 for value in captured[1].values())
    finally:
        provider.unload()


@pytest.mark.parametrize("error,category", [
    (httpx.ConnectTimeout, "connect_timeout"), (httpx.ReadTimeout, "read_timeout"),
    (httpx.WriteTimeout, "write_timeout"), (httpx.PoolTimeout, "pool_timeout"),
    (httpx.ConnectError, "network_connection"),
])
def test_transport_errors_are_typed_and_redacted(tmp_path, error, category):
    def handler(request):
        raise error("https://user:private-password@proxy.example.test unreachable", request=request)
    provider = BailianVisionProvider(settings(tmp_path), httpx.MockTransport(handler))
    try:
        with pytest.raises(CloudTransportError) as caught:
            provider._call("JSON", [], "test", max_attempts=1)
        assert caught.value.category == category
        assert "private-password" not in str(caught.value)
        assert provider.usage[0]["transport_error_category"] == category
        assert "private-password" not in json.dumps(provider.usage)
    finally:
        provider.unload()


def test_cloud_two_stages_keep_order_evidence_and_usage(tmp_path):
    requests = []
    static = {
        "primary_subject": "男孩和杯子", "shot_size": "近景", "lens": "标准",
        "lighting_vibe": "窗边暖光", "composition": "男孩居左", "confidence": 0.84,
        "visual_style_evidence": {
            "color_palette": "暖黄与蓝", "texture": "平滑动画质感",
            "visible_effects": ["Frame 1：杯内可见粉色光点"],
            "evidence": ["Frame 1：窗外偏蓝"], "uncertain_techniques": ["具体摄影机型号"],
        },
    }
    motion = {
        "subject_action": "男孩把粉色糖粒放入杯中，液体变粉",
        "camera_movement_type": "固定", "camera_movement_detail": "背景位置稳定",
        "primary_subject": "男孩", "initial_state": "手持糖粒",
        "action_steps": ["男孩放入糖粒", "杯中液体逐渐变粉"],
        "final_state": "杯中液体呈粉色", "visible_evidence": ["Frame 1：手在杯旁", "Frame 4：液体呈粉色"],
        "confidence": 0.82,
    }
    def handler(request):
        assert request.headers["authorization"] == "Bearer test-secret"
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        return response(static if len(requests) == 1 else motion)

    provider = BailianVisionProvider(settings(tmp_path), httpx.MockTransport(handler))
    frames = [frame(tmp_path, n, float(n)) for n in (4, 1, 3, 2)]
    context = ShotContext("s1", 1, 0.0, 5.0, "")
    observation = provider.analyze_shot(frames, context)
    assert observation.shot_size == "近景"
    assert observation.action_details.steps == ["男孩放入糖粒", "杯中液体逐渐变粉"]
    assert observation.visual_style_evidence.color_palette == "暖黄与蓝"
    assert len(requests) == 2
    assert [part["text"].split("|")[0].strip() for part in requests[1]["messages"][0]["content"] if part["type"] == "text"][1:] == ["Frame 1", "Frame 2", "Frame 3", "Frame 4"]
    assert requests[0]["response_format"] == {"type": "json_object"}
    usage = provider.drain_usage()
    assert len(usage) == 2 and usage[0]["prompt_tokens"] == 1000
    assert usage[0]["estimated_list_price_usd"] == estimate_list_price("qwen3.8-flash", 1000, 200)
    assert provider.drain_usage() == []
    provider.unload()


def test_cloud_stage_failure_preserves_other_stage(tmp_path):
    def handler(request):
        body = json.loads(request.content)
        if len([part for part in body["messages"][0]["content"] if part["type"] == "image_url"]) == 1:
            return response({"shot_size": "超广角"})
        return response({
            "subject_action": "城市建筑随航拍前进逐渐变大", "camera_movement_type": "航拍",
            "camera_movement_detail": "建筑尺度增大", "confidence": 0.8,
        })
    provider = BailianVisionProvider(settings(tmp_path), httpx.MockTransport(handler))
    observation = provider.analyze_shot([frame(tmp_path, n, float(n)) for n in (1, 2, 3, 4)], ShotContext("s1", 1, 0, 5, ""))
    assert observation.subject_action.startswith("城市")
    assert observation.shot_size == "未知"
    assert observation.confidence <= 0.3
    assert observation.analysis_warnings
    provider.unload()


def test_cloud_skips_local_memory_gate_and_rejects_old_provider_resume(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    db = Database(tmp_path / "jobs.sqlite3")
    db.init()
    service = JobService(cfg, db, EventBus())
    monkeypatch.setattr(service.memory, "snapshot", lambda: {"available_gb": 0.5})
    assert service._can_start_provider_inference(service.provider.health())
    with service._inference_slot():
        pass
    db.insert_job({
        "id": "old", "status": "paused_memory_pressure", "stage": "analyzing", "progress": 30,
        "input_type": "video", "script_text": "", "provider": "mlx", "model": "old-model",
        "config": service.config().model_dump(), "created_at": "now", "updated_at": "now",
    })
    assert service.resume("old") is False
    assert db.fetchone("SELECT provider,status FROM jobs WHERE id='old'")["status"] == "paused_memory_pressure"
    service.provider.unload()
    service.executor.shutdown(wait=True)


def test_montage_requires_adjacent_pairs_and_scene_evidence(tmp_path):
    provider = BailianVisionProvider(settings(tmp_path))
    ids = ["shot-001", "shot-002"]
    shots = [{"id": item, "number": n, "start_time": n-1, "end_time": n, "observation": {}} for n, item in enumerate(ids, 1)]
    frames = {item: [frame(tmp_path, n, float(n))] for n, item in enumerate(ids, 1)}
    provider._call = lambda *_args, **_kwargs: json.dumps({"relations": [{
        "left_shot_id": ids[0], "right_shot_id": ids[1],
        "observed_edit_fact": "室内切到室外", "relation_type": "场景转换",
        "possible_narrative_intent": "可能为时间省略",
        "evidence": ["shot-001 1.0s 室内", "shot-002 2.0s 室外"],
        "confidence": 0.8, "scene_change": True,
    }]}, ensure_ascii=False)
    result = provider.analyze_montage(shots, frames)
    assert result["scene_starts"] == [1, 2]
    assert result["relations"][0]["observed_edit_fact"] == "室内切到室外"
    provider.unload()


def test_cloud_comparison_requires_local_key(tmp_path, monkeypatch):
    import pytest
    from evals.compare_cloud import DEFAULT_MODELS, compare

    assert DEFAULT_MODELS == ("qwen3.8-flash",)
    monkeypatch.delenv("STORYBOARD_BAILIAN_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="STORYBOARD_BAILIAN_API_KEY"):
        compare(tmp_path / "manifest.json", tmp_path, tmp_path / "out", list(DEFAULT_MODELS))


def test_quality_manifest_accepts_evidence_and_rejects_nonadjacent_montage(tmp_path):
    import pytest
    from evals.manifest import ManifestError, load_manifest

    label = lambda value: {"observable": True, "value": value}
    labels = {
        "subject_action": label("男孩拿杯"), "shot_size": label("近景"),
        "lens": label("标准"), "camera_movement": label("固定"),
        "lighting_vibe": label("暖光"),
    }
    payload = {"schema_version": "storyboard-eval.v1", "corpus_id": "test-cloud",
               "clips": [{"id": "clip-test", "file": "clip.mp4", "sha256": "0" * 64,
                          "shots": [
                              {"number": 1, "start_time": 0, "end_time": 1, "transition_in": "start",
                               "labels": labels, "quality_labels": {
                                   "objects": ["男孩", "杯子"], "scene": "厨房",
                                   "artistic_effects": [{"description": "杯内粉色光点", "evidence_frames": [1, 3]}]}},
                              {"number": 2, "start_time": 1, "end_time": 2, "transition_in": "hard_cut", "labels": labels}],
                          "montage_labels": [{"left_shot": 1, "right_shot": 2, "relation_type": "连续动作",
                                              "scene_change": False, "evidence": ["两镜杯子相同"]}]}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert load_manifest(path)["clips"][0]["shots"][0]["quality_labels"]["objects"] == ["男孩", "杯子"]
    payload["clips"][0]["montage_labels"][0]["right_shot"] = 3
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ManifestError, match="相邻镜头"):
        load_manifest(path)


def test_vl_snapshot_uses_max_tokens_and_script_has_no_images(tmp_path):
    from app.schemas import ScriptBeat
    from app.services.script_processor import fallback_window_plan

    requests = []
    beats = [ScriptBeat(id="beat-001", text="男孩拿起杯子")]
    target = ["beat-001"]
    planned = fallback_window_plan(beats, target, 120)
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return response(planned.model_dump())
    cfg = replace(settings(tmp_path), cloud_model="qwen3-vl-plus-2025-12-19")
    provider = BailianVisionProvider(cfg, httpx.MockTransport(handler))
    result = provider.plan_script(beats, target)
    assert result.plan.shots[0].source_beat_ids == target
    assert "max_tokens" in requests[0] and "max_completion_tokens" not in requests[0]
    assert all(part["type"] == "text" for part in requests[0]["messages"][0]["content"])
    provider.unload()


def test_short_video_probe_is_explicit_and_size_limited(tmp_path):
    from evals.video_probe import run_probe
    import pytest

    clip = tmp_path / "short.mp4"
    clip.write_bytes(b"short-video-fixture")
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return response({"subject": "男孩", "movement": "向右"})
    provider = BailianVisionProvider(settings(tmp_path), httpx.MockTransport(handler))
    payload = provider._call("只分析可见运动", [], "video.short_clip_probe", video_clip_path=clip)
    assert json.loads(payload)["subject"] == "男孩"
    content = requests[0]["messages"][0]["content"]
    assert [part["type"] for part in content] == ["text", "video_url"]
    assert content[1]["video_url"]["url"].startswith("data:video/mp4;base64,")
    with pytest.raises(ValueError, match="0–4.8"):
        run_probe(clip, 0, 5.1, settings(tmp_path).cloud_model)
    provider.unload()


def test_cloud_eval_records_montage_without_a_real_api_key(tmp_path, monkeypatch):
    import hashlib
    import shutil
    import subprocess
    import pytest
    from app.schemas import ProviderHealth, VisionObservation
    from evals import run as eval_run

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    video = tmp_path / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=160x90:rate=12",
        "-t", "2", "-pix_fmt", "yuv420p", "-y", str(video),
    ], check=True)
    labels = {
        "subject_action": {"observable": True, "value": "主体沿道路缓慢移动", "keywords": ["移动"]},
        "shot_size": {"observable": True, "value": "全景"},
        "lens": {"observable": True, "value": "广角"},
        "camera_movement": {"observable": True, "value": "固定"},
        "lighting_vibe": {"observable": True, "value": "自然光"},
    }
    manifest = {"schema_version": "storyboard-eval.v1", "corpus_id": "cloud-mock",
                "clips": [{"id": "clip-mock", "file": "clip.mp4",
                           "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                           "shots": [
                               {"number": 1, "start_time": 0, "end_time": 1, "transition_in": "start", "labels": labels},
                               {"number": 2, "start_time": 1, "end_time": 2, "transition_in": "hard_cut", "labels": labels}],
                           "montage_labels": [{"left_shot": 1, "right_shot": 2,
                                               "relation_type": "连续动作", "scene_change": False,
                                               "evidence": ["主体连续"]}]}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    class FakeCloud:
        def health(self):
            return ProviderHealth(provider="bailian", model="fake", ready=True)
        def analyze_shot(self, frames, context):
            return VisionObservation(subject_action="主体移动", shot_size="全景", lens="广角",
                                     camera_movement="固定", lighting_vibe="自然光", confidence=0.8)
        def analyze_montage(self, shots, frames):
            return {"relations": [{"left_shot_id": shots[0]["id"], "right_shot_id": shots[1]["id"],
                                   "observed_edit_fact": "主体连续", "relation_type": "连续动作",
                                   "possible_narrative_intent": "未知", "evidence": ["两镜主体连续"],
                                   "confidence": 0.8, "scene_change": False}],
                    "raw_responses": ["fixture"], "scene_starts": [1]}
        def drain_usage(self):
            return []
        def unload(self):
            pass

    monkeypatch.setattr(eval_run, "_provider", lambda name, cfg: FakeCloud())
    report = eval_run.run_benchmark(path, tmp_path, tmp_path / "out", "bailian", "fake")
    assert report["clips"][0]["montage_metrics"]["label_scores"][0]["relation_correct"]
    assert report["summary"]["montage"]["relation_accuracy"] == 1.0
    assert report["summary"]["montage"]["scene_change_accuracy"] == 1.0
