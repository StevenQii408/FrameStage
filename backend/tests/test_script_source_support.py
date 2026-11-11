"""Narrative fidelity is independent of JSON validity and source-ID coverage."""
import pytest

from app.script_director.document import parse_script_document
from app.script_director.engine import conservative_plan, director_windows, validate_decision
from app.script_director.plan_models import PerformancePhase, SourceQuote


def plan(text="女人拿起杯子，然后把杯子放在桌上。"):
    document = parse_script_document(text)
    window = director_windows(document)[0]
    return document, window, conservative_plan(document, window)


def test_valid_source_ids_cannot_publish_invented_plot_results():
    document, window, decision = plan()
    shot = decision.events[0].shots[0]
    decision.events[0].title = "女子跳窗逃走"
    decision.events[0].goal = "杀死追赶者后逃走"
    shot.first_frame_state = "女人已经站到窗外"
    shot.action_steps = ["女子拿起杯子，随后跳窗逃走"]
    shot.end_state = "女子已跳出窗外"
    camera = shot.camera.model_dump()
    notes, _ = validate_decision(decision, document, window, enforce_source=True)
    serialized = decision.model_dump_json()
    assert all(word not in serialized for word in ["跳窗", "追赶者", "窗外"])
    assert "放在桌上" in shot.end_state
    assert shot.camera.model_dump() == camera
    assert shot.source_span_ids == window.source_span_ids
    assert any("原文引用" in note for note in notes)


def test_exact_subclauses_keep_real_directed_phase_times_without_new_actions():
    document, window, decision = plan()
    shot = decision.events[0].shots[0]
    span = document.content_spans[0]
    # Use the parser's actual text, including its punctuation.
    pivot = span.text.index("然后")
    first, second = span.text[:pivot], span.text[pivot:]
    shot.duration_seconds = 6
    shot.action_timeline = [
        PerformancePhase(start_seconds=0, end_seconds=2, description="伪造追逐", source_quotes=[SourceQuote(span_id=span.id, quote=first)]),
        PerformancePhase(start_seconds=2, end_seconds=6, description="伪造跳窗", source_quotes=[SourceQuote(span_id=span.id, quote=second)]),
    ]
    validate_decision(decision, document, window, enforce_source=True)
    assert [phase.description for phase in shot.action_timeline] == [first, second]
    assert [(p.start_seconds, p.end_seconds) for p in shot.action_timeline] == [(0, 2), (2, 6)]


@pytest.mark.parametrize("quote", ["女人跳窗逃走", "女人拿起杯子"])
def test_invalid_or_incomplete_quotes_do_not_pass_as_source_support(quote):
    document, window, decision = plan()
    decision.events[0].shots[0].source_quotes = [SourceQuote(span_id=window.source_span_ids[0], quote=quote)]
    with pytest.raises(ValueError, match="剧情引用"):
        validate_decision(decision, document, window, enforce_source=True)


def test_spoken_intent_is_not_permission_to_animate_the_threat():
    document, window, decision = plan("甲：我要跳窗逃走。")
    shot = decision.events[0].shots[0]
    shot.action_steps = ["甲跳出窗外"]
    shot.end_state = "甲已逃出房间"
    validate_decision(decision, document, window, enforce_source=True)
    assert shot.action_steps == ["主体保持当前姿态，按原对白或旁白自然表达"]
    assert "逃出" not in shot.end_state
    assert shot.dialogue_ranges  # Original dialogue remains lossless.


def test_unreferenced_timeline_does_not_keep_a_fabricated_secondary_action():
    document, window, decision = plan()
    shot = decision.events[0].shots[0]
    shot.duration_seconds = 4
    shot.action_timeline = [PerformancePhase(start_seconds=0, end_seconds=4, description="拿杯后跳窗逃走")]
    validate_decision(decision, document, window, enforce_source=True)
    assert shot.action_timeline == []
    assert "放在桌上" in "".join(shot.action_steps)


def test_legacy_validation_does_not_silently_recompile_saved_delivery():
    document, window, decision = plan()
    shot = decision.events[0].shots[0]
    shot.action_steps = ["女子举杯后放回桌面"]
    validate_decision(decision, document, window)
    assert shot.action_steps == ["女子举杯后放回桌面"]


def test_source_marker_cannot_authorize_later_tampering():
    document, window, decision = plan()
    validate_decision(decision, document, window, enforce_source=True)
    shot = decision.events[0].shots[0]
    shot.action_steps = ["女子跳窗逃走"]
    shot.first_frame_state = "窗外已经发生爆炸"
    validate_decision(decision, document, window, enforce_source=True)
    assert "跳窗" not in decision.model_dump_json()
    assert "爆炸" not in decision.model_dump_json()


def test_compiled_multi_shot_plan_is_idempotent_without_copying_actions_into_first_frame():
    document, window, decision = plan("女人拿起杯子，然后放回桌面。\n男孩站在桌边看着她。")
    event = decision.events[0]
    first = event.shots[0].model_copy(deep=True)
    first.source_span_ids = [window.source_span_ids[0]]
    second = first.model_copy(deep=True)
    second.source_span_ids = [window.source_span_ids[1]]
    event.shots = [first, second]
    validate_decision(decision, document, window, enforce_source=True)
    before = decision.model_dump_json()
    validate_decision(decision, document, window, enforce_source=True)
    assert decision.model_dump_json() == before
    assert "然后" not in second.first_frame_state
