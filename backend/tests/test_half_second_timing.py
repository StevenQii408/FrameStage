import math

import pytest

from app.services.generation_timing import allocate_phase_durations, ceil_half_seconds


@pytest.mark.parametrize('original,expected', [(3.23, 3.5), (3.5, 3.5), (3.51, 4),
    (4.01, 4.5), (5.483333, 5.5), (.73, 1), (3.5000000000000004, 3.5)])
def test_generation_duration_rounds_up_without_changing_source(original, expected):
    assert ceil_half_seconds(original) == expected


def test_phase_budget_ceil_once_then_preserve_order():
    values = allocate_phase_durations([1.21, 1.21, 1.21])
    assert sum(values) == 4
    assert all(value > 0 and value * 2 == int(value * 2) for value in values)
    with pytest.raises(ValueError, match='预算不足'):
        allocate_phase_durations([.1, .1, .1])


@pytest.mark.parametrize('value', [0, -1, math.nan, math.inf])
def test_invalid_duration_rejected(value):
    with pytest.raises(ValueError):
        ceil_half_seconds(value)


def test_source_contract_accepts_uncut_take_and_prompt_has_quantized_time():
    from app.schemas import Shot, ShotContract
    from app.services.prompt_service import build_prompts
    from test_prompt_profiles import observation
    assert ShotContract(generation_duration_seconds=30).generation_duration_seconds == 30
    shot = Shot(id='long-source', number=1, start_time=0, end_time=5.483333,
        duration=5.483333, time='', generation_duration_seconds=5.5)
    assert shot.end_time == 5.483333
    _, video = build_prompts('coffee', observation(), generation_duration_seconds=5.483333)
    assert video.contract.generation_duration_seconds == 5.5
    assert '时长：5.5秒' in video.positive_prompt


def test_static_success_does_not_claim_video_ready_when_dynamics_failed():
    from app.services.prompt_service import build_prompts
    from test_prompt_profiles import observation
    value = observation()
    value.subject_action = '未知'
    value.action_details.steps = []
    value.action_details.initial_state = '未知'
    value.action_details.final_state = '未知'
    value.analysis_review = {'static': {'status': 'ready'},
        'dynamic': {'status': 'pending_review'}, 'video_ready': False}
    first, video = build_prompts('partial', value, generation_duration_seconds=3.23)
    assert video.status == 'draft'
    assert first.positive_prompt and '动作：未知' not in video.positive_prompt
    assert video.contract.delivery['video_ready'] is False
