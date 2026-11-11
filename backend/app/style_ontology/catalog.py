"""Authored Chinese style vocabulary. External catalogues informed categories only.

The stable IDs and Chinese descriptions below are original project material.  The
reference repository is deliberately neither downloaded nor bundled at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List


VERSION = "style-ontology.v1"
REFERENCE = {
    "url": "https://github.com/willwulfken/MidJourney-Styles-and-Keywords-Reference",
    "commit": "1e54e7ffd4b6a9a2afc6de83bcfcff962619f299",
    "use": "分类研究与视觉对照；词条定义和文案由本项目编写",
}

# Each category is intentionally a controlled visual dimension.  New vocabulary
# changes require a new version; never reorder an existing published version.
_VOCABULARY = {
    "medium": """
photography|写实摄影
documentary-photo|纪实摄影
film-still|胶片剧照
two-d-animation|二维手绘动画
three-d-animation|三维角色动画
cel-animation|赛璐璐动画
stop-motion|定格动画
clay-stop-motion|黏土定格动画
paper-cut|剪纸动画
collage|拼贴动画
watercolor-animation|水彩动画
ink-animation|水墨动画
gouache-animation|水粉动画
crayon-animation|蜡笔绘本动画
pixel-animation|像素动画
vector-animation|矢量动画
low-poly-animation|低多边形动画
mixed-two-three-d|二维三维融合动画
rotoscope|转描动画
silhouette-animation|剪影动画
comic-panel|漫画分格动画
motion-graphics|动态图形设计
miniature-photography|微缩模型摄影
puppet-animation|偶动画
glass-painting|玻璃绘画动画
""",
    "style_family": """
pop-art|波普图形风格
art-deco|装饰艺术风格
expressionism|表现主义风格
impressionism|印象派绘画风格
surrealism|超现实主义风格
constructivism|构成主义风格
minimalism|极简主义风格
brutalism|粗野主义设计风格
retro-futurism|复古未来主义风格
cyberpunk|赛博朋克风格
solarpunk|太阳朋克风格
steampunk|蒸汽朋克风格
noir|黑色电影风格
pastoral|田园绘本风格
gothic|哥特式幻想风格
baroque|巴洛克装饰风格
folk-art|民间美术风格
new-chinese|新国潮东方幻想风格
ukiyoe|浮世绘版画风格
modernist-poster|现代主义海报风格
psychedelic|迷幻图形风格
mid-century|中世纪现代设计风格
editorial|编辑设计插画风格
fairytale|童话绘本风格
abstract-geometry|抽象几何风格
""",
    "line_language": """
bold-black-outline|粗黑轮廓
thin-ink-outline|细墨线轮廓
no-outline|无轮廓色块
variable-line-weight|线条粗细变化
dry-brush-line|干笔边缘
wet-ink-edge|湿墨晕染边缘
angular-contour|棱角轮廓
rounded-contour|圆润轮廓
broken-contour|断续轮廓
double-outline|双层描边
glowing-outline|发光轮廓
sketch-line|草图笔触
woodcut-line|木刻刀痕
crosshatch|交叉排线
stippling|点描线影
geometric-outline|几何轮廓
calligraphic-line|书法式飞线
chalk-line|粉笔线条
pencil-line|铅笔灰线
charcoal-line|炭笔擦痕
paper-tear-edge|撕纸边缘
pixel-edge|像素阶梯边缘
soft-paint-edge|柔和涂抹边缘
sharp-vector-edge|锐利矢量边缘
hand-drawn-jitter|手绘轻微抖动边缘
""",
    "character_design": """
large-head-small-body|大头小身比例
realistic-proportion|写实人体比例
geometric-body|几何化肢体
rounded-silhouette|圆润人物剪影
angular-silhouette|棱角人物剪影
expressive-eyes|夸张大眼表情
subtle-expression|克制细腻表情
expressive-limbs|夸张清晰肢体
minimal-face|极简面部特征
sculpted-face|雕塑式面部轮廓
flat-face-shapes|平面色块面部
textured-hair|分束发丝纹理
graphic-hair|图形化发型
flowing-fabric|飘动布料造型
structured-costume|硬朗服装轮廓
miniature-puppet|微缩偶人比例
paper-character|平面剪纸人物
clay-fingerprint|黏土手压角色表面
heroic-scale|英雄式身形比例
childlike-proportion|童趣角色比例
clear-pose|动作姿态轮廓清晰
layered-costume|分层服装造型
expressive-hands|手部动作清楚
silhouette-acting|剪影式肢体表演
consistent-features|稳定面部与服装特征
""",
    "palette": """
primary-four-color|红黄蓝黑四色主调
primary-three-color|红黄蓝三原色主调
warm-earth|暖土色系
cool-blue-gray|冷蓝灰色系
neon-cyan-magenta|青品红霓虹双色
vermilion-indigo|朱红与靛青对比
gold-navy|金色与深蓝对比
pastel-low-saturation|低饱和粉彩
muted-moss|灰绿苔色主调
monochrome-ink|黑白水墨层次
sepia|褐色怀旧调
complementary-orange-blue|橙蓝互补配色
limited-two-color|双色限定色板
limited-three-color|三色限定色板
warm-cool-split|暖主光与冷色阴影
high-saturation|高饱和色块
low-saturation|低饱和色阶
high-contrast|高对比明暗
low-contrast|低对比柔和明暗
gradient-sky|清澈渐变天空
mineral-pigments|矿物颜料配色
amber-lavender|琥珀与薰衣草紫
teal-coral|青绿与珊瑚红
black-red-accent|黑底单点朱红
gold-leaf-accent|金箔局部点缀
""",
    "shading": """
duotone-halftone|双色网点阴影
hard-two-step|硬边两级阴影
soft-gradient|柔和渐层阴影
flat-no-shading|平面无渐变明暗
rim-light|细窄轮廓光
backlight|逆光剪影
window-softlight|窗边柔和漫射光
directional-hardlight|硬质方向光
overcast-softlight|阴天散射光
high-key|高调均匀布光
low-key|低调局部布光
candle-warmth|烛火暖光
neon-edge|霓虹边缘光
warm-cool-shadows|暖光冷影
volumetric-beam-visible|可见雾中光束
colored-cast-shadow|彩色投影
paper-layer-shadow|纸层投影
miniature-studio-light|微缩模型布光
reflected-floor-light|地面反射补光
sun-dapple|树影斑驳日光
underlighting|下方光源照亮
side-light|侧向塑形光
ambient-even|均匀环境光
specular-highlight|局部镜面高光
matte-shading|哑光漫反射外观
""",
    "material": """
paper-fiber|纸纤维颗粒
crayon-grain|蜡笔颗粒
gouache-brush|水粉笔触
watercolor-bleed|水彩洇染
ink-wash|水墨晕染
clay-fingerprint|黏土手指压痕
felt-fabric|毛毡纤维
wood-grain|木材纹理
worn-brass|磨损黄铜表面
brushed-metal|拉丝金属表面
glass-refraction|透明玻璃折射
glazed-ceramic|瓷釉光泽
rough-stone|粗糙石面
wet-asphalt|湿润沥青反射
matte-plastic|哑光塑料外观
smooth-animation-surface|光滑动画曲面
handmade-paper|手工纸不规则纹理
printed-ink|印刷油墨颗粒
canvas-tooth|画布织纹
chalk-dust|粉笔粉尘
velvet-fabric|丝绒暗部质感
silk-sheen|丝绸柔和反光
frosted-glass|磨砂玻璃透光
rust-patina|氧化锈蚀纹理
gold-leaf|金箔片状反光
""",
    "composition": """
clean-negative-space|干净留白
dense-ornament|密集装饰画面
centered-symmetry|中心对称构图
off-center-balance|偏心平衡构图
diagonal-energy|对角线动势
layered-depth|前中后景分层
flat-depth|平面化空间
deep-perspective|深远透视线
frame-within-frame|框中框构图
foreground-occlusion|前景遮挡构图
rule-of-thirds|三分构图
graphic-silhouette|主体剪影突出
low-angle|低机位仰视
high-angle|高机位俯视
bird-eye|俯瞰平面构图
close-crop|紧凑裁切构图
wide-breathing-room|宽阔空间留量
asymmetric-tension|非对称视觉张力
repeating-shapes|重复几何形态
radial-focus|放射状视觉焦点
leading-lines|引导线汇聚
isometric-space|等距轴测空间
collage-layers|拼贴层次
split-screen|分屏画面
minimal-background|简洁背景
""",
    "graphic_motif": """
handdrawn-speedlines|手绘速度线
abstract-burst|抽象爆破形状
concentric-rings|同心几何圆环
angular-sparks|尖角火花
paper-shapes|剪纸色块
ink-splashes|墨点飞溅
smoke-ribbons|烟雾丝带
geometric-clouds|几何云纹
decorative-stars|装饰星芒
soft-particles|柔和点状粒子
rain-streaks|雨丝斜线
water-ripples|水波纹
leaf-silhouettes|叶片剪影
neon-lines|霓虹线条
circular-halftone|圆形网点纹
screenprint-offset|套印错位边缘
stitched-pattern|缝线图案
tile-mosaic|马赛克拼砖
stained-glass|彩色玻璃光块
ornate-scrollwork|卷草装饰纹
broken-grid|断裂网格
floating-geometry|悬浮几何体
brush-swipe|刷痕流线
shadow-puppets|皮影形状
lightning-arcs|短弧闪电
""",
    "visible_effect": """
motion-blur|运动拖影
chromatic-fringe|边缘色差
scanlines|细密扫描线
film-grain|胶片颗粒
posterized-colors|色阶分层
visible-bloom|高光柔和溢出
mist-layer|薄雾层
dust-motes|逆光尘点
rain-reflection|雨夜地面反射
silhouette-flash|短促剪影闪帧
ink-trail|墨迹动作拖尾
handdrawn-sparks|逐帧手绘火花
light-streak|光迹线条
paper-parallax|纸层视差
glass-caustic|玻璃折射光斑
water-caustic|水面焦散光斑
double-exposure-look|双重曝光外观
vignette|边缘暗角
heat-haze|热浪扭曲外观
pixel-blocks|像素块显露
star-particles|星点粒子
smoke-layer|分层烟雾
halftone-overlay|网点叠印
handmade-jitter|手工逐帧抖动
abstract-light-ring|抽象光环
""",
    "motion_language": """
exaggerated-pose|夸张清晰姿态
limited-animation|有限动画停顿
frame-by-frame-jitter|逐帧轻微抖动
smooth-ease|平滑缓入缓出
snappy-poses|利落关键姿态
elastic-stretch|弹性拉伸形变
physical-inertia|写实运动惯性
cloth-followthrough|布料跟随摆动
hair-followthrough|发丝跟随摆动
smear-frames|快速涂抹中间帧
stopmotion-staccato|定格式间歇运动
paper-layer-parallax|纸层运动视差
fluid-brush-trail|流动笔触拖尾
weight-shift|重心变化清楚
anticipation-pose|动作预备姿态
held-expression|表情定格停顿
clear-arc|动作弧线清楚
hand-gesture|手势变化清楚
silhouette-readable|运动剪影可读
gentle-drift|缓慢漂移
fast-snap|快速脆响转向
rhythmic-repetition|节奏性重复动作
measured-naturalism|克制自然动作
graphic-impact|图形化冲击动作
subtle-breath|细微呼吸起伏
""",
    "negative_constraint": """
no-readable-text|不生成可读文字
no-onomatopoeia|不生成拟声词
no-subtitles|不生成字幕
no-logo|不生成Logo
no-watermark|不生成水印
no-style-drift|全片不发生画风漂移
no-color-drift|不改变限定色板
no-costume-drift|不改变角色服装
no-face-drift|不改变面部特征
no-extra-limbs|不出现多余肢体
no-hand-distortion|不出现手部畸形
no-melted-geometry|不出现金属或几何形体融化
no-unmotivated-effects|不添加未指定光效
no-background-clutter|不添加杂乱背景
no-crowd-invention|不新增人群
no-prop-invention|不新增道具
no-flicker|避免无意闪烁
no-camera-jump|避免无意机位跳变
no-material-shift|不改变材质表现
no-accidental-realism|避免突然转为写实摄影
no-accidental-cartoon|避免突然转为卡通外观
no-oversaturation|避免无意过饱和
no-overexposure|避免无意过曝
no-blurry-subject|保持主体清晰
no-unreadable-symbols|不添加无关符号
""",
}

_DETAILS = {
    "line_language.bold-black-outline": ("角色及物体边缘有连续、较粗的黑色线条", "粗黑轮廓描边"),
    "palette.primary-four-color": ("红、黄、蓝为主要色块，黑色用于轮廓或暗部", "红黄蓝黑四色限定"),
    "shading.duotone-halftone": ("暗部可见规则点阵，阴影压缩为两级颜色", "暗部使用双色网点阴影"),
    "composition.clean-negative-space": ("主体周围大面积背景未填满细节", "构图保留干净留白"),
    "graphic_motif.handdrawn-speedlines": ("动作方向后方有手绘线性轨迹", "手绘速度线强调动作方向"),
    "graphic_motif.abstract-burst": ("主体周围出现放射状非具象色块", "抽象爆破形状突出动作冲击"),
    "character_design.expressive-limbs": ("角色四肢姿态幅度大且剪影清楚", "角色肢体夸张清晰"),
    "medium.three-d-animation": ("角色与环境具有连续立体体积和可见深度", "三维角色动画"),
    "material.clay-fingerprint": ("角色表面有局部按压痕与软陶颗粒", "保留黏土压痕与软陶颗粒"),
    "shading.hard-two-step": ("亮部与暗部以明确硬边分为两级", "硬边两级阴影"),
}

_CONFLICTS = {
    "medium.photography": ["medium.two-d-animation", "medium.three-d-animation", "medium.clay-stop-motion"],
    "line_language.bold-black-outline": ["line_language.no-outline"],
    "palette.high-saturation": ["palette.low-saturation"],
    "palette.high-contrast": ["palette.low-contrast"],
    "shading.flat-no-shading": ["shading.soft-gradient", "shading.duotone-halftone"],
    "composition.clean-negative-space": ["composition.dense-ornament"],
    "motion_language.limited-animation": ["motion_language.smooth-ease"],
}


@dataclass(frozen=True)
class StyleTerm:
    id: str
    category: str
    zh_label: str
    aliases: List[str]
    definition: str
    observable_cues: List[str]
    generation_phrase: str
    conflicts_with: List[str] = field(default_factory=list)
    compatible_with: List[str] = field(default_factory=list)
    source_type: str = "internally-authored"


@lru_cache(maxsize=1)
def terms() -> Dict[str, StyleTerm]:
    result: Dict[str, StyleTerm] = {}
    for category, body in _VOCABULARY.items():
        for line in body.strip().splitlines():
            slug, label = line.strip().split("|", 1)
            term_id = f"{category}.{slug}"
            cue, phrase = _DETAILS.get(term_id, (f"画面可辨认{label}的视觉表现", label))
            result[term_id] = StyleTerm(
                id=term_id, category=category, zh_label=label,
                aliases=[slug.replace("-", " ")], definition=cue,
                observable_cues=[cue] if category != "negative_constraint" else [],
                generation_phrase=phrase, conflicts_with=_CONFLICTS.get(term_id, []),
            )
    if len(result) != 300 or len(_VOCABULARY) != 12:
        raise RuntimeError("Style Ontology v1 必须包含12类、300个稳定词条")
    return result


def conflicts(ids: List[str]) -> List[str]:
    chosen = set(ids)
    return [f"{term_id} ↔ {other}" for term_id in ids for other in terms()[term_id].conflicts_with if other in chosen]
