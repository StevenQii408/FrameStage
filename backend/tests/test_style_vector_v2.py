import json
import time
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from app.db import Database
from app.main import app
from app.perception_models import EvidenceRef
from app.style_vector.embedding import normalized
from app.style_vector.index import StyleVectorIndex, rank_fusion
from app.style_vector.models import (
    ShotStyleObservationV2, StyleFeatureObservation, StyleMatch,
    ProjectVisualBible, clean_style_description, render_global,
)
from app.style_vector.ontology import RECIPES, terms, validate_catalog
from app.style_vector.workflow import _aggregate


class FakeEmbedder:
    def _vector(self, text):
        values = np.zeros(1024, dtype=np.float32)
        values[sum(text.encode()) % 1024] = 1
        return normalized(values)

    def text(self, texts):
        return [self._vector(text) for text in texts]

    def vision_text(self, texts):
        return [self._vector(text) for text in texts]

    def vision_image(self, path):
        return self._vector(Path(path).read_bytes().decode()), "fake"


def test_catalog_and_content_are_separate():
    validate_catalog()
    assert 120 <= len(terms()) <= 180
    for sample in ("黑发少女", "白色裙子", "木质桌子", "银色戒指", "橙色饮料"):
        assert clean_style_description(sample) == ""
    assert clean_style_description("低饱和青灰色调") == "低饱和青灰色调"
    assert "木质桌子" not in "".join(term.retrieval_views()[0] for term in terms().values())


def test_two_spaces_keep_separate_ranks_and_versioned_index(tmp_path):
    db = Database(tmp_path / "style.sqlite3")
    index = StyleVectorIndex(db, FakeEmbedder())
    assert not index.health()["ready"]
    manifest = index.build()
    assert manifest["term_count"] == len(terms())
    assert manifest["dimension"] == 1024
    assert index.health()["ready"]
    term = terms()["color.desaturated-cool"]
    query = index.query_text(term.retrieval_views()[0])
    assert index.search("text", "color_tone", query, 5)[0]["term_id"] == term.id
    assert all(row["term_id"] in {item.id for item in terms().values() if item.category == "color_tone"}
               for row in index.search("text", "color_tone", query, 5))
    assert index.build()["ontology_hash"] == manifest["ontology_hash"]
    fused = rank_fusion([{"term_id": "a", "score": .9, "rank": 1}],
                        [{"term_id": "b", "score": .99, "rank": 1}], ["a"])
    assert fused[0]["term_id"] == "a"
    assert fused[0]["ranks"] == {"text": 1, "alias": 1}


def test_aggregation_needs_cross_scene_evidence():
    observations, matches = [], []
    for number in range(1, 5):
        shot_id = f"shot-{number}"
        scene_id = "scene-a" if number <= 2 else "scene-b"
        evidence = EvidenceRef(frame_id=f"frame-{number}", timestamp=float(number), description="可见剪纸边缘")
        feature = StyleFeatureObservation(description="多层剪纸拼贴", observable=True,
                                          confidence=.9, evidence=[evidence])
        observations.append(ShotStyleObservationV2(
            shot_id=shot_id, scene_group_id=scene_id,
            medium_rendering=feature,
        ))
        matches.append(StyleMatch(term_id="medium.paper-cut", category="medium_rendering",
                                  scope="UNCERTAIN", shot_ids=[shot_id], scene_id=scene_id,
                                  confidence=.9, evidence=[evidence], ranks={"text": 1, "image": 2}))
    bible = _aggregate(observations, matches, {"version": "test"}, mode="observed")
    assert bible.global_core_terms == ["medium.paper-cut"]
    assert bible.coverage_profiles["medium.paper-cut"].supporting_scenes == 2
    assert bible.status == "draft"
    assert render_global(bible) == "整体画面风格：剪纸动画。"
    single = _aggregate(observations[:1], matches[:1], {}, mode="observed")
    assert not single.global_core_terms


def test_bible_rejects_conflicting_or_content_global_terms():
    bible = ProjectVisualBible(global_core_terms=["medium.paper-cut", "color.desaturated-cool", "material.paper-fiber"])
    assert "剪纸" in render_global(bible)
    try:
        ProjectVisualBible(global_core_terms=["palette.high-saturation", "palette.low-saturation"])
    except ValueError:
        pass
    else:
        raise AssertionError("冲突术语未被拒绝")
    for recipe in RECIPES.values():
        assert recipe.global_prompt and recipe.shot_constraints


def test_mock_script_uses_v2_bible_and_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_SCRIPT_STRATEGY", "legacy-v2")
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_STYLE_STRATEGY", "vector-v2")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        response = client.post("/api/jobs", data={"input_type": "script", "script_text": "女孩推开纸门。她向窗边走去。"})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        for _ in range(80):
            status = client.get(f"/api/jobs/{job_id}").json()
            if status["status"] in ("completed", "failed"):
                break
            time.sleep(.03)
        assert status["status"] == "completed", status
        result = client.get(f"/api/jobs/{job_id}/result").json()
        assert result["job"]["style_strategy"] == "vector-v2"
        assert result["visual_bible"]["status"] == "draft"
        assert all("项目画风待确认" not in item["positive_prompt"] for item in result["prompts"])
        assert all(item["contract"]["observed_style"] == "项目画风待确认" and item["warnings"] for item in result["prompts"])
        assert client.post(f"/api/jobs/{job_id}/style/draft").status_code == 409
        bible = client.get(f"/api/jobs/{job_id}/visual-bible").json()["bible"]
        # A reliable medium alone is confirmable; do not force extra dimensions
        # or alter prompts before the user explicitly confirms.
        bible["global_core_terms"] = ["medium.three-d-animation"]
        saved_sparse = client.put(f"/api/jobs/{job_id}/visual-bible", json=bible)
        assert saved_sparse.status_code == 200, saved_sparse.text
        assert saved_sparse.json()["rendered_global_prompt"] == "整体画面风格：三维角色动画。"
        before_confirmation = client.get(f"/api/jobs/{job_id}/result").json()
        assert [item["positive_prompt"] for item in before_confirmation["prompts"]] == [item["positive_prompt"] for item in result["prompts"]]
        assert client.post(f"/api/jobs/{job_id}/visual-bible/confirm").status_code == 200
        bible = client.get(f"/api/jobs/{job_id}/visual-bible").json()["bible"]
        bible["selected_recipe_id"] = "recipe.paper-cut-uncanny-geometry"
        saved = client.put(f"/api/jobs/{job_id}/visual-bible", json=bible)
        assert saved.status_code == 200, saved.text
        confirmed = client.post(f"/api/jobs/{job_id}/visual-bible/confirm")
        assert confirmed.status_code == 200, confirmed.text
        after = client.get(f"/api/jobs/{job_id}/result").json()
        assert all("多层手工剪纸拼贴动画" in item["positive_prompt"] for item in after["prompts"])
        assert {item["template_id"] for item in after["prompts"]} == {"first-frame-v7", "seedance-video-v8"}
        assert after["visual_bible"]["selected_recipe_term_ids"] == list(RECIPES["recipe.paper-cut-uncanny-geometry"].term_ids)
