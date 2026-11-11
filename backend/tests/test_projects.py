"""Project ownership, canvas CAS and legacy migration without paid inference."""
import copy
import io
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        monkeypatch.setattr(client.app.state.job_service.executor, "submit", Mock())
        yield client


def project(client, name="电影项目"):
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def picture(client, project_id=None, color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 18), color=color).save(buffer, "PNG")
    response = client.post("/api/generation-assets", files={"image": ("image.png", buffer.getvalue(), "image/png")},
                           data={"project_id": project_id} if project_id else {})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def generation(client, project_id=None, **updates):
    body = {"node_id": "gen-test", "kind": "image", "prompt": "纸片人物站在窗边", "idempotency_key": "project-test-key"}
    if project_id:
        body["project_id"] = project_id
    body.update(updates)
    response = client.post("/api/generations", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def history_version(job, asset=None):
    asset = copy.deepcopy(asset or job["assets"][0])
    return {"id": job["id"] + ":" + asset["id"], "jobId": job["id"], "asset": asset,
            "createdAt": job.get("updated_at") or job["created_at"],
            "prompt": job["request"]["prompt"], "settings": copy.deepcopy(job["request"]["settings"])}


def delivered_job(client, project_id=None, key="delivered-history"):
    from test_generation_tasks import configured, image_bytes
    from app.generation.providers import GeneratedAsset, ProviderResult
    service = client.app.state.generation_service
    _, executor = configured(service, [ProviderResult("succeeded", assets=[GeneratedAsset(image_bytes("blue", (1920, 1080)), "image/png")])])
    job = generation(client, project_id, idempotency_key=key)
    executor.run()
    return service.get(job["id"])


def test_project_homepage_create_rename_trash_restore_and_canvas(client):
    assert client.get("/api/projects").json() == []
    identity = project(client, "  初始项目  ")
    listed = client.get("/api/projects").json()
    assert listed[0]["name"] == "初始项目" and listed[0]["revision"] == 0
    assert listed[0]["analysis_count"] == listed[0]["generation_count"] == 0
    assert client.patch("/api/projects/" + identity, json={"name": "新版项目"}).json()["name"] == "新版项目"
    assert client.get(f"/api/projects/{identity}/canvas").json()["state"] == {}
    assert client.delete("/api/projects/" + identity).status_code == 200
    assert client.get("/api/projects").json() == []
    assert client.get("/api/projects?deleted=true").json()[0]["id"] == identity
    assert client.get(f"/api/projects/{identity}/canvas").status_code == 410
    assert client.post(f"/api/projects/{identity}/restore").status_code == 200
    assert client.get(f"/api/projects/{identity}/canvas").json()["revision"] == 0


def test_canvas_revision_conflict_and_project_isolation(client):
    left, right = project(client, "左项目"), project(client, "右项目")
    state = {"positions": {"node": {"x": 15, "y": 25}}, "viewport": {"x": 0, "y": 0, "zoom": .5},
             "generators": [{"id": "gen-one", "imageAssetId": "image-local", "referenceImages": [{"assetId": "image-local-two"}]}]}
    first = client.put(f"/api/projects/{left}/canvas", json={"expected_revision": 0, "state": state})
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 1
    assert client.put(f"/api/projects/{left}/canvas", json={"expected_revision": 0, "state": {}}).status_code == 409
    assert client.get(f"/api/projects/{right}/canvas").json()["state"] == {}
    assert client.get(f"/api/projects/{left}/canvas").json()["state"] == state


def test_canvas_does_not_bind_a_foreign_generation(client):
    left, right = project(client), project(client)
    saved = generation(client, left)
    response = client.put(f"/api/projects/{right}/canvas", json={"expected_revision": 0,
        "state": {"generators": [{"id": "gen-test", "generationTask": {"jobId": saved["id"]}}]}})
    assert response.status_code == 422
    assert client.get(f"/api/projects/{right}/canvas").json()["revision"] == 0


def test_canvas_keeps_all_delivered_generation_versions_and_the_pinned_version(client):
    from test_generation_tasks import configured, image_bytes
    from app.generation.providers import GeneratedAsset, ProviderResult

    identity = project(client)
    service = client.app.state.generation_service
    _, executor = configured(service, [ProviderResult("succeeded", assets=[GeneratedAsset(image_bytes(color, (1920, 1080)), "image/png")]) for color in ("blue", "orange")])
    delivered = []
    for number in (1, 2):
        created = generation(client, identity, prompt=f"第{number}版的窗边人物", idempotency_key=f"history-{number}")
        executor.run()
        delivered.append(service.get(created["id"]))
    versions = [{"id": item["id"] + ":" + item["assets"][0]["id"], "jobId": item["id"],
                 "asset": item["assets"][0], "createdAt": item["updated_at"],
                 "prompt": item["request"]["prompt"], "settings": item["request"]["settings"]} for item in reversed(delivered)]
    state = {"generators": [{"id": "gen-test", "kind": "image", "nodeMode": "generator", "manualPrompt": "下一次尚未提交的文字",
                              "generationTask": {"jobId": delivered[1]["id"], "inputSignature": "saved"},
                              "versions": versions, "activeVersionId": versions[1]["id"]}]}
    result = client.put(f"/api/projects/{identity}/canvas", json={"expected_revision": 0, "state": state})
    assert result.status_code == 200, result.text
    restored = client.get(f"/api/projects/{identity}/canvas").json()["state"]
    assert restored == state
    assert len(executor.calls) == 0
    assert client.app.state.db.fetchone("SELECT count(*) AS n FROM generation_jobs WHERE project_id=?", (identity,))["n"] == 2


@pytest.mark.parametrize("foreign_part", ["job", "asset"])
def test_canvas_history_cannot_reference_another_projects_jobs_or_assets(client, foreign_part):
    left, right = project(client), project(client)
    owned = generation(client, left, idempotency_key="owned-history")
    foreign = generation(client, right, idempotency_key="foreign-history")
    own_asset, foreign_asset = picture(client, left), picture(client, right, "blue")
    asset_id = foreign_asset if foreign_part == "asset" else own_asset
    version = history_version(foreign if foreign_part == "job" else owned, {
        "id": asset_id, "mime_type": "image/png", "width": 32, "height": 18, "url": "/api/generation-assets/" + asset_id})
    state = {"generators": [{"id": "gen-test", "kind": "image", "versions": [version]}]}
    response = client.put(f"/api/projects/{left}/canvas", json={"expected_revision": 0, "state": state})
    assert response.status_code == 422
    assert "其他项目" in response.json()["detail"]
    assert client.get(f"/api/projects/{left}/canvas").json()["revision"] == 0


def test_canvas_history_does_not_turn_an_uploaded_image_and_blocked_job_into_a_generated_result(client):
    identity = project(client)
    blocked, uploaded = generation(client, identity), picture(client, identity)
    version = history_version(blocked, {"id": uploaded, "mime_type": "image/png", "width": 32, "height": 18,
        "url": "/api/generation-assets/" + uploaded})
    response = client.put(f"/api/projects/{identity}/canvas", json={"expected_revision": 0,
        "state": {"generators": [{"id": "gen-test", "kind": "image", "versions": [version]}]}})
    assert response.status_code == 422
    assert "成功交付" in response.json()["detail"]


@pytest.mark.parametrize("field", ["id", "jobId", "createdAt", "asset", "prompt", "settings"])
def test_incomplete_generation_history_is_rejected_instead_of_lost_on_frontend_restore(client, field):
    identity = project(client)
    job = delivered_job(client, identity)
    version = history_version(job)
    version.pop(field)
    result = client.put(f"/api/projects/{identity}/canvas", json={"expected_revision": 0,
        "state": {"generators": [{"id": "gen-test", "kind": "image", "versions": [version]}]}})
    assert result.status_code == 422, result.text
    assert client.get(f"/api/projects/{identity}/canvas").json()["revision"] == 0


@pytest.mark.parametrize("changed", ["id", "createdAt", "prompt", "settings", "asset.width", "asset.height", "asset.mime_type", "asset.url"])
def test_history_snapshot_cannot_forge_successful_job_metadata(client, changed):
    identity = project(client)
    job = delivered_job(client, identity)
    version = history_version(job)
    if changed.startswith("asset."):
        field = changed.split(".")[1]
        version["asset"][field] = {"width": 9999, "height": 9999, "mime_type": "video/mp4", "url": "/api/generation-assets/other-asset"}[field]
    else:
        version[changed] = {"id": "forged-version", "createdAt": "2000-01-01T00:00:00Z", "prompt": "forged historical prompt",
                            "settings": {"aspect_ratio": "9:16", "resolution": "720p", "duration_seconds": None}}[changed]
    response = client.put(f"/api/projects/{identity}/canvas", json={"expected_revision": 0,
        "state": {"generators": [{"id": "gen-test", "kind": "image", "versions": [version]}]}})
    assert response.status_code == 422, response.text
    assert client.get(f"/api/projects/{identity}/canvas").json()["revision"] == 0
    assert client.app.state.generation_service.get(job["id"])["request"]["prompt"] == job["request"]["prompt"]


def test_history_allows_frontend_absolute_asset_urls_and_client_signature(client):
    identity = project(client)
    job = delivered_job(client, identity)
    version = history_version(job)
    version["asset"]["url"] = "http://127.0.0.1:8000" + version["asset"]["url"] + "?project_id=" + identity
    version["inputSignature"] = "client-only-signature"
    state = {"generators": [{"id": "gen-test", "kind": "image", "versions": [version], "activeVersionId": version["id"]}]}
    saved = client.put(f"/api/projects/{identity}/canvas", json={"expected_revision": 0, "state": state})
    assert saved.status_code == 200, saved.text
    assert saved.json()["state"] == state


def test_legacy_successful_history_import_is_complete_atomic_and_replayable(client):
    job = delivered_job(client)
    identity = project(client)
    version = history_version(job)
    state = {"generators": [{"id": "gen-test", "kind": "image", "versions": [version], "activeVersionId": version["id"]}]}
    # Generation and asset IDs are inferred from history even without an active task.
    body = {"state": state, "expected_revision": 0}
    first = client.post(f"/api/projects/{identity}/import-legacy", json=body)
    replay = client.post(f"/api/projects/{identity}/import-legacy", json=body)
    assert first.status_code == replay.status_code == 200, first.text
    assert first.json()["state"] == replay.json()["state"] == state
    assert first.json()["revision"] == replay.json()["revision"] == 1
    assert job["id"] in first.json()["imported"]["generation_ids"]
    assert version["asset"]["id"] in first.json()["imported"]["asset_ids"]


def test_invalid_legacy_history_does_not_claim_resources_before_validation(client):
    job = delivered_job(client)
    identity = project(client)
    version = history_version(job)
    version["prompt"] = "tampered"
    result = client.post(f"/api/projects/{identity}/import-legacy", json={"expected_revision": 0,
        "state": {"generators": [{"id": "gen-test", "kind": "image", "versions": [version]}]}})
    assert result.status_code == 422
    assert client.app.state.db.fetchone("SELECT project_id FROM generation_jobs WHERE id=?", (job["id"],))["project_id"] is None
    assert client.app.state.db.fetchone("SELECT 1 FROM project_asset_refs WHERE project_id=?", (identity,)) is None
    assert client.get(f"/api/projects/{identity}/canvas").json()["revision"] == 0


def test_generation_project_ownership_does_not_enter_content_hash(client):
    identity = project(client)
    legacy = generation(client)
    before = client.app.state.db.fetchone("SELECT payload_json,input_hash FROM generation_jobs WHERE id=?", (legacy["id"],))
    events = [dict(row) for row in client.app.state.db.fetchall("SELECT * FROM generation_events WHERE job_id=?", (legacy["id"],))]
    response = client.post(f"/api/projects/{identity}/import-legacy", json={"generation_ids": [legacy["id"]],
        "state": {"generators": [{"id": "gen-test", "generationTask": {"jobId": legacy["id"]}}]}, "expected_revision": 0})
    assert response.status_code == 200, response.text
    after = client.app.state.db.fetchone("SELECT payload_json,input_hash,project_id FROM generation_jobs WHERE id=?", (legacy["id"],))
    assert after["payload_json"] == before["payload_json"] and after["input_hash"] == before["input_hash"]
    assert [dict(row) for row in client.app.state.db.fetchall("SELECT * FROM generation_events WHERE job_id=?", (legacy["id"],))] == events
    assert after["project_id"] == identity
    same = generation(client, identity)
    assert same["id"] == legacy["id"] and same["project_id"] == identity
    assert client.get("/api/generations/" + legacy["id"] + "?project_id=" + project(client)).status_code == 404


def test_cannot_steal_task_or_idempotency_key_from_other_project(client):
    left, right = project(client), project(client)
    saved = generation(client, left)
    steal = client.post(f"/api/projects/{right}/import-legacy", json={"generation_ids": [saved["id"]]})
    assert steal.status_code == 409
    assert client.post("/api/generations", json={"project_id": right, "node_id": "gen-test", "kind": "image", "prompt": "纸片人物站在窗边", "idempotency_key": "project-test-key"}).status_code == 409
    assert client.get("/api/generations/" + saved["id"]).json()["project_id"] == left


def test_project_image_membership_and_shared_content_survive_other_project_deletion(client):
    left, right = project(client), project(client)
    asset = picture(client, left)
    assert picture(client, right) == asset
    assert client.delete(f"/api/projects/{left}").status_code == 200
    assert client.get(f"/api/generation-assets/{asset}?project_id={left}").status_code == 410
    assert client.get(f"/api/generation-assets/{asset}?project_id={right}").status_code == 200
    assert client.post(f"/api/projects/{left}/restore").status_code == 200
    assert client.get(f"/api/generation-assets/{asset}?project_id={left}").status_code == 200


def test_reference_images_cannot_cross_project_or_invent_a_model(client):
    left, right = project(client), project(client)
    asset = picture(client, left)
    body = {"node_id": "gen-refs", "project_id": right, "kind": "image", "prompt": "参考 @图片1",
            "reference_asset_id": asset,
            "reference_images": [{"reference_id": "ref-0123456789ab", "asset_id": asset, "mention": "图片1", "role": "reference"}]}
    assert client.post("/api/generations/preflight", json=body).status_code == 422
    body["project_id"] = left
    check = client.post("/api/generations/preflight", json=body)
    assert check.status_code == 200 and check.json()["valid"] and not check.json()["can_submit"]
    body["settings"] = {"model_id": "not-configured-model"}
    check = client.post("/api/generations/preflight", json=body).json()
    assert not check["valid"] and "生成模型" in check["issues"][0]
    declared = client.get("/api/generations/capabilities").json()["image"]
    assert not declared["available"]
    assert all(not model["available"] for model in declared["models"])


@pytest.mark.parametrize("change", ["unbound", "duplicate-name", "duplicate-id", "wrong-primary", "wrong-first-frame"])
def test_reference_bindings_and_mentions_are_unambiguous(client, change):
    identity = project(client)
    asset, other = picture(client, identity), picture(client, identity, "blue")
    refs = [{"reference_id": "ref-0123456789ab", "asset_id": asset, "mention": "图片1", "role": "reference"},
            {"reference_id": "ref-fedcba987654", "asset_id": other, "mention": "图片2", "role": "reference"}]
    body = {"node_id": "gen-refs", "project_id": identity, "kind": "image", "prompt": "参考 @图片1 与 @图片2", "reference_asset_id": asset, "reference_images": refs}
    if change == "unbound": body["prompt"] += " 与 @图片3"
    if change == "duplicate-name": refs[1]["mention"] = "图片1"
    if change == "duplicate-id": refs[1]["reference_id"] = refs[0]["reference_id"]
    if change == "wrong-primary": body["reference_asset_id"] = picture(client, identity, "green")
    if change == "wrong-first-frame": refs[1]["role"] = "first_frame"
    response = client.post("/api/generations/preflight", json=body)
    assert response.status_code == 200 and not response.json()["valid"], response.text


def test_delete_cancels_only_owned_work_and_restore_never_restarts_it(client):
    left, right = project(client), project(client)
    a = client.post("/api/jobs", data={"input_type": "script", "script_text": "女孩打开门。", "project_id": left}).json()["job_id"]
    b = client.post("/api/jobs", data={"input_type": "script", "script_text": "男孩走近窗户。", "project_id": right}).json()["job_id"]
    ga = generation(client, left)
    gb = generation(client, right, idempotency_key="other-project-key")
    submitted = client.app.state.job_service.executor.submit.call_count
    assert client.delete(f"/api/projects/{left}").status_code == 200
    db = client.app.state.db
    assert db.fetchone("SELECT status FROM jobs WHERE id=?", (a,))["status"] == "cancelled"
    assert db.fetchone("SELECT status FROM jobs WHERE id=?", (b,))["status"] == "queued"
    assert client.get("/api/generations/" + ga["id"]).status_code == 410
    assert client.get("/api/generations/" + gb["id"]).json()["status"] == "blocked"
    assert client.get(f"/api/jobs/{a}/events").status_code == 410
    assert client.post(f"/api/projects/{left}/restore").status_code == 200
    assert client.get("/api/generations/" + ga["id"]).json()["status"] == "cancelled"
    assert client.app.state.job_service.executor.submit.call_count == submitted


def test_deleted_project_cannot_save_or_create_new_work(client):
    identity = project(client)
    client.delete(f"/api/projects/{identity}")
    assert client.put(f"/api/projects/{identity}/canvas", json={"expected_revision": 0, "state": {}}).status_code == 410
    assert client.post("/api/jobs", data={"input_type": "script", "script_text": "女孩走来", "project_id": identity}).status_code == 410
    assert client.post("/api/generations", json={"project_id": identity, "node_id": "gen-test", "kind": "image", "prompt": "窗边人物", "idempotency_key": "dead-project-key"}).status_code == 410


def test_legacy_import_is_atomic_on_conflict_and_preserves_analysis_payload(client):
    left, right = project(client), project(client)
    old = client.post("/api/jobs", data={"input_type": "script", "script_text": "旧剧本人物对白"}).json()["job_id"]
    foreign = generation(client, right)
    before = dict(client.app.state.db.fetchone("SELECT * FROM jobs WHERE id=?", (old,)))
    response = client.post(f"/api/projects/{left}/import-legacy", json={"job_ids": [old], "generation_ids": [foreign["id"]]})
    assert response.status_code == 409
    assert dict(client.app.state.db.fetchone("SELECT * FROM jobs WHERE id=?", (old,))) == before
    state = {"jobKeys": {old: "old-node-key"}, "positions": {"old-node-key": {"x": 10, "y": 20}}}
    response = client.post(f"/api/projects/{left}/import-legacy", json={"job_ids": [old], "state": state, "expected_revision": 0})
    assert response.status_code == 200, response.text
    after = dict(client.app.state.db.fetchone("SELECT * FROM jobs WHERE id=?", (old,)))
    assert {key: value for key, value in after.items() if key != "project_id"} == {key: value for key, value in before.items() if key != "project_id"}
    assert client.get(f"/api/jobs/{old}?project_id={left}").status_code == 200
    assert client.get(f"/api/jobs/{old}?project_id={right}").status_code == 404


def test_mixed_header_and_payload_project_is_rejected(client):
    left, right = project(client), project(client)
    response = client.post("/api/generations", headers={"X-Project-ID": left}, json={"project_id": right,
        "node_id": "gen-node", "kind": "image", "prompt": "窗边人物", "idempotency_key": "conflicting-project-key"})
    assert response.status_code == 422


def test_input_hash_keeps_legacy_null_model_and_empty_reference_fields_out(client):
    plain = generation(client)
    same = generation(client, settings={"model_id": None}, reference_images=[])
    assert same["id"] == plain["id"]
    assert "model_id" not in same["request"]["settings"]
    assert "reference_images" not in same["request"]


def test_detail_recovers_tasks_missing_from_last_saved_canvas(client):
    left, right = project(client), project(client)
    job = client.post("/api/jobs", data={"input_type": "script", "script_text": "门口的人转身离开。", "project_id": left}).json()["job_id"]
    generated = generation(client, left)
    detail = client.get(f"/api/projects/{left}").json()
    assert detail["job_ids"] == [job]
    assert detail["generation_ids"] == [generated["id"]]
    assert client.get(f"/api/projects/{right}").json()["job_ids"] == []
    assert "job_ids" not in client.get("/api/projects").json()[0]


@pytest.mark.parametrize("suffix", ["", "/result", "/events", "/frames/missing", "/export", "/style", "/visual-bible", "/event-agent/runs", "/shots/missing/perception"])
def test_every_job_subroute_checks_context_before_serving_or_mutating(client, suffix):
    left, right = project(client), project(client)
    job = client.post("/api/jobs", data={"input_type": "script", "script_text": "人物向左走。", "project_id": left}).json()["job_id"]
    assert client.get(f"/api/jobs/{job}{suffix}", headers={"X-Project-ID": right}).status_code == 404
    assert client.get(f"/api/jobs/{job}{suffix}?project_id={right}").status_code == 404


@pytest.mark.parametrize("malformed", [
    {"generators": [{"generationTask": {"jobId": {"bad": "id"}}}]},
    {"generators": [{"backendReferenceAssetId": []}] , "asset_ids": [{}]},
    {"generators": [{"referenceImages": 10}]},
])
def test_malformed_canvas_references_return_validation_errors(client, malformed):
    identity = project(client)
    response = client.put(f"/api/projects/{identity}/canvas", json={"expected_revision": 0, "state": malformed})
    assert response.status_code == 422, response.text
    assert client.get(f"/api/projects/{identity}/canvas").json()["revision"] == 0


def test_legacy_migration_replay_preserves_revision_after_lost_response(client):
    identity = project(client)
    legacy = generation(client)
    payload = {"generation_ids": [legacy["id"]], "expected_revision": 0,
        "state": {"generators": [{"id": "gen-test", "generationTask": {"jobId": legacy["id"]}}]}}
    first = client.post(f"/api/projects/{identity}/import-legacy", json=payload)
    repeat = client.post(f"/api/projects/{identity}/import-legacy", json=payload)
    assert first.status_code == repeat.status_code == 200
    assert first.json()["revision"] == repeat.json()["revision"] == 1


def test_header_and_query_context_cannot_disagree(client):
    left, right = project(client), project(client)
    response = client.get(f"/api/generations/capabilities?project_id={right}", headers={"X-Project-ID": left})
    assert response.status_code == 422


def test_late_worker_results_cannot_revive_a_deleted_or_restored_project_job(client):
    identity = project(client)
    job = client.post("/api/jobs", data={"input_type": "script", "script_text": "人物放下杯子。", "project_id": identity}).json()["job_id"]
    client.delete(f"/api/projects/{identity}")
    db = client.app.state.db
    db.update_job(job, status="completed", stage="completed", progress=100)
    db.update_job(job, model_usage_json='[{"stage":"late_response","tokens":3}]')
    assert db.fetchone("SELECT status,stage,model_usage_json FROM jobs WHERE id=?", (job,))["status"] == "cancelled"
    assert json.loads(db.fetchone("SELECT model_usage_json FROM jobs WHERE id=?", (job,))["model_usage_json"])[0]["tokens"] == 3
    client.post(f"/api/projects/{identity}/restore")
    db.update_job(job, status="processing", stage="analyzing")
    assert client.get(f"/api/jobs/{job}").json()["status"] == "cancelled"


def test_project_creation_is_idempotent_during_parallel_legacy_migration(client):
    body = {"name": "恢复的旧工作台", "idempotency_key": "legacy-stable-content-hash"}
    first = client.post("/api/projects", json=body)
    second = client.post("/api/projects", json=body)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert client.post("/api/projects", json={**body, "name": "另一个名字"}).status_code == 409
    service = client.app.state.project_service
    with ThreadPoolExecutor(max_workers=4) as pool:
        parallel = list(pool.map(lambda _: service.create("并发旧工作台", "legacy-concurrent-key"), range(4)))
    assert len({item["id"] for item in parallel}) == 1
    assert len(client.get("/api/projects").json()) == 2


def test_project_search_applies_before_pagination_and_treats_wildcards_literally(client):
    project(client, "电影_完成度100%")
    project(client, "电影第二场")
    project(client, "日常短片")
    first = client.get("/api/projects", params={"limit": 1, "offset": 0, "q": "电影"}).json()
    second = client.get("/api/projects", params={"limit": 1, "offset": 1, "q": "电影"}).json()
    assert len(first) == len(second) == 1 and first[0]["id"] != second[0]["id"]
    assert len(client.get("/api/projects", params={"q": "_"}).json()) == 1
    assert len(client.get("/api/projects", params={"q": "%"}).json()) == 1
    assert client.get("/api/projects", params={"q": "不存在"}).json() == []


def test_uploaded_style_analysis_and_all_its_artifacts_are_project_scoped(client):
    left, right = project(client), project(client)
    buffer = io.BytesIO()
    Image.new("RGB", (32, 18), "orange").save(buffer, "PNG")
    response = client.post("/api/style/analyses", headers={"X-Project-ID": left},
        files={"image": ("style.png", buffer.getvalue(), "image/png")})
    assert response.status_code == 202, response.text
    identity = response.json()["analysis_id"]
    assert client.get(f"/api/style/analyses/{identity}", headers={"X-Project-ID": left}).status_code == 200
    for suffix in ("", "/history", "/diagnostics", "/images/I1", "/events"):
        assert client.get(f"/api/style/analyses/{identity}{suffix}?project_id={right}").status_code == 404
    client.delete(f"/api/projects/{left}")
    assert client.get(f"/api/style/analyses/{identity}").status_code == 410
    client.post(f"/api/projects/{left}/restore")
    assert client.get(f"/api/style/analyses/{identity}").json()["status"] == "failed"
    from app.style_direct.service import run_analysis
    run_analysis(client.app.state.job_service, identity)
    assert client.get(f"/api/style/analyses/{identity}").json()["status"] == "failed"


def test_style_reference_cannot_read_another_projects_frame(client):
    left, right = project(client), project(client)
    source = client.post("/api/jobs", data={"input_type": "script", "script_text": "人物坐在窗边。", "project_id": left}).json()["job_id"]
    calls = client.app.state.job_service.executor.submit.call_count
    response = client.post("/api/style/analyses", headers={"X-Project-ID": right}, data={"job_id": source, "frame_id": "anything"})
    assert response.status_code == 404
    assert client.app.state.job_service.executor.submit.call_count == calls
