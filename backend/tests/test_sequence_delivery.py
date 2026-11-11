"""Complete event delivery, independent of source cut/merge decisions."""
import copy

import pytest

from app.schemas import FrameArtifact
from app.services.event_sequences import derive_sequences, rerender_sequence_payload
from app.services.sequence_delivery import video_spec, video_spec_line
from test_event_sequences import handoff, style_prompt


@pytest.mark.parametrize("ratio,orientation", [("16:9", "横屏"), ("9:16", "竖屏"), ("1:1", "方形画幅"), ("2.39:1", "横屏")])
def test_video_spec_preserves_saved_ratio_and_separates_output_resolution(ratio, orientation):
    spec = video_spec(6, aspect_ratio=ratio)
    assert spec["aspect_ratio"] == ratio
    assert spec["orientation"] == orientation
    assert spec["resolution"] == "1080p"
    assert spec["resolution_source"] == "generation_setting"
    assert "时长 6 秒" in video_spec_line(spec)


def test_frame_ratio_is_not_derived_from_a_crop_or_mistaken_for_output_resolution():
    crop = dict(width=64, height=64, parent_frame_id="frame", artifact_kind="crop")
    spec = video_spec(6, frames=[crop, dict(width=768, height=432)])
    assert spec["aspect_ratio"] == "16:9" and spec["aspect_source"] == "reference_frame"
    assert "1080p" in video_spec_line(spec) and "768" not in video_spec_line(spec)
    default = video_spec(6, aspect_ratio="未知")
    assert default["aspect_source"] == "generation_default"
    assert "默认生成画幅" in video_spec_line(default)


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_invalid_generation_duration_is_rejected(duration):
    with pytest.raises(ValueError):
        video_spec(duration)


def test_full_prompt_has_subject_spec_style_and_actions_only_inside_saved_timeline():
    shots, edges = handoff()
    prompts = [style_prompt(s.id, style="整体画面风格：皮克斯式角色三维动画，圆润轮廓与块面发束。") for s in shots]
    prompts[0].contract.aspect_ratio = "16:9"
    prompts[0].contract.generation_suggestions["resolution"] = "720p"
    prompts[1].contract.delivery["action_presentation"] = {"main_action": "双方接触杯子后女人握住杯子"}
    original = [s.model_dump() for s in shots]
    payload = derive_sequences(shots, prompts=prompts, montage=edges)
    seq = payload["generation_sequences"][0]
    text = seq["seedance_prompt"]["positive_prompt"]
    assert text.index("主体：") < text.index("场景：") < text.index("视频规格") < text.index("0–2 秒")
    assert "男孩" in text.split("主体：", 1)[1].splitlines()[0]
    assert "女人" in text.split("主体：", 1)[1].splitlines()[0]
    assert "固定道具：杯子" in text
    assert "时长 6 秒；分辨率 720p；横屏 16:9" in text
    assert text.count("整体画面风格：") == 1
    assert "叙事动作：" not in text and "动作：" not in text
    assert text.count("男孩递出杯子") == 1
    assert text.count("双方接触杯子后女人握住杯子") == 1
    assert "2–3.5 秒：切至特写" in text and "3.5–6 秒：切至中景" in text
    assert "原片动作时间实测" in text
    assert seq["seedance_prompt"]["contract"]["delivery"]["video_spec"]["duration_seconds"] == 6
    assert seq["seedance_prompt"]["contract"]["orientation"] == "横屏"
    assert [s.model_dump() for s in shots] == original
    assert seq["first_frame_prompt"]["positive_prompt"].find("女人持杯，男孩松手") == -1


def test_source_frames_supply_ratio_without_overriding_an_explicit_contract():
    shots, edges = handoff()
    frame = FrameArtifact(id="f1", shot_id=shots[0].id, timestamp=0, path="f.jpg", thumbnail_path="f.jpg", width=432, height=768)
    seq = derive_sequences(shots, frames=[frame], montage=edges)["generation_sequences"][0]
    assert "竖屏 9:16" in seq["seedance_prompt"]["positive_prompt"]
    prompts = [style_prompt(s.id) for s in shots]
    prompts[0].contract.aspect_ratio = "16:9"
    seq = derive_sequences(shots, frames=[frame], prompts=prompts, montage=edges)["generation_sequences"][0]
    assert "横屏 16:9" in seq["seedance_prompt"]["positive_prompt"]


def test_format_rerender_does_not_change_source_coverage_or_stage_budgets():
    shots, edges = handoff()
    original = derive_sequences(shots, montage=edges)
    updated = rerender_sequence_payload(copy.deepcopy(original), shots)
    old, new = original["generation_sequences"][0], updated["generation_sequences"][0]
    for key in ("id", "source_shot_ids", "source_raw_shot_ids", "timeline", "generation_duration_seconds", "build_hash"):
        assert new[key] == old[key]
