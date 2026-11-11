import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.services.style_service import StyleClaim, StyleObservation, StyleProfile, build_profile, retrieve_candidates
from app.style_ontology.catalog import VERSION, conflicts, terms


def test_authored_ontology_is_versioned_and_conflict_checked():
    catalogue = terms()
    assert VERSION == "style-ontology.v1"
    assert len(catalogue) == 300
    assert len({term.category for term in catalogue.values()}) == 12
    assert all(term.definition and term.generation_phrase for term in catalogue.values())
    assert all(term.observable_cues for term in catalogue.values() if term.category != 'negative_constraint')
    assert len(retrieve_candidates("波普动画，粗黑轮廓，红黄蓝黑，网点阴影")) <= 30
    assert all(item in catalogue for item in retrieve_candidates("波普动画"))
    assert conflicts(["medium.photography", "medium.two-d-animation"])


def test_observation_requires_evidence_and_profile_is_dense():
    with pytest.raises(ValidationError, match="帧证据"):
        StyleObservation(mode="observed", dimensions={"medium": [StyleClaim(term_id="medium.two-d-animation", confidence=.8)]})
    ids = [
        "medium.two-d-animation", "style_family.pop-art", "line_language.bold-black-outline",
        "character_design.expressive-limbs", "palette.primary-four-color", "shading.duotone-halftone",
        "material.paper-fiber", "composition.clean-negative-space",
        "graphic_motif.handdrawn-speedlines", "motion_language.exaggerated-pose",
    ]
    profile = build_profile(StyleObservation(mode="creative"), ids)
    assert 80 <= len(profile.rendered_style_prompt) <= 140
    assert "整体画面风格：" in profile.rendered_style_prompt
    assert "高级" not in profile.rendered_style_prompt
    with pytest.raises(ValidationError, match="冲突"):
        StyleProfile(mode="creative", global_terms=[*ids, "medium.photography"])
    anchored = build_profile(StyleObservation(mode="creative"), ids, ["warhol"])
    anchored.include_anchor_names = False
    from app.services.style_service import render_style
    feature_text = render_style(anchored)
    assert "安迪" not in feature_text and "套印错位边缘" in feature_text


def test_mock_script_style_confirmation_updates_only_prompt_versions(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_SCRIPT_STRATEGY", "legacy-v2")
    monkeypatch.setenv("STORYBOARD_STYLE_STRATEGY", "legacy-v1")
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        created = client.post("/api/jobs", data={
            "input_type": "script",
            "script_text": "女孩推开门，来到桌边。她拿起一本书，翻开第一页。她抬头望向窗外，停在原地。",
        })
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        for _ in range(160):
            state = client.get(f"/api/jobs/{job_id}").json()
            if state["status"] in {"completed", "failed"}:
                break
            time.sleep(.05)
        assert state["status"] == "completed", state
        original = client.get(f"/api/jobs/{job_id}/result").json()
        assert original["shots"]
        before_shots = [(shot["id"], shot["start_time"], shot["end_time"]) for shot in original["shots"]]
        before_prompts = {item["id"]: item["positive_prompt"] for item in original["prompts"]}
        style = client.get(f"/api/jobs/{job_id}/style").json()
        assert style["profile"]["status"] == "draft"
        assert style["profile"]["mode"] == "creative"
        assert style["observation"]["raw_response"]
        assert {prompt["template_id"] for prompt in original["prompts"]} == {"first-frame-v7", "seedance-video-v8"}
        profile = style["profile"]
        profile["per_shot_effect_overrides"] = {original["shots"][0]["id"]: ["visible_effect.film-grain"]}
        saved = client.put(f"/api/jobs/{job_id}/style", json=profile)
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == profile["version"] + 1
        unchanged = client.get(f"/api/jobs/{job_id}/result").json()
        assert {item["id"]: item["positive_prompt"] for item in unchanged["prompts"]} == before_prompts
        confirmed = client.post(f"/api/jobs/{job_id}/style/confirm")
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "confirmed"
        result = client.get(f"/api/jobs/{job_id}/result").json()
        assert [(shot["id"], shot["start_time"], shot["end_time"]) for shot in result["shots"]] == before_shots
        assert {prompt["template_id"] for prompt in result["prompts"]} == {"first-frame-v7", "seedance-video-v8"}
        global_line = confirmed.json()["rendered_style_prompt"]
        assert all(global_line in prompt["positive_prompt"] for prompt in result["prompts"])
        assert all("局部视觉效果：" not in prompt["positive_prompt"] for prompt in result["prompts"] if prompt["prompt_type"] == "first_frame")
        assert any("局部视觉效果：" in prompt["positive_prompt"] for prompt in result["prompts"] if prompt["prompt_type"] == "video")
        history = client.get(f"/api/jobs/{job_id}/style/history").json()
        assert len(history["profiles"]) >= 2
        assert len(history["prompt_revisions"]) == len(original["prompts"])
        client.app.state.job_service.provider.propose_style = lambda _prompt, _frames: "{not-json"
        failed_regen = client.post(f"/api/jobs/{job_id}/style/draft")
        assert failed_regen.status_code == 200
        assert failed_regen.json()["profile"]["status"] == "confirmed"
        assert failed_regen.json()["observation"]["raw_tags_response"] == "{not-json"
        assert {item["id"]: item["positive_prompt"] for item in client.get(f"/api/jobs/{job_id}/result").json()["prompts"]} == {item["id"]: item["positive_prompt"] for item in result["prompts"]}


def test_project_style_uses_two_ordered_calls_and_frame_evidence():
    import json
    import re
    from types import SimpleNamespace
    from app.providers.base import FrameInput
    from app.services.style_service import DIMENSIONS, propose_style

    class Provider:
        calls = []
        def health(self):
            return SimpleNamespace(model="qwen3.8-flash")
        def propose_style(self, prompt, frames):
            self.calls.append((prompt, [frame.id for frame in frames]))
            if "画风基础标签（style.tags）" in prompt:
                return json.dumps({"tags": {key: "二维手绘动画" if key == "medium" else "未知"
                                            for key in DIMENSIONS if key != "negative_constraint"}}, ensure_ascii=False)
            candidates = re.findall(r'"id":\s*"([a-z_]+\.[a-z0-9-]+)"', prompt)
            picked = []
            for category in DIMENSIONS:
                if category in ("negative_constraint", "visible_effect"):
                    continue
                term_id = next((item for item in candidates if terms()[item].category == category), None)
                if term_id:
                    picked.append({"term_id": term_id, "frames": ["F1"], "confidence": .8, "note": "帧内可见"})
            return json.dumps({"selections": picked, "anchors": [], "uncertainties": []}, ensure_ascii=False)

    provider = Provider()
    observation, profile = propose_style(provider, "波普动画", [FrameInput("frame-1", 1.2, "/unused.jpg")], "observed")
    assert len(provider.calls) == 2
    assert all(call[1] == ["frame-1"] for call in provider.calls)
    assert observation.base_tags["medium"] == "二维手绘动画"
    assert all(ref.frame_id == "frame-1" and ref.timestamp == 1.2 for claims in observation.dimensions.values()
               for claim in claims for ref in claim.evidence)
    assert profile.status == "draft" and profile.model == "qwen3.8-flash"


def test_incomplete_observation_is_preserved_without_invented_style_recipe():
    import json
    from types import SimpleNamespace
    from app.providers.base import FrameInput
    from app.services.style_service import DIMENSIONS, propose_style

    class SparseProvider:
        def health(self):
            return SimpleNamespace(model="qwen3.8-flash")
        def propose_style(self, prompt, frames):
            if "画风基础标签（style.tags）" in prompt:
                return json.dumps({"tags": {key: "未知" for key in DIMENSIONS if key != "negative_constraint"}}, ensure_ascii=False)
            return json.dumps({"selections": [{"term_id": "medium.two-d-animation", "frames": ["F1"],
                    "confidence": .9, "note": "平面角色"}], "anchors": [], "uncertainties": []}, ensure_ascii=False)

    observation, profile = propose_style(SparseProvider(), "二维手绘动画",
                                         [FrameInput("frame-1", 1.0, "/unused.jpg")], "observed")
    assert profile is None
    assert observation.dimensions["medium"][0].evidence[0].frame_id == "frame-1"
    assert observation.raw_response and observation.raw_tags_response
    assert any("需人工补全" in item for item in observation.uncertainties)
