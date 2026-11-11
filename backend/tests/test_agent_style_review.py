"""Adversarial scope tests: review suggestions never mutate sources or spend calls."""
import copy
import json

from app.event_agent.models import EventAgentConfig
from app.event_agent.projection import project_event_agent_result
from app.event_agent.service import EventAgentService
from app.perception_models import VisualObservationV2, merge_stages
from app.style_direct.inheritance import build_scene_style_overrides
from app.style_direct.models import InterpretationClaim, StyleProfileV3, render_profile
from test_event_agent import Provider, event, frame, ref, run, source
from test_event_agent_api import client


def boundary_source(confidence, evidence):
    return {'job': {'montage_json': json.dumps({'relations': [{
        'left_shot_id': 'a', 'right_shot_id': 'b', 'confidence': confidence,
        'relation_type': '场景转换', 'observed_edit_fact': '画面切换',
        'scene_change': True, 'evidence': evidence}]})}, 'shots': [{'id': 'a'}, {'id': 'b'}]}


def test_weak_and_missing_boundary_evidence_remain_advisory_not_hard():
    for confidence, evidence in ((.4, ['局部画面变化']), (.99, [])):
        snapshot = boundary_source(confidence, evidence)
        assert not EventAgentService._protected_boundaries(snapshot)
        assessment = EventAgentService._boundary_assessments(snapshot)[0]
        assert assessment['strength'] == 'candidate'
    saved = boundary_source(.95, ['历史已记录相邻两侧画面'])
    assert EventAgentService._protected_boundaries(saved) == {('a', 'b')}
    assert EventAgentService._boundary_assessments(saved)[0]['evidence_basis'] == 'legacy-recorded-evidence'


def test_modern_boundary_requires_context_from_both_sides():
    snapshot = boundary_source(.95, ['仅有自由文本说明'])
    payload = json.loads(snapshot['job']['montage_json'])
    payload['version'] = 'montage-relations.v3'
    snapshot['job']['montage_json'] = json.dumps(payload)
    assert EventAgentService._protected_boundaries(snapshot) == set()
    for sid in ('a', 'b'):
        payload['relations'][0].setdefault('context_evidence', []).append({
            'kind': 'location', 'value': '室内', 'shot_id': sid, 'source_field': 'scene', 'quote': '室内客厅'})
    snapshot['job']['montage_json'] = json.dumps(payload)
    assert EventAgentService._protected_boundaries(snapshot) == {('a', 'b')}


def test_candidate_boundary_does_not_block_evidenced_event_and_is_shown_as_candidate():
    shots = [source(1, 0), source(2, 2)]
    shots[1].scene_group_id = 'scene-2'
    shots[1].scene_reason = 'candidate-boundary: 低置信场次猜测待复核'
    frames = [frame(shot) for shot in shots]
    provider = Provider([json.dumps({'action': 'finalize', 'events': [event(shots, frames)]})])
    candidates = [{'left_shot_id': shots[0].id, 'right_shot_id': shots[1].id, 'strength': 'candidate'}]
    result = run(shots, frames, provider, candidate_boundaries=candidates)
    assert len(result.events) == 1 and result.events[0].status == 'ready'
    assert 'candidate_boundaries' in provider.calls[0][0]
    assert shots[1].id in provider.calls[0][0]
    assert len(provider.calls) == 1


def test_saved_weak_scene_guess_is_demoted_for_entire_scene_without_editing_snapshot(client):
    from test_event_agent_pipeline import prepare
    jobs = client.app.state.job_service
    prepare(jobs)
    snapshot = jobs.event_agent_service._snapshot('source-job')
    for row in snapshot['shots'][2:]:
        row.update(scene_group_id='scene-002', scene_reason='剧情时空或连续事件变化')
    left, right = snapshot['shots'][1:3]
    snapshot['job']['montage_json'] = json.dumps({'relations': [{
        'left_shot_id': left['id'], 'right_shot_id': right['id'], 'confidence': .4,
        'relation_type': '场景转换', 'observed_edit_fact': '人物切到近景',
        'scene_change': True, 'evidence': ['只有机位变化']} ]})
    before = copy.deepcopy(snapshot)
    shots, frames = jobs.event_agent_service._inputs(snapshot)
    assert all(shot.scene_reason.startswith('candidate-boundary:') for shot in shots[2:])
    assert snapshot == before
    provider = Provider([json.dumps({'action': 'finalize', 'events': [event(shots, frames)]})])
    result = run(shots, frames, provider)
    assert len(result.events) == 1 and result.events[0].status == 'ready'


def test_fact_review_is_pending_explicit_and_does_not_rewrite_valid_sources_or_call_again():
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    before = copy.deepcopy([shot.model_dump() for shot in shots])
    request = {'source_shot_id': shots[0].id, 'field_group': 'action',
        'reason': '杯子仍在桌上，需核对已保存拿杯动作是否提前', 'evidence': [ref(frames[0], '杯子位置')]}
    provider = Provider([json.dumps({'action': 'finalize', 'events': [event(shots, frames)],
                                    'fact_review_requests': [request]}, ensure_ascii=False)])
    result = run(shots, frames, provider)
    assert len(provider.calls) == 1
    assert result.status == 'degraded' and result.events[0].status == 'ready'
    assert [shot.model_dump() for shot in shots] == before
    assert result.audit[0].fact_review_requests[0].reason == request['reason']
    payload = project_event_agent_result(result, shots, frames)
    pending = payload['sequence_metadata']['fact_review_requests'][0]
    assert pending['status'] == 'pending'
    assert pending['suggested_operation'] == {'mode': 'review', 'field_group': 'action', 'reason': request['reason']}
    assert result.config.max_model_calls_per_window == 2


def test_review_cannot_cite_another_source_frame_or_invent_frame_reference():
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    for reference in (ref(frames[1], '其他来源帧'), {'frame_id': 'invented', 'timestamp': .1, 'description': '伪造'}):
        request = {'source_shot_id': shots[0].id, 'field_group': 'action',
                   'reason': '需要核对已保存动作与当前画面是否一致', 'evidence': [reference]}
        provider = Provider([json.dumps({'action': 'finalize', 'events': [event(shots, frames)],
                                        'fact_review_requests': [request]})])
        result = run(shots, frames, provider)
        assert result.fact_review_requests == [] and len(result.events) == 1
        assert result.events[0].status == 'ready' and len(provider.calls) == 1
        assert any('拒绝无效事实复核' in warning for warning in result.warnings)


def baseline():
    return render_profile(StyleProfileV3(descriptions=[InterpretationClaim(dimension='medium_rendering',
        core_concept='基础媒介', professional_description='三维角色动画，体积明暗连续', support_claim_ids=['E1'])]))


def observed_shot(number, medium, confidence=.9, scene='dream'):
    shot = source(number, (number - 1) * 2)
    shot.scene_group_id = scene
    data = merge_stages(None, None).model_dump()
    data['artistic_effects']['medium'] = {'value': medium, 'observable': True, 'confidence': confidence,
        'evidence': [ref(frame(shot), '媒介边缘')], 'uncertainties': []}
    shot.visual_observation_v2 = VisualObservationV2.model_validate(data)
    return shot


def test_two_evidenced_scene_changes_produce_review_candidate_without_mutating_baseline():
    original = baseline()
    snapshot = copy.deepcopy(original.model_dump())
    shots = [observed_shot(1, '多层剪纸拼贴动画，纸纤维边缘清晰'),
             observed_shot(2, '剪纸动画，纸层阴影明显')]
    review = build_scene_style_overrides(shots, original)
    assert original.model_dump() == snapshot
    assert review['extra_model_calls'] == 0 and review['baseline_hash'] == original.profile_hash
    candidate = review['scene_overrides'][0]
    assert candidate['observed_family'] == 'paper' and candidate['status'] == 'review_required'
    assert candidate['supporting_shot_ids'] == [shot.id for shot in shots]
    assert len(candidate['evidence']) == 2


def test_isolated_and_low_confidence_changes_do_not_become_scene_style():
    shots = [observed_shot(1, '剪纸拼贴动画'), observed_shot(2, '剪纸动画', confidence=.4),
             observed_shot(3, '三维角色动画')]
    review = build_scene_style_overrides(shots, baseline())
    assert review['scene_overrides'] == []
    assert len(review['pending_shot_changes']) == 1
    assert review['pending_shot_changes'][0]['supporting_shot_ids'] == [shots[0].id]


def test_prop_and_clothing_description_cannot_form_a_scene_recipe():
    shots = [observed_shot(1, '白裙女孩在木桌前，端着玻璃杯'), observed_shot(2, '黑衣男子拿着银色戒指')]
    review = build_scene_style_overrides(shots, baseline())
    assert review['scene_overrides'] == review['pending_shot_changes'] == []
