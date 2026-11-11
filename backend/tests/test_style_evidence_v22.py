import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from app.db import Database, utc_now
from app.style_vector.index import StyleVectorIndex
from app.style_vector.models import ShotStyleObservationV2
from app.style_vector.ontology import RECIPES, terms, validate_catalog
from app.style_vector.recommendation import ProjectStyleReview, recipe_candidates, review_project
from app.style_vector.wire import parse_style_response
from app.style_vector.workflow import AdjudicationResponse, _adjudicate, _aggregate, auto_apply_visual_bible, draft_visual_bible
from test_style_recommendation import candidate, observations
from test_style_vector_v2 import FakeEmbedder
from test_prompt_profiles import observation as vision_observation


def test_catalog_prioritizes_realistic_cinema_and_phone_not_animation():
    validate_catalog()
    families = Counter(recipe.family for recipe in RECIPES.values())
    assert len(RECIPES) == 32
    assert len(terms()) == 151
    assert families['cinematic'] + families['documentary'] + families['phone'] == 19
    assert families['phone'] == 5
    for recipe in RECIPES.values():
        assert len(recipe.global_prompt) >= 60
        assert recipe.retrieval_description and recipe.positive_cues and recipe.negative_cues
        assert all(anchor not in recipe.retrieval_text() for anchor in recipe.optional_anchors)
        assert recipe.global_prompt not in recipe.retrieval_text()
    assert '缓入缓出' not in RECIPES['recipe.warm-stylized-3d'].retrieval_text()
    assert '写实摄影' in RECIPES['recipe.clean-digital-realism'].retrieval_description


def test_image_recipe_recall_works_without_any_text_and_does_not_average_frames():
    obs = [ShotStyleObservationV2(shot_id='a'), ShotStyleObservationV2(shot_id='b')]
    index = SimpleNamespace(query_text=Mock(side_effect=AssertionError('must not call text embedding')),
                            search_recipes=Mock(return_value=[{'term_id': 'recipe.phone-daylight', 'rank': 1}]))
    images = {'a': np.eye(2)[0], 'b': np.eye(2)[1]}
    result = recipe_candidates(index, obs, [], images)
    assert len(result) == 1
    assert result[0]['recipe_id'] == 'recipe.phone-daylight'
    assert result[0]['ranks'] == {'image': 1}
    assert result[0]['image_support_shot_ids'] == ['a', 'b']
    assert not result[0]['supported_term_ids']
    assert result[0]['status'] == 'candidate'
    assert index.search_recipes.call_count == 2
    for call in index.search_recipes.call_args_list:
        assert np.count_nonzero(call.args[1]) == 1


def test_documentary_and_photography_are_compatible_but_animation_is_not():
    obs, matches, _ = observations()
    matches[0].term_id = 'medium.photography'
    index = SimpleNamespace(query_text=Mock(return_value=np.ones(1024)), search_recipes=Mock(return_value=[
        {'term_id': 'recipe.phone-daylight', 'rank': 1},
        {'term_id': 'recipe.warm-stylized-3d', 'rank': 2}]))
    result = recipe_candidates(index, obs, matches[:1], {})
    assert [item['recipe_id'] for item in result] == ['recipe.phone-daylight']


def test_array_adjudication_is_locally_wrapped_and_still_candidate_checked():
    _, _, frames = observations()
    response = [dict(shot_id='shot-0', category='texture_material', term_id='texture.clean-digital', reason='平滑表面'),
                dict(shot_id='shot-1', category='texture_material', term_id='not-in-candidates', reason='非法候选')]
    service = SimpleNamespace(provider=SimpleNamespace(health=lambda: SimpleNamespace(provider='bailian'),
                                                      _call=Mock(return_value=json.dumps(response))), _emit=Mock())
    ambiguous = [dict(shot_id=f'shot-{i}', category='texture_material', description='',
                      candidates=[{'term_id': 'texture.clean-digital'}]) for i in range(2)]
    retrieval = {}
    result = _adjudicate(service, 'job', ambiguous, frames, retrieval)
    assert result == {('shot-0', 'texture_material'): 'texture.clean-digital'}
    assert service.provider._call.call_count == 1
    assert retrieval['adjudication'][0]['repairs'] == ['wrapped-array:decisions']


def test_array_project_review_keeps_valid_features_and_never_invents_recipe_verdict():
    obs, matches, frames = observations()
    raw = [dict(shot_id='shot-0', category='form_language', description='风格化写实',
                term_id='form.stylized-realism', confidence=.9, frame_id='frame-0'),
           dict(shot_id='shot-1', category='texture_material', description='光滑表面',
                term_id='texture.clean-digital', confidence=5, frame_id='frame-1')]
    service = SimpleNamespace(provider=SimpleNamespace(health=lambda: SimpleNamespace(provider='bailian'),
                                                      _call=Mock(return_value=json.dumps(raw))), _emit=Mock())
    candidates, retrieval = [candidate()], {}
    review_project(service, 'job', obs, matches, frames, candidates, retrieval)
    assert obs[0].form_language.observable
    assert not obs[1].texture_material.observable
    assert candidates[0]['status'] == 'candidate'
    assert not candidates[0]['evidence']
    trace = retrieval['project_review'][0]
    assert trace['status'] == 'parsed'
    assert trace['repairs'] == ['wrapped-array:features']
    assert trace['rejected_records'][0]['index'] == 1
    assert service.provider._call.call_count == 1


@pytest.mark.parametrize('raw', ['{"unexpected": []}', '"text"', '{"features": {}}',
                                     '{"features": [{"extra": true}]}', 'not-json'])
def test_invalid_structures_remain_rejected(raw):
    with pytest.raises(ValueError):
        parse_style_response(raw, ProjectStyleReview, array_key='features', trace={})


def test_local_response_budget_is_bounded():
    response = [dict(shot_id='a', category='form_language', term_id=None, reason='未知')] * 21
    with pytest.raises(ValueError, match='预算'):
        parse_style_response(json.dumps(response), AdjudicationResponse, array_key='decisions', trace={})


def test_recipe_only_change_invalidates_index(tmp_path, monkeypatch):
    index = StyleVectorIndex(Database(tmp_path / 'index.sqlite'), FakeEmbedder())
    index.build()
    assert index.health()['ready']
    monkeypatch.setattr('app.style_vector.index.recipe_hash', lambda: 'new-recipe-definition')
    assert not index.health()['ready']


def prepared_service(tmp_path, count=3):
    db = Database(tmp_path / 'jobs.sqlite')
    db.init()
    db.insert_job(dict(id='job', status='completed', stage='completed', progress=100,
                       config={'style_strategy': 'vector-v2'}, created_at=utc_now(), updated_at=utc_now()))
    obs, matches, frames = observations(count)
    for i in range(count):
        db.execute('INSERT INTO shots(id,job_id,number,start_time,end_time,duration,time,scene_group_id,observation_json) VALUES(?,?,?,?,?,?,?,?,?)',
                   (f'shot-{i}', 'job', i+1, float(i), float(i+1), 1., str(i), 'scene-1', vision_observation().model_dump_json()))
        db.execute('INSERT INTO frames(id,job_id,shot_id,timestamp,path,thumbnail_path,width,height) VALUES(?,?,?,?,?,?,?,?)',
                   (f'frame-{i}', 'job', f'shot-{i}', float(i), '/unused.jpg', '/unused.jpg', 768, 1366))
    service = SimpleNamespace(db=db, _emit=Mock(), _set_stage=Mock(),
                              provider=SimpleNamespace(health=lambda: SimpleNamespace(provider='bailian', model='qwen3.8-flash'),
                                                       drain_usage=lambda: []))
    return service, obs, matches, frames


def test_automatic_application_uses_verified_terms_not_full_recipe(tmp_path):
    service, obs, matches, _ = prepared_service(tmp_path)
    bible = _aggregate(obs, matches, {}, mode='observed')
    bible.recommended_recipes = ['recipe.warm-stylized-3d']
    service.db.update_job('job', visual_bible_json=bible.model_dump_json())
    applied = auto_apply_visual_bible(service, 'job')
    assert applied.status == 'confirmed'
    assert applied.model_provenance['application_mode'] == 'auto-evidence'
    assert not applied.selected_recipe_id
    prompts = service.db.fetchall('SELECT positive_prompt FROM prompts WHERE job_id=?', ('job',))
    assert len(prompts) == 6
    assert all('整体画面风格：三维角色动画。' in row['positive_prompt'] for row in prompts)
    assert all('项目画风待确认' not in row['positive_prompt'] and '暖土色' not in row['positive_prompt'] for row in prompts)
    assert auto_apply_visual_bible(service, 'job') is None


def test_insufficient_or_creative_evidence_is_not_automatically_invented(tmp_path):
    service, obs, matches, _ = prepared_service(tmp_path, count=1)
    bible = _aggregate(obs, matches, {}, mode='observed')
    service.db.update_job('job', visual_bible_json=bible.model_dump_json())
    assert auto_apply_visual_bible(service, 'job') is None
    bible.mode = 'creative'
    bible.selected_recipe_id = 'recipe.phone-daylight'
    service.db.update_job('job', visual_bible_json=bible.model_dump_json())
    assert auto_apply_visual_bible(service, 'job') is None
    assert not service.db.fetchall('SELECT * FROM prompts')


def test_sparse_video_pipeline_recalls_static_images_and_records_verified_evidence(tmp_path, monkeypatch):
    service, _, _, frames = prepared_service(tmp_path)
    obs = [ShotStyleObservationV2(shot_id=f'shot-{i}', scene_group_id='scene-1') for i in range(3)]
    class Index:
        def __init__(self, *args): pass
        def health(self): return {'ready': True}
        def manifest(self): return {'version': 'fake'}
        def query_image(self, path): return np.ones(1024)
        def search(self, channel, category, vector, limit):
            assert channel == 'vision'
            term = next(item for item in terms().values() if item.category == category)
            if category == 'medium_rendering': term = terms()['medium.three-d-animation']
            return [{'term_id': term.id, 'rank': 1}]

    def call(prompt, images, phase, **kwargs):
        assert phase == 'style.adjudication'
        return json.dumps([dict(shot_id=f'shot-{i}', category='medium_rendering', term_id='medium.three-d-animation', reason='可见立体体积') for i in range(3)])

    service.provider._call = Mock(side_effect=call)
    service.settings = SimpleNamespace(style_embed_concurrency=1)
    monkeypatch.setattr('app.style_vector.workflow._observations', lambda *args: obs)
    monkeypatch.setattr('app.style_vector.workflow._frames', lambda *args: frames)
    monkeypatch.setattr('app.style_vector.workflow.BailianStyleEmbedder', lambda settings: SimpleNamespace(drain_usage=lambda: [], close=lambda: None))
    monkeypatch.setattr('app.style_vector.workflow.StyleVectorIndex', Index)
    monkeypatch.setattr('app.style_vector.workflow.recipe_candidates', lambda *args, **kwargs: [])
    result = draft_visual_bible(service, 'job')
    assert result.status == 'confirmed'
    assert result.global_core_terms == ['medium.three-d-animation']
    assert all(item.medium_rendering.observable for item in obs)
    assert not any(item.motion_signature.observable for item in obs)
    assert service.provider._call.call_count == 1
    stored = json.loads(service.db.fetchone('SELECT style_retrieval_json FROM jobs WHERE id=?', ('job',))[0])
    assert all(item['category'] != 'medium_rendering' for item in stored['unresolved'])
    assert result.model_provenance['recognition_pipeline'] == 'style-evidence.v2.2'


def test_text_embedding_failure_does_not_block_image_recipe_recall():
    obs, matches, _ = observations()
    index = SimpleNamespace(query_text=Mock(side_effect=TimeoutError('text channel timeout')),
                            search_recipes=Mock(return_value=[{'term_id': 'recipe.warm-stylized-3d', 'rank': 1}]))
    retrieval = {}
    result = recipe_candidates(index, obs, matches, {'shot-0': np.ones(1024)}, retrieval=retrieval)
    assert result[0]['ranks'] == {'image': 1}
    assert retrieval['errors'][0]['phase'] == 'style.recipe_text_retrieval'
    assert retrieval['errors'][0]['detail'] == 'text channel timeout'
