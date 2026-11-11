import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List


SCHEMA_VERSION = "storyboard-eval.v1"
READABLE_SCHEMA_VERSIONS = {SCHEMA_VERSION, "storyboard-eval.v2"}
REQUIRED_LABELS = (
    "subject_action",
    "shot_size",
    "lens",
    "camera_movement",
    "lighting_vibe",
)
LENS_VALUES = {"超广角", "广角", "标准", "长焦", "未知"}
SHOT_SIZE_VALUES = {"大远景", "远景", "大全景", "全景", "中景", "中近景", "近景", "特写", "大特写", "未知"}
MOVEMENT_VALUES = {"固定", "摇镜", "俯仰", "推近", "拉远", "跟拍", "手持", "升降", "环绕", "变焦", "航拍", "复合运动", "未知"}
TRANSITION_TYPES = {"start", "hard_cut", "dissolve", "other"}


class ManifestError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def _validate_label(label: Any, location: str, name: str) -> None:
    _require(isinstance(label, dict), f"{location}.{name} 必须是对象")
    _require(isinstance(label.get("observable"), bool), f"{location}.{name}.observable 必须是布尔值")
    value = label.get("value")
    _require(isinstance(value, str) and value.strip(), f"{location}.{name}.value 不能为空")
    if name == "lens":
        _require(value in LENS_VALUES, f"{location}.lens.value 必须是视觉焦段类别之一：{sorted(LENS_VALUES)}")
    if name == "shot_size":
        _require(value in SHOT_SIZE_VALUES, f"{location}.shot_size.value 必须是规范景别之一：{sorted(SHOT_SIZE_VALUES)}")
    if name == "camera_movement":
        _require(value in MOVEMENT_VALUES, f"{location}.camera_movement.value 必须是规范运动类别之一：{sorted(MOVEMENT_VALUES)}")
    keywords = label.get("keywords", [])
    _require(isinstance(keywords, list) and all(isinstance(item, str) and item.strip() for item in keywords), f"{location}.{name}.keywords 必须是非空字符串数组")


def load_manifest(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"无法读取 manifest：{exc}") from exc
    _require(isinstance(payload, dict), "manifest 根节点必须是对象")
    _require(payload.get("schema_version") in READABLE_SCHEMA_VERSIONS, "schema_version 必须是 storyboard-eval.v1 或 storyboard-eval.v2")
    legacy_duration_policy = payload["schema_version"] == SCHEMA_VERSION
    _require(isinstance(payload.get("corpus_id"), str) and payload["corpus_id"].strip(), "corpus_id 不能为空")
    clips = payload.get("clips")
    _require(isinstance(clips, list) and clips, "clips 必须是非空数组")
    clip_ids = set()
    for clip_index, clip in enumerate(clips):
        location = f"clips[{clip_index}]"
        _require(isinstance(clip, dict), f"{location} 必须是对象")
        clip_id = clip.get("id")
        _require(isinstance(clip_id, str) and re.fullmatch(r"clip-[a-z0-9-]+", clip_id or "") is not None, f"{location}.id 格式无效")
        _require(clip_id not in clip_ids, f"重复的 clip id：{clip_id}")
        clip_ids.add(clip_id)
        relative_file = clip.get("file")
        _require(isinstance(relative_file, str) and relative_file.strip(), f"{location}.file 不能为空")
        file_path = Path(relative_file)
        _require(not file_path.is_absolute() and ".." not in file_path.parts, f"{location}.file 必须是素材目录内的相对路径")
        _require(re.fullmatch(r"[0-9a-f]{64}", clip.get("sha256", "")) is not None, f"{location}.sha256 必须是 64 位小写十六进制")
        tags = clip.get("tags", [])
        _require(isinstance(tags, list) and all(isinstance(tag, str) and tag.strip() for tag in tags), f"{location}.tags 必须是字符串数组")
        shots = clip.get("shots")
        _require(isinstance(shots, list) and shots, f"{location}.shots 必须是非空数组")
        previous_end = -1.0
        for shot_index, shot in enumerate(shots):
            shot_location = f"{location}.shots[{shot_index}]"
            _require(isinstance(shot, dict), f"{shot_location} 必须是对象")
            _require(shot.get("number") == shot_index + 1, f"{shot_location}.number 必须连续且从 1 开始")
            start = shot.get("start_time")
            end = shot.get("end_time")
            _require(isinstance(start, (int, float)) and isinstance(end, (int, float)), f"{shot_location} 时间必须是数字")
            _require(math.isfinite(start) and math.isfinite(end) and 0 <= float(start) < float(end), f"{shot_location} 必须满足有限的 0 <= start_time < end_time")
            _require(float(start) >= previous_end, f"{shot_location} 与前一镜头重叠")
            previous_end = float(end)
            transition = shot.get("transition_in")
            expected_transition = "start" if shot_index == 0 else None
            _require(transition in TRANSITION_TYPES, f"{shot_location}.transition_in 无效")
            if expected_transition:
                _require(transition == expected_transition, f"{shot_location}.transition_in 必须是 start")
            else:
                _require(transition != "start", f"{shot_location}.transition_in 不能是 start")
            labels = shot.get("labels")
            _require(isinstance(labels, dict), f"{shot_location}.labels 必须是对象")
            for label_name in REQUIRED_LABELS:
                _validate_label(labels.get(label_name), f"{shot_location}.labels", label_name)
            quality = shot.get("quality_labels")
            if "perception_labels" in shot:
                from pydantic import ValidationError
                from evals.perception_gold import HumanVisualTruth
                try:
                    truth = HumanVisualTruth.model_validate(shot["perception_labels"])
                    truth.validate_shot_range(float(start), float(end))
                except (ValidationError, ValueError) as exc:
                    raise ManifestError(f"{shot_location}.perception_labels 人工证据无效：{exc}") from exc
            if quality is not None:
                _require(isinstance(quality, dict), f"{shot_location}.quality_labels 必须是对象")
                objects = quality.get("objects", [])
                _require(isinstance(objects, list) and all(isinstance(value, str) and value.strip() for value in objects), f"{shot_location}.quality_labels.objects 必须是字符串数组")
                _require(isinstance(quality.get("scene", ""), str), f"{shot_location}.quality_labels.scene 必须是字符串")
                effects = quality.get("artistic_effects", [])
                _require(isinstance(effects, list), f"{shot_location}.quality_labels.artistic_effects 必须是数组")
                for effect in effects:
                    _require(isinstance(effect, dict) and isinstance(effect.get("description"), str) and effect["description"].strip() and isinstance(effect.get("evidence_frames"), list) and all(type(value) is int and 1 <= value <= 4 for value in effect["evidence_frames"]), f"{shot_location} 艺术效果必须有描述和 1–4 帧证据")
        relations = clip.get("montage_labels", [])
        _require(isinstance(relations, list), f"{location}.montage_labels 必须是数组")
        for relation in relations:
            _require(isinstance(relation, dict) and type(relation.get("left_shot")) is int and type(relation.get("right_shot")) is int and relation["right_shot"] == relation["left_shot"] + 1 and 1 <= relation["left_shot"] < len(shots), f"{location} 跨镜标注必须连接相邻镜头")
            _require(relation.get("relation_type") in {"连续动作", "反应镜头", "视觉呼应", "时间省略", "场景转换", "未知"}, f"{location} 跨镜关系类别无效")
            _require(type(relation.get("scene_change")) is bool, f"{location} 场次边界必须是布尔值")
            _require(isinstance(relation.get("evidence"), list) and all(isinstance(value, str) and value.strip() for value in relation["evidence"]), f"{location} 跨镜证据必须是字符串数组")
        generation = clip.get("generation_shots")
        if generation is not None:
            _require(isinstance(generation, list) and generation, f"{location}.generation_shots 必须是非空数组")
            previous_end = 0.0
            seen_scenes = set()
            current_scene = None
            for index, item in enumerate(generation):
                at = f"{location}.generation_shots[{index}]"
                _require(isinstance(item, dict), f"{at} 必须是对象")
                _require(set(item) == {"start_time", "end_time", "scene_id", "role"}, f"{at} 字段不符合生成镜头标注格式")
                start, end = item["start_time"], item["end_time"]
                _require(isinstance(start, (int, float)) and isinstance(end, (int, float)), f"{at} 时间必须是数字")
                _require(math.isfinite(start) and math.isfinite(end) and abs(float(start) - previous_end) <= 0.002 and 0 <= float(start) < float(end), f"{at} 时间线必须连续且有限")
                if legacy_duration_policy:
                    _require(float(end) - float(start) < 5.0, f"{at} 必须严格短于 5 秒（旧版v1规则）")
                _require(item["role"] in {"regular", "salient_insert"}, f"{at}.role 无效")
                scene_id = item["scene_id"]
                _require(isinstance(scene_id, str) and scene_id.strip(), f"{at}.scene_id 不能为空")
                if scene_id != current_scene:
                    _require(scene_id not in seen_scenes, f"{at} 场次 ID 不得在离开后再次出现")
                    seen_scenes.add(scene_id)
                    current_scene = scene_id
                previous_end = float(end)
            _require(abs(previous_end - float(shots[-1]["end_time"])) <= 0.05, f"{location}.generation_shots 未覆盖完整素材")
        if "event_groups" in clip:
            _require(not legacy_duration_policy, "事件真值请使用 storyboard-eval.v2")
            groups = clip["event_groups"]
            _require(isinstance(groups, list) and groups and all(isinstance(group, list) and group for group in groups), f"{location}.event_groups 必须是非空事件组")
            numbers = [number for group in groups for number in group]
            _require(all(type(number) is int for number in numbers) and numbers == list(range(1, len(shots) + 1)), f"{location}.event_groups 必须原序恰好覆盖全部来源镜头")
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_clips(manifest: Dict[str, Any], media_dir: Path) -> List[Dict[str, Any]]:
    resolved = []
    root = media_dir.resolve()
    for clip in manifest["clips"]:
        path = (root / clip["file"]).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ManifestError(f"素材越出 STORYBOARD_EVAL_MEDIA_DIR：{clip['id']}") from exc
        if not path.is_file():
            raise ManifestError(f"素材不存在：{clip['id']} ({clip['file']})")
        actual_hash = sha256_file(path)
        if actual_hash != clip["sha256"]:
            raise ManifestError(f"素材 SHA-256 不匹配：{clip['id']}")
        resolved.append({**clip, "_path": path})
    return resolved
