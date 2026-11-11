import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


UNKNOWN_TOKENS = ("未知", "无法判断", "不可判断", "不确定", "unknown", "n/a", "not visible", "cannot determine")

SHOT_SIZE_ORDER = ("大远景", "远景", "全景", "中景", "中近景", "近景", "特写", "大特写")
CANONICAL_SIZE_ORDER = ("大远景", "远景", "全景", "中景", "近景", "特写", "大特写")
SHOT_SIZE_ALIASES = {
    "extreme wide": "大远景", "establishing": "大远景", "long shot": "远景", "wide": "全景",
    "full shot": "全景", "medium": "中景", "medium close": "中近景", "close-up": "特写",
    "close up": "特写", "extreme close": "大特写",
}
LENS_ALIASES = {
    "超广角": ("超广角", "ultra wide", "fisheye"),
    "广角": ("广角", "wide angle", "wide-angle", "14mm", "18mm", "20mm", "24mm", "28mm", "35mm"),
    "标准": ("标准", "normal lens", "standard lens", "40mm", "50mm", "55mm"),
    "长焦": ("长焦", "telephoto", "portrait lens", "70mm", "85mm", "100mm", "135mm", "200mm"),
}
MOVEMENT_ALIASES = {
    "固定": ("固定", "静止", "locked", "static", "tripod"),
    "摇镜": ("摇镜", "pan", "panning"),
    "俯仰": ("俯仰", "tilt"),
    "推近": ("推近", "push-in", "push in", "dolly in"),
    "拉远": ("拉远", "pull-out", "pull out", "dolly out"),
    "跟拍": ("跟拍", "tracking", "follow"),
    "手持": ("手持", "handheld"),
    "升降": ("升降", "crane", "jib"),
    "环绕": ("环绕", "orbit", "arc shot"),
    "变焦": ("变焦", "zoom"),
    "航拍": ("航拍", "aerial", "drone"),
    "复合运动": ("复合运动", "compound movement"),
}


def is_unknown(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return not text or any(token in text for token in UNKNOWN_TOKENS)


def _contains_alias(value: Any, aliases: Dict[str, Iterable[str]]) -> Optional[str]:
    text = str(value or "").strip().lower()
    if is_unknown(text):
        return "未知"
    for canonical, variants in aliases.items():
        if any(variant.lower() in text for variant in variants):
            return canonical
    return None


def normalize_lens(value: Any) -> Optional[str]:
    return _contains_alias(value, LENS_ALIASES)


def normalize_movement(value: Any) -> Optional[str]:
    category = str(value or "").split("：", 1)[0].split(":", 1)[0].strip()
    if category in MOVEMENT_ALIASES:
        return category
    return _contains_alias(value, MOVEMENT_ALIASES)


def normalize_shot_size(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if is_unknown(text):
        return "未知"
    if "大全景" in text or "大远景" in text:
        return "大远景"
    if "中近景" in text:
        return "中近景"
    for canonical in reversed(SHOT_SIZE_ORDER):
        if canonical in text:
            return canonical
    for alias, canonical in sorted(SHOT_SIZE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in text:
            return canonical
    return None


def match_boundaries(predicted: Sequence[float], expected: Sequence[float], tolerance: float = 0.3) -> Dict[str, Any]:
    remaining = list(enumerate(predicted))
    matches: List[Tuple[int, int, float]] = []
    for expected_index, target in enumerate(expected):
        candidates = [(abs(value - target), list_index, predicted_index, value) for list_index, (predicted_index, value) in enumerate(remaining) if abs(value - target) <= tolerance]
        if not candidates:
            continue
        error, list_index, predicted_index, _ = min(candidates)
        matches.append((expected_index, predicted_index, error))
        remaining.pop(list_index)
    tp = len(matches)
    fp = len(predicted) - tp
    fn = len(expected) - tp
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1, "matches": matches}


def score_transitions(predicted: Sequence[float], transitions: Sequence[Dict[str, Any]], tolerance: float = 0.3) -> Dict[str, Any]:
    all_expected = [float(item["time"]) for item in transitions]
    all_matches = match_boundaries(predicted, all_expected, tolerance)
    matched_expected = {match[0] for match in all_matches["matches"]}
    matched_predicted = {match[1] for match in all_matches["matches"]}
    hard_indexes = [index for index, item in enumerate(transitions) if item["type"] == "hard_cut"]
    hard_tp = sum(index in matched_expected for index in hard_indexes)
    hard_fn = len(hard_indexes) - hard_tp
    unmatched_predictions = len(predicted) - len(matched_predicted)
    hard_precision = hard_tp / (hard_tp + unmatched_predictions) if hard_tp + unmatched_predictions else 1.0
    hard_recall = hard_tp / (hard_tp + hard_fn) if hard_tp + hard_fn else 1.0
    hard_f1 = 2 * hard_precision * hard_recall / (hard_precision + hard_recall) if hard_precision + hard_recall else 0.0
    by_type = {}
    for transition_type in ("hard_cut", "dissolve", "other"):
        indexes = [index for index, item in enumerate(transitions) if item["type"] == transition_type]
        by_type[transition_type] = {"expected": len(indexes), "detected": sum(index in matched_expected for index in indexes)}
    return {"tp": hard_tp, "fp": unmatched_predictions, "fn": hard_fn, "precision": hard_precision, "recall": hard_recall, "f1": hard_f1, "by_type": by_type, "matches": all_matches["matches"]}


def score_observation(labels: Dict[str, Any], prediction: Dict[str, Any]) -> Dict[str, Any]:
    observable = {name: bool(label["observable"]) for name, label in labels.items()}
    unknown = {name: is_unknown(prediction.get(name)) for name in labels}
    action_keywords = labels["subject_action"].get("keywords", [])
    action_text = str(prediction.get("subject_action", "")).lower()
    action_keyword_match = not action_keywords or any(keyword.lower() in action_text for keyword in action_keywords)
    expected_size = normalize_shot_size(labels["shot_size"]["value"])
    predicted_size = normalize_shot_size(prediction.get("shot_size"))
    size_adjacent = False
    comparable_expected = "近景" if expected_size == "中近景" else expected_size
    comparable_predicted = "近景" if predicted_size == "中近景" else predicted_size
    if comparable_expected in CANONICAL_SIZE_ORDER and comparable_predicted in CANONICAL_SIZE_ORDER:
        size_adjacent = abs(CANONICAL_SIZE_ORDER.index(comparable_expected) - CANONICAL_SIZE_ORDER.index(comparable_predicted)) <= 1
    expected_lens = normalize_lens(labels["lens"]["value"])
    predicted_lens = normalize_lens(prediction.get("lens"))
    expected_movement = normalize_movement(labels["camera_movement"]["value"])
    predicted_movement = normalize_movement(prediction.get("camera_movement"))
    confidence = float(prediction.get("confidence", 0) or 0)
    unknown_count = sum(unknown.get(name, False) for name in ("subject_action", "shot_size", "lens", "camera_movement", "lighting_vibe"))
    return {
        "observable": observable,
        "unknown": unknown,
        "action_keyword_match": action_keyword_match,
        "shot_size_expected": expected_size,
        "shot_size_predicted": predicted_size,
        "shot_size_adjacent": size_adjacent,
        "lens_expected": expected_lens,
        "lens_predicted": predicted_lens,
        "lens_correct": expected_lens is not None and expected_lens == predicted_lens,
        "movement_expected": expected_movement,
        "movement_predicted": predicted_movement,
        "movement_correct": expected_movement is not None and expected_movement == predicted_movement,
        "unknown_count": unknown_count,
        "confidence": confidence,
        "confidence_rule_pass": unknown_count < 2 or confidence <= 0.5,
    }
