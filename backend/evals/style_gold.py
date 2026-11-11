"""Validate human-authored style truth and score exported StyleProfiles.

No media or model-generated labels are bundled with the repository.  The real
60-sample gate remains pending until reviewers provide licensed local samples.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.style_ontology.catalog import terms


class HumanStyleEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame_id: str = Field(min_length=1)
    timestamp: float = Field(ge=0)
    description: str = Field(min_length=1)


class HumanStyleSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_id: str = Field(min_length=1)
    source: Literal["human"] = "human"
    reviewer: str = Field(min_length=1)
    reviewed_at: str = Field(min_length=1)
    rights: Literal["owned", "licensed"]
    rights_reference: str = Field(min_length=1)
    media_file: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    expected_terms: List[str] = Field(min_length=1)
    evidence: Dict[str, List[HumanStyleEvidence]]
    notes: str = ""

    @model_validator(mode="after")
    def valid_truth(self):
        catalogue = terms()
        if Path(self.media_file).is_absolute() or ".." in Path(self.media_file).parts:
            raise ValueError("media_file 必须是素材目录下的安全相对路径")
        if any(item not in catalogue for item in self.expected_terms):
            raise ValueError("人工标注包含未知画风术语")
        if len(set(self.expected_terms)) != len(self.expected_terms):
            raise ValueError("人工标注存在重复术语")
        if any(item not in self.expected_terms for item in self.evidence):
            raise ValueError("证据引用了未标注的术语")
        if any(not self.evidence.get(item) for item in self.expected_terms
               if catalogue[item].category != "negative_constraint"):
            raise ValueError("观察画风术语必须带人工帧证据")
        return self


class HumanStyleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["style-gold.v1"] = "style-gold.v1"
    samples: List[HumanStyleSample] = Field(min_length=60)

    @model_validator(mode="after")
    def coverage(self):
        if len({item.sample_id for item in self.samples}) != len(self.samples):
            raise ValueError("样本 ID 重复")
        categories = {terms()[term].category for item in self.samples for term in item.expected_terms}
        if categories != {item.category for item in terms().values()}:
            raise ValueError("60 样本必须覆盖全部12个画风维度")
        return self


def verify_media(sample: HumanStyleSample, media_dir: Path) -> None:
    root = media_dir.resolve()
    path = (root / sample.media_file).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError(f"素材不存在或越界：{sample.sample_id}")
    digestor = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digestor.update(chunk)
    digest = digestor.hexdigest()
    if digest != sample.sha256.lower():
        raise ValueError(f"素材 SHA-256 不匹配：{sample.sample_id}")


def score(prediction: dict, gold: HumanStyleSample) -> dict:
    profile = prediction.get("style_profile") or prediction.get("profile") or {}
    selected = set(profile.get("global_terms") or []) | set(profile.get("negative_constraints") or [])
    expected = set(gold.expected_terms)
    tp = len(selected & expected)
    precision = tp / len(selected) if selected else 0.0
    recall = tp / len(expected) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    mediums = {item for item in expected if terms()[item].category == "medium"}
    return {
        "sample_id": gold.sample_id, "exact_term_precision": precision,
        "exact_term_recall": recall, "exact_term_f1": f1,
        "medium_match": bool(selected & mediums) if mediums else None,
        "missing_terms": sorted(expected - selected),
        "extra_terms": sorted(selected - expected),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, help="JSON map: sample_id -> exported storyboard result")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    golden = HumanStyleSet.model_validate_json(args.gold.read_text(encoding="utf-8"))
    for sample in golden.samples:
        verify_media(sample, args.media_dir)
    report = {"schema_version": "style-review.v1", "sample_count": len(golden.samples),
              "reviewers": sorted({item.reviewer for item in golden.samples}),
              "medium_accuracy": None, "mean_exact_term_f1": None, "samples": []}
    if args.predictions:
        predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
        missing = set(item.sample_id for item in golden.samples) - set(predictions)
        if missing:
            raise ValueError(f"缺少预测结果：{sorted(missing)}")
        report["samples"] = [score(predictions[item.sample_id], item) for item in golden.samples]
        medium_rows = [item for item in report["samples"] if item["medium_match"] is not None]
        report["medium_accuracy"] = sum(item["medium_match"] for item in medium_rows) / len(medium_rows) if medium_rows else None
        report["mean_exact_term_f1"] = sum(item["exact_term_f1"] for item in report["samples"]) / len(golden.samples)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
