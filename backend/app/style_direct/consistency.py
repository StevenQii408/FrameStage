"""Conservative, offline contradiction checks; not a semantic truth classifier.

Only explicit opposite controls are compared. Local highlight/shadow treatments
are kept separate from the overall palette. Unknown vocabulary is never turned
into a fabricated match or used to request another model call.
"""
import re

from .library import positive_mention

VERSION = "style-consistency.v1"

# These are mutually exclusive *for the same treatment/region*, not globally.
CONTROLS = {
    "color_tone": {
        "temperature": {
            "cool": ("冷灰", "灰蓝", "蓝灰", "青蓝", "冷蓝", "冷调", "冷色调", "冷色主调"),
            "warm": ("暖金", "暖黄", "暖橙", "暖调", "暖色调", "暖色主调", "琥珀色", "色调偏暖", "偏暖色调"),
            "neutral": ("中性色调", "中性色彩", "中性主调"),
        },
        "saturation": {
            "low": ("低饱和", "低彩度", "低纯度", "去饱和"),
            "high": ("高饱和", "高彩度", "高纯度"),
        },
    },
    "lighting_shading": {
        "shadow_edge": {
            "soft": ("柔和阴影边缘", "柔软阴影边界"),
            "hard": ("硬边阴影", "硬质阴影边界", "锐利阴影边缘", "清晰硬影边界"),
        },
        "contrast": {
            "low": ("低反差", "低对比度"),
            "high": ("高反差", "高对比度"),
        },
        "key": {"low": ("低调照明", "低调明暗", "整体低调"),
                "high": ("高调照明", "高调明暗", "整体高调")},
    },
    "texture_material": {
        "finish": {"matte": ("低反射哑光", "哑光表面", "漫射哑光"),
                   "mirror": ("镜面表面", "强镜面反射", "高反射镜面")},
        "texture": {"smooth": ("均匀无纹理", "完全无纹理"),
                    "rough": ("粗颗粒表面", "粗糙颗粒表面")},
    },
    "form_language": {
        "contour": {"round": ("圆润轮廓", "圆润流畅的角色轮廓", "曲线主导", "圆弧轮廓"),
                    "angular": ("棱角化轮廓", "尖锐几何轮廓", "棱面主导")},
    },
}

REGIONS = {
    "shadow": ("暗部", "阴影", "暗侧"),
    "highlight": ("高光", "亮部", "亮侧", "亮面"),
    "edge": ("边缘", "轮廓光"),
    "local": ("局部", "点缀", "少量"),
}


def control_signature(text, dimension):
    """Return explicit control values keyed by region, preserving mixed recipes."""
    signature = {}
    clauses = [part for part in re.split(r"[，；。;,]", text) if part.strip()]
    for clause in clauses:
        regions = [region for region, words in REGIONS.items()
                   if any(positive_mention(clause, word) for word in words)]
        # A clause describing both shadows and highlights is a relationship,
        # not evidence that either local temperature is the overall temperature.
        region = regions[0] if len(regions) == 1 else "relationship" if regions else "overall"
        for control, values in CONTROLS.get(dimension, {}).items():
            # Shadow-edge width is already local to shadows; a soft volume
            # gradient does not contradict a hard cast shadow.
            control_region = "shadow" if control == "shadow_edge" else region
            matches = {value for value, words in values.items()
                       if any(positive_mention(clause, word) for word in words)}
            if matches:
                signature.setdefault((control, control_region), set()).update(matches)
    return signature


def check_consistency(text, reference_texts, dimension, *, reject_new_controls=False):
    """Detect known contradictions without claiming full entailment.

    Added controls are only rejected for a compressed phrase; a professional
    paraphrase is not rejected merely because its vocabulary is richer.
    """
    observed = {}
    for reference in reference_texts:
        for key, values in control_signature(reference, dimension).items():
            observed.setdefault(key, set()).update(values)
    proposed = control_signature(text, dimension)
    conflicts, unsupported = [], []
    for (control, region), values in proposed.items():
        supported = observed.get((control, region), set())
        detail = {"control": control, "region": region, "proposed": sorted(values),
                  "supported": sorted(supported)}
        if supported and values.isdisjoint(supported):
            conflicts.append(detail)
        elif region == "overall" and not supported and any(key[0] == control for key in observed):
            # Do not flatten local cool shadows/warm highlights into an
            # unsupported all-over palette, including the detailed text.
            detail["reason"] = "local-control-promoted-to-overall"
            unsupported.append(detail)
        elif reject_new_controls and (not supported or values - supported):
            unsupported.append(detail)
    return {"version": VERSION, "coverage": "explicit-controls-only", "conflicts": conflicts,
            "unsupported_controls": unsupported, "acceptable": not conflicts and not unsupported}


def consistent_phrase(item):
    """Renderer safeguard, also valid for profiles built outside StylePipeline."""
    if not item.concise_phrase:
        return ""
    result = check_consistency(item.concise_phrase, [item.professional_description], item.dimension,
                               reject_new_controls=True)
    return item.concise_phrase if result["acceptable"] else ""
