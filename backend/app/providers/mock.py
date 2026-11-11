import json
import re
from typing import List

from app.config import Settings
from app.providers.base import FrameInput, ShotContext
from app.schemas import ActionDetails, ProviderHealth, ScriptBeat, ScriptPlanResult, SpatialEvidence, VisionObservation
from app.services.script_processor import fallback_window_plan


class MockVisionProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider="mock", model="mock-vision", quantization="none", ready=True, estimated_memory_mb=50, max_images_per_shot=4, max_context_tokens=self.settings.vlm_max_context_tokens, max_new_tokens=self.settings.vlm_max_new_tokens, timeout_seconds=self.settings.vlm_timeout_seconds)

    def analyze_style(self, prompt, frames, *, phase, schema, timeout_seconds, max_tokens):
        """Explicit synthetic test data; never claims to assess a real image."""
        values = [("medium_rendering", "二维图形表现，轮廓和色块保持平面组织"),
                  ("form_language", "几何轮廓清晰，形状边缘利落，细节适度简化"),
                  ("color_tone", "限定色块对比明确，明度层次清楚"),
                  ("texture_material", "表面纹理克制，色块边缘干净")]
        aliases = [frame.id for frame in frames]
        if schema.__name__ == "StyleEvidenceV3":
            return json.dumps({"claims": [{"id": f"C{i+1}", "dimension": dim, "observation": text,
                "images": aliases, "confidence": .8} for i, (dim, text) in enumerate(values)],
                "gaps": [], "uncertainties": ["Mock合成结果，仅验证工作流"]}, ensure_ascii=False)
        scope = "global" if "范围：project" in prompt else "reference"
        return json.dumps({"descriptive_name": "Mock平面图形", "descriptions": [
            {"dimension": dim, "core_concept": text.split("，")[0], "professional_description": text,
             "support_claim_ids": [f"C{i+1}"], "scope": scope, "scene_id": None} for i, (dim, text) in enumerate(values)],
            "generation_constraints": [], "uncertainties": ["Mock合成结果，不代表真实准确率"]}, ensure_ascii=False)

    def analyze_event_window(self, prompt, frames, *, phase, schema, timeout_seconds, max_tokens):
        """Synthetic event contracts for local E2E; this does not assess images."""
        payload = json.loads(prompt.split("\n输入：\n", 1)[1])
        known_ids = {frame.id for frame in frames}
        observations = {item["id"]: item for item in payload["source_observations"]}
        events = []
        for index, source_id in enumerate(payload["owned_source_shot_ids"], 1):
            shot = observations[source_id]
            frame = next((item for item in payload["frames"] if item["shot_id"] == source_id and item["id"] in known_ids), None)
            if frame is None:
                raise ValueError("Mock窗口缺少来源证据；不虚构帧引用")
            text = shot["action"] or shot["action_details"].get("initial_state") or "Mock来源状态"
            fid = f"mock-fact-{index}"
            events.append({
                "title": "Mock单来源工作流（非真实事件判断）", "goal": text,
                "source_shot_ids": [source_id],
                "causal_facts": [{"id": fid, "text": text, "source_shot_ids": [source_id], "critical": True,
                                  "evidence": [{"frame_id": frame["id"], "timestamp": frame["timestamp"], "description": text}]}],
                "presentation": [{"source_shot_id": source_id, "disposition": "preserve", "reason": "Mock仅验证接口，不判断语义合并"}],
                "generation_mode": "continuous",
                "stages": [{"id": f"mock-stage-{index}", "source_shot_ids": [source_id], "fact_ids": [fid],
                            "duration_seconds": min(payload["budget"]["generation_max_seconds"], shot["end_time"] - shot["start_time"]),
                            "transition": "initial"}], "confidence": 0.0,
            })
        return json.dumps({"action": "finalize", "events": events}, ensure_ascii=False)

    def propose_style(self, prompt: str, frames: List[FrameInput]) -> str:
        from app.services.style_service import DIMENSIONS
        from app.style_ontology.catalog import terms
        if "画风基础标签（style.tags）" in prompt:
            return json.dumps({"tags": {"medium": "二维手绘动画", "style_family": "波普图形风格",
                         "line_language": "粗黑轮廓", "character_design": "夸张清晰肢体",
                         "palette": "红黄蓝黑四色限定", "shading": "双色网点阴影",
                         "material": "纸纤维颗粒", "composition": "干净留白",
                         "graphic_motif": "手绘速度线", "visible_effect": "未知", "motion_language": "夸张清晰姿态"}}, ensure_ascii=False)

        candidate_ids = re.findall(r'"id":\s*"([a-z_]+\.[a-z0-9-]+)"', prompt)
        chosen = []
        for category in DIMENSIONS:
            if category in ("visible_effect", "negative_constraint"):
                continue
            term_id = next((item for item in candidate_ids if item in terms() and terms()[item].category == category), None)
            if term_id:
                chosen.append({"term_id": term_id, "frames": ["F1"] if frames else [],
                               "confidence": 0.8, "note": "Mock 画风证据"})
        return json.dumps({"selections": chosen, "anchors": [], "uncertainties": []}, ensure_ascii=False)

    def analyze_shot(self, frames: List[FrameInput], context: ShotContext) -> VisionObservation:
        presets = [
            ("主体沿环境缓慢移动，动作连续克制", "全景", "广角", "跟拍：从左向右缓慢平移", "蓝调逆光与薄雾"),
            ("主体停下并转向画外声音，动作在末帧收住", "近景", "标准", "固定：末段焦点转移", "暖色实景光与深蓝阴影"),
            ("玻璃杯轻微震动，主体抬头确认异响", "特写", "长焦", "推近：主体尺度缓慢增大", "单一暖光与可见尘埃"),
            ("黑暗中的手伸出，主体后退保持警觉", "中景", "广角", "手持：随主体轻微后退", "高反差侧光与压暗阴影"),
        ]
        item = presets[(context.number - 1) % len(presets)]
        return VisionObservation(
            subject_action=item[0], shot_size=item[1], lens=item[2], camera_movement=item[3], lighting_vibe=item[4],
            lighting_type="混合", lighting_detail=item[4], composition="主体清晰，前景与背景分层",
            spatial_evidence=SpatialEvidence(foreground="少量环境遮挡", midground="主要主体", background="连续场景环境", relationships="主体与环境保持稳定空间关系"),
            action_details=ActionDetails(primary_subject="主要主体", initial_state="主体位于画面中", steps=[item[0]], interaction_target="环境", final_state="动作在末帧自然收束", emotion_change="平稳", evidence=["Frame 1→4：主体位置与姿态连续变化"]),
            confidence=0.86,
        )

    def summarize_video(self, frames: List[FrameInput]) -> str:
        return "角色和场景在相邻镜头中保持连续；按时间顺序依据画面证据描述动作，不补充不可见剧情。"

    def group_raw_shots(self, frames: List[FrameInput], protected_after: List[int]) -> List[List[int]]:
        return [[int(frame.id.rsplit("-", 1)[-1])] for frame in sorted(frames, key=lambda item: item.timestamp)]

    def group_scenes(self, frames: List[FrameInput]) -> List[int]:
        return [1] if frames else []

    def plan_script_director(self, prompt: str, *, schema, max_tokens: int) -> str:
        from app.script_director.engine import DirectorWindow, conservative_plan
        payload = json.JSONDecoder().raw_decode(prompt.split("\n输入：\n", 1)[1])[0]
        # The payload is a typed source snapshot, not synthetic visual evidence.
        from app.script_director.models import ScriptDocument, ScriptSpan, ScriptCharacter, ScriptScene
        # The enclosing document already verified these exact source slices.
        document = ScriptDocument.model_construct(source_text="",
            spans=[ScriptSpan.model_validate(item) for item in payload["source_spans"]],
            characters=[ScriptCharacter.model_validate(item) for item in payload["characters"]],
            scenes=[ScriptScene.model_validate(item) for item in payload["scenes"]])
        window = DirectorWindow(1, payload["scene_id"], payload["target_span_ids"])
        return conservative_plan(document, window).model_dump_json()

    def plan_script(self, beats: List[ScriptBeat], target_beat_ids: List[str]) -> ScriptPlanResult:
        return ScriptPlanResult(plan=fallback_window_plan(beats, target_beat_ids), raw_responses=["mock deterministic script plan"])

    def unload(self) -> None:
        return None
