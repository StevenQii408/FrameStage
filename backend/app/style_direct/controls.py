"""Count distinct observed controls, not claim IDs or synonym occurrences.

These are conservative visual mechanisms, not recipe signatures or a classifier.
Only literal, positive observation text contributes; no traits are supplied here.
"""
import re

from .library import positive_mention

VERSION = "style-visual-controls.v1"
PATTERNS = {
    "form_language": {
        "proportions": r"夸张.{0,6}比例|比例.{0,6}夸张|大头|大眼|头部.{0,12}(?:偏大|大于)|(?:眼部|眼睛).{0,12}(?:放大|较大面积)|放大.{0,6}眼部",
        "hair_blocks": r"块面.{0,16}(?:发|头发)|(?:发束|头发).{0,16}(?:块面|体块|几何)|几何发束|发.{0,12}体块",
        "contour": r"曲线.{0,8}轮廓|轮廓.{0,8}(?:圆润|圆弧|几何|渐细|收束)|圆润.{0,8}轮廓|切线衔接|顺滑转折|尖锐几何轮廓",
        "face_simplification": r"(?:面部|五官).{0,8}(?:简化|细节少)|简化.{0,8}(?:面部|五官)",
        "volume_hierarchy": r"主辅体量|大中小梯级|体量.{0,8}(?:梯级|层级)",
        "facets": r"棱面|低多边形|棱角化|(?:几何|形状).{0,8}(?:抽象|简化)",
        "outline": r"粗.{0,4}轮廓线|无轮廓线|轮廓线.{0,6}(?:粗|细|变化)",
    },
    "lighting_shading": {
        "shadow_edge": r"阴影.{0,8}(?:边缘|边界|宽缓|分区|硬|柔)|硬质.{0,6}阴影",
        "tonal_structure": r"明暗.{0,8}(?:过渡|连续|层级|分区)|分色阴影|网点阴影",
        "highlight_extent": r"高光.{0,10}(?:克制|受控|点状|片状|范围|集中)",
        "shape_following_light": r"(?:亮带|亮部|反光).{0,8}随形|亮.{0,8}暗.{0,8}(?:对照|对比)",
    },
    "color_tone": {
        "palette_limit": r"限定.{0,8}色|双色|四色|互补色",
        "temperature_relationship": r"冷暖分离|青蓝.{0,10}暖|冷.{0,10}暖.{0,8}(?:对照|分离|对比)",
        "saturation": r"低饱和|低彩度|高饱和|高彩度",
        "value_structure": r"黑位.{0,8}层次|明度.{0,8}(?:梯级|层级|对照)",
    },
    "texture_material": {
        "paper_structure": r"纸纤维|纸纹|手工裁切",
        "brush_structure": r"笔触.{0,12}(?:方向|分布|连续)|沿.{0,8}笔触",
        "grain_distribution": r"颗粒.{0,8}(?:方向|分布)|网点",
        "surface_partition": r"反射.{0,12}(?:宽度|分区)|粗糙度分区|稳定区域边界",
        "low_frequency_surface": r"低频纹理|块面化表面",
        "handmade_marks": r"压痕|手塑.{0,8}(?:痕迹|表面)",
    },
    "medium_rendering": {},
}


def visual_controls(text, dimension):
    # Examples of local wet skin and parenthetical narrative must not increase
    # global support even when the surrounding shading observation is shared.
    text = re.sub(r"[（(）)]", "，", text)
    keys = set()
    for key, pattern in PATTERNS.get(dimension, {}).items():
        if any(positive_mention(text, m.group()) for m in re.finditer(pattern, text)):
            keys.add((dimension, key))
    if not keys and dimension != 'medium_rendering':
        from .reliability import specificity_score
        # Open vocabulary may support a concrete relational control, once per
        # dimension; repeating unfamiliar synonyms cannot manufacture density.
        if specificity_score(text, dimension):
            keys.add((dimension, 'other-observed-relationship'))
    return keys


def generic_control_clause(text, dimension):
    """A broad heading repeat can go; detailed eye/head relationships stay visible."""
    if dimension == 'form_language' and visual_controls(text, dimension) == {('form_language', 'proportions')}:
        remainder = re.sub(r'角色|造型|风格化|采用|设计|处理|卡通化|夸张|比例|的', '', text)
        return not remainder.strip(' ：:，。')
    return False
