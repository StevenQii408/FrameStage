from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class ProjectModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ProjectCreate(ProjectModel):
    name: str = Field(default="未命名项目", min_length=1, max_length=100)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=160)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("项目名称不能为空")
        return value


class CanvasSave(ProjectModel):
    expected_revision: int = Field(ge=0)
    state: Dict[str, Any]


class LegacyImport(ProjectModel):
    job_ids: List[str] = Field(default_factory=list, max_length=120)
    generation_ids: List[str] = Field(default_factory=list, max_length=240)
    asset_ids: List[str] = Field(default_factory=list, max_length=500)
    state: Optional[Dict[str, Any]] = None
    expected_revision: Optional[int] = Field(default=None, ge=0)


class AssetBinding(ProjectModel):
    asset_ids: List[str] = Field(max_length=500)
