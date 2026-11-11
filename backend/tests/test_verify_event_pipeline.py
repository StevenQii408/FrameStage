"""Paid evaluation must be explicit and may never overwrite production state."""
from pathlib import Path

import pytest

from evals.verify_event_pipeline import verify


def test_real_cloud_verification_requires_explicit_opt_in(tmp_path):
    with pytest.raises(ValueError, match="allow-paid"):
        verify(tmp_path / "missing.sqlite3", "source", tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_verification_refuses_production_database_as_target(tmp_path):
    path = tmp_path / "validation.sqlite3"
    with pytest.raises(ValueError, match="differ"):
        verify(path, "source", tmp_path, allow_paid=True)
    assert not path.exists()
