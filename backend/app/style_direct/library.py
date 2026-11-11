"""Authored cinema knowledge, strict compilation, and evidence-only feature matching.

No network, embedding, model inference, or task mutation occurs in this module.
The editable source lives in Prompts; the packaged artifact is reproducible.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = "cinema-style-library.v1"
MATCHER_VERSION = "style-signatures.v4"
SOURCE_ROOT = Path(__file__).resolve().parents[3] / "Prompts" / "cinema_style_library"
ARTIFACT = Path(__file__).with_name("cinema_library.v1.json")
CORE_DIMENSIONS = {"medium_rendering", "form_language", "color_tone", "lighting_shading",
                   "texture_material", "motion_signature"}
Dimension = Literal["medium_rendering", "form_language", "color_tone", "lighting_shading",
                    "texture_material", "motion_signature", "photography", "local_effects", "technical_knowledge"]
Family = Literal["photo", "2d", "3d", "handmade", "painting", "design"]
Category = Literal["cinema", "documentary", "commercial", "animation2d", "animation3d", "handmade", "painting", "design"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StyleTerm(Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    label: str = Field(min_length=2, max_length=80)
    dimension: Dimension
    definition: str = Field(min_length=16, max_length=600)
    aliases: list[str] = Field(default_factory=list, max_length=16)
    # AND between groups, OR within a group. Generic single-word hits are not sufficient.
    positive_cues: list[list[str]] = Field(min_length=2, max_length=6)
    negative_cues: list[str] = Field(default_factory=list, max_length=12)
    generation_phrase: str = Field(min_length=6, max_length=180)
    scope: Literal["global", "shot", "knowledge"]
    source_ids: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def meaningful_cues(self):
        groups = []
        for alternatives in self.positive_cues:
            if not alternatives or any(not cue.strip() or len(cue) > 100 for cue in alternatives):
                raise ValueError("术语线索必须是非空同义备选组")
            group = frozenset(cue.casefold().strip() for cue in alternatives)
            if len(group) != len(alternatives) or group in groups:
                raise ValueError("同义词或特征条件重复，不能重复加分")
            groups.append(group)
        if self.scope == "global" and self.dimension not in CORE_DIMENSIONS:
            raise ValueError("摄影、局部特效和技术知识不能进入全局六维")
        return self


class SignatureGroup(Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    dimension: Dimension
    term_ids: list[str] = Field(min_length=1, max_length=4)


class StyleRecipe(Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    label: str = Field(min_length=4, max_length=100)
    family: Family
    category: Category
    definition: str = Field(min_length=24, max_length=600)
    aliases: list[str] = Field(default_factory=list, max_length=12)
    signature_groups: list[SignatureGroup] = Field(min_length=2, max_length=5)
    supporting_term_ids: list[str] = Field(default_factory=list, max_length=12)
    insufficient_evidence: list[str] = Field(min_length=2, max_length=12)
    confusable_with: list[str] = Field(default_factory=list, max_length=8)
    distinction: str = Field(min_length=16, max_length=500)
    compact_prompt: str = Field(min_length=50, max_length=140)
    detailed_description: str = Field(min_length=80, max_length=1000)
    generation_constraints: list[str] = Field(default_factory=list, max_length=8)
    source_ids: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def cross_dimension(self):
        if len({group.id for group in self.signature_groups}) != len(self.signature_groups):
            raise ValueError("配方特征组ID重复")
        if len({group.dimension for group in self.signature_groups}) < 2:
            raise ValueError("具体风格必须具有至少两个不同维度的特征组合")
        if any(group.dimension not in CORE_DIMENSIONS for group in self.signature_groups):
            raise ValueError("具体摄影或技术知识不能成为全局画风鉴别组")
        return self


class QuestionAnswer(Strict):
    question: str = Field(min_length=5, max_length=300)
    answer: str = Field(min_length=12, max_length=1500)


class KnowledgeCard(Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    title: str = Field(min_length=4, max_length=150)
    summary: str = Field(min_length=60, max_length=2000)
    related_recipe_ids: list[str] = Field(default_factory=list, max_length=20)
    related_term_ids: list[str] = Field(default_factory=list, max_length=30)
    questions: list[QuestionAnswer] = Field(min_length=2, max_length=8)
    source_ids: list[str] = Field(min_length=1, max_length=8)


class SourceReference(Strict):
    id: str
    name: str
    url: str
    license: str
    policy: Literal["authored-reference", "research-only", "open-data"]
    revision: str = Field(min_length=2)
    license_url: str = ""
    notes: str


class Taxonomy(Strict):
    version: Literal["cinema-style-taxonomy.v1"]
    dimensions: list[str]
    non_global_dimensions: list[str]
    category_targets: dict[str, int]
    rules: list[str]

    @model_validator(mode="after")
    def supported_dimensions(self):
        if set(self.dimensions) != CORE_DIMENSIONS or len(self.dimensions) != len(CORE_DIMENSIONS):
            raise ValueError("分类表的全局六维与运行时不一致")
        if set(self.non_global_dimensions) != {"photography", "local_effects", "technical_knowledge"}:
            raise ValueError("局部/技术分类边界无效")
        if any(value < 1 for value in self.category_targets.values()):
            raise ValueError("覆盖目标必须为正整数")
        return self


class CinemaLibrary(Strict):
    version: Literal["cinema-style-library.v1"] = VERSION
    source_hash: str = ""
    terms: list[StyleTerm]
    recipes: list[StyleRecipe]
    knowledge: list[KnowledgeCard]
    sources: list[SourceReference]
    taxonomy: Optional[Taxonomy] = None

    @model_validator(mode="after")
    def valid_relationships(self):
        for name in ("terms", "recipes", "knowledge", "sources"):
            ids = [item.id for item in getattr(self, name)]
            if len(ids) != len(set(ids)):
                raise ValueError("知识库ID重复：" + name)
        terms = {item.id: item for item in self.terms}
        recipes = {item.id: item for item in self.recipes}
        sources = {item.id for item in self.sources}
        for item in [*self.terms, *self.recipes, *self.knowledge]:
            if set(item.source_ids) - sources:
                raise ValueError("来源引用不存在：" + item.id)
        for recipe in self.recipes:
            for group in recipe.signature_groups:
                if len(group.term_ids) != len(set(group.term_ids)) or set(group.term_ids) - terms.keys():
                    raise ValueError("配方引用无效或重复术语：" + recipe.id)
                for identifier in group.term_ids:
                    term = terms[identifier]
                    if term.dimension != group.dimension or term.scope != "global":
                        raise ValueError("配方鉴别组维度/作用域不一致：" + recipe.id + "/" + identifier)
            if set(recipe.supporting_term_ids) - terms.keys():
                raise ValueError("辅助术语不存在：" + recipe.id)
            if any(terms[t].scope != "global" for t in recipe.supporting_term_ids):
                raise ValueError("局部术语不能用于全局配方：" + recipe.id)
            if set(recipe.confusable_with) - recipes.keys() or recipe.id in recipe.confusable_with:
                raise ValueError("混淆配方引用不存在或自引用：" + recipe.id)
        for card in self.knowledge:
            if set(card.related_term_ids) - terms.keys() or set(card.related_recipe_ids) - recipes.keys():
                raise ValueError("知识卡引用不存在：" + card.id)
        return self


def source_files(root=SOURCE_ROOT):
    root = Path(root)
    return [root / "sources.v1.json", root / "taxonomy.v1.json", *sorted((root / "terms").glob("*.json")),
            *sorted((root / "recipes").glob("*.json")), *sorted((root / "knowledge").glob("*.json"))]


def read_source(root=SOURCE_ROOT):
    root = Path(root)
    combined = {"version": VERSION, "terms": [], "recipes": [], "knowledge": [], "sources": []}
    hash_input = []
    for path in source_files(root):
        data = json.loads(path.read_text(encoding="utf8"))
        if path.name == "taxonomy.v1.json":
            combined["taxonomy"] = Taxonomy.model_validate(data).model_dump()
            hash_input.append({"path": path.name, "data": data})
            continue
        key = "sources" if path.parent == root else path.parent.name
        expected = "cinema-style-sources.v1" if key == "sources" else VERSION
        if set(data) != {"version", key} or data["version"] != expected or not isinstance(data[key], list):
            raise ValueError("源文件结构/版本无效：" + str(path))
        combined[key].extend(data[key])
        hash_input.append({"path": str(path.relative_to(root)), "data": data})
    if not all(combined[key] for key in ("terms", "recipes", "knowledge", "sources")):
        raise ValueError("知识库源文件缺少术语、配方、解释或来源")
    combined["source_hash"] = hashlib.sha256(json.dumps(hash_input, ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":")).encode()).hexdigest()
    return CinemaLibrary.model_validate(combined)


@lru_cache(maxsize=1)
def runtime_library():
    # Do not silently fall back to the old generic cue catalogue on a broken build.
    return CinemaLibrary.model_validate_json(ARTIFACT.read_text(encoding="utf8"))


def reset_cache():
    from .lexicon import reset_matching_cache
    runtime_library.cache_clear()
    retrieval_text.cache_clear()
    reset_matching_cache()
    _cue_pattern.cache_clear()
    anchors = sys.modules.get(__package__ + ".anchors")
    if anchors is not None:
        anchors.catalog.cache_clear()


def overview(library):
    lines = ["# 电影视觉风格配方总览", "", "本文件由源 JSON 生成；请修改 terms / recipes / knowledge 后重新构建。",
             "", "来源哈希：`" + library.source_hash + "`", "",
             "这些配方是视觉参考与创作方案，不是作者来源鉴定；未经画面支持的特征不自动进入识别结果。", ""]
    for recipe in library.recipes:
        lines.extend(["## " + recipe.label, "", "ID：`" + recipe.id + "` · " + recipe.category, "",
                      "**整体画面风格**：" + recipe.compact_prompt, "", recipe.detailed_description, "",
                      "**鉴别组合**："])
        terms = {term.id: term for term in library.terms}
        lines.extend(["", *["- " + group.dimension + "：" + "；".join(terms[t].generation_phrase for t in group.term_ids)
                             for group in recipe.signature_groups], "",
                      "**不足以单独判定**：" + "；".join(recipe.insufficient_evidence), "",
                      "**相近风格区别**：" + recipe.distinction, ""])
        if recipe.generation_constraints:
            lines.extend(["**生成约束（非观察事实）**：" + "；".join(recipe.generation_constraints), ""])
        lines.extend(["来源：" + "、".join(recipe.source_ids), ""])
    lines.extend(["# 区别与问答", ""])
    for card in library.knowledge:
        lines.extend(["## " + card.title, "", card.summary, ""])
        for qa in card.questions:
            lines.extend(["### " + qa.question, "", qa.answer, ""])
        lines.extend(["来源：" + "、".join(card.source_ids), ""])
    return "\n".join(lines)


def build(root=SOURCE_ROOT, artifact=ARTIFACT):
    library = read_source(root)
    artifact = Path(artifact)
    # Same-directory replacement keeps a failed build from corrupting the active package.
    temporary = artifact.with_suffix(".json.tmp")
    temporary.write_text(library.model_dump_json(indent=2), encoding="utf8")
    schema_dir = Path(root) / "schemas"
    schema_dir.mkdir(exist_ok=True)
    (schema_dir / "library.schema.json").write_text(json.dumps(CinemaLibrary.model_json_schema(), ensure_ascii=False,
                                                            indent=2) + "\n", encoding="utf8")
    (Path(root) / "CATALOG.md").write_text(overview(library), encoding="utf8")
    temporary.replace(artifact)
    reset_cache()
    return library


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


@lru_cache(maxsize=4096)
def retrieval_text(text):
    from .lexicon import normalized_cues
    return normalized_cues(text)


_MENTION_BOUNDARY = re.compile(
    r"[，。；,;!?！？，\n]|(?<!\d)\.(?!\d)|而是|但是|然而|不过|反而|但|却|"
    r"\b(?:but|however|yet|instead)\b", re.I)
_AFFIRMATIVE_QUALIFIER = re.compile(
    r"不是没有|并非没有|并非不存在|不(?:仅|只|止)(?:是)?|\bnot\s+(?:only|merely|just)\b", re.I)
NEGATED_PREFIX = re.compile(
    r"(?:没有|并非|不是|未见|未(?:能)?观察到|未发现|看不到|看不出|缺乏|缺少|"
    r"不具有|不具备|不呈现|不带有|不存在|不明显|不清楚|不明确|而非|"
    r"\bno\b|\bnot\b|\bwithout\b|\babsence of\b|\black(?:s|ing)?\b).*$|"
    r"(?:无|不)(?:明显(?:的)?|任何(?:的)?|可见(?:的)?|清晰(?:的)?)?\s*$|"
    r"(?:^|\s)非(?!常|凡|但).*$", re.I)
NEGATED_SUFFIX = re.compile(
    r"^\s*(?:(?:[与和及、]|\band\b)\s*.{1,40})?"
    r"(?:的(?:特征|效果|表现|外观))?\s*"
    r"(?:(?:也|都|均|并|仍|则|完全|基本|几乎|其实|确实)\s*)*"
    r"(?:不存在|不成立|不明显|不清楚|不突出|不可见|未见|未被观察到|未观察到|"
    r"未发现|未呈现|缺失|没有(?:出现|显现|体现|表现)|没有$|不足|"
    r"(?:并非|不是)(?:可见|明显|主要)(?:特征|效果)?)|"
    r"^\s*(?:(?:is|are|was|were)\s+)?(?:absent|missing|invisible|unobserved)\b|"
    r"^\s*(?:(?:is|are|was|were)\s+)?(?:not|never)\s+"
    r"(?:present|visible|observed|apparent|noticeable|evident|detectable)\b|"
    r"^\s*(?:does|do|did)\s+not\s+exist\b", re.I)


@lru_cache(maxsize=1024)
def _cue_pattern(cue):
    pattern = re.escape(cue)
    if re.search(r"[a-z]", cue):
        pattern = r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])"
    return re.compile(pattern)


def positive_mention(text, cue):
    """Require an affirmed occurrence within its clause, including trailing denial.

    This is a bounded textual gate, not a proof that a feature is visible in an image.
    Contrast resets the denial scope; absence cues such as '无轮廓线' remain intact.
    """
    text, cue = retrieval_text(text), retrieval_text(cue)
    if not cue:
        return False
    boundaries = list(_MENTION_BOUNDARY.finditer(text))
    for match in _cue_pattern(cue).finditer(text):
        left = max((boundary.end() for boundary in boundaries if boundary.end() <= match.start()), default=0)
        right = min((boundary.start() for boundary in boundaries if boundary.start() >= match.end()), default=len(text))
        prefix = _AFFIRMATIVE_QUALIFIER.sub('', text[left:match.start()])
        suffix = text[match.end():right]
        if not NEGATED_PREFIX.search(prefix) and not NEGATED_SUFFIX.search(suffix):
            return True
    return False


def term_matches(term, text):
    if any(positive_mention(text, cue) for cue in term.negative_cues):
        return False
    # Labels and model confidence cannot stand in for the authored feature combination.
    return all(any(positive_mention(text, cue) for cue in alternatives)
               for alternatives in term.positive_cues)


def group_evidence(recipe, claims, library=None):
    """Match each complete feature within same-dimension facts, never fabricated concatenation."""
    library = library or runtime_library()
    terms = {term.id: term for term in library.terms}
    matched = {}
    for group in recipe.signature_groups:
        by_term = {}
        for identifier in group.term_ids:
            term = terms[identifier]
            ids = [getattr(claim, "id", "") for claim in claims
                   if claim.dimension == group.dimension and getattr(claim, "confidence", 1) >= .5
                   and term_matches(term, claim.observation)]
            if ids:
                by_term[identifier] = ids
        if len(by_term) == len(group.term_ids):
            matched[group.id] = by_term
    return matched


def search(query, *, section="all", limit=8, library=None):
    """Local bilingual/Chinese substring search for future grounded QA; not style recognition."""
    library = library or runtime_library()
    tokens = [normalize(part) for part in re.split(r"\s+", query) if part.strip()]
    if not tokens or len(query) > 1000:
        return []
    sections = ("terms", "recipes", "knowledge") if section == "all" else (section,)
    if any(name not in {"terms", "recipes", "knowledge"} for name in sections):
        raise ValueError("检索类型无效")
    results = []
    source_map = {source.id: source.model_dump() for source in library.sources}
    for name in sections:
        for item in getattr(library, name):
            value = item.model_dump()
            # Provenance URLs and JSON keys should not produce false content matches.
            searchable = {key: content for key, content in value.items() if key not in {"id", "source_ids"}}
            text = normalize(json.dumps(searchable, ensure_ascii=False))
            score = sum(text.count(token) for token in tokens if token in text)
            if score:
                results.append({"section": name, "id": item.id, "score": score, "entry": value,
                                "sources": [source_map[key] for key in item.source_ids]})
    return sorted(results, key=lambda row: (-row["score"], row["id"]))[:min(30, max(1, limit))]
