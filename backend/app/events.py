import asyncio
import threading


class EventBus:
    def __init__(self):
        self._waiters = {}
        self._guard = threading.Lock()

    def notify(self, job_id: str) -> None:
        with self._guard:
            waiters = tuple(self._waiters.get(job_id, ()))
        for loop, future in waiters:
            try:
                loop.call_soon_threadsafe(self._wake, future)
            except RuntimeError:
                pass  # A disconnect may already have closed the owning loop.

    @staticmethod
    def _wake(future):
        if not future.done():
            future.set_result(None)

    async def wait(self, job_id: str, timeout: float = 2.0) -> None:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        waiter = (loop, future)
        with self._guard:
            self._waiters.setdefault(job_id, set()).add(waiter)
        try:
            await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            with self._guard:
                remaining = self._waiters.get(job_id)
                if remaining is not None:
                    remaining.discard(waiter)
                    if not remaining:
                        self._waiters.pop(job_id, None)
