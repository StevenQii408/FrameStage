import json
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from app.db import Database
from app.perception_models import EvidenceRef
from app.providers.base import FrameInput
from app.style_vector.index import StyleVectorIndex, exact_aliases
from app.style_vector.models import (
    ProjectVisualBible, ShotStyleObservationV2, StyleFeatureObservation, StyleMatch,
    clean_style_description, render_global, resolve_effective,
)
from app.style_vector.ontology import RECIPES
from app.style_vector.recommendation import grounded_term, recipe_candidates, review_project
from app.style_vector.workflow import _aggregate
from test_style_vector_v2 import FakeEmbedder


def observations(count=3):
    result, matches, frames = [], [], {}
    for i in range(count):
        shot_id, frame_id = f"shot-{i}", f"frame-{i}"
        evidence = [EvidenceRef(frame_id=frame_id, timestamp=float(i), description="立体体块与体积明暗")]
        result.append(ShotStyleObservationV2(shot_id=shot_id, scene_group_id="scene-1",
            medium_rendering=StyleFeatureObservation(description="3D动画", observable=True, confidence=.9, evidence=evidence)))
        matches.append(StyleMatch(term_id="medium.three-d-animation", category="medium_rendering",
            scope="UNCERTAIN", shot_ids=[shot_id], confidence=.9, evidence=evidence))
        frames[shot_id] = FrameInput(frame_id, float(i), "/unused.jpg")
    return result, matches, frames


@pytest.mark.parametrize("description,category,expected", [
    ("风格化写实", "form_language", "风格化写实"),
    ("细腻平滑", "texture_material", "细腻平滑"),
    ("表面细腻平滑，衣服呈蓝色，玻璃杯通透", "texture_material", "表面细腻平滑"),
    ("暖色调，点缀粉色饮料", "color_tone", "暖色调"),
    ("采用PBR与次表面散射", "texture_material", ""),
])
def test_clausal_cleaning_preserves_style_not_content(description, category, expected):
    assert clean_style_description(description, category) == expected


def test_sparse_media_does_not_invent_subtype_or_palette():
    assert exact_aliases("3D动画", "medium_rendering") == ["medium.three-d-animation"]
    assert grounded_term("medium.three-d-animation", "3D动画")
    assert not grounded_term("medium.low-poly-animation", "3D动画，细腻平滑曲面")
    assert not grounded_term("medium.low-poly-animation", "不是低多边形动画")
    assert grounded_term("color.warm-tone", "暖色调")
    assert not grounded_term("palette.warm-earth", "暖色调")
    obs, matches, _ = observations()
    bible = _aggregate(obs, matches, {}, mode="observed")
    assert render_global(bible) == "整体画面风格：三维角色动画。"
    assert "三个维度" not in "".join(bible.unresolved_features)
    assert bible.status == "draft"
    assert not bible.selected_recipe_id


def test_complete_recipe_vectors_are_cached_and_channel_separated(tmp_path):
    embedder = FakeEmbedder()
    embedder.text = Mock(wraps=embedder.text)
    embedder.vision_text = Mock(wraps=embedder.vision_text)
    index = StyleVectorIndex(Database(tmp_path / "style.sqlite"), embedder)
    manifest = index.build()
    assert manifest["recipe_count"] == len(RECIPES)
    assert manifest["recipe_hash"]
    recipe = RECIPES["recipe.warm-stylized-3d"]
    query = index.query_text(recipe.retrieval_text())
    assert "皮克斯" not in recipe.retrieval_text()
    embedder.text.reset_mock()
    embedder.vision_text.reset_mock()
    assert index.search_recipes("text", query)[0]["term_id"] == recipe.id
    index.search_recipes("vision", query)
    assert not embedder.text.called
    assert not embedder.vision_text.called


def test_recipe_retrieval_filters_medium_and_retains_unknown_traits():
    class Index:
        def query_text(self, query):
            assert "3D动画" in query and "衣服" not in query
            return np.ones(1024)

        def search_recipes(self, channel, query):
            return [{"term_id": item, "rank": rank} for rank, item in enumerate([
                "recipe.poetic-luminous-2d", "recipe.warm-stylized-3d", "recipe.fairytale-stylized-3d"], 1)]

    obs, matches, _ = observations()
    candidates = recipe_candidates(Index(), obs, matches, {})
    assert [item["recipe_id"] for item in candidates] == ["recipe.warm-stylized-3d", "recipe.fairytale-stylized-3d"]
    assert all(item["status"] == "candidate" for item in candidates)
    assert candidates[0]["supported_term_ids"] == ["medium.three-d-animation"]
    assert "motion_language.smooth-ease" in candidates[0]["missing_term_ids"]
    assert candidates[0]["optional_anchors"]


def candidate():
    return {"recipe_id": "recipe.fairytale-stylized-3d", "status": "candidate",
            "supported_term_ids": ["medium.three-d-animation"], "missing_term_ids": [], "evidence": []}


def test_one_project_review_adds_only_visible_missing_facts_with_canonical_evidence():
    obs, matches, frames = observations(8)
    response = {"features": [], "recipes": [{"recipe_id": "recipe.fairytale-stylized-3d",
                 "status": "supported", "reason": "部分造型支持，其他特征仍待确认", "frame_ids": ["frame-0"]}]}
    for shot in (0, 2, 5, 7):
        response["features"].append({"shot_id": f"shot-{shot}", "category": "form_language",
            "description": "风格化写实", "term_id": "form.stylized-realism", "confidence": .9, "frame_id": f"frame-{shot}"})
    response["features"].extend([
        {"shot_id": "shot-0", "category": "motion_signature", "description": "平滑缓入缓出",
         "term_id": "motion_language.smooth-ease", "confidence": .9, "frame_id": "frame-0"},
        {"shot_id": "shot-0", "category": "texture_material", "description": "衣服材质柔软",
         "term_id": "texture.clean-digital", "confidence": .9, "frame_id": "frame-0"},
        {"shot_id": "shot-2", "category": "color_tone", "description": "暖色调",
         "term_id": "palette.pastel-low-saturation", "confidence": .9, "frame_id": "another-job-frame"},
    ])
    call = Mock(return_value=json.dumps(response, ensure_ascii=False))
    service = SimpleNamespace(provider=SimpleNamespace(health=lambda: SimpleNamespace(provider="bailian"), _call=call), _emit=Mock())
    candidates, retrieval = [candidate()], {}
    review_project(service, "job-1", obs, matches, frames, candidates, retrieval)
    assert call.call_count == 1
    assert call.call_args.kwargs["max_attempts"] == 1
    assert len(call.call_args.args[1]) == 4
    assert obs[0].form_language.description == "风格化写实"
    assert obs[0].form_language.evidence[0].timestamp == 0.0
    assert not obs[0].motion_signature.observable
    assert not obs[0].texture_material.observable
    assert not obs[2].color_tone.observable
    assert candidates[0]["status"] == "partial"
    assert "raw_response" in retrieval["project_review"][0]
    bible = _aggregate(obs, matches, {}, mode="observed")
    assert not bible.selected_recipe_id
    assert "低饱和" not in bible.rendered_global_prompt


def test_review_parse_failure_keeps_observations_and_never_retries():
    obs, matches, frames = observations()
    call = Mock(return_value="not JSON")
    service = SimpleNamespace(provider=SimpleNamespace(health=lambda: SimpleNamespace(provider="bailian"), _call=call), _emit=Mock())
    retrieval = {}
    review_project(service, "job-1", obs, matches, frames, [candidate()], retrieval)
    assert call.call_count == 1
    assert len(matches) == 3
    assert not obs[0].form_language.observable
    assert retrieval["errors"][0]["phase"] == "style.recipe_review"


def test_recipe_requires_explicit_selection_and_anchor_is_not_a_fact():
    bible = ProjectVisualBible(global_core_terms=["medium.three-d-animation"],
        recipe_recommendations=[candidate()], recommended_recipes=["recipe.fairytale-stylized-3d"])
    assert "童话" not in render_global(bible)
    assert "迪士尼" not in render_global(bible)
    bible.selected_recipe_id = "recipe.fairytale-stylized-3d"
    effective = resolve_effective(bible, "shot-1", "scene-1")
    assert RECIPES[bible.selected_recipe_id].global_prompt in effective["global"]
    assert "迪士尼" not in effective["global"]
