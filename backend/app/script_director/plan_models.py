"""Director decisions, separate from historical v2 and video observations."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DialogueRange(StrictModel):
    span_id: str
    start: int = Field(default=0, ge=0)
    end: int = Field(gt=0)


class PropState(StrictModel):
    prop: str
    holder: str


class SourceQuote(StrictModel):
    """An exact authored action, never a model-authored entailment claim."""
    span_id: str
    quote: str = Field(min_length=2)


class PerformancePhase(StrictModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0, le=12)
    description: str = Field(min_length=2)
    source_quotes: List[SourceQuote] = Field(default_factory=list, max_length=12)


class CameraPlan(StrictModel):
    shot_size: Literal["大远景", "远景", "全景", "中景", "近景", "特写", "大特写"] = "中景"
    lens: Literal["超广角", "广角", "标准", "长焦"] = "标准"
    angle: Literal["平视", "低角度", "高角度", "俯视", "倾斜", "人物视点"] = "平视"
    movement: Literal["固定", "摇镜", "俯仰", "推近", "拉远", "跟拍", "手持", "升降", "环绕"] = "固定"
    composition: str = "人物关系清楚，保留视线与动作方向"
    lighting: str = "保持场景内光线连续"
    axis_side: Literal["A", "B", "neutral"] = "A"
    reestablishes_axis: bool = False


class DirectedShot(StrictModel):
    source_span_ids: List[str] = Field(min_length=1)
    primary_subject: str = "原文主体"
    character_ids: List[str] = Field(default_factory=list)
    first_frame_state: str = Field(min_length=2)
    action_steps: List[str] = Field(default_factory=list, max_length=4)
    end_state: str = Field(min_length=2)
    shot_function: Literal["交代空间", "动作推进", "对白交流", "人物反应", "线索揭示", "场景过渡"]
    cut_reason: str = Field(min_length=2)
    camera: CameraPlan = Field(default_factory=CameraPlan)
    duration_seconds: float = Field(ge=0.5, le=12)
    dialogue_ids: List[str] = Field(default_factory=list)
    dialogue_ranges: List[DialogueRange] = Field(default_factory=list)
    prop_states_before: List[PropState] = Field(default_factory=list)
    prop_states_after: List[PropState] = Field(default_factory=list)
    continuity_notes: List[str] = Field(default_factory=list)
    creative_assumptions: List[str] = Field(default_factory=list)
    action_timeline: List[PerformancePhase] = Field(default_factory=list, max_length=4)
    source_quotes: List[SourceQuote] = Field(default_factory=list, max_length=24)
    source_support_version: Optional[str] = None


class DirectedEvent(StrictModel):
    title: str = Field(min_length=2)
    goal: str = Field(min_length=2)
    scene_id: str
    source_span_ids: List[str] = Field(min_length=1)
    shots: List[DirectedShot] = Field(min_length=1, max_length=120)
    continues_event_id: Optional[str] = Field(default=None, min_length=1)


class ToolRequest(StrictModel):
    name: Literal["get_source_span", "get_character_profile", "get_scene_state", "lookup_film_grammar", "estimate_dialogue_duration"]
    span_ids: List[str] = Field(default_factory=list, max_length=24)
    character_ids: List[str] = Field(default_factory=list, max_length=12)
    rule_ids: List[str] = Field(default_factory=list, max_length=8)


class DirectorDecision(StrictModel):
    action: Literal["use_tools", "finalize"]
    tool_requests: List[ToolRequest] = Field(default_factory=list, max_length=4)
    events: List[DirectedEvent] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def check_phase(self):
        if self.action == "use_tools" and (not self.tool_requests or self.events):
            raise ValueError("use_tools 只能包含工具请求，不能提前提交事件")
        if self.action == "finalize" and (not self.events or self.tool_requests):
            raise ValueError("finalize 必须有事件且不得再请求工具")
        return self
