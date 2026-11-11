import hashlib
import json

import pytest
from pydantic import ValidationError

from evals.style_direct_gold import GoldSet, run
from test_style_direct import image_bytes


def manifest(data, **changes):
    sample = dict(id="gold-01", source="human", reviewer="human-reviewer", rights="owned",
                  rights_reference="user-owned", media_file="frame.png", sha256=hashlib.sha256(data).hexdigest(),
                  family="animation", features=[dict(dimension="form_language", observable=True,
                                                     accepted_descriptions=["圆润轮廓"])])
    sample.update(changes)
    return dict(schema_version="style-direct-gold.v3", samples=[sample])


@pytest.mark.parametrize("changes", [{"source": "model"}, {"media_file": "../frame.png"}, {"reviewer": ""}])
def test_gold_rejects_generated_labels_and_unsafe_media(changes):
    with pytest.raises(ValidationError):
        GoldSet.model_validate(manifest(image_bytes(), **changes))


def test_verify_all_media_before_any_paid_request(tmp_path, monkeypatch):
    data = image_bytes()
    (tmp_path / "frame.png").write_bytes(data)
    raw = manifest(data)
    raw["samples"].append({**raw["samples"][0], "id": "bad", "sha256": "0" * 64})
    monkeypatch.setattr("evals.style_direct_gold.analyze_file", lambda *a, **k: pytest.fail("must not call model"))
    with pytest.raises(ValueError, match="SHA"):
        run(GoldSet.model_validate(raw), tmp_path, tmp_path / "report", live=True)


def test_mock_gold_output_is_not_claimed_quality_acceptance(tmp_path):
    data = image_bytes()
    (tmp_path / "frame.png").write_bytes(data)
    summary = run(GoldSet.model_validate(manifest(data)), tmp_path, tmp_path / "report")
    assert summary["mode"] == "mock" and summary["model_requests"] == 2
    assert summary["quality_scores"] is None and not summary["human_review_complete"]
    item = json.loads((tmp_path / "report/sample-001/review.json").read_text())
    assert item["prediction"]["profile"]["descriptions"]
    assert item["human_review"]["facts_accurate"] is None
