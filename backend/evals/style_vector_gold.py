"""Human-only, licensed benchmark for Cinema Style Vector v2.

No reference images or model-produced gold labels are shipped in the repo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.style_vector.models import clean_style_description
from app.style_vector.ontology import CATEGORIES, terms


class HumanEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame_id: str = Field(min_length=1)
    timestamp: float = Field(ge=0)
    description: str = Field(min_length=1)


class StyleVectorGoldSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_id: str = Field(min_length=1)
    source: Literal["human"] = "human"
    reviewer: str = Field(min_length=1)
    reviewed_at: str = Field(min_length=1)
    rights: Literal["owned", "licensed"]
    rights_reference: str = Field(min_length=1)
    media_file: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    category: Literal["medium_rendering", "form_language", "color_tone", "lighting_shading", "texture_material", "motion_signature"]
    query: str = Field(min_length=2)
    expected_term_ids: List[str] = Field(min_length=1)
    expected_scope: Literal["GLOBAL", "SCENE", "SHOT", "UNCERTAIN"]
    scene_id: str = ""
    evidence: List[HumanEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_truth(self):
        path = Path(self.media_file)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("素材文件必须是安全相对路径")
        catalogue = terms()
        if any(term not in catalogue or catalogue[term].category != self.category for term in self.expected_term_ids):
            raise ValueError("人工术语与维度不一致")
        if len(set(self.expected_term_ids)) != len(self.expected_term_ids):
            raise ValueError("人工术语重复")
        if self.expected_scope == "SCENE" and not self.scene_id:
            raise ValueError("场次级真值缺少 scene_id")
        return self


class StyleVectorGoldSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["style-vector-gold.v2"] = "style-vector-gold.v2"
    samples: List[StyleVectorGoldSample] = Field(min_length=60)
    adversarial_content: List[str] = Field(min_length=30)

    @model_validator(mode="after")
    def coverage(self):
        if len({sample.sample_id for sample in self.samples}) != len(self.samples):
            raise ValueError("样本 ID 重复")
        if {sample.category for sample in self.samples} != set(CATEGORIES):
            raise ValueError("黄金集必须覆盖六类核心画风")
        if {sample.expected_scope for sample in self.samples} != {"GLOBAL", "SCENE", "SHOT", "UNCERTAIN"}:
            raise ValueError("黄金集必须覆盖四种作用范围")
        if len(set(self.adversarial_content)) != len(self.adversarial_content):
            raise ValueError("内容对抗样例重复")
        return self


def verify_media(sample: StyleVectorGoldSample, media_dir: Path) -> None:
    root = media_dir.resolve()
    path = (root / sample.media_file).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError(f"素材不存在或越界：{sample.sample_id}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != sample.sha256.lower():
        raise ValueError(f"素材 SHA-256 不一致：{sample.sample_id}")


def _macro_f1(actual: List[str], predicted: List[str]) -> float:
    scores = []
    for label in ("GLOBAL", "SCENE", "SHOT", "UNCERTAIN"):
        tp = sum(a == p == label for a, p in zip(actual, predicted))
        fp = sum(a != label and p == label for a, p in zip(actual, predicted))
        fn = sum(a == label and p != label for a, p in zip(actual, predicted))
        denominator = 2 * tp + fp + fn
        scores.append(2 * tp / denominator if denominator else 1.0)
    return sum(scores) / len(scores)


def score(gold: StyleVectorGoldSet, predictions: Dict[str, dict]) -> dict:
    missing = {item.sample_id for item in gold.samples} - set(predictions)
    if missing:
        raise ValueError(f"缺少预测：{sorted(missing)}")
    rows = []
    scopes, predicted_scopes = [], []
    for item in gold.samples:
        found = predictions[item.sample_id]
        candidates = list(found.get("candidate_ids") or [])[:5]
        position = next((n + 1 for n, term in enumerate(candidates) if term in item.expected_term_ids), None)
        scope = found.get("scope") or "UNCERTAIN"
        if scope not in ("GLOBAL", "SCENE", "SHOT", "UNCERTAIN"):
            raise ValueError(f"无效范围：{item.sample_id}")
        scopes.append(item.expected_scope)
        predicted_scopes.append(scope)
        rows.append({"sample_id": item.sample_id, "recall_at_5": position is not None,
                     "reciprocal_rank": 1 / position if position else 0,
                     "scope_correct": scope == item.expected_scope,
                     "global_false_promotion": scope == "GLOBAL" and item.expected_scope != "GLOBAL"})
    leakage = [text for text in gold.adversarial_content
               if any(clean_style_description(text, category) for category in CATEGORIES)]
    count = len(rows)
    non_global = sum(item.expected_scope != "GLOBAL" for item in gold.samples)
    return {
        "schema_version": "style-vector-report.v2", "sample_count": count,
        "recall_at_5": sum(row["recall_at_5"] for row in rows) / count,
        "mrr": sum(row["reciprocal_rank"] for row in rows) / count,
        "scope_macro_f1": _macro_f1(scopes, predicted_scopes),
        "global_false_promotion_rate": sum(row["global_false_promotion"] for row in rows) / non_global,
        "adversarial_content_leakage_rate": len(leakage) / len(gold.adversarial_content),
        "leaked_content": leakage, "samples": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    golden = StyleVectorGoldSet.model_validate_json(args.gold.read_text(encoding="utf-8"))
    for sample in golden.samples:
        verify_media(sample, args.media_dir)
    result = {"schema_version": "style-vector-report.v2", "sample_count": len(golden.samples),
              "reviewers": sorted({item.reviewer for item in golden.samples}), "metrics": None}
    if args.predictions:
        result["metrics"] = score(golden, json.loads(args.predictions.read_text(encoding="utf-8")))
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
