import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.providers.bailian import BailianVisionProvider
from app.script_director.document import parse_script_document
from app.script_director.engine import DirectorWindow, conservative_plan, director_windows
from app.script_director.plan_models import DirectorDecision
from app.services.prompt_service import build_prompts
from app.schemas import ProviderHealth, VisionObservation
from test_bailian import response, settings
from test_script_document import RAIN_SCRIPT


def wait_done(client, job_id):
    for _ in range(150):
        state = client.get(f"/api/jobs/{job_id}").json()
        if state["status"] in ("completed", "failed", "cancelled"):
            return state
        time.sleep(.02)
    raise AssertionError("script task did not complete")


def configure(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_SCRIPT_STRATEGY", "director-v3")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))


def test_rain_script_pipeline_preserves_profiles_dialogue_sounds_and_two_prompts(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", data={"input_type": "script", "script_text": RAIN_SCRIPT}).json()["job_id"]
        final = wait_done(client, job_id)
        assert final["status"] == "completed", final
        result = client.get(f"/api/jobs/{job_id}/result").json()
        assert result["frames"] == []
        assert result["job"]["segmentation_profile"] == "script-v3-rules"
        assert result["job"]["config"]["script_strategy"] == "director-v3"
        assert result["sequence_metadata"]["event_grouping_policy"] == "script-director-v3"
        assert len(result["prompts"]) == len(result["shots"]) * 2
        assert all(shot["timing_origin"] == "script-performance" and shot["script_direction"] for shot in result["shots"])
        assert all("【人物】" not in shot["action"] and "40岁" not in shot["action"] and shot["action"] != "”" for shot in result["shots"])
        assert all(0 < shot["duration"] <= 12 for shot in result["shots"])
        assert any(shot["duration"] > 5 for shot in result["shots"])
        assert all(prompt["contract"]["generation_duration_seconds"] == next(s["duration"] for s in result["shots"] if s["id"] == prompt["shot_id"]) for prompt in result["prompts"])
        assert not any("无台词" in prompt["positive_prompt"] or "无人声" in prompt["positive_prompt"] for prompt in result["prompts"])
        assert not any("。。" in prompt["positive_prompt"] for prompt in result["prompts"])
        expected = [span for span in parse_script_document(RAIN_SCRIPT).spans if span.role == "dialogue"]
        actual = [part for shot in result["shots"] for part in shot["script_direction"]["dialogue"]]
        assert [part["text"] for part in actual] == [span.text for span in expected]
        assert [part["speaker_name"] for part in actual] == [span.speaker for span in expected]
        assert not any("被击毙" in prompt["positive_prompt"] or "死亡" in prompt["positive_prompt"] for prompt in result["prompts"])
        assert len(result["metadata"]["script_planning"]["document"]["characters"]) == 2
        events = client.get(f"/api/jobs/{job_id}/events").text
        assert "script.document_ready" in events and "script.scene_ready" in events
        assert events.index("prompt.ready") < events.rindex("script.window_ready")
        assert client.get(f"/api/jobs/{job_id}/export").status_code == 200


def test_metadata_only_does_not_fabricate_shots(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", data={"input_type": "script", "script_text": "【人物】\n程远（40岁，刑警）"}).json()["job_id"]
        final = wait_done(client, job_id)
        assert final["status"] == "failed" and "只有人物设定" in final["error"]
        assert client.get(f"/api/jobs/{job_id}/result").json()["shots"] == []


def test_first_scene_is_usable_while_second_waits_and_cancel_keeps_only_published_shots(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    blocked, release = threading.Event(), threading.Event()
    with TestClient(app) as client:
        provider = client.app.state.job_service.provider
        original = provider.plan_script_director

        def delayed(prompt, **kwargs):
            if '"scene_id": "scene_0002"' in prompt:
                blocked.set()
                release.wait(5)
            return original(prompt, **kwargs)

        provider.plan_script_director = delayed
        job_id = client.post("/api/jobs", data={"input_type": "script", "script_text": "内景：客厅 日\n女孩推门进入。\n外景：街道 夜\n汽车驶过。"}).json()["job_id"]
        try:
            assert blocked.wait(3)
            partial = client.get(f"/api/jobs/{job_id}/result").json()
            assert partial["job"]["status"] == "processing"
            assert len(partial["shots"]) == 1 and len(partial["prompts"]) == 2
            assert partial["shots"][0]["scene_group_id"] == "scene_0001"
            client.post(f"/api/jobs/{job_id}/cancel")
        finally:
            release.set()
        final = wait_done(client, job_id)
        assert final["status"] == "cancelled"
        assert len(client.get(f"/api/jobs/{job_id}/result").json()["shots"]) == 1


def test_style_rerender_keeps_script_dialogue_and_duration(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", data={"input_type": "script", "script_text": "程远：你先离开，我留在这里等你回来。"}).json()["job_id"]
        assert wait_done(client, job_id)["status"] == "completed"
        service = client.app.state.job_service
        shot = service.db.fetchone("SELECT * FROM shots WHERE job_id=?", (job_id,))
        observation = VisionObservation.model_validate_json(shot["observation_json"])
        prompts = build_prompts(shot["id"], observation, generation_duration_seconds=shot["generation_duration_seconds"])
        assert "你先离开，我留在这里等你回来。" in prompts[1].positive_prompt
        assert "无台词" not in prompts[1].positive_prompt
        assert prompts[1].contract.generation_duration_seconds == shot["duration"]


def test_cloud_director_is_flash_text_only_schema_and_single_attempt(tmp_path):
    requests = []
    document = parse_script_document("程远：走！")
    final = conservative_plan(document, director_windows(document)[0])
    def handler(request):
        requests.append(json.loads(request.content))
        return response(final.model_dump())
    provider = BailianVisionProvider(settings(tmp_path), httpx.MockTransport(handler))
    try:
        raw = provider.plan_script_director("导演结构测试", schema=DirectorDecision, max_tokens=8192)
        assert DirectorDecision.model_validate_json(raw).action == "finalize"
        assert len(requests) == 1
        body = requests[0]
        assert body["model"] == "qwen3.8-flash" and body["enable_thinking"] is False
        assert body["temperature"] == 0
        assert body["response_format"]["type"] == "json_schema"
        assert [item["type"] for item in body["messages"][0]["content"]] == ["text"]
        assert provider.usage[0]["phase"] == "script_director.plan"
    finally:
        provider.unload()


@pytest.mark.parametrize("label", ["整体画面风格", "【整体画面风格】", "风格"])
def test_explicit_style_is_not_dialogue_and_script_health_identifies_new_strategy(tmp_path, monkeypatch, label):
    configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        health = client.get("/api/health").json()["configuration"]
        assert health["script_strategy"] == "director-v3" and health["script_director_version"] == "script-director.v3"
        job_id = client.post("/api/jobs", data={"input_type": "script", "script_text": f"{label}：多层剪纸拼贴动画，纸纤维清晰。\n男孩递出杯子。"}).json()["job_id"]
        assert wait_done(client, job_id)["status"] == "completed"
        result = client.get(f"/api/jobs/{job_id}/result").json()
        assert len(result["shots"]) == 1
        assert result["shots"][0]["script_direction"]["dialogue"] == []
        assert not result["metadata"]["script_planning"]["document"]["characters"]
        assert all("整体画面风格：多层剪纸拼贴动画，纸纤维清晰。" in prompt["positive_prompt"] for prompt in result["prompts"])
        assert not any("未指定画风" in item for item in result["shots"][0]["script_direction"]["creative_assumptions"])


def test_same_scene_continuation_keeps_event_identity_source_audit_and_published_shots(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    text = "内景：客厅 日\n" + "".join(f"男孩将第{number}只杯子放到桌上。" for number in range(1, 14))
    document = parse_script_document(text)
    blocked, release = threading.Event(), threading.Event()
    payloads = []
    with TestClient(app) as client:
        provider = client.app.state.job_service.provider
        monkeypatch.setattr(provider, "health", lambda: ProviderHealth(provider="bailian", model="offline-script-test", ready=True))

        def offline_director(prompt, **kwargs):
            payload = json.JSONDecoder().raw_decode(prompt.split("\n输入：\n", 1)[1])[0]
            payloads.append(payload)
            window = DirectorWindow(payload["window_number"], payload["scene_id"], payload["target_span_ids"])
            decision = conservative_plan(document, window)
            shots = [shot for event in decision.events for shot in event.shots]
            event = decision.events[0].model_copy(update={"source_span_ids": list(window.source_span_ids), "shots": shots,
                "title": "摆好桌上的杯子", "goal": "男孩继续完成摆放杯子的同一目标"})
            if payload["window_number"] == 2:
                event.continues_event_id = payload["previous_state"]["open_event"]["id"]
                blocked.set()
                release.wait(5)
            return DirectorDecision(action="finalize", events=[event]).model_dump_json()

        monkeypatch.setattr(provider, "plan_script_director", offline_director)
        job_id = client.post("/api/jobs", data={"input_type": "script", "script_text": text}).json()["job_id"]
        try:
            assert blocked.wait(3)
            partial = client.get(f"/api/jobs/{job_id}/result").json()
            assert partial["job"]["status"] == "processing" and partial["shots"]
            original_shots, original_prompts = partial["shots"], partial["prompts"]
        finally:
            release.set()
        assert wait_done(client, job_id)["status"] == "completed"
        result = client.get(f"/api/jobs/{job_id}/result").json()
        assert result["shots"][:len(original_shots)] == original_shots
        assert result["prompts"][:len(original_prompts)] == original_prompts
        plan = result["metadata"]["script_planning"]
        assert len(plan["events"]) == 1 and plan["model_calls"] == 2
        event = plan["events"][0]
        assert event["window_numbers"] == [1, 2]
        assert event["source_span_ids"] == [span.id for span in document.content_spans]
        assert {shot["script_direction"]["event_id"] for shot in result["shots"]} == {event["id"]}
        stored = json.loads(client.app.state.job_service.db.fetchone("SELECT script_plan_json FROM jobs WHERE id=?", (job_id,))["script_plan_json"])
        assert [audit["event_ids"] for audit in stored["window_audit"]] == [[event["id"]], [event["id"]]]
        assert stored["window_audit"][1]["events"][0]["continues_event_id"] == event["id"]
        assert [sid for audit in stored["window_audit"] for sid in audit["source_span_ids"]] == event["source_span_ids"]
        assert all(sequence["generation_duration_seconds"] <= 12 and len(sequence["source_shot_ids"]) <= 4 for sequence in result["generation_sequences"])
        assert payloads[1]["previous_state"]["open_event"]["window_number"] == 1


def test_pipeline_speaker_attribution_and_pronoun_profiles_keep_exact_source(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    text = "【人物】\n程远（40岁，刑警）\n老K（50岁，线人）\n程远看向老K，老K说：“快走。”\n老K伸出手。他停在半空。"
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", data={"input_type": "script", "script_text": text}).json()["job_id"]
        assert wait_done(client, job_id)["status"] == "completed"
        result = client.get(f"/api/jobs/{job_id}/result").json()
        dialogue = [part for shot in result["shots"] for part in shot["script_direction"]["dialogue"]]
        assert [(part["text"], part["speaker_name"]) for part in dialogue] == [('“快走。”', "老K")]
        source = result["metadata"]["script_planning"]["document"]
        assert source["source_text"] == text and source["attribution_audit"][0]["method"] == "explicit"
        pronoun = next(ref for span in source["spans"] if span["text"] == "他停在半空。" for ref in span["entity_refs"] if ref["kind"] == "pronoun")
        assert pronoun["method"] == "same-scene-context"
        assert pronoun["character_id"] == next(character["id"] for character in source["characters"] if character["name"] == "老K")
