"""Real ASGI regressions: CORS read restrictions must also guard side effects."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import load_local_security
from app.main import app
from app.security import SESSION_HEADER


ORIGIN = "http://127.0.0.1:5174"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOARD_LOCAL_AUTH", "1")
    monkeypatch.delenv("STORYBOARD_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("STORYBOARD_LOCAL_API_TOKEN", raising=False)
    monkeypatch.setenv("STORYBOARD_PROVIDER", "mock")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as value:
        yield value


def bootstrap(client):
    response = client.get("/api/session", headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.json()["header"] == SESSION_HEADER
    return response.json()["token"]


@pytest.mark.parametrize("origin", ["https://audit-untrusted.example", "null", "http://127.0.0.1:5999"])
def test_cross_origin_simple_form_cannot_create_or_bill_jobs(client, origin):
    before = client.app.state.db.fetchone("SELECT COUNT(*) AS n FROM jobs")["n"]
    response = client.post("/api/jobs", headers={"Origin": origin},
                           data={"input_type": "script", "script_text": "女人拿起杯子。"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert client.app.state.db.fetchone("SELECT COUNT(*) AS n FROM jobs")["n"] == before


def test_token_is_required_for_trusted_origin_and_cli_writes(client):
    for headers in ({}, {"Origin": ORIGIN}):
        response = client.post("/api/projects", headers=headers, json={"name": "blocked"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "session_token_missing"
    token = bootstrap(client)
    response = client.post("/api/projects", headers={"Origin": ORIGIN, SESSION_HEADER: token}, json={"name": "trusted"})
    assert response.status_code == 201, response.text
    # A CLI already possessing the secret is authenticated, without an Origin.
    assert client.post("/api/projects", headers={SESSION_HEADER: token}, json={"name": "cli"}).status_code == 201


def test_authenticated_browser_can_submit_form_job(client, monkeypatch):
    submissions = []
    def submit(text, **kwargs):
        submissions.append(text)
        return "job-authenticated-example"
    monkeypatch.setattr(client.app.state.job_service, "create_script_job", submit)
    response = client.post("/api/jobs", headers={"Origin": ORIGIN, SESSION_HEADER: bootstrap(client)},
                           data={"input_type": "script", "script_text": "女人拿起杯子。"})
    assert response.status_code == 202, response.text
    assert submissions == ["女人拿起杯子。"]


def test_invalid_token_can_be_refreshed_but_does_not_mutate(client):
    stale = client.post("/api/projects", headers={"Origin": ORIGIN, SESSION_HEADER: "expired"}, json={"name": "blocked"})
    assert stale.status_code == 403
    assert stale.json()["error"]["code"] == "session_token_invalid"
    assert stale.headers["access-control-allow-origin"] == ORIGIN
    current = bootstrap(client)
    assert client.post("/api/projects", headers={"Origin": ORIGIN, SESSION_HEADER: current}, json={"name": "ok"}).status_code == 201


def test_valid_token_cannot_override_untrusted_origin(client):
    token = bootstrap(client)
    response = client.post("/api/projects", headers={"Origin": "https://audit-untrusted.example", SESSION_HEADER: token}, json={"name": "blocked"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"


def test_bootstrap_is_not_a_plain_navigation_or_unauthenticated_cli_endpoint(client):
    for headers in ({}, {"Origin": "null"}, {"Origin": "https://audit-untrusted.example"},
                    {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate"}):
        response = client.get("/api/session", headers=headers)
        assert response.status_code == 403
        assert "token" not in response.json()
    assert client.get("/api/session", headers={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}).status_code == 200
    assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("host", ["audit-untrusted.example", "localhost.evil.example", "localhost:invalid", "localhost@evil.example", "localhost/path", "[::1]:99999"])
def test_untrusted_or_malformed_hosts_are_rejected(client, host):
    response = client.get("/api/health", headers={"Host": host})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "host_not_allowed"


def test_ipv6_and_same_origin_bootstrap_and_preflight(client):
    assert client.get("/api/health", headers={"Host": "[::1]:8000"}).status_code == 200
    response = client.get("/api/session", headers={"Host": "localhost:8000", "Origin": "http://localhost:8000"})
    assert response.status_code == 200
    response = client.options("/api/projects", headers={"Origin": ORIGIN,
        "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,x-frameforge-session,x-project-id"})
    assert response.status_code == 200


def test_no_origin_cross_site_request_is_rejected_even_with_token(client):
    response = client.post("/api/projects", headers={"Sec-Fetch-Site": "cross-site", SESSION_HEADER: bootstrap(client)}, json={"name": "blocked"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"


def test_token_is_process_scoped_and_is_not_health_or_configuration_output(client):
    token = bootstrap(client)
    assert token not in client.get("/api/health").text
    assert "token=" not in repr(client.app.state.local_session)
    from app.security import LocalSession
    assert LocalSession.create().token != token


def test_explicit_cli_token_and_configuration_validation(monkeypatch):
    monkeypatch.setenv("STORYBOARD_LOCAL_API_TOKEN", "example-test-token-" + "x" * 32)
    assert load_local_security().api_token.startswith("example-test-token-")
    assert "example-test-token" not in repr(load_local_security())
    monkeypatch.setenv("STORYBOARD_LOCAL_API_TOKEN", "too-short")
    with pytest.raises(ValueError, match="32"):
        load_local_security()
    monkeypatch.delenv("STORYBOARD_LOCAL_API_TOKEN")
    monkeypatch.setenv("STORYBOARD_ALLOWED_HOSTS", "*")
    with pytest.raises(ValueError, match="主机名"):
        load_local_security()
    monkeypatch.setenv("STORYBOARD_ALLOWED_HOSTS", "localhost:8000")
    with pytest.raises(ValueError, match="主机名"):
        load_local_security()
    monkeypatch.delenv("STORYBOARD_LOCAL_AUTH")
    monkeypatch.delenv("STORYBOARD_ALLOWED_HOSTS")
    assert load_local_security().enabled is True


def test_analysis_sse_drains_tail_committed_between_event_and_status_reads(monkeypatch):
    """The first connection delivers its terminal fact, not just a reconnect."""
    from fastapi import FastAPI
    from app.api.jobs import router
    calls = []
    final = {"sequence": 7, "type": "job.completed", "payload": {"job_id": "job-race"}}
    def events_since(job_id, cursor):
        calls.append(cursor)
        return [] if len(calls) == 1 else [final]
    isolated = FastAPI()
    isolated.state.job_service = SimpleNamespace(status=lambda _: {"status": "completed"},
                                                db=SimpleNamespace(events_since=events_since))
    isolated.include_router(router, prefix="/api")
    with TestClient(isolated) as connection:
        response = connection.get("/api/jobs/job-race/events")
        assert response.status_code == 200
        assert "event: job.completed" in response.text
        assert json.dumps(final) in response.text
        assert calls == [0, 0]


def test_analysis_sse_rejects_cursor_overflow():
    from fastapi import FastAPI
    from app.api.jobs import router
    isolated = FastAPI()
    isolated.state.job_service = SimpleNamespace(status=lambda _: {"status": "completed"})
    isolated.include_router(router, prefix="/api")
    with TestClient(isolated) as connection:
        assert connection.get("/api/jobs/job-race/events", params={"after": 2**63}).status_code == 422
