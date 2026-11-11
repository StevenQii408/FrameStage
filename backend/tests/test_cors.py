"""Actual browser-origin regression: Vite's old fallback port was rejected."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.config import load_cors_origins
from app.main import app


def test_workspace_and_legacy_origins_support_project_request_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        for origin in ("http://127.0.0.1:5174", "http://localhost:5174", "http://127.0.0.1:5173", "http://localhost:5173"):
            response = client.options("/api/projects", headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-project-id",
            })
            assert response.status_code == 200, response.text
            assert response.headers["access-control-allow-origin"] == origin
            actual = client.get("/api/projects", headers={"Origin": origin})
            assert actual.status_code == 200
            assert actual.headers["access-control-allow-origin"] == origin
        assert client.options("/api/projects", headers={
            "Origin": "http://127.0.0.1:5999", "Access-Control-Request-Method": "GET"
        }).status_code == 400


def test_private_cors_allowlist_is_loaded_and_environment_takes_precedence(tmp_path, monkeypatch):
    path = tmp_path / "backend.env"
    path.write_text("STORYBOARD_CORS_ORIGINS=https://workspace.example/,https://workspace.example\n", encoding="utf-8")
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(path))
    monkeypatch.delenv("STORYBOARD_CORS_ORIGINS", raising=False)
    assert load_cors_origins() == ("https://workspace.example",)
    monkeypatch.setenv("STORYBOARD_CORS_ORIGINS", "http://127.0.0.1:5174")
    assert load_cors_origins() == ("http://127.0.0.1:5174",)


def test_custom_allowlist_rejects_other_origins(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("STORYBOARD_CORS_ORIGINS", "https://workspace.example")
    isolated = FastAPI()
    isolated.add_middleware(CORSMiddleware, allow_origins=list(load_cors_origins()), allow_methods=["*"], allow_headers=["*"])
    with TestClient(isolated) as client:
        assert client.options("/api/projects", headers={"Origin": "http://127.0.0.1:5174", "Access-Control-Request-Method": "GET"}).status_code == 400
        accepted = client.options("/api/projects", headers={"Origin": "https://workspace.example", "Access-Control-Request-Method": "GET"})
        assert accepted.status_code == 200
