"""Deterministic delivery checks, not a substitute for visual accuracy evaluation."""
import re

VERSION = "style-reliability.v4"

_CUES = {
    "medium_rendering": r"剪纸|拼贴|黏土|粘土|水彩|水墨|油画|定格|赛璐璐|版画",
    "form_language": r"曲线.*轮廓|轮廓.*(?:圆润|几何)|圆润.*轮廓|几何轮廓|形状边缘|大头|大眼|头部.*(?:偏大|大于)|(?:眼睛|眼部).*(?:放大|较大面积)|放大.*(?:眼部|眉毛)|夸张.*比例|比例.*夸张|结构简化|简化.*五官|棱面|低多边形|几何化|粗.*轮廓|无轮廓|块面.*发|发.*(?:体块|块面)|主辅体量|大中小梯级|切线衔接|顺滑转折",
    "color_tone": r"限定.*色|四色|互补色|冷暖分离|青蓝.*暖|低饱和|低彩度|灰蓝|青灰|黑位.*层次",
    "lighting_shading": r"阴影.*(?:边缘|宽缓|分区|硬|柔)|明暗.*(?:过渡|连续|层级)|高光.*(?:克制|受控|点状|片状|范围)|网点阴影|分色阴影|硬质.*阴影",
    "texture_material": r"纸纤维|纸纹|手工裁切|笔触|颗粒.*(?:方向|分布)|网点|压痕|低频纹理|块面化|反射.*(?:宽度|分区)|粗糙度分区|稳定区域边界",
}
_GENERIC = re.compile(
    r"(?:[23][dD]|二维|三维)(?:角色|数字|计算机)?(?:动画|渲染(?:风格)?)?|"
    r"风格化写实|写实风格|卡通风格|(?:统一|整体)?(?:暖|冷)色调(?:为主)?|"
    r"(?:皮肤|表面)?(?:呈现|具有)?(?:通透感|平滑|细腻|细腻平滑|光滑)|"
    r"(?:柔和|柔软)(?:光影|光影过渡)|平滑表面|饱和度适中|细节丰富|精美(?:造型)?|电影感|高级感"
)


def specificity_score(text, dimension):
    """Rank only traits already present; never expand a phrase from the catalogue."""
    if re.search(r'^(?:无可见|未见|看不到|没有|无法|未观察到)', text.strip()):
        return 0
    from .library import positive_mention
    if any(positive_mention(text, m.group()) for m in re.finditer(_CUES.get(dimension, r"(?!)"), text)):
        return 2
    # Open vocabulary: missing a reviewed cue does not mean an unfamiliar style is wrong.
    # Only broad stand-alone phrases are ruled out; this is not a closed style whitelist.
    parts = re.split(r'[，；。]', text)
    for part in parts:
        part = re.sub(r'^(?:整体画面风格：|画面呈现|整体色彩以|角色采用|画面采用)', '', part).strip()
        if len(part) >= 8 and not _GENERIC.fullmatch(part) and re.search(
                r'对照|分离|对比|限定|梯级|层级|分区|遵循.*比例|比例.*抽象|经过.*抽象|随形|沿.*方向|由.*构成|从.*过渡|相对于', part):
            return 1
    return 0


def feature_quality(descriptions):
    applicable = [d for d in descriptions if d.scope in ("reference", "global")]
    dimensions = {d.dimension for d in applicable}
    distinctive = {d.dimension for d in applicable if specificity_score(d.professional_description, d.dimension)}
    sufficient = "medium_rendering" in dimensions and len(dimensions) >= 3 and len(distinctive) >= 2
    return {"check": "deterministic-specificity-heuristic", "sufficient": sufficient,
            "dimensions": sorted(dimensions), "distinctive_dimensions": sorted(distinctive),
            "note": "区分特征检查不等于真实画风准确率，不据此强制命名工作室"}


def usable_baseline(profile):
    """Scene-only and empty results cannot be frozen as a project baseline."""
    return bool(profile and (profile.edited_prompt or any(d.scope in ("reference", "global") for d in profile.descriptions)))


def resolution(profile):
    if not usable_baseline(profile):
        return "unknown"
    return "resolved" if profile.application_status in ("ready", "user-edited") else "partial"


def warn_partial(observation, profile=None):
    warning = ("画风字段部分待复核，当前视觉参考已通过证据校验；未确定的局部特征未应用，动作与摄影结果不受影响"
               if profile and (profile.reference or profile.anchor) else
               "画风仅部分确定，已保留有证据的特征，具体风格名称可能未知；动作与摄影结果不受影响")
    if warning not in observation.analysis_warnings:
        observation.analysis_warnings.append(warning)
