from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from app.config import load_settings
from app.db import Database
from app.events import EventBus
from app.projects.models import ProjectError
from app.services.job_service import JobService


def test_concurrent_submissions_cannot_overfill_the_single_worker_queue(tmp_path, monkeypatch):
    settings = replace(load_settings(), provider="mock", data_dir=tmp_path, max_pending_jobs=2)
    db = Database(tmp_path / "jobs.sqlite")
    db.init()
    service = JobService(settings, db, EventBus())
    # Do not run inference: keep accepted jobs queued while browsers race.
    monkeypatch.setattr(service.executor, "submit", lambda *args, **kwargs: None)
    def submit(number):
        try:
            return service.create_script_job(f"人物拿起杯子{number}。")
        except ProjectError as error:
            assert error.status_code == 429
            return None
    try:
        with ThreadPoolExecutor(max_workers=4) as callers:
            results = list(callers.map(submit, range(6)))
        accepted = [identity for identity in results if identity]
        assert len(accepted) == 2
        assert db.fetchone("SELECT COUNT(*) AS total FROM jobs")["total"] == 2
        assert len(list((tmp_path / "jobs").iterdir())) == 2
        service.cancel(accepted[0])
        assert service.create_script_job("人物放下杯子。")
    finally:
        service.executor.shutdown(wait=True)
        service.provider.unload()
