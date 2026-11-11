"""Bilingual retrieval normalization, never translation or new visual evidence."""
import re
import unicodedata
from functools import lru_cache


# Whole English words/phrases map to the existing authored cue vocabulary.
# Original claims and evidence are kept unchanged for audit and model interpretation.
ALIASES = {
    '三维': ('3d', 'three dimensional', 'three-dimensional', 'cgi', 'cg animation'),
    '二维': ('2d', 'two dimensional', 'two-dimensional', 'cel animation'),
    '写实摄影': ('live action', 'live-action', 'real photography', 'photographic image', 'documentary photography'),
    '摄影': ('photography', 'photographic'),
    '手绘': ('hand drawn', 'hand-drawn'), '夸张': ('exaggerated', 'exaggeration'),
    '表情': ('expressive', 'expressive faces', 'facial expressions'),
    '曲线': ('curved', 'curves', 'curvilinear'), '流线': ('flowing contours', 'streamlined'),
    '几何': ('geometric', 'geometry'), '块面': ('shape blocks', 'block forms'),
    '比例': ('proportions', 'proportion'), '材质': ('material', 'materials'),
    '通透': ('luminous', 'translucent'), '背景': ('background', 'backgrounds'),
    '天空': ('sky', 'skies'), '逆光': ('backlit', 'backlighting', 'backlight'),
    '光晕': ('bloom', 'halo'), '水彩': ('watercolor', 'watercolour'),
    '柔和': ('soft', 'gentle', 'diffused'), '笔触': ('brush strokes', 'brushstrokes', 'brushwork'),
    '线稿': ('linework', 'line art'), '色块': ('color blocks', 'colour blocks'),
    '分色': ('cel shading', 'cel-shaded'), '硬边': ('hard edges', 'hard-edged'),
    '网点': ('halftone',), '粗黑': ('bold black outlines',), '限定色': ('limited palette',),
    '波普': ('pop art',), '爆破': ('burst shapes',),
    '多边形': ('low poly', 'low-poly', 'polygonal'), '三角': ('triangular',),
    '棱面': ('faceted', 'facets'), '平滑曲面': ('smooth curved surfaces',),
    '照片级': ('photorealistic', 'photorealism'), '微观': ('microscopic',), '反射': ('reflections', 'reflective'),
    '纸': ('paper',), '剪纸': ('paper cut', 'paper-cut', 'papercut'), '纤维': ('fibers', 'fibres', 'fiber', 'fibre'),
    '拼贴': ('collage',), '裁切': ('cut edges', 'cutout'), '层叠': ('layered',),
    '黏土': ('clay',), '压痕': ('indentations',), '手塑': ('hand sculpted', 'hand-sculpted'),
    '微缩': ('miniature',), '晕染': ('wash', 'washes'), '纸白': ('paper white',),
    '渗化': ('bleeding edges',), '厚涂': ('impasto',), '油画': ('oil painting',), '颜料': ('pigment',),
    '水墨': ('ink wash',), '留白': ('negative space',), '金属高光': ('metallic highlights',),
    '扁平': ('flat',), '矢量': ('vector',), '无纹理': ('textureless',),
    '像素': ('pixel art',), '低分辨率': ('low resolution',), '调色板': ('palette',),
    '阶梯': ('stepped edges',), '低调': ('low key', 'low-key'),
    '黑白': ('black and white', 'monochrome'), '高反差': ('high contrast', 'high-contrast'),
    '硬光': ('hard light',), '深黑': ('deep blacks',), '高调': ('high key', 'high-key'),
    '自然': ('natural', 'naturalistic'), '写实': ('realistic', 'realism'),
    '层次': ('tonal layers', 'tonal separation'), '冷调': ('cool tones', 'cool-toned'),
    '青灰': ('blue gray', 'blue grey'), '低饱和': ('muted', 'desaturated'),
    '颗粒': ('grain', 'grainy'), '暖调': ('warm tones', 'warm-toned'),
    '褪色': ('faded',), '柔化': ('softened',), '霓虹': ('neon',), '混色': ('mixed colors',),
    '品红': ('magenta',), '实景光源': ('practical lights', 'practical lighting'),
    '日常': ('everyday',), '宽景深': ('deep depth of field', 'deep focus'),
    '自然光': ('natural light', 'daylight'), '数字': ('digital',), '室内': ('indoor', 'interior'),
    '纪实': ('documentary',), '环境': ('environmental',), '洁净': ('clean',),
    '高光': ('highlights',), '中性': ('neutral',),
    # These are visible feature paraphrases, never style names or whole recipes.
    '轮廓': ('描边', '外形边界', 'outline', 'outlines', 'contours'),
    '硬边阴影': ('阴影边界锐利', '阴影边缘锐利', '阴影呈现边缘锐利的色块',
                  '边缘锐利的阴影色块', 'hard-edged shadows', 'sharply bounded shadows'),
}


@lru_cache(maxsize=1)
def _alias_patterns():
    pairs = sorted(((alias, cue) for cue, aliases in ALIASES.items() for alias in aliases),
                   key=lambda pair: -len(pair[0]))
    return [(re.compile(r'(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])', re.I), cue)
            for alias, cue in pairs]


@lru_cache(maxsize=4096)
def normalized_cues(text):
    """Normalize aliases in place so a negated feature never gains a positive tail."""
    normalized = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text).casefold()).strip()
    for pattern, cue in _alias_patterns():
        normalized = pattern.sub(cue, normalized)
    return normalized


def reset_matching_cache():
    normalized_cues.cache_clear()
    _alias_patterns.cache_clear()


def clauses(text):
    """Split independent Chinese/English clauses, without cutting decimal numbers."""
    return [part.strip() for part in re.split(r'[，；。;,]|(?<!\d)\.(?!\d)', text) if part.strip()]
