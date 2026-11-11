"""Reviewed, versioned Cinema Visual Style vocabulary.

The checked-in JSON is the production source of truth. V1 is never imported here.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path


VERSION = "style-ontology.v2"
CATEGORIES = (
    "medium_rendering", "form_language", "color_tone",
    "lighting_shading", "texture_material", "motion_signature",
)
CATALOG_PATH = Path(__file__).with_name("catalog.v2.json")


@dataclass(frozen=True)
class StyleTermV2:
    id: str
    category: str
    name_cn: str
    name_en: str
    definition: str
    positive_cues: tuple[str, ...]
    negative_cues: tuple[str, ...]
    aliases: tuple[str, ...]
    generation_phrase: str
    compatible_with: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()

    def retrieval_views(self) -> tuple[str, ...]:
        return (
            f"{self.name_cn}。{self.definition}。可见线索：{'；'.join(self.positive_cues)}。",
            f"{self.name_cn}，{'，'.join(self.aliases)}。区别：不属于{'、'.join(self.negative_cues) or '其他媒介'}。",
            self.generation_phrase,
        )


@dataclass(frozen=True)
class StyleRecipeV2:
    id: str
    name: str
    suitable_for: tuple[str, ...]
    term_ids: tuple[str, ...]
    global_prompt: str
    shot_constraints: str
    forbidden_drift: tuple[str, ...]
    optional_anchors: tuple[str, ...] = ()
    family: str = ""
    retrieval_description: str = ""
    positive_cues: tuple[str, ...] = ()
    negative_cues: tuple[str, ...] = ()
    dynamic_description: str = ""

    def retrieval_text(self) -> str:
        # No artist/studio names or story objects in the vector query/index.
        # Retrieval is about visible appearance, not suitable genres, device
        # provenance, prohibitions, or motion that a still cannot establish.
        if self.retrieval_description:
            return "视觉特征：" + self.retrieval_description + "。区分线索：" + "；".join(self.positive_cues)
        return f"{self.name}。视觉配方：{self.global_prompt}"


@lru_cache(maxsize=1)
def _source() -> dict:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if payload.get("version") != VERSION:
        raise ValueError("Style Ontology 文件版本不匹配")
    return payload


@lru_cache(maxsize=1)
def terms() -> dict[str, StyleTermV2]:
    result = {}
    for raw in _source()["terms"]:
        item = StyleTermV2(**{
            **raw,
            **{key: tuple(raw.get(key, [])) for key in (
                "positive_cues", "negative_cues", "aliases", "compatible_with", "conflicts_with"
            )},
        })
        if item.id in result:
            raise ValueError(f"重复术语 ID：{item.id}")
        result[item.id] = item
    if not 120 <= len(result) <= 180:
        raise ValueError(f"Style Ontology v2 首版术语应为120–180项，实际{len(result)}")
    return result


def ontology_hash() -> str:
    payload = [vars(item) for item in terms().values()]
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _recipes() -> dict[str, StyleRecipeV2]:
    result = {}
    for raw in _source()["recipes"]:
        item = StyleRecipeV2(**{
            **raw,
            **{key: tuple(raw.get(key, [])) for key in ("suitable_for", "term_ids", "forbidden_drift", "optional_anchors", "positive_cues", "negative_cues")},
        })
        if item.id in result:
            raise ValueError(f"重复风格配方 ID：{item.id}")
        result[item.id] = item
    return result


RECIPES = _recipes()


def validate_catalog() -> None:
    catalogue = terms()
    for item in catalogue.values():
        if item.category not in CATEGORIES or not item.definition or not item.positive_cues:
            raise ValueError(f"不完整风格术语：{item.id}")
        if any(other not in catalogue for other in item.conflicts_with + item.compatible_with):
            raise ValueError(f"无效术语关系：{item.id}")
        if item.id in item.conflicts_with:
            raise ValueError(f"术语不能与自身冲突：{item.id}")
    for recipe in RECIPES.values():
        if any(item not in catalogue for item in recipe.term_ids + recipe.forbidden_drift):
            raise ValueError(f"配方引用了未知术语：{recipe.id}")
        if not recipe.global_prompt or not recipe.shot_constraints:
            raise ValueError(f"配方内容不完整：{recipe.id}")
        if not recipe.retrieval_description or not recipe.positive_cues or not recipe.negative_cues:
            raise ValueError(f"配方缺少静态检索描述及区分线索：{recipe.id}")
