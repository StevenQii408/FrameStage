"""Offline event-delivery regressions: no provider or artifact reads are needed."""
import copy
import json
import re

import pytest

from app.event_agent.models import EventAgentConfig, EventProposal, NarrativeEvent
from app.event_agent.engine import build_event_windows, validate_event_proposals
from app.event_agent.projection import project_event_agent_result
from app.event_agent.rendering import VERSION, render_event_clips
from app.schemas import PromptBundle, ShotContract, VisualFacts
from test_event_agent import Provider, event, final, frame, run, source


def narrative(shots, frames, **options):
    return NarrativeEvent(
        id="event-render-test", anchor_shot_id=shots[0].id,
        initial_state=shots[0].action_details.initial_state,
        final_state=shots[-1].action_details.final_state,
        window_id="window-1", version_hash="saved-event-hash",
        **event(shots, frames, **options),
    )


def stages(shots, mode):
    return [{"id": f"stage-{index}", "source_shot_ids": [shot.id], "fact_ids": [f"fact-{index}"],
             "duration_seconds": duration, "transition": "initial" if index == 1 else "cut" if mode == "internal_cuts" and index == 2 else "continuation",
             "photography": {"shot_size": "特写" if index == 2 else "中景", "lens": "长焦" if index == 2 else "标准",
                             "camera_movement": "固定", "composition": f"第{index}段已有主体与道具", "intent": "生成摄影建议"}}
            for index, (shot, duration) in enumerate(zip(shots, [1.5, 2.5, 4.0]), 1)]


def fixture_sources():
    shots = [source(1, 0, action="男孩递出杯子"), source(2, 2, action="女孩接住杯子"), source(3, 4, action="女子饮用杯中液体")]
    shots[0].action_details.primary_subject = "男孩"
    shots[0].action_details.initial_state = "男孩站在桌旁，杯子仍在自己手中，尚未递出"
    shots[0].visual_facts = VisualFacts(scene="厨房", people=["男孩"], objects=["杯子"])
    shots[1].action_details.primary_subject = "女孩"
    shots[1].visual_facts = VisualFacts(scene="厨房", people=["女孩"], objects=["杯子"])
    shots[2].action_details.primary_subject = "女子"
    shots[2].visual_facts = VisualFacts(scene="厨房", people=["女子"], objects=["杯子", "镜子"])
    shots[2].action_details.final_state = "女子饮用后仍握住杯子"
    return shots, [frame(shot) for shot in shots]


def timed_lines(text):
    return [line for line in text.splitlines() if re.match(r"^\d+(?:\.\d+)?–\d+(?:\.\d+)? 秒：", line)]


def test_single_semantic_stage_uses_complete_action_without_forced_time_slicing():
    shots, frames = fixture_sources()
    saved = narrative(shots, frames)
    clip = render_event_clips(saved, shots, EventAgentConfig())[0]
    text = clip.seedance_prompt.positive_prompt
    assert VERSION == "event-agent-render.v5"
    assert len(clip.timeline) == 1 and not timed_lines(text)
    assert "动作：男孩递出杯子；女孩接住杯子；女子饮用杯中液体" in text
    assert "动作概述" not in text and "动作（建议生成时长" not in text
    assert "视频规格：时长 8 秒；分辨率 1080p；竖屏 9:16" in text
    assert "未设置画幅，采用默认生成画幅" not in text
    assert clip.delivery["video_spec"]["aspect_source"] == "generation_default"
    assert "整体画面风格：" not in text
    assert any("observed_style" in warning for warning in clip.warnings)


@pytest.mark.parametrize("mode", ["continuous", "internal_cuts"])
def test_all_stage_actions_and_transitions_live_in_the_existing_timed_intervals(mode):
    shots, frames = fixture_sources()
    saved = narrative(shots, frames, mode=mode, stages=stages(shots, mode))
    before = copy.deepcopy(saved.model_dump())
    clip = render_event_clips(saved, shots, EventAgentConfig(), style_text="整体画面风格：柔和水彩表现")[0]
    text = clip.seedance_prompt.positive_prompt
    lines = timed_lines(text)
    assert [line.split(" 秒：")[0] for line in lines] == ["0–1.5", "1.5–4", "4–8"]
    assert [(stage.generation_start_seconds, stage.generation_end_seconds) for stage in clip.timeline] == [(0.0, 1.5), (1.5, 4.0), (4.0, 8.0)]
    assert clip.generation_duration_seconds == 8.0
    assert saved.model_dump() == before
    assert all(line.count(shot.action) == 1 for line, shot in zip(lines, shots))
    assert "连续承接，女子饮用杯中液体" in lines[2]
    assert "阶段1：" not in text and "阶段2：" not in text
    assert text.count("整体画面风格：") == 1
    if mode == "internal_cuts":
        assert "切换画面，女孩接住杯子" in lines[1]
        assert "摄影：景别：特写；焦段：长焦" in lines[1]
        assert all(line.count("摄影：") == 1 for line in lines)
        assert text.count("摄影：") == len(lines)
    else:
        assert "切换画面" not in text
        assert all("镜头" not in line for line in lines)
        assert text.count("摄影：") == 1
        assert all(stage.photography == saved.photography for stage in clip.timeline)
        headings = ["主体：", "场景：", "视频规格：", "起点：", "动作时间线：", "摄影：", "整体画面风格：", "结束状态：", "声音与约束："]
        assert [text.index(heading) for heading in headings] == sorted(text.index(heading) for heading in headings)


def test_video_subject_includes_later_seen_participants_and_props_but_first_frame_stays_static():
    shots, frames = fixture_sources()
    clip = render_event_clips(narrative(shots, frames), shots, EventAgentConfig())[0]
    first = clip.first_frame_prompt.positive_prompt
    video = clip.seedance_prompt.positive_prompt
    subject = video.splitlines()[0]
    assert all(value in subject for value in ["男孩", "女孩", "女子", "杯子", "镜子"])
    assert subject.count("杯子") == 1
    assert "男孩站在桌旁，杯子仍在自己手中，尚未递出" in first
    assert all(value not in first for value in ["女孩", "女子", "镜子", "接住", "饮用"])
    assert "运镜：" not in first
    assert all(label not in first for label in ["声音：", "声音与约束：", "收束：", "结束状态：", "动作时间线："])
    assert all(shot.action in video for shot in shots)
    assert "女子饮用后仍握住杯子" in video


def test_saved_aspect_ratio_wins_over_source_frame_dimensions():
    shots, frames = fixture_sources()
    frames[0] = frames[0].model_copy(update={"width": 1920, "height": 1080})
    shots[0].prompts = [PromptBundle(id="source-first", shot_id=shots[0].id, prompt_type="first_frame", platform="image", version=1,
        positive_prompt="已有首帧", negative_prompt="", contract=ShotContract(aspect_ratio="竖屏 9:16"))]
    before = [shot.model_dump() for shot in shots]
    clip = render_event_clips(narrative(shots, frames), shots, EventAgentConfig(), frames=frames)[0]
    assert "视频规格：时长 8 秒；分辨率 1080p；竖屏 9:16。" in clip.seedance_prompt.positive_prompt
    assert "默认生成画幅" not in clip.seedance_prompt.positive_prompt
    assert "图像规格：1080p；竖屏 9:16。" in clip.first_frame_prompt.positive_prompt
    assert [shot.model_dump() for shot in shots] == before


def test_aspect_fallback_uses_uncropped_first_source_frames_and_ignores_later_sources():
    shots, frames = fixture_sources()
    frames[0] = frames[0].model_copy(update={"width": 1920, "height": 1080})
    frames[1] = frames[1].model_copy(update={"width": 720, "height": 1280})
    crop = frames[0].model_copy(update={"id": "crop", "parent_frame_id": frames[0].id, "artifact_kind": "crop", "width": 300, "height": 300})
    clip = render_event_clips(narrative(shots, frames), shots, EventAgentConfig(), frames=[crop, *frames])[0]
    assert "横屏 16:9。" in clip.seedance_prompt.positive_prompt
    assert "图像规格：1080p；横屏 16:9。" in clip.first_frame_prompt.positive_prompt
    assert "默认生成画幅" not in clip.seedance_prompt.positive_prompt


def test_unknown_earlier_contract_does_not_hide_a_later_usable_aspect_ratio():
    shots, frames = fixture_sources()
    base = dict(shot_id=shots[0].id, prompt_type="first_frame", platform="image", positive_prompt="已有首帧", negative_prompt="")
    shots[0].prompts = [PromptBundle(id="unknown-ratio", **base, contract=ShotContract(aspect_ratio="待设定")),
                       PromptBundle(id="known-ratio", **base, contract=ShotContract(aspect_ratio="4:3"))]
    clip = render_event_clips(narrative(shots, frames), shots, EventAgentConfig())[0]
    assert "横屏 4:3。" in clip.seedance_prompt.positive_prompt


def test_compact_body_preserves_exact_decisions_style_and_repeated_actions():
    shots, frames = fixture_sources()
    saved = narrative(shots, frames, stages=stages(shots, "internal_cuts"), mode="internal_cuts")
    saved.stages[0].duration_seconds = 1.533333
    saved.stages[1].duration_seconds = 2.244444
    saved.causal_facts[2].text = "女子再次饮用杯中液体"
    before = saved.model_dump(mode="json")
    style = "剪纸拼贴，纸纤维边缘清晰，暗部保留层次。"
    clip = render_event_clips(saved, shots, EventAgentConfig(), style_text=style)[0]
    text = clip.seedance_prompt.positive_prompt
    assert saved.model_dump(mode="json") == before
    assert "再次饮用" in text and text.count(style) == 1
    assert [line.split(" 秒：")[0] for line in timed_lines(text)] == ["0–1.5", "1.5–4", "4–8"]
    assert clip.generation_duration_seconds == 8.0
    assert "时长 8 秒" in text
    assert not any(value in text for value in ("不是原片动作时间实测", "未分析原片音轨", "生成摄影编排建议", "。。"))
    assert clip.delivery["source_audio_analyzed"] is False
    assert clip.delivery["timing_origin"] == "generation-plan-not-source-measurement"
    assert clip.delivery["prompt_lint"]["video"]["passed"]
    assert text.count("声音与约束：") == 1


def test_rendering_keeps_whole_source_partition_ids_and_existing_duration_budget():
    shots = [source(index, (index - 1) * 2) for index in range(1, 7)]
    frames = [frame(shot) for shot in shots]
    intervals = [{"id": f"stage-{index}", "source_shot_ids": [shot.id], "fact_ids": [f"fact-{index}"],
                  "duration_seconds": 3.0, "transition": "initial" if index == 1 else "continuation"}
                 for index, shot in enumerate(shots, 1)]
    saved = narrative(shots, frames, stages=intervals)
    before = copy.deepcopy(saved.model_dump())
    clips = render_event_clips(saved, shots, EventAgentConfig(), frames=frames)
    assert len(clips) == 2
    assert [sid for clip in clips for sid in clip.source_shot_ids] == [shot.id for shot in shots]
    assert sum(clip.generation_duration_seconds for clip in clips) == 18.0
    assert saved.model_dump() == before
    for index, clip in enumerate(clips, 1):
        assert clip.id == f"event-render-test-clip-{index:02d}"
        assert clip.seedance_prompt.id == clip.id + "-seedance"
        assert len(timed_lines(clip.seedance_prompt.positive_prompt)) == len(clip.timeline)
        assert clip.timeline[0].generation_start_seconds == 0.0
        assert clip.timeline[-1].generation_end_seconds == clip.generation_duration_seconds


def test_goal_phases_integrate_handoff_angles_and_feedback_without_dropping_actions_or_retiming():
    actions = ["男孩递出杯子", "女子伸手接杯", "女子接稳杯子", "女子饮水", "女子停顿后再次饮水"]
    shots = [source(index, index - 1, 1.0, action=action) for index, action in enumerate(actions, 1)]
    # A source hand close-up is evidence, not a mandatory output camera cut.
    shots[1].shot_size = "特写"
    frames = [frame(shot) for shot in shots]
    durations = [1.0, 2.0, 1.0, 2.0, 2.0]
    phases = ["递交与接稳"] * 3 + ["饮用与反馈"] * 2
    intervals = [{"id": f"stage-{index}", "semantic_phase": phase,
                  "source_shot_ids": [shot.id], "fact_ids": [f"fact-{index}"],
                  "duration_seconds": duration, "transition": "initial" if index == 1 else "continuation"}
                 for index, (shot, phase, duration) in enumerate(zip(shots, phases, durations), 1)]
    proposal = event(shots, frames, stages=intervals, dispositions={shots[-1].id: "preserve"})
    proposal["causal_facts"][-1]["kind"] = "reaction"
    before = [shot.model_dump() for shot in shots]
    provider = Provider([final(proposal)])
    result = run(shots, frames, provider, protected_source_shot_ids={shots[-1].id})
    assert result.status == "ready" and len(provider.calls) == 1
    assert "不是所有事件都强制两段" in provider.calls[0][0]
    assert len(result.events[0].stages) == 5  # Original model timing remains auditable.
    clip = result.generation_clips[0]
    assert [stage.semantic_phase for stage in clip.timeline] == ["递交与接稳", "饮用与反馈"]
    assert [stage.source_shot_ids for stage in clip.timeline] == [[shot.id for shot in shots[:3]], [shot.id for shot in shots[3:]]]
    assert [(stage.generation_start_seconds, stage.generation_end_seconds) for stage in clip.timeline] == [(0.0, 4.0), (4.0, 8.0)]
    assert len(timed_lines(clip.seedance_prompt.positive_prompt)) == 2
    assert "切换画面" not in clip.seedance_prompt.positive_prompt
    assert all(clip.seedance_prompt.positive_prompt.count(action) == 1 for action in actions)
    assert [shot.model_dump() for shot in shots] == before
    sequence = project_event_agent_result(result, shots, frames)["generation_sequences"][0]
    assert [step for stage in sequence["timeline"] for step in stage["action_steps"]] == actions
    assert [stage["semantic_phase"] for stage in sequence["timeline"]] == ["递交与接稳", "饮用与反馈"]
    assert sequence["source_start_time"] == 0.0 and sequence["source_end_time"] == 5.0
    assert sequence["generation_duration_seconds"] == 8.0
    assert sequence["source_raw_shot_ids"] == [rid for shot in shots for rid in shot.source_raw_shot_ids]


@pytest.mark.parametrize("necessary_detail", ["cut", "camera", "reveal"])
def test_semantic_phase_coalescing_keeps_necessary_cut_detail_and_reveal_stages(necessary_detail):
    shots = [source(index, index - 1, 1.0) for index in range(1, 4)]
    frames = [frame(shot) for shot in shots]
    intervals = [{"id": f"stage-{index}", "semantic_phase": "观察杯子",
                  "source_shot_ids": [shot.id], "fact_ids": [f"fact-{index}"],
                  "duration_seconds": 2.0, "transition": "initial" if index == 1 else "continuation"}
                 for index, shot in enumerate(shots, 1)]
    if necessary_detail == "cut":
        intervals[1]["transition"] = "cut"
    elif necessary_detail == "camera":
        # Equal explicit photography still must not absorb an intended detail.
        camera = {"shot_size": "特写", "lens": "长焦", "camera_movement": "固定",
                  "composition": "已见杯中内容", "intent": "必要信息特写"}
        for interval in intervals:
            interval["photography"] = camera
    proposal = event(shots, frames, stages=intervals, mode="internal_cuts",
                     dispositions={shots[1].id: "preserve"})
    if necessary_detail == "reveal":
        proposal["causal_facts"][1]["kind"] = "reveal"
    result = run(shots, frames, Provider([final(proposal)]))
    assert result.status == "ready"
    clip = result.generation_clips[0]
    assert len(clip.timeline) == 3
    assert clip.timeline[1].transition == ("cut" if necessary_detail == "cut" else "continuation")
    assert [stage.fact_ids for stage in clip.timeline] == [[f"fact-{index}"] for index in range(1, 4)]
    assert clip.generation_duration_seconds == 6.0


def test_phase_coalescing_keeps_repeated_observed_actions_and_all_critical_fact_ids():
    shots = [source(index, index - 1, 1.0, action="女子饮用") for index in range(1, 4)]
    frames = [frame(shot) for shot in shots]
    intervals = [{"id": f"stage-{index}", "semantic_phase": "连续饮用",
                  "source_shot_ids": [shot.id], "fact_ids": [f"fact-{index}"],
                  "duration_seconds": 2.0, "transition": "initial" if index == 1 else "continuation"}
                 for index, shot in enumerate(shots, 1)]
    result = run(shots, frames, Provider([final(event(shots, frames, stages=intervals))]))
    clip = result.generation_clips[0]
    assert len(clip.timeline) == 1
    assert clip.timeline[0].fact_ids == ["fact-1", "fact-2", "fact-3"]
    assert clip.timeline[0].action == "女子饮用；女子饮用；女子饮用"
    assert clip.generation_duration_seconds == 6.0
    one_phase = event(shots, frames, duration=6.0)
    one_phase["stages"][0]["semantic_phase"] = "连续饮用"
    result = run(shots, frames, Provider([final(one_phase)]))
    assert result.generation_clips[0].timeline[0].action == "女子饮用；女子饮用；女子饮用"


def test_phase_names_do_not_authorize_reordering_saved_actions():
    shots, frames = fixture_sources()
    proposal = event(shots, frames)
    proposal["stages"][0]["semantic_phase"] = "递交与饮用"
    proposal["stages"][0]["fact_ids"] = ["fact-3", "fact-1", "fact-2"]
    result = run(shots, frames, Provider([final(proposal), final(proposal)]))
    assert result.status == "degraded"
    assert "semantic phases cannot reorder saved actions" in " ".join(result.audit[0].validation_errors)


def test_phase_coalescing_respects_existing_whole_source_partitions_and_twelve_second_limit():
    shots = [source(index, (index - 1) * 2) for index in range(1, 7)]
    frames = [frame(shot) for shot in shots]
    intervals = [{"id": f"stage-{index}", "semantic_phase": "持续完成已见动作",
                  "source_shot_ids": [shot.id], "fact_ids": [f"fact-{index}"],
                  "duration_seconds": 3.0, "transition": "initial" if index == 1 else "continuation"}
                 for index, shot in enumerate(shots, 1)]
    saved = narrative(shots, frames, stages=intervals)
    legacy = saved.model_copy(deep=True)
    for stage in legacy.stages:
        stage.semantic_phase = None
    legacy_clips = render_event_clips(legacy, shots, EventAgentConfig())
    clips = render_event_clips(saved, shots, EventAgentConfig())
    assert len(clips) == 2
    assert [clip.source_shot_ids for clip in clips] == [clip.source_shot_ids for clip in legacy_clips]
    assert [clip.generation_duration_seconds for clip in clips] == [clip.generation_duration_seconds for clip in legacy_clips]
    assert [sid for clip in clips for stage in clip.timeline for sid in stage.source_shot_ids] == [shot.id for shot in shots]
    assert [fid for clip in clips for stage in clip.timeline for fid in stage.fact_ids] == [f"fact-{index}" for index in range(1, 7)]
    assert all(len(clip.timeline) == 1 and clip.generation_duration_seconds <= 12.0 for clip in clips)
    assert sum(clip.generation_duration_seconds for clip in clips) == 18.0


def test_same_named_phases_with_different_generation_photography_remain_separate():
    shots = [source(index, index - 1, 1.0) for index in range(1, 3)]
    frames = [frame(shot) for shot in shots]
    intervals = [{"id": f"stage-{index}", "semantic_phase": "递交与接稳",
                  "source_shot_ids": [shot.id], "fact_ids": [f"fact-{index}"],
                  "duration_seconds": 3.0, "transition": "initial" if index == 1 else "continuation",
                  "photography": {"shot_size": "中景", "lens": "标准", "camera_movement": movement,
                                  "composition": "已见人物与杯子", "intent": "生成摄影建议"}}
                 for index, (shot, movement) in enumerate(zip(shots, ["固定", "跟拍"]), 1)]
    result = run(shots, frames, Provider([final(event(shots, frames, mode="internal_cuts", stages=intervals))]))
    clip = result.generation_clips[0]
    assert len(clip.timeline) == 2
    assert [stage.photography.camera_movement for stage in clip.timeline] == ["固定", "跟拍"]
    assert clip.generation_duration_seconds == 6.0


def test_two_stage_action_summaries_are_concise_delivery_with_all_original_facts_and_evidence():
    actions = ["男孩将杯子递向女子", "女子伸手接杯", "女子接稳杯子", "女子饮水", "女子停顿后再次饮水"]
    shots = [source(index, index - 1, 1.0, action=action) for index, action in enumerate(actions, 1)]
    frames = [frame(shot) for shot in shots]
    summary_handoff = "男孩递出杯子，女子伸手接过并握稳。"
    summary_drink = "女子饮水，短暂停顿后再次饮用。"
    intervals = [
        {"id": "handoff", "semantic_phase": "递交与接稳", "action_summary": summary_handoff,
         "source_shot_ids": [shot.id for shot in shots[:3]], "fact_ids": ["fact-1", "fact-2", "fact-3"],
         "duration_seconds": 3.0, "transition": "initial"},
        {"id": "drink", "semantic_phase": "饮用与反馈", "action_summary": summary_drink,
         "source_shot_ids": [shot.id for shot in shots[3:]], "fact_ids": ["fact-4", "fact-5"],
         "duration_seconds": 5.0, "transition": "continuation"},
    ]
    proposal = event(shots, frames, stages=intervals)
    proposal["causal_facts"][-1]["kind"] = "reaction"
    proposal["generation_subjects"] = ["男孩", "女子"]
    proposal["generation_objects"] = ["杯子"]
    result = run(shots, frames, Provider([final(proposal)]))
    assert result.status == "ready"
    clip = result.generation_clips[0]
    assert [stage.action for stage in clip.timeline] == [summary_handoff, summary_drink]
    assert len(timed_lines(clip.seedance_prompt.positive_prompt)) == 2
    assert [fact.text for fact in result.events[0].causal_facts] == actions
    assert [fid for stage in clip.timeline for fid in stage.fact_ids] == [f"fact-{index}" for index in range(1, 6)]
    sequence = project_event_agent_result(result, shots, frames)["generation_sequences"][0]
    assert [step for stage in sequence["timeline"] for step in stage["action_steps"]] == actions
    assert sequence["seedance_prompt"]["contract"]["delivery"]["agent_event"]["stages"][0]["action_summary"] == summary_handoff
    assert all(fact["evidence"][0]["frame_id"] == frames[index].id for index, fact in enumerate(sequence["causal_facts"]))
    assert sequence["seedance_prompt"]["positive_prompt"].splitlines()[0] == "主体：男孩、女子；事件道具：杯子"


def test_coalesced_action_summaries_keep_feedback_repeated_drinking_and_separate_reveal():
    shots = [source(index, index - 1, 1.0, action=action) for index, action in enumerate(
        ["女子饮水", "女子微笑", "女子再次饮水", "杯中露出已见虫子"], 1)]
    frames = [frame(shot) for shot in shots]
    summaries = ["女子饮水。", "女子露出微笑。", "女子再次饮水。", "切至杯中细节，已见虫子显露。"]
    intervals = [{"id": f"stage-{index}", "semantic_phase": "饮用与反馈", "action_summary": summary,
                  "source_shot_ids": [shot.id], "fact_ids": [f"fact-{index}"],
                  "duration_seconds": 2.0, "transition": "initial" if index == 1 else "cut" if index == 4 else "continuation"}
                 for index, (shot, summary) in enumerate(zip(shots, summaries), 1)]
    intervals[-1]["photography"] = {"shot_size": "特写", "lens": "长焦", "camera_movement": "固定",
                                    "composition": "已见杯中内容", "intent": "必要信息特写"}
    proposal = event(shots, frames, stages=intervals, mode="internal_cuts", dispositions={shots[-1].id: "preserve"})
    proposal["causal_facts"][1]["kind"] = "reaction"
    proposal["causal_facts"][-1]["kind"] = "reveal"
    result = run(shots, frames, Provider([final(proposal)]))
    clip = result.generation_clips[0]
    assert len(clip.timeline) == 2
    assert clip.timeline[0].action == "；".join(summaries[:3])
    assert clip.timeline[0].fact_ids == ["fact-1", "fact-2", "fact-3"]
    assert clip.timeline[1].action == summaries[-1] and clip.timeline[1].transition == "cut"
    assert clip.timeline[1].photography.shot_size == "特写"


def test_partial_stage_does_not_use_whole_stage_summary_or_later_clip_subjects():
    shots = [source(index, index - 1, 1.0, action=f"女子完成已见动作{index}") for index in range(1, 4)]
    shots[-1].action_details.primary_subject = "来访者"
    shots[-1].visual_facts.people = ["来访者"]
    shots[-1].visual_facts.objects = ["门铃"]
    frames = [frame(shot) for shot in shots]
    summary = "女子先完成前序动作，来访者随后在门铃旁完成最后动作。"
    intervals = [{"id": "whole-phase", "semantic_phase": "事件推进", "action_summary": summary,
                  "source_shot_ids": [shot.id for shot in shots], "fact_ids": ["fact-1", "fact-2", "fact-3"],
                  "duration_seconds": 9.0, "transition": "initial"}]
    proposal = event(shots, frames, stages=intervals)
    proposal["generation_subjects"] = ["女子", "来访者"]
    proposal["generation_objects"] = ["杯子", "门铃"]
    result = run(shots, frames, Provider([final(proposal)]),
                 config=EventAgentConfig(timing_policy="authored", generation_target_seconds=6.0, generation_max_seconds=6.0))
    assert result.status == "ready" and len(result.generation_clips) == 2
    for clip in result.generation_clips:
        assert summary not in clip.seedance_prompt.positive_prompt
        assert clip.timeline[0].action == "；".join(shot.action for shot in shots if shot.id in clip.source_shot_ids)
    first_subject = result.generation_clips[0].seedance_prompt.positive_prompt.splitlines()[0]
    assert "来访者" not in first_subject and "门铃" not in first_subject


@pytest.mark.parametrize("summary", ["", "a" * 261, "女子饮水。\n未来结果", "女子与男孩交谈。"])
def test_action_summary_constraints_repair_once_and_keep_source_facts_on_fallback(summary):
    shots = [source(1, 0, action="女子饮水")]
    frames = [frame(shots[0])]
    proposal = event(shots, frames)
    proposal["stages"][0]["action_summary"] = summary
    provider = Provider([final(proposal), final(proposal)])
    result = run(shots, frames, provider)
    assert result.status == "degraded" and len(provider.calls) == 2
    assert result.events[0].causal_facts[0].text == shots[0].action
    assert result.events[0].source_shot_ids == [shots[0].id]


@pytest.mark.parametrize("selection", ["new-subject", "new-object", "duplicate", "foreign-source"])
def test_generation_label_selection_normalizes_exact_owned_source_labels_without_model_retry(selection):
    shots = [source(1, 0)]
    frames = [frame(shots[0])]
    proposal = event(shots, frames)
    if selection == "new-subject":
        proposal["generation_subjects"] = ["不存在的人物"]
    elif selection == "new-object":
        proposal["generation_objects"] = ["枪"]
    elif selection == "duplicate":
        proposal["generation_subjects"] = ["女子", "女子"]
    else:
        extra = source(2, 2)
        extra.action_details.primary_subject = "事件外人物"
        extra.visual_facts.people = ["事件外人物"]
        shots.append(extra)
        frames.append(frame(extra))
        proposal["generation_subjects"] = ["事件外人物"]
    response = final(proposal) if selection != "foreign-source" else final(proposal, event(shots[-1:], frames[-1:]))
    provider = Provider([response])
    result = run(shots, frames, provider)
    assert result.status == "degraded"
    assert len(provider.calls) == 1 and all(item.status == "ready" for item in result.events)
    assert not result.audit[0].validation_errors
    normalized = result.events[0]
    assert normalized.generation_subjects == (["女子"] if selection == "duplicate" else None)
    assert normalized.generation_objects is None
    repairs = [item for item in result.raw_responses[0]["local_repairs"] if item["field"] in ("generation_subjects", "generation_objects")]
    assert len(repairs) == 1
    assert repairs[0]["from"] == proposal[repairs[0]["field"]]
    assert repairs[0]["to"] == getattr(normalized, repairs[0]["field"])
    assert "展示需复核" in " ".join(result.generation_clips[0].warnings)


def test_direct_generation_label_validator_still_rejects_new_owned_source_labels():
    shots = [source(1, 0)]
    frames = [frame(shots[0])]
    proposal = event(shots, frames)
    proposal["generation_subjects"] = ["事件外人物"]
    with pytest.raises(ValueError, match="exact saved labels from owned event sources"):
        validate_event_proposals([EventProposal.model_validate(proposal)],
            build_event_windows(shots, EventAgentConfig())[0], shots, frames)


def test_generation_selection_does_not_inject_later_subject_into_source_first_frame():
    shots, frames = fixture_sources()
    proposal = event(shots, frames)
    proposal["generation_subjects"] = ["男孩", "女子"]
    proposal["generation_objects"] = ["杯子"]
    result = run(shots, frames, Provider([final(proposal)]))
    clip = result.generation_clips[0]
    assert clip.seedance_prompt.positive_prompt.splitlines()[0] == "主体：男孩、女子；事件道具：杯子"
    assert "女子" not in clip.first_frame_prompt.positive_prompt
    assert "女孩" not in clip.seedance_prompt.positive_prompt.splitlines()[0]
    assert "镜子" not in clip.seedance_prompt.positive_prompt.splitlines()[0]


@pytest.mark.parametrize("saved_action,unsupported_summary,visible_summary,term", [
    ("女子闭眼撅嘴后睁眼微笑", "女子闭眼撅嘴品尝饮料，随后微笑。", "女子闭眼撅嘴，随后睁眼微笑。", "品尝"),
    ("女子面红有汗珠，嘴巴张合并眨眼", "女子嘴巴张合喘息，显得极度疲惫。", "女子面红有汗珠，嘴巴张合并眨眼。", "喘息"),
    ("女子面红有汗珠，嘴巴张合并眨眼", "女子显得极度疲惫。", "女子面红有汗珠，嘴巴张合并眨眼。", "疲惫"),
])
def test_inferred_summary_labels_require_current_stage_fact_support_and_repair_once(
        saved_action, unsupported_summary, visible_summary, term):
    shots = [source(1, 0, action=saved_action)]
    frames = [frame(shots[0])]
    # Goals and unverified compatibility emotion strings cannot support a term.
    shots[0].action_details.emotion_change = "品尝后极度疲惫，中毒导致喘息"
    invalid = event(shots, frames)
    invalid["goal"] = "品尝后极度疲惫，中毒导致喘息"
    invalid["stages"][0]["action_summary"] = unsupported_summary
    repaired = copy.deepcopy(invalid)
    repaired["stages"][0]["action_summary"] = visible_summary
    provider = Provider([final(invalid), final(repaired)])
    result = run(shots, frames, provider)
    assert result.status == "ready" and len(provider.calls) == 2
    assert [call[2]["phase"] for call in provider.calls] == ["event_agent.initial", "event_agent.repair"]
    assert f"Unsupported inferred action summary term '{term}'" in " ".join(result.audit[0].validation_errors)
    assert term in provider.calls[-1][0]
    assert result.generation_clips[0].timeline[0].action == visible_summary
    assert result.events[0].causal_facts[0].text == saved_action


@pytest.mark.parametrize("term", ["品尝", "吹气", "喘息", "疲惫", "中毒", "生病", "疼痛", "焦虑", "导致"])
def test_risk_summary_label_is_allowed_when_current_stage_validated_fact_explicitly_supports_it(term):
    text = f"女子出现已保存的{term}状态"
    shots = [source(1, 0, action=text)]
    frames = [frame(shots[0])]
    proposal = event(shots, frames)
    proposal["stages"][0]["action_summary"] = text + "。"
    result = run(shots, frames, Provider([final(proposal)]))
    assert result.status == "ready"
    assert result.generation_clips[0].timeline[0].action == text + "。"


def test_risk_summary_label_cannot_use_another_stage_fact_as_support():
    shots = [source(1, 0, action="女子品尝饮料"), source(2, 2, action="女子閉眼后睁眼微笑")]
    frames = [frame(shot) for shot in shots]
    intervals = [
        {"id": "taste", "source_shot_ids": [shots[0].id], "fact_ids": ["fact-1"],
         "duration_seconds": 3.0, "transition": "initial", "action_summary": "女子品尝饮料。"},
        {"id": "feedback", "source_shot_ids": [shots[1].id], "fact_ids": ["fact-2"],
         "duration_seconds": 3.0, "transition": "continuation", "action_summary": "女子品尝后闭眼微笑。"},
    ]
    invalid = event(shots, frames, stages=intervals)
    repaired = copy.deepcopy(invalid)
    repaired["stages"][1]["action_summary"] = "女子闭眼，随后睁眼微笑。"
    result = run(shots, frames, Provider([final(invalid), final(repaired)]))
    assert result.status == "ready"
    assert "Unsupported inferred action summary term '品尝'" in " ".join(result.audit[0].validation_errors)
    assert result.generation_clips[0].timeline[1].fact_ids == ["fact-2"]


@pytest.mark.parametrize("safe_summary", ["女子闭眼后吹气或品尝，具体无法判断。", "女子闭眼后可能品尝饮料。", "女子闭眼后有杯前嘴部动作。"])
def test_uncertain_fact_cannot_authorize_certain_risk_summary_label(safe_summary):
    text = "女子闭眼后吹气或品尝，具体无法判断"
    shots = [source(1, 0, action=text)]
    frames = [frame(shots[0])]
    invalid = event(shots, frames)
    invalid["stages"][0]["action_summary"] = "女子闭眼后品尝饮料。"
    repaired = copy.deepcopy(invalid)
    repaired["stages"][0]["action_summary"] = safe_summary
    result = run(shots, frames, Provider([final(invalid), final(repaired)]))
    assert result.status == "ready"
    assert "term '品尝' only has uncertain fact.text support" in " ".join(result.audit[0].validation_errors)
    assert result.events[0].causal_facts[0].text == text
    assert result.generation_clips[0].timeline[0].action == safe_summary


def test_inference_guard_allows_natural_visible_handoff_drinking_smile_and_looking_wording():
    shots = [source(1, 0, action="男孩递杯，女子接稳杯子，饮用后微笑并看向男孩")]
    frames = [frame(shots[0])]
    summary = "男孩递出杯子，女子接过握稳并喝水，随后微笑看向男孩。"
    proposal = event(shots, frames)
    proposal["stages"][0]["action_summary"] = summary
    result = run(shots, frames, Provider([final(proposal)]))
    assert result.status == "ready" and result.audit[0].usage.model_calls == 1
    assert result.generation_clips[0].timeline[0].action == summary


@pytest.mark.parametrize("term", ["喝光", "喝完", "喝尽", "一饮而尽"])
def test_drinking_fact_cannot_authorize_complete_consumption_result_in_summary(term):
    shots = [source(1, 0, action="女子饮用杯中液体")]
    frames = [frame(shots[0])]
    invalid = event(shots, frames)
    invalid["stages"][0]["action_summary"] = f"女子将杯中液体{term}。"
    repaired = copy.deepcopy(invalid)
    repaired["stages"][0]["action_summary"] = "女子饮用杯中液体。"
    result = run(shots, frames, Provider([final(invalid), final(repaired)]))
    assert result.status == "ready"
    assert f"Unsupported inferred action summary term '{term}'" in " ".join(result.audit[0].validation_errors)
    assert result.generation_clips[0].timeline[0].action == "女子饮用杯中液体。"


@pytest.mark.parametrize("summary_kind", ["unsupported", "uncertain", "speech"])
def test_exhausted_summary_repair_preserves_eight_source_event_and_audits_field_fallback(summary_kind):
    shots = [source(index, index - 1, 1.0) for index in range(1, 10)]
    if summary_kind == "uncertain":
        shots[3].action = "女子吹气或品尝，具体无法判断"
        shots[3].action_details.steps = [shots[3].action]
    frames = [frame(shot) for shot in shots]
    bad_summary = "女子闭眼撅嘴品尝饮料。" if summary_kind != "speech" else "女子与男孩交谈。"
    intervals = [
        {"id": "handoff", "semantic_phase": "递交与接稳", "action_summary": "女子完成前序可见动作。",
         "source_shot_ids": [shot.id for shot in shots[:3]], "fact_ids": ["fact-1", "fact-2", "fact-3"],
         "duration_seconds": 3.0, "transition": "initial"},
        {"id": "drink", "semantic_phase": "饮用与反馈", "action_summary": bad_summary,
         "source_shot_ids": [shot.id for shot in shots[3:8]], "fact_ids": [f"fact-{index}" for index in range(4, 9)],
         "duration_seconds": 5.0, "transition": "continuation"},
    ]
    proposal = event(shots[:8], frames[:8], stages=intervals)
    last = event(shots[8:], frames[8:], duration=2.0)
    response = final(proposal, last)
    provider = Provider([response, response])
    result = run(shots, frames, provider)
    assert result.status == "degraded" and len(provider.calls) == 2
    assert [len(item.source_shot_ids) for item in result.events] == [8, 1]
    assert all(item.status == "ready" for item in result.events)
    assert [len(clip.source_shot_ids) for clip in result.generation_clips] == [8, 1]
    clip = result.generation_clips[0]
    assert len(clip.timeline) == 2
    assert clip.timeline[0].action == "女子完成前序可见动作。"
    assert clip.timeline[1].action == "；".join(shot.action for shot in shots[3:8])
    assert clip.timeline[1].fact_ids == [f"fact-{index}" for index in range(4, 9)]
    assert result.events[0].stages[1].action_summary is None
    assert [fact.text for fact in result.events[0].causal_facts] == [shot.action for shot in shots[:8]]
    repairs = [repair for repair in result.audit[0].raw_responses[-1]["local_repairs"] if repair["field"] == "action_summary"]
    assert len(repairs) == 1 and repairs[0]["from"] == bad_summary and repairs[0]["to"] is None
    assert repairs[0]["stage_id"] == "drink" and repairs[0]["recovery"] == "complete_source_fact_text"
    assert "摘要需复核" in " ".join(clip.warnings)
    assert result.audit[0].usage.model_calls == 2 and result.audit[0].usage.tool_calls == 0
    assert json.loads(result.raw_responses[-1]["response"])["events"][0]["stages"][1]["action_summary"] == bad_summary


@pytest.mark.parametrize("unsafe_field", ["fact", "evidence", "ownership"])
def test_summary_and_display_field_recovery_never_waive_fact_evidence_or_ownership_errors(unsafe_field):
    shots = [source(index, index - 1, 1.0) for index in range(1, 9)]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames)
    proposal["stages"][0]["action_summary"] = "女子品尝饮料。"
    proposal["generation_subjects"] = ["事件外人物"]
    if unsafe_field == "fact":
        proposal["causal_facts"][0]["text"] = "女子出现无保存证据的新动作"
    elif unsafe_field == "evidence":
        proposal["causal_facts"][0]["evidence"][0]["frame_id"] = "foreign-frame"
    elif unsafe_field == "ownership":
        proposal["stages"][0]["source_shot_ids"] = [shot.id for shot in shots[:-1]]
        proposal["stages"][0]["fact_ids"] = [f"fact-{index}" for index in range(1, 8)]
    response = final(proposal)
    provider = Provider([response, response])
    result = run(shots, frames, provider)
    assert result.status == "degraded" and len(provider.calls) == 2
    assert len(result.events) == 8 and all(item.status == "fallback" for item in result.events)
    assert [sid for item in result.events for sid in item.source_shot_ids] == [shot.id for shot in shots]


def test_optional_label_normalization_preserves_eight_source_event_with_valid_selection_subset():
    shots = [source(index, index - 1, 1.0) for index in range(1, 9)]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames)
    proposal["generation_subjects"] = ["女子", "条纹衫男孩", "男孩", "女子"]
    proposal["generation_objects"] = ["杯子", "无依据新物件", "杯子"]
    provider = Provider([final(proposal)])
    result = run(shots, frames, provider)
    assert result.status == "degraded" and len(provider.calls) == 1
    assert len(result.events) == len(result.generation_clips) == 1
    assert result.events[0].source_shot_ids == [shot.id for shot in shots]
    assert result.events[0].generation_subjects == ["女子", "男孩"]
    assert result.events[0].generation_objects == ["杯子"]
    assert result.generation_clips[0].seedance_prompt.positive_prompt.splitlines()[0] == "主体：女子、男孩；事件道具：杯子"
    assert [fact.text for fact in result.events[0].causal_facts] == [shot.action for shot in shots]
    assert result.audit[0].usage.model_calls == 1 and result.audit[0].validation_errors == []
    repairs = [item for item in result.raw_responses[0]["local_repairs"] if item["field"].startswith("generation_")]
    assert len(repairs) == 2
    assert json.loads(result.raw_responses[0]["response"])["events"][0]["generation_subjects"] == proposal["generation_subjects"]


@pytest.mark.parametrize("label_field,novel_label", [("generation_subjects", "神秘医生"), ("generation_objects", "神秘药丸")])
def test_removed_novel_display_label_cannot_survive_in_action_summary(label_field, novel_label):
    shots = [source(1, 0, action="女子握稳杯子并饮水")]
    frames = [frame(shots[0])]
    proposal = event(shots, frames)
    proposal[label_field] = [novel_label]
    summary = f"{novel_label}使女子握稳杯子并饮水。"
    proposal["stages"][0]["action_summary"] = summary
    provider = Provider([final(proposal)])
    result = run(shots, frames, provider)
    assert result.status == "degraded" and len(provider.calls) == 1
    assert result.events[0].status == "ready" and result.events[0].stages[0].action_summary is None
    assert novel_label not in result.generation_clips[0].seedance_prompt.positive_prompt
    repairs = [item for item in result.raw_responses[0]["local_repairs"] if item["field"] == "action_summary"]
    assert len(repairs) == 1 and repairs[0]["from"] == summary and repairs[0]["unsupported_labels"] == [novel_label]


def test_label_not_in_people_list_can_remain_in_summary_when_same_stage_fact_literally_supports_it():
    text = "条纹衫男孩递出杯子，女子接稳杯子"
    shots = [source(1, 0, action=text)]
    frames = [frame(shots[0])]
    proposal = event(shots, frames)
    proposal["generation_subjects"] = ["条纹衫男孩", "女子"]
    proposal["stages"][0]["action_summary"] = text + "。"
    result = run(shots, frames, Provider([final(proposal)]))
    assert result.status == "degraded" and result.events[0].generation_subjects == ["女子"]
    assert result.generation_clips[0].timeline[0].action == text + "。"
    assert not any(item["field"] == "action_summary" for item in result.raw_responses[0]["local_repairs"])


def test_explicit_empty_generation_objects_keeps_no_props_without_background_refill_or_warning():
    shots = [source(1, 0, action="女子微笑")]
    shots[0].visual_facts.objects = ["沙发", "台灯"]
    frames = [frame(shots[0])]
    proposal = event(shots, frames)
    proposal["generation_objects"] = []
    provider = Provider([final(proposal)])
    result = run(shots, frames, provider)
    assert result.status == "ready" and len(provider.calls) == 1
    assert result.events[0].generation_objects == []
    subject = result.generation_clips[0].seedance_prompt.positive_prompt.splitlines()[0]
    assert "沙发" not in subject and "台灯" not in subject and "事件道具" not in subject
    assert not result.warnings
    assert not any(item["field"] == "generation_objects" for item in result.raw_responses[0]["local_repairs"])
