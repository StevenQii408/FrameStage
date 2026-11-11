"""Tests never inherit credentials from the developer's private .env."""
import pytest


@pytest.fixture(autouse=True)
def isolated_private_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(tmp_path / "test-private.env"))
    # Existing unit/integration tests isolate business rules from local browser
    # authentication. Security regressions explicitly enable it and bootstrap.
    monkeypatch.setenv("STORYBOARD_LOCAL_AUTH", "0")
    monkeypatch.setenv("STORYBOARD_ALLOWED_HOSTS", "127.0.0.1,localhost,::1,testserver")
    # Mock workflows never load a model; memory pressure is injected explicitly
    # by dedicated tests, not inherited from the machine running the suite.
    monkeypatch.setenv("STORYBOARD_MIN_AVAILABLE_MEMORY_GB", "0")
    monkeypatch.setenv("STORYBOARD_TARGET_AVAILABLE_MEMORY_GB", "0")
