"""Real local I/O and isolated state; never contacts a paid provider."""
import io
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.config import load_settings
from app.db import Database
from app.events import EventBus
from app.providers.bailian import BailianVisionProvider
from app.runtime import (ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded,
                         check_execution, current_execution, execution_scope)
from app.services.job_service import JobService
from app.services.video_processor import _run
from app.style_direct.pipeline import StylePipeline
from app.style_direct.service import create_analysis, prepare_image


def test_parent_budget_and_cancel_are_shared_without_leaking_scope():
    cancelled = [False]
    parent = ExecutionContext(max_model_calls=1, cancelled=lambda: cancelled[0])
    with execution_scope(parent):
        child = ExecutionContext(parent=parent)
        with execution_scope(child):
            child.admit_model_call()
            with pytest.raises(ExecutionDeadlineExceeded):
                child.admit_model_call()
            cancelled[0] = True
            with pytest.raises(ExecutionCancelled):
                check_execution()
        assert current_execution() is parent
    assert current_execution() is None and parent.model_calls == 1


@contextmanager
def drip_server():
    started = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            started.set()
            try:
                for byte in payload:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(.02)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", started
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def local_provider(tmp_path, url):
    provider = BailianVisionProvider(replace(load_settings(), cloud_api_key="fake-test-key",
        cloud_base_url=url, data_dir=tmp_path, cloud_trust_env=False))
    # Only this test bypasses public HTTPS endpoint validation. The real
    # AsyncClient, network transport and production _call remain unchanged.
    provider.health = lambda: SimpleNamespace(ready=True)
    return provider


def test_real_drip_http_obeys_absolute_deadline_and_keeps_unknown_cost(tmp_path):
    with drip_server() as (url, _):
        provider = local_provider(tmp_path, url)
        started = time.monotonic()
        try:
            with pytest.raises(ExecutionDeadlineExceeded):
                provider._call("JSON", [], "test.deadline", timeout_seconds=.15, max_attempts=1)
            assert time.monotonic() - started < .8
            assert provider.usage[0]["status"] == "timed_out"
            assert provider.usage[0]["estimated_list_price_cny"] is None
            assert provider.usage[0]["billing_status"] == "unknown"
        finally:
            provider.unload()


def test_real_http_can_be_cancelled_mid_response(tmp_path):
    with drip_server() as (url, sent):
        cancelled = threading.Event()
        provider = local_provider(tmp_path, url)
        def cancel():
            assert sent.wait(2)
            cancelled.set()
        thread = threading.Thread(target=cancel)
        thread.start()
        try:
            with execution_scope(ExecutionContext(cancelled=cancelled.is_set)):
                with pytest.raises(ExecutionCancelled):
                    provider._call("JSON", [], "test.cancel", timeout_seconds=3, max_attempts=1)
            assert len(provider.usage) == 1 and provider.usage[0]["status"] == "cancelled"
        finally:
            thread.join(timeout=2)
            provider.unload()


def test_request_journal_is_written_before_send_and_after_result(tmp_path):
    journal = []
    def handler(request):
        assert journal[-1][0] == "runtime.request_started"
        return httpx.Response(200, json={"id": "request-audit", "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})
    provider = BailianVisionProvider(replace(load_settings(), cloud_api_key="secret-not-in-journal",
        cloud_base_url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"), httpx.MockTransport(handler))
    try:
        with execution_scope(ExecutionContext(on_event=lambda name, data: journal.append((name, data)))):
            assert provider._call("private script", [], "test.journal", max_attempts=1) == "{}"
        assert [item[0] for item in journal] == ["runtime.request_started", "runtime.request_finished"]
        assert journal[0][1]["attempt_id"] == journal[1][1]["attempt_id"]
        assert journal[1][1]["total_tokens"] == 7
        assert "secret-not-in-journal" not in json.dumps(journal)
        assert "private script" not in json.dumps(journal)
    finally:
        provider.unload()


def test_script_cancel_during_first_response_never_sends_correction():
    from app.script_director.engine import director_windows, plan_window
    from app.script_director.document import parse_script_document
    cancelled, calls = [False], []
    class Provider:
        def health(self):
            return SimpleNamespace(ready=True, provider="bailian")
        def plan_script_director(self, *args, **kwargs):
            calls.append(1)
            cancelled[0] = True
            return "invalid-json"
    document = parse_script_document("女人拿起杯子。")
    with pytest.raises(ExecutionCancelled):
        plan_window(Provider(), document, director_windows(document)[0], cancelled=lambda: cancelled[0])
    assert len(calls) == 1


def test_style_cancel_does_not_start_interpretation(tmp_path):
    cancelled, calls = [False], []
    class Provider:
        def analyze_style(self, *args, **kwargs):
            calls.append(1)
            cancelled[0] = True
            return '{"claims":[],"gaps":[],"uncertainties":["未知"]}'
    output = io.BytesIO()
    Image.new("RGB", (80, 80)).save(output, format="PNG")
    asset = prepare_image(output.getvalue(), tmp_path / "input", "I1")
    pipeline = StylePipeline(Provider(), [asset], tmp_path / "crops", cancelled=lambda: cancelled[0])
    with pytest.raises(ExecutionCancelled):
        pipeline.run()
    assert len(calls) == 1


def test_local_process_timeout_and_cancel_reap_children(tmp_path):
    marker = tmp_path / "late-child.txt"
    # Child inherits the group. If it survives cancellation it writes later.
    child_code = f"import time; time.sleep(.8); open({str(marker)!r},'w').write('leaked')"
    code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child_code!r}]); time.sleep(5)"
    with pytest.raises(subprocess.TimeoutExpired):
        _run([sys.executable, "-c", code], timeout=.15)
    time.sleep(.9)
    assert not marker.exists()
    cancelled = threading.Event()
    timer = threading.Timer(.1, cancelled.set)
    timer.start()
    try:
        with execution_scope(ExecutionContext(cancelled=cancelled.is_set)):
            with pytest.raises(ExecutionCancelled):
                _run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=3)
    finally:
        timer.cancel()


def test_cached_style_does_not_leave_unreferenced_directory(tmp_path):
    settings = replace(load_settings(), provider="mock", data_dir=tmp_path)
    database = Database(tmp_path / "test.sqlite3")
    database.init()
    service = JobService(settings, database, EventBus())
    image = io.BytesIO()
    Image.new("RGB", (64, 64), (12, 34, 56)).save(image, format="PNG")
    try:
        first = create_analysis(service, [(image.getvalue(), None)], synchronous=True)
        second = create_analysis(service, [(image.getvalue(), None)], synchronous=True)
        assert second["cached"] and second["analysis_id"] == first["analysis_id"]
        assert len(list((tmp_path / "style").iterdir())) == 1
    finally:
        service.executor.shutdown(wait=True)
        service.provider.unload()


def test_cancelled_style_preserves_unknown_usage_and_unrelated_parent_usage(tmp_path, monkeypatch):
    settings = replace(load_settings(), provider="mock", data_dir=tmp_path)
    database = Database(tmp_path / "test.sqlite3")
    database.init()
    service = JobService(settings, database, EventBus())
    cancelled = [False]
    service._is_cancelled = lambda identity: cancelled[0]
    parent_usage = {"phase": "shot.static", "estimated_list_price_cny": .01}
    class Provider:
        def __init__(self):
            self.usage = [parent_usage]
        def health(self):
            return SimpleNamespace(model="qwen3.8-flash")
        def analyze_style(self, *args, **kwargs):
            self.usage.append({"phase": "style.direct.evidence", "status": "cancelled",
                               "usage_available": False, "estimated_list_price_cny": None})
            cancelled[0] = True
            raise ExecutionCancelled("cancelled")
        def drain_usage(self):
            usage, self.usage = self.usage, []
            return usage
    provider = Provider()
    service.provider = provider
    image = io.BytesIO()
    Image.new("RGB", (64, 64)).save(image, format="PNG")
    try:
        result = create_analysis(service, [(image.getvalue(), {"origin_job_id": "parent"})], synchronous=True)
        assert result["status"] == "cancelled"
        assert len(result["result"]["usage"]) == 1
        assert result["result"]["estimated_list_price_cny"] is None
        assert result["result"]["cost_estimate_complete"] is False
        assert provider.usage == [parent_usage]
    finally:
        service.executor.shutdown(wait=True)
