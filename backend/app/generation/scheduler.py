"""A bounded scheduler dispatches network steps; cloud waiting owns no worker."""
from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor


class GenerationScheduler:
    def __init__(self, service, config, *, dispatch_interval=0.2):
        self.service, self.config = service, config
        self.owner = f"{os.getpid()}:{uuid.uuid4().hex}"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._io = ThreadPoolExecutor(max_workers=config.io_workers, thread_name_prefix="generation-http")
        self._delivery = ThreadPoolExecutor(max_workers=config.download_workers, thread_name_prefix="generation-assets")
        self._futures = {}
        self._dispatch_interval = dispatch_interval
        self._thread = threading.Thread(target=self._loop, name="generation-scheduler", daemon=True)
        self._thread.start()

    def wake(self):
        self._wake.set()

    def _loop(self):
        next_http = 0.0
        next_recovery = time.monotonic() + 30
        while not self._stop.is_set():
            try:
                if time.monotonic() >= next_recovery:
                    self.service.queue.recover_dead_leases()
                    self.service._recover()
                    next_recovery = time.monotonic() + 30
                for future in list(self._futures):
                    if future.done():
                        # _step translates public failures; an unexpected local
                        # scheduling error remains retryable at its checkpoint.
                        try:
                            future.result()
                        except Exception:
                            pass
                        self._futures.pop(future, None)
                deliveries = [kind for final, kind in self._futures.values() if final]
                io_count = sum(not final for final, _ in self._futures.values())
                if len(deliveries) < self.config.download_workers:
                    excluded = tuple(kind for kind in ("image", "video")
                        if deliveries.count(kind) >= getattr(self.config, kind + "_finalize_workers"))
                    entry = self.service.queue.claim(self.owner, self.config, finalizing=True, excluded_kinds=excluded)
                    if entry:
                        try:
                            future = self._delivery.submit(self.service._step, entry, self.owner)
                        except Exception:
                            self.service.queue.release_owner_for_job(entry["job_id"], self.owner)
                            raise
                        self._futures[future] = (True, entry["job"]["kind"])
                if io_count < self.config.io_workers and time.monotonic() >= next_http:
                    entry = self.service.queue.claim(self.owner, self.config)
                    reconcile = False
                    if entry is None:
                        entry = self.service.queue.claim_reconciliation(self.owner, self.config)
                        reconcile = entry is not None
                    if entry:
                        try:
                            action = self.service._reconcile_remote if reconcile else self.service._step
                            future = self._io.submit(action, entry, self.owner)
                        except Exception:
                            if reconcile:
                                self.service.queue.defer_reconciliation(entry["job_id"], self.owner, 1)
                            else:
                                self.service.queue.release_owner_for_job(entry["job_id"], self.owner)
                            raise
                        self._futures[future] = (False, entry["job"]["kind"])
                        next_http = time.monotonic() + self._dispatch_interval
            except Exception:
                # A busy SQLite file must not kill the scheduling thread.
                pass
            if self._futures:
                delay = 0.1
            else:
                due = self.service.queue.next_due()
                delay = min(30, max(1, due - time.time())) if due is not None else 30
            self._wake.wait(timeout=delay)
            self._wake.clear()

    def close(self):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2)
        # An already sent submit is allowed to save its remote ID. Its finally
        # block releases the lease, so restart never blindly repeats a POST.
        self._io.shutdown(wait=False, cancel_futures=False)
        self._delivery.shutdown(wait=False, cancel_futures=False)
