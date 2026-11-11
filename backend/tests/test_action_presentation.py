"""Golden action cases: semantic phases, not equal subdivision of four frames."""

import pytest

from app.perception_models import ActionPhase, EvidenceRef
from app.schemas import ActionDetails, VisionObservation
from app.services.action_presentation import build_action_presentation


def observed_action(summary, steps=(), *, accompanying=(), phases=(), step_evidence=()):
    return VisionObservation(
        subject_action=summary,
        shot_size="中景",
        lens="标准",
        camera_movement="固定",
        lighting_vibe="自然柔光",
        action_details=ActionDetails(
            primary_subject="画面主体",
            initial_state="主体位于画面中央",
            steps=list(steps),
            final_state="保持画面中可见的结束状态",
            accompanying_states=list(accompanying),
            action_phases=list(phases),
            step_evidence=list(step_evidence),
        ),
    )


def evidence(frame="frame-1", timestamp=10.0, description="画面中的可见状态"):
    return EvidenceRef(frame_id=frame, timestamp=timestamp, description=description)


def phase(description, indices, *, start=None, end=None, frame="frame-1", timestamp=10.0):
    return ActionPhase(
        description=description,
        source_step_indices=indices,
        evidence=[evidence(frame, timestamp, description)],
        confidence=0.9,
        generation_start_seconds=start,
        generation_end_seconds=end,
    )


def text_of(result):
    return "；".join([result["main_action"], *(item["description"] for item in result["phases"])])


# The summaries are deliberately specific, while some source observations describe
# finer poses. The renderer must not assign those poses fabricated start/end times.
GOLDEN_ACTIONS = [
    ("drinking", "女子饮用杯中液体", ["女子仰头", "女子饮用液体", "女子停止饮用"], ["女子", "饮用"]),
    ("stir-and-scoop", "女孩搅拌后用勺子舀起饮料", ["女孩搅拌饮料", "女孩用勺子舀起饮料"], ["搅拌", "舀起"]),
    ("walk-and-place-book", "女孩向右走到桌前并放下书", ["女孩向右走近桌子", "女孩把书放在桌面"], ["向右", "书"]),
    ("drink-and-smash", "女子饮用后摔碎杯子", ["女子饮用杯中液体", "女子摔碎杯子"], ["饮用", "摔碎"]),
    ("handover-and-receive", "男人将书递给女孩，女孩接过书", ["男人将书递给女孩", "女孩接过书"], ["男人", "女孩", "递给", "接过"]),
    ("static-cup", "男孩双手持杯保持站立", ["男孩双手持杯保持站立"], ["持杯", "站立"]),
    ("late-appearance", "男子进入画面后站在门边", ["男子进入画面", "男子站在门边"], ["进入", "门边"]),
    ("visible-head-turn", "女子低头转向男孩", ["女子头部回正", "女子低头转向男孩"], ["低头", "男孩"]),
    ("directional-handover", "女孩将杯子向右递给男人", ["女孩将杯子向右移动", "男人接过杯子"], ["女孩", "向右", "杯子"]),
    ("turn-and-enter", "男人转身走进房间", ["男人转身", "男人走进房间"], ["转身", "房间"]),
    ("phone-and-leave", "女子接起电话后离开座位", ["女子接起电话", "女子离开座位"], ["电话", "离开"]),
    ("door-and-wait", "男子打开门并停在门口", ["男子打开门", "男子停在门口"], ["打开", "门口"]),
    ("run-and-stop", "女孩奔跑后停在路边", ["女孩向前奔跑", "女孩停在路边"], ["奔跑", "停在"]),
    ("rolling-object", "球沿地面向左滚动", ["球沿地面向左滚动"], ["球", "向左"]),
    ("bird-takeoff", "鸟张开翅膀并飞离树枝", ["鸟张开翅膀", "鸟飞离树枝"], ["翅膀", "飞离"]),
    ("pill-release", "手指松开药片，使药片落入杯中", ["手指松开药片", "药片落入杯中"], ["松开", "药片", "杯中"]),
    ("close-box", "男子合上盒盖", ["男子将盒盖向下移动", "男子合上盒盖"], ["合上", "盒盖"]),
    ("slice-and-serve", "厨师切开水果并将水果放到盘中", ["厨师切开水果", "厨师将水果放到盘中"], ["切开", "盘中"]),
    ("car-turn-and-stop", "汽车向左转弯后停在路边", ["汽车向左转弯", "汽车停在路边"], ["向左", "停在"]),
    ("reaction", "女子放下杯子后笑容消失并皱眉", ["女子放下杯子", "女子笑容消失并皱眉"], ["放下", "笑容", "皱眉"]),
    ("hand-motion-only", "手将杯子移离面部", ["手将杯子移离面部"], ["杯子", "移离"]),
    ("camera-only", "无人机航拍城市并缓慢下降", ["城市建筑的可见范围逐渐变化"], ["城市", "下降"]),
    ("opening-and-reveal", "女孩拉开抽屉，取出钥匙", ["女孩拉开抽屉", "女孩取出钥匙"], ["抽屉", "钥匙"]),
    ("two-subjects", "男人转向女子，女子后退一步", ["男人转向女子", "女子后退一步"], ["男人", "女子", "后退"]),
]


@pytest.mark.parametrize("case,summary,steps,required", GOLDEN_ACTIONS, ids=[case[0] for case in GOLDEN_ACTIONS])
def test_golden_actions_do_not_receive_equal_division(case, summary, steps, required):
    result = build_action_presentation(observed_action(summary, steps), 4.8)
    assert result["schema_version"] == "action-presentation.v1"
    assert result["timing_mode"] in {"none", "sequence"}
    assert all(token in text_of(result) for token in required)
    assert all(item["start_seconds"] is None and item["end_seconds"] is None for item in result["phases"])
    assert "0.425" not in str(result)


@pytest.mark.parametrize("summary", ["未知", "人物移动", "表情变化", "画面变化"])
def test_missing_or_generic_summary_preserves_independent_source_events(summary):
    steps = ["女子饮用杯中液体", "女子摔碎杯子"]
    result = build_action_presentation(observed_action(summary, steps), 4.8)
    assert "饮用" in text_of(result) and "摔碎" in text_of(result)
    assert result["source"] == "source_steps"
    assert result["timing_mode"] != "generation-suggestion"


@pytest.mark.parametrize(
    "summary,steps,retained_event",
    [
        ("女子饮用杯中液体", ["女子饮用杯中液体", "女子摔碎杯子"], "摔碎"),
        ("女孩走到桌前", ["女孩走到桌前", "女孩把书放在桌面"], "书"),
        ("女子喝下饮料", ["女子喝下饮料", "女子笑容消失并皱眉"], "皱眉"),
    ],
)
def test_summary_does_not_silently_drop_distinct_key_events(summary, steps, retained_event):
    result = build_action_presentation(observed_action(summary, steps), 4.8)
    assert retained_event in text_of(result)
    assert result["warnings"]


def test_accompanying_state_is_not_an_extra_timed_phase():
    result = build_action_presentation(
        observed_action("女子饮用杯中液体", ["女子饮用液体"], accompanying=["男孩抬头注视女子"]), 1.7
    )
    assert result["accompanying_states"] == ["男孩抬头注视女子"]
    assert result["timing_mode"] == "none"
    assert not result["phases"]
    assert all(word not in str(result) for word in ("全程", "始终", "一直"))


def test_unknown_actions_do_not_invent_a_drinking_or_gaze_event():
    result = build_action_presentation(observed_action("未知"), 1.7)
    assert "未知" in result["main_action"]
    assert "饮用" not in result["main_action"] and "注视" not in result["main_action"]
    assert result["source"] == "unknown"
    assert result["phases"] == [] and result["accompanying_states"] == []
    assert result["timing_mode"] == "none"


def two_phase_observation(first_start=0.0, first_end=2.6, second_start=2.6, second_end=4.8):
    return observed_action(
        "女子饮用后放下杯子并皱眉",
        ["女子拿起杯子", "女子饮用杯中液体", "女子放下杯子", "女子皱眉"],
        phases=[
            phase("女子拿起杯子并饮用", [1, 2], start=first_start, end=first_end),
            phase("女子放下杯子并皱眉", [3, 4], start=second_start, end=second_end, frame="frame-4", timestamp=14.0),
        ],
    )


def test_two_semantic_phases_keep_model_suggested_not_equal_ranges():
    result = build_action_presentation(two_phase_observation(), 4.8)
    assert result["source"] == "phases"
    assert result["timing_mode"] == "generation-suggestion"
    assert [(item["start_seconds"], item["end_seconds"]) for item in result["phases"]] == [(0.0, 2.5), (2.5, 5.0)]
    assert [item["source_step_indices"] for item in result["phases"]] == [[1, 2], [3, 4]]
    assert result["evidence_granularity"] == "item"
    assert len(result["phases"]) == 2


def test_untimed_phases_preserve_order_without_fabricating_boundaries():
    visual = two_phase_observation(None, None, None, None)
    result = build_action_presentation(visual, 4.8)
    assert result["timing_mode"] == "sequence"
    assert [item["description"] for item in result["phases"]] == ["女子拿起杯子并饮用", "女子放下杯子并皱眉"]
    assert all(item["start_seconds"] is None and item["end_seconds"] is None for item in result["phases"])


@pytest.mark.parametrize(
    "ranges",
    [(0.0, 1.5, 2.0, 4.8), (0.0, 3.0, 2.0, 4.8), (0.2, 2.6, 2.6, 4.8), (0.0, 2.6, 2.6, 5.0)],
    ids=["gap", "overlap", "missing-start", "past-duration"],
)
def test_invalid_generation_ranges_degrade_to_untimed_description(ranges):
    result = build_action_presentation(two_phase_observation(*ranges), 4.8)
    assert result["timing_mode"] != "generation-suggestion"
    assert all(item["start_seconds"] is None and item["end_seconds"] is None for item in result["phases"])
    assert "饮用" in text_of(result) and "皱眉" in text_of(result)
    assert result["warnings"]


@pytest.mark.parametrize("indices", [([1, 2], [2, 3, 4]), ([1], [3, 4]), ([3, 4], [1, 2])], ids=["duplicate", "missing", "reversed"])
def test_invalid_source_ownership_does_not_remove_actions(indices):
    visual = two_phase_observation()
    visual.action_details.action_phases = [
        phase("女子拿起杯子并饮用", indices[0], start=0.0, end=2.6),
        phase("女子放下杯子并皱眉", indices[1], start=2.6, end=4.8),
    ]
    result = build_action_presentation(visual, 4.8)
    assert result["timing_mode"] != "generation-suggestion"
    assert "饮用" in text_of(result) and "皱眉" in text_of(result)
    assert result["warnings"]


def test_frame_timestamps_are_not_treated_as_relative_action_boundaries():
    visual = two_phase_observation(None, None, None, None)
    result = build_action_presentation(visual, 4.8)
    assert result["timing_mode"] == "sequence"
    assert all(item["start_seconds"] is None for item in result["phases"])
    assert [item["evidence"][0]["timestamp"] for item in result["phases"]] == [10.0, 14.0]


def test_a_single_semantic_phase_is_rendered_without_timing():
    visual = observed_action(
        "女子饮用杯中液体", ["女子拿起杯子", "女子饮用杯中液体"],
        phases=[phase("女子拿起杯子并饮用", [1, 2], start=0.0, end=4.8)],
    )
    result = build_action_presentation(visual, 4.8)
    assert result["timing_mode"] == "none"
    assert all(item["start_seconds"] is None and item["end_seconds"] is None for item in result["phases"])


def test_presentation_is_deterministic_and_does_not_mutate_detailed_observations():
    visual = two_phase_observation()
    before = visual.model_dump(mode="json")
    assert build_action_presentation(visual, 4.8) == build_action_presentation(visual, 4.8)
    assert visual.model_dump(mode="json") == before


def test_per_item_evidence_does_not_disappear_when_summary_falls_back():
    refs = [[evidence("frame-1", 0.1, "女子饮用")], [evidence("frame-4", 1.6, "女子摔碎杯子")]]
    visual = observed_action("未知", ["女子饮用杯中液体", "女子摔碎杯子"], step_evidence=refs)
    result = build_action_presentation(visual, 1.7)
    assert result["evidence_granularity"] == "item"
    assert visual.action_details.step_evidence == refs
    assert result["timing_mode"] != "generation-suggestion"


@pytest.mark.parametrize("summary", ["女子把杯子向左移动", "女子移动杯子"])
def test_explicit_direction_conflict_or_omission_preserves_source(summary):
    result = build_action_presentation(observed_action(summary, ["女子把杯子向右移动"]), 2.0)
    assert result["main_action"] == "女子把杯子向右移动"
    assert result["source"] == "source_steps" and result["warnings"]


def test_background_pose_is_not_scheduled_as_a_later_phase():
    visual = observed_action("女子饮用液体", ["女子饮用液体", "男孩保持抬头注视姿态"], phases=[
        phase("女子饮用液体", [1], start=0.0, end=1.0, timestamp=.2),
        phase("男孩保持抬头注视姿态", [2], start=1.0, end=2.0, frame="frame-2", timestamp=1.8),
    ])
    result = build_action_presentation(visual, 2.0)
    assert result["timing_mode"] == "none" and result["phases"] == []
    assert "女子饮用" in result["main_action"] and result["warnings"]


def test_compact_rendering_shortens_only_delivery_and_preserves_exact_phase_data():
    from copy import deepcopy
    from app.services.action_presentation import render_action_lines
    visual = two_phase_observation(0, 2.634782, 2.634782, 4.8)
    result = build_action_presentation(visual, 4.8)
    before = deepcopy(result)
    compact = "\n".join(render_action_lines(result, compact=True))
    assert "0–2.5 秒：女子拿起杯子并饮用。" in compact
    assert "2.5–5 秒：女子放下杯子并皱眉。" in compact
    assert visual.action_details.action_phases[0].generation_end_seconds == 2.634782
    assert "不是原片动作时间实测" not in compact
    assert "不是原片动作时间实测" in "\n".join(render_action_lines(result))
    assert result == before


def test_compact_rendering_keeps_sampled_state_scope_and_tiny_intervals():
    from app.services.action_presentation import render_action_lines
    result = build_action_presentation(two_phase_observation(0, .003, .003, 4.8), 4.8)
    result["accompanying_states"] = ["男孩抬头注视女子"]
    compact = "\n".join(render_action_lines(result, compact=True))
    assert "0–0.5 秒" in compact
    assert "伴随状态（采样可见）：男孩抬头注视女子。" in compact
    assert "全程" not in compact
