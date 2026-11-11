"""Normalize and validate saved/cloud visual responses without loading models."""
import json
import re
from typing import Any, Dict, List, Optional, Type, TypeVar

from app.schemas import ActionDetails, MotionVisionObservation, SpatialEvidence, StaticVisionObservation, VisionObservation


StageModel = TypeVar("StageModel", StaticVisionObservation, MotionVisionObservation)


class VisionResponseParser:
    @staticmethod
    def _normalize_script_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Repair only predictable container/scalar mistakes from a model response."""

        normalized = dict(payload)
        shots = normalized.get("shots")
        if not isinstance(shots, list):
            return normalized
        repaired = []
        for raw_shot in shots:
            if not isinstance(raw_shot, dict):
                repaired.append(raw_shot)
                continue
            shot = dict(raw_shot)
            for key in ("source_beat_ids", "action_steps", "continuity_anchors", "warnings"):
                value = shot.get(key)
                if isinstance(value, str):
                    if value.strip() in {"", "无", "未知"} and key in {"continuity_anchors", "warnings"}:
                        shot[key] = []
                    else:
                        shot[key] = [value]
            confidence = shot.get("confidence")
            if isinstance(confidence, str):
                label = confidence.strip()
                if label in {"高", "较高"}:
                    shot["confidence"] = 0.8
                elif label in {"中", "中等"}:
                    shot["confidence"] = 0.6
                elif label in {"低", "较低"}:
                    shot["confidence"] = 0.4
                else:
                    try:
                        shot["confidence"] = float(label)
                    except ValueError:
                        pass
            repaired.append(shot)
        normalized["shots"] = repaired
        return normalized

    @staticmethod
    def _raw_text(raw: Any) -> str:
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            for key in ("text", "output", "content"):
                value = raw.get(key)
                if isinstance(value, str):
                    return value
            message = raw.get("message")
            if isinstance(message, dict):
                value = message.get("content")
                if isinstance(value, str):
                    return value
            choices = raw.get("choices")
            if isinstance(choices, list) and choices:
                return VisionResponseParser._raw_text(choices[0])
        for attribute in ("text", "生成文本", "output"):
            value = getattr(raw, attribute, None)
            if isinstance(value, str):
                return value
        return str(raw)

    @classmethod
    def _parse_payload(cls, raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict) and not any(key in raw for key in ("text", "output", "content", "message", "choices")):
            return dict(raw)
        text = cls._raw_text(raw).strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
        candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
        if not candidate or not candidate.startswith("{") or not candidate.endswith("}"):
            raise ValueError("VLM 未返回可解析的 JSON 对象")
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                payload = json.loads(repaired)
            except json.JSONDecodeError as exc:
                raise ValueError(f"VLM JSON 解析失败：{exc.msg}") from exc
        if not isinstance(payload, dict):
            raise ValueError("VLM JSON 顶层必须是对象")
        return payload

    @staticmethod
    def _normalize(value: Any, aliases: Dict[str, str]) -> Any:
        if not isinstance(value, str):
            return value
        compact = re.sub(r"[\s_-]+", "", value.strip().lower())
        return aliases.get(compact, value.strip())

    @classmethod
    def _validate_stage(cls, raw: Any, model: Type[StageModel], aliases: Dict[str, Dict[str, str]]) -> StageModel:
        payload = cls._parse_payload(raw)
        for field, mapping in aliases.items():
            if field in payload:
                payload[field] = cls._normalize(payload[field], mapping)
        try:
            return model.model_validate(payload)
        except Exception as exc:
            raise ValueError(f"VLM JSON 字段校验失败：{exc}") from exc

    @classmethod
    def _parse_static(cls, raw: Any) -> StaticVisionObservation:
        payload = cls._parse_payload(raw)
        evidence = payload.get("spatial_evidence")
        if isinstance(evidence, str):
            payload["spatial_evidence"] = {"relationships": evidence, "observable": True}
        elif isinstance(evidence, dict) and not isinstance(evidence.get("observable", True), bool):
            marker = str(evidence.get("observable", "")).lower()
            evidence["observable"] = not any(token in marker for token in ("不可", "无法", "false", "no", "否"))
        return cls._validate_stage(
            payload,
            StaticVisionObservation,
            {
                "shot_size": {
                    "extremewideshot": "大远景", "ews": "大远景", "大远景": "大远景", "大全景": "大远景",
                    "wideshot": "远景", "longshot": "远景", "远景": "远景",
                    "fullshot": "全景", "全景": "全景",
                    "mediumshot": "中景", "中景": "中景",
                    "mediumcloseup": "近景", "中近景": "近景", "近景": "近景",
                    "closeup": "特写", "特写": "特写",
                    "extremecloseup": "大特写", "大特写": "大特写", "未知": "未知", "unknown": "未知",
                },
                "lens": {
                    "ultrawide": "超广角", "超广角": "超广角", "wide": "广角", "广角": "广角",
                    "normal": "标准", "standard": "标准", "标准": "标准",
                    "telephoto": "长焦", "long": "长焦", "长焦": "长焦", "未知": "未知", "unknown": "未知",
                },
                "lighting_type": {
                    "softnatural": "自然柔光", "naturalsoft": "自然柔光", "自然柔光": "自然柔光",
                    "harddirectional": "硬质方向光", "硬质方向光": "硬质方向光",
                    "highkey": "高调", "高调": "高调", "lowkey": "低调", "低调": "低调",
                    "backlight": "逆光", "逆光": "逆光", "practical": "实景光源", "实景光源": "实景光源",
                    "volumetric": "体积光", "体积光": "体积光", "mixed": "混合", "混合": "混合",
                    "unknown": "未知", "未知": "未知",
                },
            },
        )

    @classmethod
    def _parse_motion(cls, raw: Any) -> MotionVisionObservation:
        payload = cls._parse_payload(raw)
        steps = payload.get("action_steps")
        if isinstance(steps, str):
            payload["action_steps"] = [steps]
        elif isinstance(steps, list) and len(steps) > 3:
            payload["action_steps"] = [*steps[:2], "；".join(str(step) for step in steps[2:])]
        evidence = payload.get("visible_evidence")
        if isinstance(evidence, str):
            payload["visible_evidence"] = [evidence]
        return cls._validate_stage(
            payload,
            MotionVisionObservation,
            {
                "camera_movement_type": {
                    "static": "固定", "locked": "固定", "固定": "固定",
                    "pan": "摇镜", "摇镜": "摇镜", "tilt": "俯仰", "俯仰": "俯仰",
                    "pushin": "推近", "dollyin": "推近", "推近": "推近",
                    "pullout": "拉远", "dollyout": "拉远", "拉远": "拉远",
                    "tracking": "跟拍", "follow": "跟拍", "跟拍": "跟拍",
                    "handheld": "手持", "手持": "手持", "crane": "升降", "升降": "升降",
                    "orbit": "环绕", "环绕": "环绕", "zoom": "变焦", "变焦": "变焦",
                    "aerial": "航拍", "drone": "航拍", "航拍": "航拍",
                    "compound": "复合运动", "复合运动": "复合运动", "未知": "未知", "unknown": "未知",
                }
            },
        )

    @staticmethod
    def _is_unknown(value: str) -> bool:
        return not value.strip() or value.strip().startswith("未知")

    @classmethod
    def _is_generic_action(cls, motion: MotionVisionObservation) -> bool:
        compact = re.sub(r"[，。,.；;：:\s]", "", motion.subject_action)
        generic = {"人物移动", "主体移动", "人物表情变化", "表情变化", "画面变化", "动作变化", "人物动作", "未知"}
        return compact in generic

    @classmethod
    def _merge_stages(
        cls,
        static: Optional[StaticVisionObservation],
        motion: Optional[MotionVisionObservation],
        static_raw: str,
        motion_raw: str,
        warnings: List[str],
    ) -> VisionObservation:
        shot_size = static.shot_size if static else "未知"
        lens = static.lens if static else "未知"
        lighting = static.lighting_vibe if static else "未知"
        composition = static.composition if static else "未知"
        lighting_type = static.lighting_type if static else "未知"
        lighting_detail = static.lighting_detail if static else "未知"
        action = motion.subject_action if motion else "未知"
        if motion and motion.camera_movement_type != "未知":
            detail = motion.camera_movement_detail.strip()
            camera = motion.camera_movement_type if cls._is_unknown(detail) else f"{motion.camera_movement_type}：{detail}"
        else:
            camera = "未知"
        confidences = [stage.confidence for stage in (static, motion) if stage is not None]
        confidence = sum(confidences) / len(confidences)
        if static is None or motion is None:
            confidence = min(confidence, 0.3)
        unknown_fields = [name for name, value in (("主体动作", action), ("景别", shot_size), ("焦段", lens), ("运镜", camera), ("光线", lighting)) if cls._is_unknown(value)]
        if len(unknown_fields) >= 2:
            confidence = min(confidence, 0.5)
            warnings.append(f"关键字段中有 {len(unknown_fields)} 项未知（{'、'.join(unknown_fields)}），需要人工复核")
        raw_response = json.dumps({"static": static_raw, "motion": motion_raw}, ensure_ascii=False)
        spatial = static.spatial_evidence if static else SpatialEvidence(observable=False)
        style = static.visual_style_evidence if static else None
        facts = static.visual_facts if static else None
        details = ActionDetails(
            primary_subject=(motion.primary_subject if motion and not cls._is_unknown(motion.primary_subject) else static.primary_subject if static else "未知"),
            initial_state=motion.initial_state if motion else "未知",
            steps=motion.action_steps if motion else [],
            interaction_target=motion.interaction_target if motion else "无",
            final_state=motion.final_state if motion else "未知",
            emotion_change=motion.emotion_change if motion else "未知",
            evidence=motion.visible_evidence if motion else [],
        )
        return VisionObservation(subject_action=action, shot_size=shot_size, lens=lens, camera_movement=camera, lighting_vibe=lighting, composition=composition, lighting_type=lighting_type, lighting_detail=lighting_detail, spatial_evidence=spatial, action_details=details, visual_style_evidence=style or {}, visual_facts=facts or {}, confidence=confidence, raw_response=raw_response, analysis_warnings=warnings)

    @classmethod
    def _parse_observation(cls, raw: Any) -> VisionObservation:
        """Parse the legacy one-stage schema for backward compatibility."""
        text = cls._raw_text(raw).strip()
        payload = cls._parse_payload(raw)
        try:
            return VisionObservation.model_validate({**payload, "raw_response": text})
        except Exception as exc:
            raise ValueError(f"VLM JSON 字段校验失败：{exc}") from exc
