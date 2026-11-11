"""Integration seam only: no real generation service is contacted here."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Protocol


CLOUD_CANCELLED_MESSAGE = "云端已取消"


@dataclass(frozen=True)
class GeneratedAsset:
    """Provider adapters must download/verify bytes, never expose remote URLs."""
    data: bytes = b""
    mime_type: str = ""
    # Real adapter implementations must validate playable video before delivery.
    width: int = 0
    height: int = 0
    duration_seconds: Optional[float] = None
    path: Optional[Path] = None
    provider_spec: Optional[dict] = None


@dataclass(frozen=True)
class ProviderResult:
    state: str
    progress: Optional[float] = None
    assets: List[GeneratedAsset] = field(default_factory=list)
    error_code: str = "provider_failed"
    message: str = "生成服务未能完成任务"
    retryable: bool = False
    # Private delivery checkpoint; presigned URLs must never enter public SSE.
    remote_assets: List[dict] = field(default_factory=list)


class GenerationProvider(Protocol):
    name: str
    configured: bool

    def capabilities(self) -> dict: ...
    def submit(self, request: dict, *, idempotency_key: str) -> str: ...
    def poll(self, task_id: str) -> ProviderResult: ...
    def cancel(self, task_id: str) -> None: ...


class DisabledGenerationProvider:
    name = "disabled"
    configured = False

    def capabilities(self):
        return {"configured": False, "provider": self.name,
                "reason": "图片与视频生成 API 尚未接入；可保存任务，当前不会开始生成或产生费用。",
                "image": {"available": False, "models": []}, "video": {"available": False, "models": []},
                "progress_mode": "unavailable", "billing_mode": "not_submitted"}

    def submit(self, request, *, idempotency_key):
        raise RuntimeError("生成 Provider 尚未接入")

    def poll(self, task_id):
        raise RuntimeError("生成 Provider 尚未接入")

    def cancel(self, task_id):
        return None
