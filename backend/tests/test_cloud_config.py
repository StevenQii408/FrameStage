import json
import os
import stat
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cloud_setup import diagnostics, save_cloud_config
from app.config import load_settings
from app.main import app


CLOUD_VARS = ("STORYBOARD_PROVIDER", "STORYBOARD_CLOUD_MODEL", "STORYBOARD_BAILIAN_API_KEY",
              "DASHSCOPE_API_KEY", "STORYBOARD_BAILIAN_BASE_URL")


def clear(monkeypatch):
    for name in CLOUD_VARS:
        monkeypatch.delenv(name, raising=False)


def test_cloud_is_default_and_stale_mlx_cannot_start_local(monkeypatch):
    clear(monkeypatch)
    assert load_settings().provider == "bailian"
    assert load_settings().cloud_model == "qwen3.8-flash"
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mlx")
    settings = load_settings()
    assert settings.provider == "bailian" and settings.config_notes
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    assert load_settings().provider == "mock"


def test_cloud_network_settings_default_direct_and_can_opt_into_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(tmp_path / "absent.env"))
    for name in ("STORYBOARD_CLOUD_TRUST_ENV", "STORYBOARD_CLOUD_CONNECT_TIMEOUT_SECONDS",
                 "STORYBOARD_CLOUD_READ_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert not settings.cloud_trust_env
    assert settings.cloud_connect_timeout_seconds == 8
    assert settings.cloud_read_timeout_seconds == 75
    monkeypatch.setenv("STORYBOARD_CLOUD_TRUST_ENV", "1")
    monkeypatch.setenv("STORYBOARD_CLOUD_CONNECT_TIMEOUT_SECONDS", "4")
    assert load_settings().cloud_trust_env
    assert load_settings().cloud_connect_timeout_seconds == 4


def test_generic_memory_defaults_do_not_reserve_a_local_model(monkeypatch):
    monkeypatch.delenv("STORYBOARD_MIN_AVAILABLE_MEMORY_GB", raising=False)
    monkeypatch.delenv("STORYBOARD_TARGET_AVAILABLE_MEMORY_GB", raising=False)
    settings = load_settings()
    assert settings.min_available_memory_gb == 1
    assert settings.target_available_memory_gb == 2


def test_cloud_service_does_not_have_local_provider_entry(tmp_path, monkeypatch):
    from app.db import Database
    from app.events import EventBus
    from app.services.job_service import JobService
    clear(monkeypatch)
    database = Database(tmp_path / "jobs.sqlite3")
    database.init()
    with pytest.raises(ValueError, match="未知 Provider"):
        JobService(replace(load_settings(), provider="mlx"), database, EventBus())


def test_private_file_survives_new_shell_and_is_not_exported(tmp_path, monkeypatch):
    clear(monkeypatch)
    path = tmp_path / "cloud.env"
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(path))
    save_cloud_config(path, "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions/",
                      "fixture-private-key")
    settings = load_settings()
    assert settings.provider == "bailian"
    assert settings.cloud_api_key == "fixture-private-key"
    assert settings.cloud_key_source == "private-env-file"
    assert settings.cloud_base_url.endswith("/compatible-mode/v1")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "STORYBOARD_BAILIAN_API_KEY" not in os.environ
    assert "fixture-private-key" not in repr(settings)


def test_environment_overrides_private_file_without_stale_key(tmp_path, monkeypatch):
    clear(monkeypatch)
    path = tmp_path / "cloud.env"
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(path))
    save_cloud_config(path, "https://first.example/compatible-mode/v1", "saved-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "new-env-key")
    assert load_settings().cloud_api_key == "new-env-key"
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    assert load_settings().cloud_api_key == "saved-key"
    path.unlink()
    assert load_settings().cloud_api_key == ""


def test_dry_diagnostics_do_not_request_or_print_key(tmp_path, monkeypatch):
    clear(monkeypatch)
    settings = replace(load_settings(), cloud_api_key="secret-test-key",
                       cloud_base_url="https://example.com/compatible-mode/v1")
    def fail(request):
        pytest.fail("dry diagnostics must not request cloud")
    result = diagnostics(settings, transport=httpx.MockTransport(fail))
    assert result["configuration_ready"] and result["authenticated"] is None
    assert "secret-test-key" not in json.dumps(result)


def test_live_check_reports_auth_failure_and_redacts_key(monkeypatch):
    clear(monkeypatch)
    settings = replace(load_settings(), cloud_api_key="fixture-secret",
                       cloud_base_url="https://example.com/compatible-mode/v1")
    result = diagnostics(settings, live=True, transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error": {"message": "Unauthorized"}})))
    assert result["authenticated"] is False and "401" in result["detail"]
    assert result["usage"][0]["status"] == "failed"
    assert "fixture-secret" not in json.dumps(result)


def test_missing_key_prevents_job_and_health_does_not_expose_secrets(tmp_path, monkeypatch):
    clear(monkeypatch)
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["provider"]["provider"] == "bailian"
        assert not health["provider"]["ready"]
        assert health["configuration"]["api_key_source"] == "none"
        response = client.post("/api/jobs", data={"input_type": "script", "script_text": "男孩拿起杯子。"})
        assert response.status_code == 503
        assert "app.cloud_setup" in response.json()["detail"]
        assert client.app.state.db.fetchone("SELECT COUNT(*) AS n FROM jobs")["n"] == 0


def test_cloud_launcher_ignores_mlx_and_never_launches_without_key(monkeypatch):
    from app.start_cloud import main
    clear(monkeypatch)
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mlx")
    assert main([]) == 2
    assert os.environ["STORYBOARD_PROVIDER"] == "mlx"  # caller's env is restored on failure


def test_invalid_config_does_not_create_private_file(tmp_path, monkeypatch):
    clear(monkeypatch)
    path = tmp_path / "invalid.env"
    with pytest.raises(ValueError):
        save_cloud_config(path, "https://你的工作区ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "fixture")
    assert not path.exists()


def test_live_check_success_uses_flash_without_local_inference_dependency(monkeypatch):
    clear(monkeypatch)
    settings = replace(load_settings(), cloud_api_key="fixture-secret",
                       cloud_base_url="https://example.com/compatible-mode/v1")
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}],
                                        "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}})
    result = diagnostics(settings, live=True, transport=httpx.MockTransport(handler))
    assert result["authenticated"] is True and result["live_check"] == "success"
    assert requests[0]["model"] == "qwen3.8-flash"


def test_setup_preserves_unrelated_private_settings(tmp_path, monkeypatch):
    clear(monkeypatch)
    monkeypatch.delenv("STORYBOARD_DATA_DIR", raising=False)
    monkeypatch.delenv("STORYBOARD_VLM_TIMEOUT_SECONDS", raising=False)
    path = tmp_path / "cloud.env"
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(path))
    path.write_text("STORYBOARD_DATA_DIR=./data-mlx\nSTORYBOARD_VLM_TIMEOUT_SECONDS=300\n", encoding="utf-8")
    save_cloud_config(path, "https://example.com/compatible-mode/v1", "fixture-key")
    settings = load_settings()
    assert settings.data_dir.name == "data-mlx" and settings.vlm_timeout_seconds == 300


def test_cloud_entry_prefers_saved_credentials_over_stale_shell(tmp_path, monkeypatch):
    from app.start_cloud import cloud_environment
    clear(monkeypatch)
    path = tmp_path / "cloud.env"
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(path))
    save_cloud_config(path, "https://saved.example/compatible-mode/v1", "saved-key")
    monkeypatch.setenv("STORYBOARD_BAILIAN_API_KEY", "stale-key")
    monkeypatch.setenv("STORYBOARD_BAILIAN_BASE_URL", "https://old.example/compatible-mode/v1")
    with cloud_environment():
        settings = load_settings()
        assert settings.cloud_api_key == "saved-key"
        assert settings.cloud_base_url == "https://saved.example/compatible-mode/v1"
        assert settings.cloud_key_source == "private-env-file"
    assert os.environ["STORYBOARD_BAILIAN_API_KEY"] == "stale-key"
