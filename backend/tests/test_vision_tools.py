"""SDK-independent parsing and cloud planning regressions migrated from MLX tests."""
from dataclasses import replace

import pytest

from app.config import load_settings
from app.providers.base import FrameInput, ScriptPlanError
from app.providers.bailian import BailianVisionProvider
from app.providers.vision_common import VisionAnalysisTools
from app.providers.vision_responses import VisionResponseParser
from app.schemas import ScriptBeat


@pytest.fixture
def cloud_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    vision = BailianVisionProvider(replace(load_settings(), perception_v2_enabled=False))
    yield vision
    vision.unload()


def test_parse_observation_accepts_fenced_json_and_repairs_trailing_comma():
    payload = """分析如下：\n```json\n{\"subject_action\": \"人物抬头\", \"shot_size\": \"近景\", \"lens\": \"50mm\", \"camera_movement\": \"固定\", \"lighting_vibe\": \"柔光\", \"composition\": \"居中\", \"confidence\": 0.8,}\n```\n"""
    observation = VisionResponseParser._parse_observation(payload)
    assert observation.subject_action == "人物抬头"
    assert observation.confidence == 0.8


def test_parse_observation_rejects_invalid_schema():
    with pytest.raises(ValueError, match="字段校验失败"):
        VisionResponseParser._parse_observation('{"subject_action": "缺字段"}')


def test_static_stage_rejects_lens_taxonomy_in_shot_size():
    payload = '{"primary_subject":"城市","shot_size":"超广角","lens":"超广角","lighting_vibe":"日落","composition":"俯瞰","confidence":0.8}'
    with pytest.raises(ValueError, match="字段校验失败"):
        VisionResponseParser._parse_static(payload)


def test_stage_parsers_repair_common_container_shape_errors():
    static = VisionResponseParser._parse_static('{"primary_subject":"城市","shot_size":"全景","lens":"广角","lighting_vibe":"日落","composition":"俯瞰","spatial_evidence":{"foreground":"河流","midground":"城市","background":"天空","relationships":"俯瞰","observable":"城市建筑、河流、天空"},"confidence":0.8}')
    motion = VisionResponseParser._parse_motion('{"subject_action":"航拍城市并拉远","camera_movement_type":"航拍","camera_movement_detail":"向后移动","action_steps":["一","二","三","四"],"visible_evidence":"Frame 1→4：建筑尺度缩小","confidence":0.8}')
    assert static.spatial_evidence.observable is True
    assert len(motion.action_steps) == 3 and "四" in motion.action_steps[-1]
    assert motion.visible_evidence == ["Frame 1→4：建筑尺度缩小"]


def test_unknown_fields_cap_merged_confidence():
    static = VisionResponseParser._parse_static('{"primary_subject":"城市","shot_size":"全景","lens":"未知","lighting_vibe":"未知","composition":"俯瞰","confidence":0.9}')
    motion = VisionResponseParser._parse_motion('{"subject_action":"城市景观随航拍变化","camera_movement_type":"未知","camera_movement_detail":"未知","confidence":0.9}')
    result = VisionResponseParser._merge_stages(static, motion, "static", "motion", [])
    assert result.confidence == 0.5
    assert any("需要人工复核" in warning for warning in result.analysis_warnings)


def test_script_planner_retries_once_and_validates_exact_beat_coverage(cloud_tools, monkeypatch):
    vision = cloud_tools
    responses = iter([
        "not-json",
        '''{"shots":[{"source_beat_ids":["B001"],"scene_group_id":"scene-001","primary_subject":"女孩","first_frame_state":"女孩站在门外，手尚未触碰门把","action_steps":["女孩伸手握住门把","女孩推门进入"],"interaction_target":"门","end_state":"女孩站在室内，门已打开","emotion_change":"平静","shot_size":"中景","lens":"标准","camera_movement_type":"跟拍","camera_movement_detail":"沿进入方向轻微跟随","lighting_type":"自然柔光","lighting_detail":"室外柔光进入室内","composition":"门框形成框景","foreground":"门框","midground":"女孩","background":"室内","spatial_relationships":"女孩由门外移到门内","continuity_anchors":["蓝色外套","由左向右"],"duration_seconds":4.5,"confidence":0.82,"warnings":[]}]}''',
    ])
    calls = []

    def generate(*args, **kwargs):
        calls.append((args[0], kwargs))
        return next(responses)

    monkeypatch.setattr(vision, "_call", generate)
    beats = [ScriptBeat(id="B001", text="女孩推门进入", paragraph_hint="paragraph-001")]
    result = vision.plan_script(beats, ["B001"])
    assert len(calls) == 2
    assert calls[0][1]["max_tokens"] == 2048
    assert "上次校验失败" in calls[1][0]
    assert result.plan.shots[0].source_beat_ids == ["B001"]
    assert result.raw_responses[0] == "not-json"


def test_script_planner_exposes_both_invalid_raw_responses_for_fallback_persistence(cloud_tools, monkeypatch):
    vision = cloud_tools
    monkeypatch.setattr(vision, "_call", lambda *args, **kwargs: "not-json")
    beats = [ScriptBeat(id="B001", text="女孩推门进入")]
    with pytest.raises(ScriptPlanError) as error:
        vision.plan_script(beats, ["B001"])
    assert error.value.raw_responses == ["not-json", "not-json"]


def test_script_payload_normalizes_only_known_scalar_and_list_mistakes():
    payload = VisionResponseParser._normalize_script_payload({"shots": [{
        "source_beat_ids": "B001", "action_steps": "女孩推门", "continuity_anchors": "无",
        "warnings": "无", "confidence": "高",
    }]})
    assert payload["shots"][0] == {
        "source_beat_ids": ["B001"], "action_steps": ["女孩推门"], "continuity_anchors": [],
        "warnings": [], "confidence": 0.8,
    }


def test_raw_shot_grouping_uses_strict_adjacent_groups_and_protected_boundaries(cloud_tools, monkeypatch):
    vision = cloud_tools
    monkeypatch.setattr(vision.helper, "_contact_sheets", lambda frames, output: ["sheet.jpg"])
    monkeypatch.setattr(vision, "_call", lambda *args: '{"groups":[[1,2],[3,4]]}')
    frames = [FrameInput(f"raw-{index:03d}", float(index), "unused.jpg") for index in range(1, 5)]
    assert vision.group_raw_shots(frames, protected_after=[1]) == [[1], [2], [3, 4]]


def test_contact_sheet_builds_ordered_cpu_evidence_without_changing_sources(tmp_path):
    from PIL import Image
    frames = []
    for index in range(7):
        path = tmp_path / f"frame-{index}.png"
        Image.new("RGBA", (900, 1600), (index * 30, 60, 90, 255)).save(path)
        frames.append(FrameInput(str(index), float(index), str(path)))
    sheets = VisionAnalysisTools._contact_sheets(frames, tmp_path / "sheets")
    assert len(sheets) == 2
    with Image.open(sheets[0]) as image:
        assert image.size == (960, 400) and image.mode == "RGB"
    with Image.open(frames[0].path) as image:
        assert image.size == (900, 1600) and image.mode == "RGBA"


def test_cloud_request_and_helpers_have_no_local_inference_dependency(tmp_path, monkeypatch):
    import builtins
    import httpx
    original_import = builtins.__import__
    def no_local_sdk(name, *args, **kwargs):
        if name.split(".")[0] in {"mlx", "mlx_vlm"}:
            pytest.fail("Cloud analysis attempted to import a retired local inference SDK")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_local_sdk)
    cfg = replace(load_settings(), cloud_api_key="fixture", cloud_base_url="https://example.com/compatible-mode/v1")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": '{"ok":true}'}}]}))
    vision = BailianVisionProvider(cfg, transport)
    try:
        assert vision.helper._parse_payload(vision._call("返回 JSON", [], "check")) == {"ok": True}
        assert not hasattr(cfg, "model_path") and not hasattr(cfg, "model_name")
    finally:
        vision.unload()
