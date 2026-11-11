"""Generation is independent of source-video analysis and never implies an asset."""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


TERMINAL = frozenset({"blocked", "succeeded", "failed", "cancelled", "interrupted"})
ACTIVE = frozenset({"queued", "submitting", "running", "finalizing"})
VIDEO_DURATION_MIN_SECONDS = 2
VIDEO_DURATION_MAX_SECONDS = 30
VIDEO_DURATION_EXTENSION_MAX_SECONDS = 30
VIDEO_DURATION_DEFAULT_SECONDS = 5
TRANSITIONS = {
    "queued": {"submitting", "cancelled", "failed", "interrupted"},
    "submitting": {"running", "cancelled", "failed", "interrupted"},
    "running": {"running", "finalizing", "cancelled", "failed", "interrupted"},
    "finalizing": {"succeeded", "cancelled", "failed", "interrupted"},
    "blocked": {"cancelled"},
}


class GenerationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class GenerationSettings(GenerationModel):
    model_id: Optional[str] = Field(default=None, min_length=1, max_length=160)
    aspect_ratio: Literal["21:9", "16:9", "9:16", "1:1", "4:3", "3:4"] = "16:9"
    resolution: Literal["480p", "512p", "720p", "1080p", "1536p", "2048p", "2160p"] = "1080p"
    duration_seconds: Optional[int] = Field(default=None, ge=VIDEO_DURATION_MIN_SECONDS, le=VIDEO_DURATION_MAX_SECONDS)


class ReferenceImage(GenerationModel):
    reference_id: str = Field(pattern=r"^ref-[a-f0-9]{12}$")
    asset_id: str = Field(pattern=r"^genasset-[a-f0-9]{64}$")
    mention: str = Field(pattern=r"^图片[1-9][0-9]*$", max_length=20)
    role: Literal["reference", "first_frame"] = "reference"


class GenerationRequest(GenerationModel):
    project_id: Optional[str] = Field(default=None, min_length=1, max_length=160)
    node_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    kind: Literal["image", "video"]
    prompt: str = Field(min_length=1, max_length=10000)
    reference_asset_id: Optional[str] = Field(default=None, max_length=100, pattern=r"^genasset-[a-f0-9]{64}$")
    reference_images: list[ReferenceImage] = Field(default_factory=list, max_length=8)
    settings: GenerationSettings = Field(default_factory=GenerationSettings)

    @model_validator(mode="after")
    def validate_request(self):
        if not self.prompt.strip():
            raise ValueError("Prompt 不能为空")
        if self.kind == "image" and self.settings.duration_seconds is not None:
            raise ValueError("图片任务不能设置视频时长")
        if self.kind == "video" and self.settings.duration_seconds is None:
            self.settings.duration_seconds = VIDEO_DURATION_DEFAULT_SECONDS
        return self


class GenerationCreate(GenerationRequest):
    idempotency_key: str = Field(min_length=8, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")


class ReplaySettings(GenerationSettings):
    """Only reconstruct an already stored request; never authorize new work."""
    duration_seconds: Optional[Union[int, float]] = Field(default=None, gt=0, le=VIDEO_DURATION_EXTENSION_MAX_SECONDS)


class GenerationReplay(GenerationCreate):
    settings: ReplaySettings = Field(default_factory=ReplaySettings)

    @model_validator(mode="after")
    def validate_request(self):
        if not self.prompt.strip():
            raise ValueError("Prompt 不能为空")
        if self.kind == "image" and self.settings.duration_seconds is not None:
            raise ValueError("图片任务不能设置视频时长")
        if self.kind == "video" and self.settings.duration_seconds is None:
            self.settings.duration_seconds = 5.0  # The historical omitted default.
        return self


class RetryRequest(GenerationModel):
    idempotency_key: str = Field(min_length=8, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")


class RemoteStopConfirmation(GenerationModel):
    confirmed_remote_stopped: bool

    @model_validator(mode="after")
    def explicit_confirmation(self):
        if self.confirmed_remote_stopped is not True:
            raise ValueError("必须先在云端确认任务已停止，再显式释放未知并发名额")
        return self


class GenerationError(ValueError):
    def __init__(self, status_code, message, code="generation_error"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
