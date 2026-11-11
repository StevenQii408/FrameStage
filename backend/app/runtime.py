"""Small shared execution boundary; no model or domain policy lives here."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import time
from typing import Callable, Optional


class ExecutionCancelled(RuntimeError):
    """Caller intent: do not start more work or publish a late result."""


class ExecutionDeadlineExceeded(TimeoutError):
    """The absolute wall-clock budget expired, including network reads."""


@dataclass
class ExecutionContext:
    run_id: str = ""
    deadline: Optional[float] = None
    cancelled: Callable[[], bool] = field(default=lambda: False, repr=False)
    max_model_calls: Optional[int] = None
    model_calls: int = 0
    parent: Optional["ExecutionContext"] = field(default=None, repr=False)
    on_event: Optional[Callable[[str, dict], None]] = field(default=None, repr=False)

    def check(self):
        if self.parent is not None:
            self.parent.check()
        if self.cancelled():
            raise ExecutionCancelled("任务已取消，不再执行请求或发布结果")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ExecutionDeadlineExceeded("任务总时间预算耗尽")

    def admit_model_call(self):
        self.check()
        if self.max_model_calls is not None and self.model_calls >= self.max_model_calls:
            raise ExecutionDeadlineExceeded("任务模型请求总预算耗尽")
        if self.parent is not None:
            self.parent.admit_model_call()
        self.model_calls += 1

    def record(self, name, payload):
        if self.parent is not None:
            self.parent.record(name, payload)
        elif self.on_event is not None:
            self.on_event(name, {"runtime_version": "execution.v1", "run_id": self.run_id, **payload})


_current: ContextVar[Optional[ExecutionContext]] = ContextVar("storyboard_execution", default=None)


def current_execution():
    return _current.get()


def check_execution():
    current = current_execution()
    if current is not None:
        current.check()


def execution_deadline(timeout: float):
    current = current_execution()
    local = time.monotonic() + max(.001, timeout)
    return min(local, current.deadline) if current and current.deadline is not None else local


@contextmanager
def execution_scope(context: ExecutionContext):
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)
