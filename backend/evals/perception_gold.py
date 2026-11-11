"""Human-only visual truth, optionally attached to each v1 manifest shot."""
from __future__ import annotations

from typing import List, Literal, Optional, Union
from pydantic import Field, model_validator

from app.perception_models import EvidenceModel


class HumanEvidence(EvidenceModel):
    frame_number: int = Field(ge=1, le=4)
    timestamp: float = Field(ge=0)
    description: str = Field(min_length=1)


class HumanFact(EvidenceModel):
    value: Union[str, List[str]]
    observable: bool = Field(strict=True)
    evidence: List[HumanEvidence]

    @model_validator(mode="after")
    def observable_evidence(self):
        if self.observable and not self.evidence:
            raise ValueError("人工可判断字段必须附具体帧证据")
        if not self.observable and self.value not in ("未知", []):
            raise ValueError("不可判断的人工字段值必须是未知或空数组")
        return self


class GoldenFields(EvidenceModel):
    primary_subject: HumanFact
    people: HumanFact
    objects: HumanFact
    scene: HumanFact
    foreground: HumanFact
    midground: HumanFact
    background: HumanFact
    relationships: HumanFact
    initial_state: HumanFact
    action_steps: HumanFact
    final_state: HumanFact
    shot_size: HumanFact
    lens: HumanFact
    camera_movement: HumanFact
    lighting: HumanFact
    artistic_effects: HumanFact


class HumanVisualTruth(EvidenceModel):
    schema_version: Literal["perception-gold.v1"] = "perception-gold.v1"
    source: Literal["human"] = "human"
    reviewer: str = Field(min_length=1)
    reviewed_at: str = Field(min_length=1)
    fields: GoldenFields
    important_closeup: bool
    scene_id: str = Field(min_length=1)
    notes: Optional[str] = None

    def validate_shot_range(self, start, end):
        for field in self.fields:
            for ref in field[1].evidence:
                if not start <= ref.timestamp < end:
                    raise ValueError("人工证据时间戳必须位于当前镜头内")
