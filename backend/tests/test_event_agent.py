"""Offline contract tests: never initialize a production database or call a model."""
import copy
import json
import time

import pytest
from pydantic import ValidationError

from app.event_agent import (
    EventAgentConfig, EventResponseSchema, ToolResult, build_event_windows,
    run_event_agent,
)
from app.schemas import ActionDetails, FrameArtifact, Shot, VisualFacts


def source(index, start, duration=2.0, action=None):
    text = action or f"女子完成已观察动作{index}"
    return Shot(
        id=f"shot-{index}", number=index, start_time=float(start), end_time=float(start + duration),
        duration=float(duration), time=f"{start}–{start + duration}", status="ready", confidence=0.9,
        action=text, shot_size="中景", lens="标准", camera_movement="固定", composition="人物居中",
        scene_group_id="scene-1", visual_facts=VisualFacts(scene="厨房", people=["男孩", "女子"], objects=["杯子"]),
        action_details=ActionDetails(primary_subject="女子", interaction_target="杯子",
                                     initial_state="杯子在人物手中", final_state="杯子在人物手中", steps=[text]),
        source_edit_times=[float(start)],
    )


def frame(shot, suffix="base", offset=0.1):
    return FrameArtifact(id=f"{shot.id}-{suffix}", shot_id=shot.id, timestamp=float(shot.start_time + offset),
                         path=f"/evidence/{shot.id}-{suffix}.jpg", thumbnail_path="/evidence/thumb.jpg")


def ref(artifact, text):
    return {"frame_id": artifact.id, "timestamp": artifact.timestamp, "description": text}


def event(shots, frames, *, stages=None, duration=8.0, mode="continuous", dispositions=None):
    artifacts = {artifact.shot_id: artifact for artifact in frames}
    ids = [shot.id for shot in shots]
    facts = [{"id": f"fact-{index}", "text": shot.action, "source_shot_ids": [shot.id],
              "evidence": [ref(artifacts[shot.id], shot.action)], "critical": True, "kind": "action"}
             for index, shot in enumerate(shots, 1)]
    return {
        "title": "递杯后饮用", "goal": "完成杯子递交与饮用", "source_shot_ids": ids,
        "causal_facts": facts,
        "presentation": [{"source_shot_id": sid, "disposition": (dispositions or {}).get(sid, "integrate"),
                          "reason": "同一动作的不同角度", "evidence": [ref(artifacts[sid], "真实来源角度")]} for sid in ids],
        "generation_mode": mode,
        "stages": stages or [{"id": "whole-action", "source_shot_ids": ids,
                               "fact_ids": [fact["id"] for fact in facts], "duration_seconds": duration,
                               "transition": "initial"}],
        "photography": {"shot_size": "中景", "lens": "标准", "camera_movement": "固定",
                        "composition": "在同一画面中保留已见人物与杯子", "intent": "统一生成摄影建议"},
        "confidence": 0.92,
    }


def final(*events):
    return json.dumps({"action": "finalize", "events": list(events)}, ensure_ascii=False)


class Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def analyze_event_window(self, prompt, frames, **kwargs):
        self.calls.append((prompt, frames, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value


class Registry:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.deadline = None
        self.extra_frames = 0

    def call(self, request):
        self.calls.append(request)
        result = self.results.pop(0)
        self.extra_frames += len(result.frames)
        return result


def run(shots, frames, provider, *, registry=None, config=None, **kwargs):
    return run_event_agent(
        job_id="job", run_id="run", shots=shots, frames=frames, provider=provider,
        tool_factory=lambda window, known_frames, remaining: registry or Registry([]),
        config=config or EventAgentConfig(timing_policy="authored"), **kwargs,
    )


@pytest.mark.parametrize("values", [
    {"window_seconds": 17.0}, {"max_model_calls_per_window": 3}, {"max_tool_calls_per_window": 3},
    {"max_extra_frames_per_window": 7}, {"generation_max_seconds": 13.0}, {"max_windows": 65},
    {"max_model_calls_per_window": "2"}, {"overlap_seconds": 4.0, "window_seconds": 4.0},
    {"generation_target_seconds": 10.0, "generation_max_seconds": 8.0}, {"unused": True},
])
def test_strict_hard_budgets(values):
    with pytest.raises(ValidationError):
        EventAgentConfig(**values)


def test_response_adapter_has_provider_schema_contract():
    schema = EventResponseSchema.model_json_schema()
    assert schema["discriminator"]["propertyName"] == "action"
    assert {"request_evidence", "finalize"} == set(schema["discriminator"]["mapping"])


def test_handoff_detail_drinking_merges_as_continuous_and_preserves_sources():
    shots = [source(1, 0, action="男孩将杯子递给女子"),
             source(2, 2, action="双方手部接触同一杯子"),
             source(3, 4, action="女子接稳杯子并飲用")]
    shots[0].action_details.initial_state = "杯子仍在男孩手中，女子尚未伸手"
    shots[2].action_details.final_state = "女子飲用后仍持杯"
    frames = [frame(shot) for shot in shots]
    before = [shot.model_dump() for shot in shots]
    provider = Provider([final(event(shots, frames))])
    result = run(shots, frames, provider, style_text="柔和水彩表现")
    assert result.status == "ready"
    assert len(result.events) == len(result.generation_clips) == 1
    clip = result.generation_clips[0]
    assert clip.source_shot_ids == [shot.id for shot in shots]
    assert len(clip.timeline) == 1 and clip.timeline[0].transition == "initial"
    assert "切换画面" not in clip.seedance_prompt.positive_prompt
    assert "动作：男孩将杯子递给女子；双方手部接触同一杯子；女子接稳杯子并飲用" in clip.seedance_prompt.positive_prompt
    assert clip.delivery["timing_origin"] == "generation-plan-not-source-measurement"
    assert "0–8 秒：" not in clip.seedance_prompt.positive_prompt
    assert "首帧状态：" in clip.first_frame_prompt.positive_prompt
    assert shots[0].action_details.initial_state in clip.first_frame_prompt.positive_prompt
    assert shots[0].action_details.initial_state in clip.seedance_prompt.positive_prompt
    assert shots[2].action not in clip.first_frame_prompt.positive_prompt
    assert shots[2].action in clip.seedance_prompt.positive_prompt
    assert [shot.model_dump() for shot in shots] == before
    assert len(provider.calls) == 1


def test_more_than_four_sources_are_allowed_in_one_clip():
    shots = [source(index, index - 1, 1.0) for index in range(1, 7)]
    frames = [frame(shot) for shot in shots]
    result = run(shots, frames, Provider([final(event(shots, frames))]))
    assert len(result.generation_clips) == 1
    assert len(result.generation_clips[0].source_shot_ids) == 6


def test_important_reveal_remains_preserved_with_explicit_internal_cut():
    shots = [source(1, 0, action="女子端起杯子"), source(2, 2, action="杯中露出已见虫子")]
    frames = [frame(shot) for shot in shots]
    stages = [
        {"id": "lift", "source_shot_ids": [shots[0].id], "fact_ids": ["fact-1"], "duration_seconds": 3.0, "transition": "initial"},
        {"id": "reveal", "source_shot_ids": [shots[1].id], "fact_ids": ["fact-2"], "duration_seconds": 2.0, "transition": "cut",
         "photography": {"shot_size": "特写", "lens": "长焦", "camera_movement": "固定", "composition": "杯中已见内容", "intent": "重要揭示的生成摄影建议"}},
    ]
    proposal = event(shots, frames, stages=stages, mode="internal_cuts", dispositions={shots[1].id: "preserve"})
    proposal["causal_facts"][1]["kind"] = "reveal"
    result = run(shots, frames, Provider([final(proposal)]))
    clip = result.generation_clips[0]
    assert result.events[0].presentation[1].disposition == "preserve"
    assert clip.timeline[1].transition == "cut"
    text = clip.seedance_prompt.positive_prompt
    assert "3–5 秒：切换画面，杯中露出已见虫子；摄影：景别：特写；焦段：长焦" in text
    assert "阶段2：" not in text
    assert "生成编排建议，不是原片动作时间实测" not in text
    assert clip.delivery["timing_origin"] == "generation-plan-not-source-measurement"


def test_critical_fact_cannot_be_hidden_as_an_omitted_insert():
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames, dispositions={shots[1].id: "omit"})
    provider = Provider([final(proposal), final(proposal)])
    result = run(shots, frames, provider)
    assert result.status == "degraded"
    assert all(item.status == "fallback" for item in result.events)
    assert "Critical causal facts cannot be omitted" in " ".join(result.audit[0].validation_errors)
    assert len(provider.calls) == 2


def test_incidental_omission_remains_owned_without_presenting_incidental_action():
    shots = [source(1, 0, action="女子端杯"), source(2, 2, action="背景路人经过"), source(3, 4, action="女子饮水")]
    frames = [frame(shot) for shot in shots]
    stages = [
        {"id": "lift", "source_shot_ids": [shots[0].id], "fact_ids": ["fact-1"], "duration_seconds": 3.0, "transition": "initial"},
        {"id": "drink", "source_shot_ids": [shots[2].id], "fact_ids": ["fact-3"], "duration_seconds": 4.0, "transition": "continuation"},
    ]
    proposal = event(shots, frames, stages=stages, dispositions={shots[1].id: "omit"})
    proposal["causal_facts"][1]["critical"] = False
    result = run(shots, frames, Provider([final(proposal)]))
    assert result.status == "ready"
    assert len(result.source_coverage) == 3
    assert "背景路人经过" not in result.generation_clips[0].seedance_prompt.positive_prompt
    assert result.generation_clips[0].source_shot_ids == [shot.id for shot in shots]


def test_duration_partition_uses_only_whole_sources_without_dropping_facts():
    shots = [source(index, (index - 1) * 2) for index in range(1, 7)]
    frames = [frame(shot) for shot in shots]
    stages = [{"id": f"stage-{index}", "source_shot_ids": [shot.id], "fact_ids": [f"fact-{index}"],
               "duration_seconds": 3.0, "transition": "initial" if index == 1 else "continuation"}
              for index, shot in enumerate(shots, 1)]
    result = run(shots, frames, Provider([final(event(shots, frames, stages=stages))]))
    assert len(result.generation_clips) >= 2
    assert [sid for clip in result.generation_clips for sid in clip.source_shot_ids] == [shot.id for shot in shots]
    assert all(clip.generation_duration_seconds <= 12 for clip in result.generation_clips)
    assert sum(clip.generation_duration_seconds for clip in result.generation_clips) == 18.0
    assert all(shot.action in "\n".join(clip.seedance_prompt.positive_prompt for clip in result.generation_clips) for shot in shots)


def test_invalid_json_gets_exactly_one_repair_using_same_images():
    shots = [source(1, 0)]
    frames = [frame(shots[0])]
    provider = Provider(["{broken", final(event(shots, frames))])
    result = run(shots, frames, provider)
    assert result.status == "ready"
    assert [call[2]["phase"] for call in provider.calls] == ["event_agent.initial", "event_agent.repair"]
    assert result.audit[0].usage.model_calls == 2
    assert provider.calls[0][1] == provider.calls[1][1]
    assert result.raw_responses[0]["response"] == "{broken"


def test_blank_and_failed_responses_consume_budget_and_fallback():
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    provider = Provider(["", RuntimeError("offline model")])
    result = run(shots, frames, provider)
    assert result.status == "degraded"
    assert len(result.events) == 2 and len(provider.calls) == 2
    assert result.usage_summary["model_calls"] == 2
    assert len(result.raw_responses) == 2
    assert len(result.source_coverage) == 2


@pytest.mark.parametrize("mutation", ["bad-frame", "bad-time", "context-owner", "invented-text"])
def test_invalid_references_or_ungrounded_text_fallback(mutation):
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames)
    if mutation == "bad-frame":
        proposal["causal_facts"][0]["evidence"][0]["frame_id"] = "not-a-real-frame"
    elif mutation == "bad-time":
        proposal["causal_facts"][0]["evidence"][0]["timestamp"] = 0.9
    elif mutation == "context-owner":
        proposal["source_shot_ids"][0] = "read-only-context"
    else:
        proposal["causal_facts"][0]["text"] = "女子发现杯子有毒，这是她生气的原因"
    provider = Provider([final(proposal), final(proposal)])
    result = run(shots, frames, provider)
    assert result.status == "degraded" and all(item.status == "fallback" for item in result.events)
    assert [item.source_shot_id for item in result.source_coverage] == [shot.id for shot in shots]
    assert result.audit[0].usage.model_calls == 2


def test_tools_with_no_new_images_do_not_trigger_second_model_call():
    shots = [source(1, 0)]
    frames = [frame(shots[0])]
    request = json.dumps({"action": "request_evidence", "tool_requests": [
        {"name": "get_neighbor_context", "arguments": {}},
        {"name": "compare_frames", "arguments": {"first_frame_id": frames[0].id, "last_frame_id": frames[0].id}},
    ]})
    provider = Provider([request])
    registry = Registry([ToolResult(name="get_neighbor_context", status="success", output={"shots": []}),
                         ToolResult(name="compare_frames", status="error", error="not enough images")])
    result = run(shots, frames, provider, registry=registry)
    assert len(provider.calls) == 1
    assert result.audit[0].usage.tool_calls == 2
    assert result.audit[0].usage.extra_frames == 0
    assert result.status == "degraded"
    assert "no new visual evidence" in " ".join(result.warnings)


def test_new_tool_frame_permits_one_refinement_with_explicit_deadline():
    shots = [source(1, 0)]
    frames = [frame(shots[0])]
    extra = frame(shots[0], "extra", 0.5)
    request = json.dumps({"action": "request_evidence", "tool_requests": [
        {"name": "sample_interval_frames", "arguments": {"start_time": 0.0, "end_time": 1.0, "max_frames": 1}},
    ]})
    provider = Provider([request, final(event(shots, [extra]))])
    registry = Registry([ToolResult(name="sample_interval_frames", status="success", frames=[extra])])
    result = run(shots, frames, provider, registry=registry)
    assert result.status == "ready"
    assert [call[2]["phase"] for call in provider.calls] == ["event_agent.initial", "event_agent.refine"]
    assert registry.deadline is not None
    assert result.audit[0].usage.extra_frames == 1
    assert result.frames == [extra]
    assert [image.id for image in provider.calls[1][1]] == [frames[0].id, extra.id]


def test_new_comparison_metrics_permit_refinement_without_extra_images():
    shots = [source(1, 0)]
    frames = [frame(shots[0])]
    request = json.dumps({"action": "request_evidence", "tool_requests": [
        {"name": "compare_frames", "arguments": {"first_frame_id": frames[0].id, "last_frame_id": frames[0].id}},
    ]})
    registry = Registry([ToolResult(name="compare_frames", status="success", output={"feature_flow_x": 2.0, "tracked_features": 20})])
    provider = Provider([request, final(event(shots, frames))])
    result = run(shots, frames, provider, registry=registry)
    assert result.status == "ready" and len(provider.calls) == 2
    assert result.audit[0].usage.extra_frames == 0
    assert "feature_flow_x" in provider.calls[1][0]


def test_second_call_request_cannot_trigger_more_tools_or_a_third_call():
    shots = [source(1, 0)]
    frames = [frame(shots[0])]
    extra = frame(shots[0], "extra")
    request = json.dumps({"action": "request_evidence", "tool_requests": [
        {"name": "crop_frame", "arguments": {"frame_id": frames[0].id, "bbox": [0.0, 0.0, 0.5, 0.5]}},
    ]})
    provider = Provider([request, request])
    registry = Registry([ToolResult(name="crop_frame", status="success", frames=[extra])])
    result = run(shots, frames, provider, registry=registry)
    assert len(provider.calls) == 2 and len(registry.calls) == 1
    assert result.status == "degraded"


def test_failed_partial_tool_extraction_still_consumes_extra_image_budget():
    shots = [source(1, 0)]
    frames = [frame(shots[0])]
    request = json.dumps({"action": "request_evidence", "tool_requests": [
        {"name": "sample_interval_frames", "arguments": {"start_time": 0.0, "end_time": 1.0, "max_frames": 6}},
    ]})
    registry = Registry([])

    def failed(request):
        registry.extra_frames += 3
        return ToolResult(name=request.name, status="error", error="failed after 3 saved images")

    registry.call = failed
    result = run(shots, frames, Provider([request]), registry=registry)
    assert result.audit[0].usage.extra_frames == 3 and result.audit[0].usage.tool_calls == 1


def test_timeout_discards_late_model_result_without_repair():
    shots = [source(1, 0)]
    frames = [frame(shots[0])]

    def slow():
        time.sleep(0.01)
        return final(event(shots, frames))

    provider = Provider([slow])
    result = run(shots, frames, provider, config=EventAgentConfig(window_timeout_seconds=0.003))
    assert len(provider.calls) == 1 and result.status == "degraded"
    assert "deadline exceeded" in " ".join(result.warnings)
    assert result.raw_responses[0]["response"]


def test_transport_error_falls_back_without_a_repair_retry():
    shots = [source(1, 0)]
    frames = [frame(shots[0])]
    provider = Provider([RuntimeError("HTTP 429")])
    result = run(shots, frames, provider)
    assert len(provider.calls) == 1 and result.status == "degraded"
    assert result.audit[0].usage.model_calls == 1
    assert "no transport retry" in " ".join(result.warnings)


def test_cancellation_retains_raw_audit_and_exact_source_coverage():
    shots = [source(1, 0), source(2, 2), source(3, 4), source(4, 6)]
    frames = [frame(shot) for shot in shots]
    cancelled = {"value": False}

    def finish_then_cancel():
        cancelled["value"] = True
        return final(event(shots[:2], frames[:2]))

    provider = Provider([finish_then_cancel])
    emitted = []
    result = run(shots, frames, provider,
                 config=EventAgentConfig(window_seconds=4.0, overlap_seconds=1.0),
                 cancelled=lambda: cancelled["value"], emit=lambda kind, payload: emitted.append((kind, payload)))
    assert result.status == "cancelled" and len(provider.calls) == 1
    assert len(result.source_coverage) == len(shots)
    window_events = [payload for kind, payload in emitted if kind == "event_agent.window"]
    assert len(window_events) == 2 and window_events[0]["raw_responses"]
    assert all(payload["status"] == "cancelled" for payload in window_events)


def test_windows_are_whole_source_disjoint_with_read_only_tail():
    shots = [source(index, (index - 1) * 3, 3.0) for index in range(1, 8)]
    windows = build_event_windows(shots, EventAgentConfig())
    assert len(windows) == 2
    assert windows[0].source_shot_ids == [shot.id for shot in shots[:4]]
    assert windows[1].source_shot_ids == [shot.id for shot in shots[4:]]
    assert windows[1].context_shot_ids == [shots[3].id]
    assert windows[1].context_start_time == 10.0
    assert [sid for window in windows for sid in window.source_shot_ids] == [shot.id for shot in shots]


def test_frame_selection_covers_every_owned_source_before_dense_samples():
    shots = [source(index, index - 1, 1.0) for index in range(1, 7)]
    frames = [frame(shot, f"sample-{index}", 0.1 + index * 0.1)
              for shot in shots for index in range(5)]
    def selected_response():
        artifacts = [next(item for item in frames if item.id == image.id) for image in provider.calls[-1][1]]
        return final(event(shots, artifacts))

    provider = Provider([selected_response])
    result = run(shots, frames, provider, config=EventAgentConfig(max_initial_frames_per_window=6))
    assert result.status == "ready"
    assert {image.id.split("-sample-")[0] for image in provider.calls[0][1]} == {shot.id for shot in shots}


def test_guessed_unshown_source_frame_is_not_accepted_as_evidence():
    shots = [source(1, 0)]
    frames = [frame(shots[0], "first", 0.1), frame(shots[0], "unshown", 0.8)]
    proposal = event(shots, [frames[1]])
    provider = Provider([final(proposal), final(proposal)])
    result = run(shots, frames, provider, config=EventAgentConfig(max_initial_frames_per_window=1))
    assert result.status == "degraded"
    assert "Evidence frame does not exist" in " ".join(result.audit[0].validation_errors)


def test_missing_source_image_is_isolated_without_discarding_available_source():
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shots[0])]
    provider = Provider([final(event(shots[:1], frames))])
    result = run(shots, frames, provider)
    assert len(provider.calls) == 1 and result.status == "degraded"
    assert [saved.status for saved in result.events] == ["ready", "fallback"]
    assert len(result.source_coverage) == 2


def test_continuous_event_can_cross_windows_and_keeps_stable_anchor():
    shots = [source(index, (index - 1) * 2) for index in range(1, 5)]
    frames = [frame(shot, offset=1.1) for shot in shots]
    first, second = event(shots[:2], frames[:2], duration=4.0), event(shots[2:], frames[2:], duration=4.0)
    second["goal"] = "递杯后继续饮用，不要求标题逐字相同"
    second["continuation_of_anchor_shot_id"] = shots[0].id
    second["continuation_evidence"] = [ref(frames[1], shots[1].action), ref(frames[2], shots[2].action)]
    provider = Provider([final(first), final(second)])
    result = run(shots, frames, provider, config=EventAgentConfig(window_seconds=4.0, overlap_seconds=1.0))
    assert result.status == "ready" and len(result.events) == 1
    assert result.events[0].anchor_shot_id == shots[0].id
    assert result.events[0].source_shot_ids == [shot.id for shot in shots]
    assert len(result.source_coverage) == 4 and len(result.generation_clips) == 1
    assert result.generation_clips[0].generation_duration_seconds == 8.0
    assert "previous_tail_event" in provider.calls[1][0]
    assert len({fact.id for fact in result.events[0].causal_facts}) == 4


def test_unrelated_cross_window_continuation_keeps_valid_new_event_without_repair():
    shots = [source(index, (index - 1) * 2) for index in range(1, 5)]
    shots[2].scene_group_id = shots[3].scene_group_id = "scene-2"
    frames = [frame(shot) for shot in shots]
    first, second = event(shots[:2], frames[:2], duration=4.0), event(shots[2:], frames[2:], duration=4.0)
    second["continuation_of_anchor_shot_id"] = shots[0].id
    second["continuation_evidence"] = [ref(frames[1], shots[1].action), ref(frames[2], shots[2].action)]
    provider = Provider([final(first), final(second)])
    result = run(shots, frames, provider, config=EventAgentConfig(window_seconds=4.0, overlap_seconds=1.0))
    assert len(result.events) == 2 and all(item.status == "ready" for item in result.events)
    assert len(provider.calls) == 2 and len(result.source_coverage) == 4
    assert "保留完整独立事件" in " ".join(result.warnings)


def test_maximum_windows_bounds_paid_calls_and_fallback_covers_rest():
    shots = [source(index, (index - 1) * 2) for index in range(1, 5)]
    frames = [frame(shot) for shot in shots]
    provider = Provider([final(event(shots[:2], frames[:2], duration=4.0))])
    result = run(shots, frames, provider, config=EventAgentConfig(window_seconds=4.0, overlap_seconds=1.0, max_windows=1))
    assert len(provider.calls) == 1 and len(result.source_coverage) == 4
    assert result.audit[1].usage.model_calls == 0


def test_render_sections_style_scope_guards_and_hashes_are_stable():
    shots = [source(1, 0)]
    shots[0].visual_style_evidence.character_design = "原片角色的旧服饰"
    frames = [frame(shots[0])]
    response = final(event(shots, frames))
    first = run(shots, frames, Provider([response]), style_text="铅笔素描")
    second = run(shots, frames, Provider([response]), style_text="铅笔素描")
    third = run(shots, frames, Provider([response]), style_text="水彩")
    clip = first.generation_clips[0]
    for prompt in (clip.first_frame_prompt, clip.seedance_prompt):
        text = prompt.positive_prompt
        labels = (["主体：", "场景：", "图像规格：", "首帧状态：", "摄影：", "整体画面风格：", "画面约束："]
            if prompt.prompt_type == "first_frame" else
            ["主体：", "场景：", "视频规格：", "起点：", "动作：", "摄影：", "整体画面风格：", "结束状态：", "声音与约束："])
        positions = [text.index(label) for label in labels]
        assert positions == sorted(positions)
        assert "不生成文字、字幕、Logo或水印" in text
        if prompt.prompt_type == "video":
            assert text.count("无对白、旁白") == 1
        else:
            assert "声音" not in text and "无对白" not in text
        assert "原片角色的旧服饰" not in text
        assert "35mm" not in text
        assert "整体画面风格：铅笔素描" in text
    assert clip.first_frame_prompt.platform == "generic-image"
    assert clip.seedance_prompt.platform == "seedance"
    assert first.events[0].id == second.events[0].id == third.events[0].id
    assert first.events[0].version_hash == third.events[0].version_hash
    assert first.version_hash == second.version_hash and first.version_hash != third.version_hash


def test_style_label_is_not_duplicated_and_saved_dress_stays_in_content():
    shots = [source(1, 0)]
    shots[0].visual_facts.people = ["穿蓝色外衣的女子"]
    frames = [frame(shots[0])]
    result = run(shots, frames, Provider([final(event(shots, frames))]), style_text="整体画面风格：铅笔素描")
    for prompt in (result.generation_clips[0].first_frame_prompt, result.generation_clips[0].seedance_prompt):
        assert prompt.positive_prompt.count("整体画面风格：") == 1
        assert "穿蓝色外衣的女子" in prompt.positive_prompt.split("场景：")[0]
        assert "穿蓝色外衣" not in prompt.positive_prompt.split("整体画面风格：")[1].splitlines()[0]


def test_known_action_cannot_be_replaced_by_a_static_fact_only():
    shots = [source(1, 0, action="女子把杯子打碎")]
    frames = [frame(shots[0])]
    proposal = event(shots, frames)
    proposal["causal_facts"][0]["text"] = "杯子"
    proposal["causal_facts"][0]["evidence"][0]["description"] = "杯子"
    provider = Provider([final(proposal), final(proposal)])
    result = run(shots, frames, provider)
    assert result.status == "degraded"
    assert "Saved source action" in " ".join(result.audit[0].validation_errors)
    assert "女子把杯子打碎" in result.generation_clips[0].seedance_prompt.positive_prompt


def test_self_authored_evidence_description_cannot_authorize_an_invented_fact():
    shots = [source(1, 0)]
    frames = [frame(shots[0])]
    proposal = event(shots, frames)
    invented = "女子发现杯子有毒，决定报复男孩"
    proposal["causal_facts"].append({
        "id": "invented", "text": invented, "source_shot_ids": [shots[0].id],
        "evidence": [ref(frames[0], invented)], "critical": True, "kind": "reveal",
    })
    proposal["presentation"][0]["disposition"] = "preserve"
    proposal["stages"][0]["fact_ids"].append("invented")
    result = run(shots, frames, Provider([final(proposal), final(proposal)]))
    assert result.status == "degraded"
    assert "image descriptions are not independent verification" in " ".join(result.audit[0].validation_errors)
    assert invented not in result.generation_clips[0].seedance_prompt.positive_prompt


def test_joint_fact_requires_all_source_evidence_and_one_complete_source_stage():
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames)
    proposal["causal_facts"][0]["source_shot_ids"] = [shot.id for shot in shots]
    result = run(shots, frames, Provider([final(proposal), final(proposal)]))
    assert "every claimed whole source" in " ".join(result.audit[0].validation_errors)
    proposal["causal_facts"][0]["evidence"].append(ref(frames[1], shots[1].action))
    proposal["stages"] = [
        {"id": "first", "source_shot_ids": [shots[0].id], "fact_ids": ["fact-1"], "duration_seconds": 3.0, "transition": "initial"},
        {"id": "second", "source_shot_ids": [shots[1].id], "fact_ids": ["fact-2"], "duration_seconds": 3.0, "transition": "continuation"},
    ]
    result = run(shots, frames, Provider([final(proposal), final(proposal)]))
    assert result.status == "degraded"
    assert "Generated action must reference facts from its whole source stage" in " ".join(result.audit[0].validation_errors)


def test_joint_fact_is_never_repeated_as_a_complete_action_in_partial_clips():
    shots = [source(1, 0), source(2, 2)]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames, duration=10.0)
    proposal["causal_facts"][0]["source_shot_ids"] = [shot.id for shot in shots]
    proposal["causal_facts"][0]["evidence"].append(ref(frames[1], shots[1].action))
    result = run(shots, frames, Provider([final(proposal)]),
                 config=EventAgentConfig(timing_policy="authored", generation_target_seconds=6.0, generation_max_seconds=6.0))
    assert result.status == "degraded" and len(result.events) == 2
    assert all(item.status == "fallback" for item in result.events)
    assert [clip.source_shot_ids for clip in result.generation_clips] == [[shots[0].id], [shots[1].id]]
    assert "complete causal-fact ownership" in " ".join(result.warnings)


def test_partitioned_internal_cuts_have_unique_ordered_clip_and_prompt_ids():
    shots = [source(index, (index - 1) * 2) for index in range(1, 7)]
    frames = [frame(shot) for shot in shots]
    stages = [{"id": f"stage-{index}", "source_shot_ids": [shot.id], "fact_ids": [f"fact-{index}"],
               "duration_seconds": 3.0, "transition": "initial" if index == 1 else "cut"}
              for index, shot in enumerate(shots, 1)]
    result = run(shots, frames, Provider([final(event(shots, frames, stages=stages, mode="internal_cuts"))]))
    clip_ids = [clip.id for clip in result.generation_clips]
    assert len(set(clip_ids)) == len(clip_ids)
    assert [clip_id.rsplit("-", 1)[-1] for clip_id in clip_ids] == [f"{index:02d}" for index in range(1, len(clip_ids) + 1)]
    assert len({clip.first_frame_prompt.id for clip in result.generation_clips}) == len(clip_ids)


def test_generated_photography_composition_cannot_insert_future_reveal_into_first_frame():
    shots = [source(1, 0, action="女子端起杯子"), source(2, 2, action="杯中虫子露出")]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames)
    proposal["photography"]["composition"] = "人物递出杯子并饮用，杯中虫子清晰可见"
    result = run(shots, frames, Provider([final(proposal)]))
    initial = result.generation_clips[0].first_frame_prompt.positive_prompt
    assert "虫子" not in initial and "饮用" not in initial
    assert "构图：人物居中" in initial


def test_contradictory_free_text_camera_movements_are_rejected():
    shots = [source(1, 0)]
    frames = [frame(shots[0])]
    proposal = event(shots, frames)
    proposal["photography"]["camera_movement"] = "固定镜头，并持续向人物推近"
    result = run(shots, frames, Provider([final(proposal), final(proposal)]))
    assert result.status == "degraded"
    assert "固定镜头，并持续向人物推近" not in result.generation_clips[0].seedance_prompt.positive_prompt


def test_cross_window_fact_and_stage_namespace_collisions_cannot_erase_actions():
    shots = [source(index, (index - 1) * 2) for index in range(1, 5)]
    frames = [frame(shot, offset=1.1) for shot in shots]
    first = event(shots[:2], frames[:2], duration=4.0)
    second = event(shots[2:], frames[2:], duration=4.0)
    first["causal_facts"][0]["id"] = "shot-3:fact-1"
    first["causal_facts"][1]["id"] = "shot-3:fact-1~1"
    first["stages"][0]["id"] = "shot-3:whole-action"
    first["stages"][0]["fact_ids"] = ["shot-3:fact-1", "shot-3:fact-1~1"]
    second["continuation_of_anchor_shot_id"] = shots[0].id
    second["continuation_evidence"] = [ref(frames[1], shots[1].action), ref(frames[2], shots[2].action)]
    result = run(shots, frames, Provider([final(first), final(second)]),
                 config=EventAgentConfig(window_seconds=4.0, overlap_seconds=1.0))
    assert result.status == "ready" and len(result.events) == 1
    merged = result.events[0]
    assert len({fact.id for fact in merged.causal_facts}) == 4
    assert len({stage.id for stage in merged.stages}) == 2
    text = "\n".join(clip.seedance_prompt.positive_prompt for clip in result.generation_clips)
    assert all(shot.action in text for shot in shots)


def test_trusted_reveal_source_cannot_be_relabelled_as_an_omitted_state_insert():
    shots = [source(1, 0, action="女子端起杯子"), source(2, 2, action="杯中露出虫子")]
    frames = [frame(shot) for shot in shots]
    proposal = event(shots, frames, dispositions={shots[1].id: "omit"})
    fact = proposal["causal_facts"][1]
    fact.update(text=shots[1].action_details.initial_state, kind="state", critical=False)
    fact["evidence"][0]["description"] = fact["text"]
    proposal["stages"][0]["source_shot_ids"] = [shots[0].id]
    proposal["stages"][0]["fact_ids"] = ["fact-1"]
    provider = Provider([final(proposal), final(proposal)])
    result = run(shots, frames, provider, protected_source_shot_ids={shots[1].id, "foreign-source"})
    assert result.status == "degraded"
    assert "Trusted protected reveal/reaction sources" in " ".join(result.audit[0].validation_errors)
    text = "\n".join(clip.seedance_prompt.positive_prompt for clip in result.generation_clips)
    assert "杯中露出虫子" in text
    assert result.source_coverage[1].source_shot_id == shots[1].id
    assert '"trusted_protected_source_shot_ids":["shot-2"]' in provider.calls[0][0]


def test_trusted_protected_action_cannot_be_downgraded_to_noncritical_fact():
    shots = [source(1, 0, action="杯中露出虫子")]
    frames = [frame(shots[0])]
    proposal = event(shots, frames, dispositions={shots[0].id: "preserve"})
    proposal["causal_facts"][0].update(kind="state", critical=False)
    result = run(shots, frames, Provider([final(proposal), final(proposal)]),
                 protected_source_shot_ids={shots[0].id})
    assert result.status == "ready"
    assert result.events[0].causal_facts[0].critical
    assert result.raw_responses[0]["local_repairs"][0]["field"] == "critical"


def test_trusted_protection_stays_scoped_and_survives_cross_window_merge():
    shots = [source(index, (index - 1) * 2) for index in range(1, 5)]
    shots[1].action = shots[1].action_details.steps[0] = "杯中露出虫子"
    frames = [frame(shot, offset=1.1) for shot in shots]
    first = event(shots[:2], frames[:2], duration=4.0, dispositions={shots[1].id: "preserve"})
    second = event(shots[2:], frames[2:], duration=4.0)
    second["continuation_of_anchor_shot_id"] = shots[0].id
    second["continuation_evidence"] = [ref(frames[1], shots[1].action), ref(frames[2], shots[2].action)]
    provider = Provider([final(first), final(second)])
    result = run(shots, frames, provider,
                 config=EventAgentConfig(window_seconds=4.0, overlap_seconds=1.0),
                 protected_source_shot_ids={shots[1].id})
    assert result.status == "ready" and len(result.events) == 1
    assert result.events[0].presentation[1].disposition == "preserve"
    assert '"trusted_protected_source_shot_ids":[]' in provider.calls[1][0]
    assert "杯中露出虫子" in result.generation_clips[0].seedance_prompt.positive_prompt
