"""Keyless correctness and budget tests; not real visual-accuracy claims."""
import copy
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import load_settings
from app.db import Database, utc_now
from app.events import EventBus
from app.main import app
from app.providers.bailian import BailianVisionProvider
from app.services.job_service import JobService
from app.style_direct.models import EvidenceGap, StyleEvidenceV3, render_profile
from app.style_direct.pipeline import StylePipeline
from app.style_direct.service import apply_style, create_analysis, get_analysis, prepare_image, update_profile
from test_prompt_profiles import observation


def image_bytes(size=(160, 240), color=(105, 120, 135)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def claim(id="C1", dimension="medium_rendering", text="立体体积清晰，表面细节经过动画化简化", images=None):
    return dict(id=id, dimension=dimension, observation=text, images=images or ["I1"], confidence=.85)


def evidence(gaps=None):
    return {"claims": [claim(), claim("C2", "form_language", "曲线轮廓主导，面部结构简化但表情清晰"),
                       claim("C3", "lighting_shading", "阴影边缘宽缓，明暗过渡连续且高光克制")], "gaps": gaps or [], "uncertainties": []}


def interpretation(scope="reference"):
    return {"descriptive_name": "圆润柔光三维视觉", "descriptions": [
        dict(dimension=c["dimension"], core_concept=c["observation"].split("，")[0],
             professional_description=c["observation"], support_claim_ids=[c["id"]], scope=scope)
        for c in evidence()["claims"]], "generation_constraints": [], "uncertainties": []}


class FakeProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def health(self):
        return SimpleNamespace(model="qwen3.8-flash")

    def analyze_style(self, prompt, frames, **options):
        self.calls.append({"prompt": prompt, "frames": frames, **options})
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def pipeline(tmp_path, responses, *, scope="reference", assets=None):
    assets = assets or [prepare_image(image_bytes(), tmp_path / "input", "I1")]
    provider = FakeProvider(responses)
    return StylePipeline(provider, assets, tmp_path / "crops", scope=scope), provider


def test_normal_two_stage_preserves_specific_traits_without_embedding(tmp_path, monkeypatch):
    from app.style_vector.embedding import BailianStyleEmbedder
    monkeypatch.setattr(BailianStyleEmbedder, "__init__", Mock(side_effect=AssertionError("embedding forbidden")))
    p, provider = pipeline(tmp_path, [evidence(), interpretation()])
    result = p.run()
    assert len(provider.calls) == 2
    assert not result["tools"]
    assert "阴影边缘宽缓" in result["profile"]["rendered_prompt"]
    assert result["profile"]["application_status"] == "ready"
    assert result["profile"]["generation_constraints"] == []
    assert "motion_signature" not in {c["dimension"] for c in result["evidence"]["claims"]}


def test_unknown_name_does_not_erase_known_features(tmp_path):
    b = interpretation(); b["descriptive_name"] = "参考画风"
    p, _ = pipeline(tmp_path, [evidence(), b])
    result = p.run()
    assert "曲线轮廓主导" in result["profile"]["rendered_prompt"]
    assert "项目画风待确认" not in result["profile"]["rendered_prompt"]


def test_empty_shell_is_corrected_once_but_unknown_with_reason_is_not(tmp_path):
    p, provider = pipeline(tmp_path, ["[]", evidence(), interpretation()])
    result = p.run()
    assert len(provider.calls) == 3 and result["budget"]["correction_used"]
    assert result["profile"]["descriptions"]
    p, provider = pipeline(tmp_path / "unknown", [{"claims": [], "uncertainties": ["图像完全遮挡，无法观察"]}])
    assert not p.run()["profile"]["descriptions"] and len(provider.calls) == 1


def test_specific_hair_and_clothing_do_not_enter_style(tmp_path):
    a = evidence()
    a["claims"].append(claim("bad", "texture_material", "头发卷曲，衣物材质柔软"))
    p, _ = pipeline(tmp_path, [a, interpretation()])
    result = p.run()
    assert "衣物" not in result["profile"]["rendered_prompt"]
    assert "卷曲" not in result["profile"]["rendered_prompt"]
    assert result["profile"]["application_status"] == "partial"


def test_one_native_acquisition_round_and_two_crops_max(tmp_path):
    gap = dict(dimension="texture_material", question="颗粒是否沿笔触分布？", image="I1",
               tool="compare_regions", regions=[[0, 0, .5, .5], [.5, .5, 1, 1]], expected_evidence="核对局部颗粒方向")
    a = evidence([gap, gap])
    supplemental = {"claims": [claim("S1", "texture_material", "表面颗粒沿笔触方向连续分布", ["I2", "I3"])], "gaps": [gap], "uncertainties": []}
    b = interpretation()
    b["descriptions"].append(dict(dimension="texture_material", core_concept="方向性笔触颗粒",
                                 professional_description="方向性笔触与颗粒相连，保留手绘表面表现", support_claim_ids=["S1"]))
    p, provider = pipeline(tmp_path, [a, supplemental, b])
    result = p.run()
    assert len(provider.calls) == 3
    assert result["budget"]["crop_count"] == 2
    assert result["budget"]["supplement_rounds"] == 1
    assert all(len(call["frames"]) <= 4 for call in provider.calls)
    assert "方向性笔触与颗粒" in result["profile"]["rendered_prompt"]
    assert "手绘表面" in result["profile"]["detailed_prompt"]
    assert len(result["tools"]) == 1
    for asset in result["assets"][1:]:
        assert asset["parent_alias"] == "I1"
        assert asset["width"] == 80 and asset["height"] == 120
        assert len(asset["bbox"]) == 4


@pytest.mark.parametrize("dimension,question", [("motion_signature", "帧率是否为12帧"), ("texture_material", "是否采用SSS次表面散射")])
def test_unresolvable_gap_does_not_trigger_tools(tmp_path, dimension, question):
    a = evidence([dict(dimension=dimension, question=question, image="I1", tool="crop_image", regions=[[0, 0, 1, 1]], expected_evidence="确认")])
    p, provider = pipeline(tmp_path, [a, interpretation()])
    result = p.run()
    assert len(provider.calls) == 2 and not result["tools"]


def test_nonobservable_motion_and_invalid_refs_drop_only_one_claim(tmp_path):
    a = evidence(); a["claims"].extend([claim("bad1", "motion_signature", "动作惯性真实"), claim("bad2", "texture_material", "细腻纸纤维", ["I999"])])
    p, _ = pipeline(tmp_path, [a, interpretation()])
    result = p.run()
    assert len(result["evidence"]["claims"]) == 3
    assert len(result["audit"]) == 2
    assert "曲线轮廓" in result["profile"]["rendered_prompt"]


def test_professional_description_cannot_use_invented_or_wrong_dimension_fact(tmp_path):
    b = interpretation()
    b["descriptions"].extend([
        dict(dimension="texture_material", core_concept="纸张", professional_description="清晰纸纤维边缘", support_claim_ids=["C1"]),
        dict(dimension="color_tone", core_concept="低饱和", professional_description="低饱和灰蓝色板", support_claim_ids=["MISSING"])])
    p, _ = pipeline(tmp_path, [evidence(), b])
    result = p.run()
    assert len(result["profile"]["descriptions"]) == 3
    assert "纸纤维" not in result["profile"]["rendered_prompt"]


def test_local_repair_preserves_visible_clause_without_algorithm(tmp_path):
    a = evidence(); a["claims"].append(claim("C4", "texture_material", "次表面散射，曲面光泽细腻"))
    p, _ = pipeline(tmp_path, [a, interpretation()])
    result = p.run()
    assert result["evidence"]["claims"][-1]["observation"] == "曲面光泽细腻"
    assert result["audit"][0]["level"] == "info"


@pytest.mark.parametrize("text", ["黑发少女穿着白色裙子", "木质桌子", "银色戒指", "橙色饮料", "使用Unreal和PBR"])
def test_content_and_technical_leakage_are_not_style_facts(tmp_path, text):
    a = evidence(); a["claims"].append(claim("C4", "texture_material", text))
    p, _ = pipeline(tmp_path, [a, interpretation()])
    result = p.run()
    assert len(result["evidence"]["claims"]) == 3


def test_sole_structure_correction_uses_image_and_stops_loop(tmp_path):
    p, provider = pipeline(tmp_path, ["broken JSON", evidence(), "broken again"])
    result = p.run()
    assert len(provider.calls) == 3
    assert all(call["frames"] for call in provider.calls)
    assert result["budget"]["correction_used"]
    assert result["profile"]["application_status"] == "partial"
    assert "曲线轮廓" in result["profile"]["rendered_prompt"]


def test_auth_failure_stops_after_one_call(tmp_path):
    p, provider = pipeline(tmp_path, [RuntimeError("百炼请求失败，HTTP 401")])
    result = p.run()
    assert len(provider.calls) == 1 and not result["profile"]["descriptions"]


def test_partial_crop_does_not_count_as_independent_global_frame(tmp_path):
    assets = [prepare_image(image_bytes(color=(i * 30, 100, 100)), tmp_path / str(i) / "input", f"I{i+1}", {"scene_id": "scene-a"}) for i in range(4)]
    a = evidence()
    b = interpretation("global")
    p, _ = pipeline(tmp_path, [a, b], scope="project", assets=assets)
    result = p.run()
    assert not result["profile"]["descriptions"]
    assert all("局部帧" in item["reason"] for item in result["audit"])


def test_shared_deadline_prevents_calls_after_budget_exhausted(tmp_path):
    p, provider = pipeline(tmp_path, [])
    p.deadline = p.clock() - 1
    result = p.run()
    assert not provider.calls
    assert result["budget"]["model_requests"] == 0


def test_preprocessing_preserves_ratio_and_refuses_animated_or_fake_file(tmp_path):
    asset = prepare_image(image_bytes((2000, 4000)), tmp_path / "input", "I1")
    assert (asset["width"], asset["height"]) == (768, 1536)
    with pytest.raises(ValueError):
        prepare_image(b"not a real PNG", tmp_path / "bad", "I1")
    animated = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(animated, format="GIF", save_all=True, append_images=[Image.new("RGB", (32, 32), "blue")])
    with pytest.raises(ValueError, match="静态图片"):
        prepare_image(animated.getvalue(), tmp_path / "gif", "I1")


def test_cloud_style_interface_uses_flash_no_hidden_retry(tmp_path):
    settings = replace(load_settings(), cloud_api_key="test-key", cloud_base_url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
    provider = BailianVisionProvider(settings)
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(503)
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        provider.analyze_style("输出JSON", [], phase="style.direct.evidence", schema=StyleEvidenceV3, timeout_seconds=10, max_tokens=2400)
    assert len(requests) == 1
    assert requests[0]["model"] == "qwen3.8-flash" and requests[0]["enable_thinking"] is False
    provider.unload()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("STORYBOARD_STYLE_STRATEGY", raising=False)
    with TestClient(app) as c:
        yield c


def finished(client, id):
    for _ in range(100):
        data = client.get(f"/api/style/analyses/{id}").json()
        if data["status"] in ("completed", "partial", "failed"):
            return data
        time.sleep(.01)
    raise AssertionError("analysis remained running")


def test_image_api_async_restore_cache_edit_history_and_evidence(client):
    with patch('app.style_vector.embedding.BailianStyleEmbedder', side_effect=AssertionError('embedding forbidden')):
        created = client.post('/api/style/analyses', files={'image': ('portrait.png', image_bytes(), 'image/png')})
        assert created.status_code == 202, created.text
        id = created.json()['analysis_id']
        result = finished(client, id)
        assert result['status'] == 'completed'
        assert result['result']['budget']['model_requests'] == 2
        assert 'original_path' not in json.dumps(result)
        assert client.get(f'/api/style/analyses/{id}/images/I1').status_code == 200
        assert client.get(f'/api/style/analyses/{id}/images/I999').status_code == 404
        cached = client.post('/api/style/analyses', files={'image': ('portrait.png', image_bytes(), 'image/png')}).json()
        assert cached['cached'] and cached['analysis_id'] == id
        saved = client.put(f'/api/style/analyses/{id}/profile', json={'rendered_prompt': '整体画面风格：清晰平面图形，限定色块对比。', 'expected_version': 1})
        assert saved.status_code == 200
        assert saved.json()['result']['profile']['version'] == 2
        assert client.put(f'/api/style/analyses/{id}/profile', json={'rendered_prompt': '整体画面风格：新版', 'expected_version': 1}).status_code == 409
        assert len(client.get(f'/api/style/analyses/{id}/history').json()['revisions']) == 2
        assert 'style.direct.finished' in client.get(f'/api/style/analyses/{id}/events').text


def test_image_api_rejects_invalid_input_and_paths(client):
    assert client.post('/api/style/analyses').status_code == 400
    assert client.post('/api/style/analyses', files={'image': ('fake.png', b'fake', 'image/png')}).status_code == 422
    assert client.post('/api/style/analyses', data={'job_id': 'x', 'frame_id': '../../.env'}).status_code == 404


def test_apply_preserves_shots_and_old_prompt_versions(client):
    service = client.app.state.job_service
    now = utc_now()
    service.db.insert_job(dict(id='job-target', status='completed', stage='completed', progress=100,
        input_type='script', config={'style_strategy': 'legacy-v1'}, created_at=now, updated_at=now))
    visual = observation()
    service.db.execute('INSERT INTO shots(id,job_id,number,start_time,end_time,duration,time,observation_json) VALUES(?,?,?,?,?,?,?,?)',
                       ('shot-target', 'job-target', 1, 0, 4.5, 4.5, '00:00', visual.model_dump_json()))
    from app.services.prompt_service import build_prompts
    for prompt in build_prompts('shot-target', visual):
        service.db.execute('INSERT INTO prompts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (prompt.id,'job-target',prompt.shot_id,prompt.prompt_type,prompt.platform,prompt.version,prompt.positive_prompt,prompt.negative_prompt,prompt.status,prompt.template_id,prompt.template_version,'[]',prompt.contract.model_dump_json()))
    before = service.db.fetchall('SELECT * FROM prompts WHERE job_id=?', ('job-target',))
    created = client.post('/api/style/analyses', files={'image': ('a.png', image_bytes(), 'image/png')}).json()
    result = finished(client, created['analysis_id'])
    applied = client.post('/api/jobs/job-target/style/apply', json={'analysis_id': created['analysis_id'], 'expected_version': 1})
    assert applied.status_code == 200, applied.text
    after = service.db.fetchall('SELECT * FROM prompts WHERE job_id=?', ('job-target',))
    assert len(after) == 2 and {p['template_id'] for p in after} == {'first-frame-v7', 'seedance-video-v8'}
    assert all(result['result']['profile']['rendered_prompt'] in p['positive_prompt'] for p in after)
    assert service.db.fetchone('SELECT end_time FROM shots WHERE id=?', ('shot-target',))['end_time'] == 4.5
    revisions = service.db.fetchall('SELECT payload_json FROM prompt_revisions WHERE job_id=?', ('job-target',))
    assert {json.loads(r['payload_json'])['positive_prompt'] for r in revisions} == {r['positive_prompt'] for r in before}
    assert service.result('job-target').style_analysis['analysis_id'] == created['analysis_id']
    # A hand edit is protected, even during explicit application.
    service.db.execute("UPDATE prompts SET positive_prompt='手动修改' WHERE id=?", (after[0]['id'],))
    assert client.post('/api/jobs/job-target/style/apply', json={'analysis_id': created['analysis_id'], 'expected_version': 1}).status_code == 409


def test_cloud_start_prefers_direct_even_if_private_file_has_old_strategy(tmp_path, monkeypatch):
    path = tmp_path / 'private.env'
    path.write_text('STORYBOARD_STYLE_STRATEGY=vector-v2\n', encoding='utf8')
    monkeypatch.setenv('STORYBOARD_ENV_FILE', str(path))
    monkeypatch.delenv('STORYBOARD_STYLE_STRATEGY', raising=False)
    assert load_settings().style_strategy == 'vector-v2'  # explicit regression launch remains possible
    assert load_settings(prefer_private_cloud=True).style_strategy == 'qwen-direct-v3'


def seed_video_style(service, job_id="job-video", project_id=None):
    now = utc_now()
    service.db.insert_job(dict(id=job_id, project_id=project_id, status='completed', stage='completed', progress=100,
        input_type='video', config={'style_strategy': 'qwen-direct-v3'}, created_at=now, updated_at=now))
    service.db.update_job(job_id, style_direct_json=json.dumps({'profile': {'application_status': 'user-edited'}, 'applied_at': 'old'}))
    service.db.execute('INSERT INTO shots(id,job_id,number,start_time,end_time,duration,time,scene_group_id) VALUES(?,?,?,?,?,?,?,?)',
                       (job_id + '-shot', job_id, 1, 0, 4, 4, '00:00', 'scene-1'))
    frame = service.settings.data_dir / (job_id + '.png')
    frame.write_bytes(image_bytes())
    service.db.execute('INSERT INTO frames(id,job_id,shot_id,timestamp,path,thumbnail_path,width,height) VALUES(?,?,?,?,?,?,?,?)',
                       (job_id + '-frame', job_id, job_id + '-shot', 1, str(frame), str(frame), 128, 128))


def test_explicit_project_reanalysis_is_restored_without_replacing_applied_state(client):
    seed_video_style(client.app.state.job_service)
    created = client.post('/api/jobs/job-video/style/direct/analyze')
    assert created.status_code == 202, created.text
    result = finished(client, created.json()['analysis_id'])
    restored = client.get('/api/jobs/job-video/style/direct').json()
    assert restored['analysis']['analysis_id'] == result['analysis_id']
    assert restored['state']['applied_at'] == 'old'
    assert restored['state']['profile']['application_status'] == 'user-edited'


def test_concurrent_reanalysis_shares_one_worker_and_allows_explicit_rerun(client, monkeypatch):
    service = client.app.state.job_service
    seed_video_style(service)
    submissions = []
    monkeypatch.setattr(service.executor, "submit", lambda *args: submissions.append(args))
    url = "/api/jobs/job-video/style/direct/analyze"
    start = Barrier(4)

    def request(_):
        start.wait(timeout=5)
        response = client.post(url)
        assert response.status_code == 202, response.text
        return response.json()["analysis_id"]

    with ThreadPoolExecutor(max_workers=4) as callers:
        ids = list(callers.map(request, range(4)))
    assert len(set(ids)) == len(submissions) == 1
    assert [path.name for path in (service.settings.data_dir / "style").iterdir()] == [ids[0]]
    assert len(service.db.fetchall("SELECT id FROM style_analyses")) == 1
    assert client.get("/api/jobs/job-video/style/direct").json()["state"]["pending_analysis_id"] == ids[0]

    run, *args = submissions[0]
    run(*args)
    assert client.get("/api/style/analyses/" + ids[0]).json()["status"] == "completed"
    again = client.post(url)
    assert again.status_code == 202
    assert again.json()["analysis_id"] != ids[0] and len(submissions) == 2
    state = client.get("/api/jobs/job-video/style/direct").json()["state"]
    assert state["pending_analysis_id"] == again.json()["analysis_id"] and state["applied_at"] == "old"


def test_same_pixels_isolated_by_origin_job_project_and_policy(client, monkeypatch):
    service = client.app.state.job_service
    submissions = []
    monkeypatch.setattr(service.executor, "submit", lambda *args: submissions.append(args))
    left, right = [client.post("/api/projects", json={"name": name}).json()["id"] for name in ("左", "右")]
    ids = []
    for job_id, project_id in (("a", left), ("b", left), ("c", right)):
        seed_video_style(service, job_id, project_id)
        response = client.post(f"/api/jobs/{job_id}/style/direct/analyze", headers={"x-project-id": project_id})
        assert response.status_code == 202, response.text
        ids.append(response.json()["analysis_id"])
    monkeypatch.setattr("app.style_direct.service.VERSION", "updated-policy")
    ids.append(client.post("/api/jobs/a/style/direct/analyze", headers={"x-project-id": left}).json()["analysis_id"])
    assert len(set(ids)) == len(submissions) == 4

    # Also isolate uploads with identical bytes and no job/frame provenance.
    def upload(project_id):
        return client.post("/api/style/analyses", headers={"x-project-id": project_id},
                           files={"image": ("same.png", image_bytes(), "image/png")}).json()["analysis_id"]
    left_analysis = upload(left)
    right_analysis = upload(right)
    assert left_analysis != right_analysis
    for run, *args in submissions[-2:]:
        run(*args)
    assert upload(left) == left_analysis and upload(right) == right_analysis
    assert len(submissions) == 6


def test_supplement_and_structure_correction_share_four_request_budget(tmp_path):
    gap = dict(dimension='texture_material', question='有纸纤维吗？', image='I1', tool='crop_image',
               regions=[[0, 0, .5, .5]], expected_evidence='边缘纤维连续分布')
    a = evidence([gap])
    a['claims'].append(claim('S1', 'texture_material', '边缘有可见颗粒'))
    extra = dict(claims=[claim('S1', 'texture_material', '纸纤维沿边缘连续分布', ['I2'])], gaps=[], uncertainties=[])
    p, provider = pipeline(tmp_path, [a, extra, 'not json', interpretation()])
    result = p.run()
    assert len(provider.calls) == 4 and result['budget']['correction_used']
    ids = [c['id'] for c in result['evidence']['claims']]
    assert len(ids) == len(set(ids)) and 'S2' in ids
