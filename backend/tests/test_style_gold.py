import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.style_gold import HumanStyleSample, HumanStyleSet, score, verify_media
from app.style_ontology.catalog import terms


def sample_payload(index: int, term_id: str, digest: str):
    return {
        "sample_id": f"sample-{index:03d}", "source": "human", "reviewer": "人工标注员",
        "reviewed_at": "2026-09-28", "rights": "owned", "rights_reference": "创作者本人", "media_file": "sample.png", "sha256": digest,
        "expected_terms": [term_id],
        "evidence": {} if term_id.startswith("negative_constraint.") else
        {term_id: [{"frame_id": "F1", "timestamp": 0.0, "description": "人工可见视觉证据"}]},
    }


def test_human_gold_requires_60_licensed_samples_and_all_dimensions(tmp_path):
    media = tmp_path / "sample.png"
    media.write_bytes(b"authored sample fixture")
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    categories = {}
    for item in terms().values():
        categories.setdefault(item.category, item.id)
    samples = [sample_payload(index, list(categories.values())[index % 12], digest) for index in range(60)]
    gold = HumanStyleSet(samples=samples)
    assert len(gold.samples) == 60
    verify_media(gold.samples[0], tmp_path)
    report = score({"style_profile": {"global_terms": [samples[0]["expected_terms"][0]]}}, gold.samples[0])
    assert report["exact_term_f1"] == 1 and report["medium_match"]
    with pytest.raises(ValidationError):
        HumanStyleSet(samples=samples[:59])
    with pytest.raises(ValidationError, match="证据"):
        HumanStyleSample.model_validate({**samples[0], "evidence": {}})
    with pytest.raises(ValidationError, match="相对路径"):
        HumanStyleSample.model_validate({**samples[0], "media_file": "../outside.png"})


def test_media_hash_must_match(tmp_path):
    (tmp_path / "sample.png").write_bytes(b"a")
    sample = HumanStyleSample.model_validate(sample_payload(1, "medium.photography", "0" * 64))
    with pytest.raises(ValueError, match="SHA-256"):
        verify_media(sample, Path(tmp_path))
