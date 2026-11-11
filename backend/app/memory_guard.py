import threading
from contextlib import contextmanager
from typing import Dict, Optional


class MemoryPressureError(RuntimeError):
    pass


class MemoryGuard:
    def __init__(self, minimum_available_gb: float = 1, target_available_gb: float = 2):
        self.minimum_available_bytes = int(minimum_available_gb * 1024 ** 3)
        self.target_available_bytes = int(target_available_gb * 1024 ** 3)
        self._lock = threading.Lock()
        self.last_snapshot: Dict[str, Optional[float]] = {}
        self.last_inference_before: Dict[str, Optional[float]] = {}
        self.last_inference_after: Dict[str, Optional[float]] = {}

    def snapshot(self) -> Dict[str, Optional[float]]:
        try:
            import psutil
            vm = psutil.virtual_memory()
            snapshot = {"available_gb": vm.available / 1024 ** 3, "used_gb": vm.used / 1024 ** 3, "percent": float(vm.percent)}
        except Exception:
            snapshot = {"available_gb": None, "used_gb": None, "percent": None}
        self.last_snapshot = snapshot
        return snapshot

    def can_start_inference(self) -> bool:
        available = self.snapshot().get("available_gb")
        return available is None or available >= self.minimum_available_bytes / 1024 ** 3

    @contextmanager
    def inference_slot(self):
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            raise MemoryPressureError("另一个镜头正在推理，当前任务被限制为串行执行")
        try:
            before = self.snapshot()
            self.last_inference_before = dict(before)
            available = before.get("available_gb")
            if available is not None and available < self.minimum_available_bytes / 1024 ** 3:
                raise MemoryPressureError("系统可用内存低于安全阈值，已暂停新的 VLM 推理")
            yield
        finally:
            self._lock.release()
            self.last_inference_after = dict(self.snapshot())
