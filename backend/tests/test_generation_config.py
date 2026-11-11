import json
import os
from pathlib import Path
import stat

from dotenv import dotenv_values
import pytest

from app.generation.config import (DEFAULT_GENERATION_BASE_URL, load_generation_settings,
                                   normalize_generation_base_url)
from app.generation_setup import main, save_generation_config, status


@pytest.fixture(autouse=True)
def clean_generation_env(monkeypatch):
    for name in tuple(os.environ):
        if (name.startswith(("STORYBOARD_GENERATION_", "STORYBOARD_IMAGE_", "STORYBOARD_VIDEO_"))
                or name in {"STORYBOARD_BAILIAN_API_KEY", "STORYBOARD_BAILIAN_BASE_URL", "DASHSCOPE_API_KEY"}):
            monkeypatch.delenv(name)


def test_generation_is_opt_in_and_models_are_exact():
    config = load_generation_settings()
    assert not config.enabled
    assert config.image_model == "qwen-image-3.0-pro"
    assert config.video_model == "wan3.0-video"
    assert config.global_inflight == 16 and config.image_inflight == 10
    assert config.video_inflight == 16 and config.max_pending == 100
    assert config.poll_interval_seconds == 15 and config.image_submit_rpm == 5
    assert config.image_base_url == config.video_base_url == DEFAULT_GENERATION_BASE_URL


def test_saved_bailian_credentials_are_reused_without_exporting(tmp_path, monkeypatch):
    path = tmp_path / "private.env"
    path.write_text("STORYBOARD_BAILIAN_API_KEY='fixture-private-secret'\n"
                    "STORYBOARD_BAILIAN_BASE_URL='https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'\n")
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(path))
    config = load_generation_settings()
    assert config.image_api_key == config.video_api_key == "fixture-private-secret"
    assert config.image_key_source == config.video_key_source == "private-env-file"
    assert config.image_base_url == config.video_base_url == "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1"
    assert "fixture-private-secret" not in repr(config)
    assert "STORYBOARD_BAILIAN_API_KEY" not in os.environ


def test_env_alias_can_override_saved_shared_key(tmp_path, monkeypatch):
    path = tmp_path / "private.env"
    path.write_text("STORYBOARD_BAILIAN_API_KEY='saved-fixture-secret'\n")
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(path))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "environment-fixture-secret")
    assert load_generation_settings().image_api_key == "environment-fixture-secret"
    assert load_generation_settings().video_api_key == "environment-fixture-secret"


def test_per_kind_overrides_are_independent(monkeypatch):
    monkeypatch.setenv("STORYBOARD_BAILIAN_API_KEY", "shared-fixture-secret")
    monkeypatch.setenv("STORYBOARD_IMAGE_API_KEY", "image-fixture-secret")
    monkeypatch.setenv("STORYBOARD_VIDEO_API_KEY", "video-fixture-secret")
    monkeypatch.setenv("STORYBOARD_IMAGE_BASE_URL", "https://image.cn-beijing.maas.aliyuncs.com/api/v1")
    monkeypatch.setenv("STORYBOARD_VIDEO_BASE_URL", "https://video.cn-beijing.maas.aliyuncs.com/api/v1")
    config = load_generation_settings()
    assert config.image_api_key == "image-fixture-secret" and config.video_api_key == "video-fixture-secret"
    assert config.image_base_url != config.video_base_url


def test_specific_saved_key_overrides_generic_environment_key(tmp_path, monkeypatch):
    path = tmp_path / "private.env"
    path.write_text("STORYBOARD_IMAGE_API_KEY='specific-file-fixture-secret'\n")
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(path))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "generic-env-fixture-secret")
    config = load_generation_settings()
    assert config.image_api_key == "specific-file-fixture-secret"
    assert config.image_key_source == "private-env-file"
    assert config.video_api_key == "generic-env-fixture-secret"


def test_disabled_generation_does_not_break_legacy_analysis_base(monkeypatch):
    monkeypatch.setenv("STORYBOARD_BAILIAN_BASE_URL", "https://legacy-analysis.example.test/compatible-mode/v1")
    config = load_generation_settings()
    assert config.image_base_url == config.video_base_url == DEFAULT_GENERATION_BASE_URL
    assert len(config.config_notes) == 1
    assert "legacy-analysis" not in config.config_notes[0]
    monkeypatch.setenv("STORYBOARD_GENERATION_ENABLED", "1")
    with pytest.raises(ValueError):
        load_generation_settings()


def test_explicit_bad_generation_base_is_never_silently_replaced(monkeypatch):
    monkeypatch.setenv("STORYBOARD_IMAGE_BASE_URL", "https://invalid-generation.example.test")
    with pytest.raises(ValueError):
        load_generation_settings()


def test_video_can_reuse_image_override_on_same_endpoint(monkeypatch):
    monkeypatch.setenv("STORYBOARD_IMAGE_API_KEY", "image-only-fixture-secret")
    monkeypatch.setenv("STORYBOARD_IMAGE_BASE_URL", "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1")
    config = load_generation_settings()
    assert config.video_api_key == config.image_api_key
    assert config.video_base_url == config.image_base_url
    monkeypatch.setenv("STORYBOARD_VIDEO_BASE_URL", "https://different.us-east-1.maas.aliyuncs.com/api/v1")
    assert not load_generation_settings().video_api_key


@pytest.mark.parametrize("url", [
    "http://dashscope.aliyuncs.com/api/v1", "https://secret@dashscope.aliyuncs.com/api/v1",
    "https://dashscope.aliyuncs.com/api/v1?api_key=secret", "https://dashscope.aliyuncs.com/api/v1#secret",
    "https://127.0.0.1/api/v1", "https://dashscope.aliyuncs.com.evil.test/api/v1",
    "https://dashscope.aliyuncs.com:8000/api/v1", "https://dashscope.aliyuncs.com/api/v1/tasks/123",
])
def test_non_official_or_credential_bearing_urls_are_rejected(url):
    with pytest.raises(ValueError):
        normalize_generation_base_url(url)


def test_root_and_chat_bases_normalize_to_native_api():
    for path in ("", "/", "/api/v1/", "/compatible-mode/v1/", "/compatible-mode/v1/chat/completions/"):
        assert normalize_generation_base_url("https://dashscope.aliyuncs.com" + path) == DEFAULT_GENERATION_BASE_URL


@pytest.mark.parametrize(("name", "value"), [
    ("STORYBOARD_GENERATION_MAX_INFLIGHT", "33"), ("STORYBOARD_GENERATION_MAX_INFLIGHT", "0"),
    ("STORYBOARD_GENERATION_MAX_INFLIGHT", "1.5"), ("STORYBOARD_GENERATION_POLL_INTERVAL_SECONDS", "nan"),
    ("STORYBOARD_GENERATION_REQUEST_TIMEOUT_SECONDS", "inf"), ("STORYBOARD_GENERATION_ENABLED", "maybe"),
    ("STORYBOARD_IMAGE_MODEL", "qwen-image-2.0"), ("STORYBOARD_VIDEO_MODEL", "wan2.7-t2v"),
])
def test_invalid_budgets_and_silent_model_changes_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        load_generation_settings()


def test_setup_preserves_unrelated_config_and_uses_private_permissions(tmp_path):
    path = tmp_path / "generation.env"
    path.write_text("# private deployment\nSTORYBOARD_BAILIAN_API_KEY='shared-fixture-secret'\n"
                    "STORYBOARD_CLOUD_MODEL='qwen3.8-flash'\nCUSTOM_VALUE='keep-me'\n")
    save_generation_config(path, image_api_key="new-image-fixture-secret",
                           image_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    values = dotenv_values(path)
    assert values["STORYBOARD_BAILIAN_API_KEY"] == "shared-fixture-secret"
    assert values["CUSTOM_VALUE"] == "keep-me" and values["STORYBOARD_CLOUD_MODEL"] == "qwen3.8-flash"
    assert values["STORYBOARD_IMAGE_API_KEY"] == "new-image-fixture-secret"
    assert values["STORYBOARD_GENERATION_ENABLED"] == "0"
    assert values["STORYBOARD_VIDEO_MODEL"] == "wan3.0-video"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "# private deployment" in path.read_text()


def test_setup_preserves_previously_explicit_enable_state(tmp_path):
    path = tmp_path / "generation.env"
    path.write_text("STORYBOARD_GENERATION_ENABLED=1\n")
    save_generation_config(path)
    assert dotenv_values(path)["STORYBOARD_GENERATION_ENABLED"] == "1"


def test_setup_failure_is_atomic_and_never_stores_partial_key(tmp_path):
    path = tmp_path / "generation.env"
    original = "CUSTOM_VALUE='keep-me'\n"
    path.write_text(original)
    with pytest.raises(ValueError):
        save_generation_config(path, image_api_key="valid-fixture-secret", video_api_key="<your-key>")
    assert path.read_text() == original
    assert not list(tmp_path.glob(".generation-config-*"))


def test_status_redacts_keys_and_performs_no_network(monkeypatch, capsys):
    import httpx
    def unexpected(*args, **kwargs):
        raise AssertionError("configuration must not call any API")
    monkeypatch.setattr(httpx.Client, "request", unexpected)
    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected)
    monkeypatch.setenv("STORYBOARD_BAILIAN_API_KEY", "never-print-fixture-secret")
    assert main(["--status"]) == 0
    output = capsys.readouterr().out
    assert "never-print-fixture-secret" not in output
    result = json.loads(output)
    assert result["image"]["api_key_configured"] and result["video"]["api_key_configured"]
    assert not result["enabled"] and result["live_check"] == "not-performed"
    assert "never-print-fixture-secret" not in json.dumps(status())


def test_error_cli_never_echoes_sensitive_bad_values(monkeypatch, capsys):
    monkeypatch.setenv("STORYBOARD_IMAGE_BASE_URL", "https://private-token@dashscope.aliyuncs.com/api/v1")
    assert main(["--status"]) == 2
    assert "private-token" not in capsys.readouterr().out


def test_configure_keeps_existing_shared_key_without_duplicate_secret(monkeypatch, tmp_path, capsys):
    path = tmp_path / "private.env"
    path.write_text("STORYBOARD_BAILIAN_API_KEY='shared-fixture-secret'\n")
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(path))
    monkeypatch.setattr("builtins.input", lambda *_: "")
    monkeypatch.setattr("app.generation_setup.getpass.getpass", lambda *_: "")
    assert main(["--configure"]) == 0
    values = dotenv_values(path)
    assert values["STORYBOARD_BAILIAN_API_KEY"] == "shared-fixture-secret"
    assert "STORYBOARD_IMAGE_API_KEY" not in values and "STORYBOARD_VIDEO_API_KEY" not in values
    assert "shared-fixture-secret" not in capsys.readouterr().out
