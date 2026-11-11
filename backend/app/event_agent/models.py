"""Strict, independent contracts for the bounded event storyboard experiment."""
from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.perception_models import EvidenceRef
from app.schemas import FrameArtifact


class EventModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class EventAgentConfig(EventModel):
    window_seconds: float = Field(default=14.0, gt=0, le=16)
    overlap_seconds: float = Field(default=2.0, ge=0, le=4)
    max_model_calls_per_window: int = Field(default=2, ge=1, le=2)
    max_tool_calls_per_window: int = Field(default=2, ge=0, le=2)
    max_extra_frames_per_window: int = Field(default=6, ge=0, le=6)
    window_timeout_seconds: float = Field(default=90.0, gt=0, le=180)
    timing_policy: Literal["source-faithful", "authored"] = "source-faithful"
    generation_target_seconds: float = Field(default=8.0, gt=0, le=12)
    generation_max_seconds: float = Field(default=12.0, gt=0, le=12)
    max_windows: int = Field(default=12, ge=1, le=64)
    max_initial_frames_per_window: int = Field(default=20, ge=1, le=24)
    max_tokens: int = Field(default=6144, ge=256, le=8192)

    @model_validator(mode="after")
    def validate_limits(self):
        if self.overlap_seconds >= self.window_seconds:
            raise ValueError("overlap_seconds must be smaller than window_seconds")
        if self.generation_target_seconds > self.generation_max_seconds:
            raise ValueError("generation_target_seconds must not exceed generation_max_seconds")
        return self


class EventWindow(EventModel):
    id: str
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    source_shot_ids: List[str] = Field(min_length=1)
    context_shot_ids: List[str] = Field(default_factory=list)
    context_start_time: Optional[float] = Field(default=None, ge=0)
    source_spans: List[Dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_window(self):
        if self.end_time <= self.start_time:
            raise ValueError("window end must be after start")
        if len(set(self.source_shot_ids)) != len(self.source_shot_ids):
            raise ValueError("owned source IDs must be unique")
        if set(self.source_shot_ids) & set(self.context_shot_ids):
            raise ValueError("context source IDs are read-only and cannot be owned")
        return self


ToolName = Literal["inspect_boundary", "sample_interval_frames", "compare_frames", "crop_frame", "get_neighbor_context"]


class ToolRequest(EventModel):
    name: ToolName
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(EventModel):
    name: ToolName
    status: str
    output: Dict[str, Any] = Field(default_factory=dict)
    frames: List[FrameArtifact] = Field(default_factory=list)
    elapsed_seconds: float = Field(default=0.0, ge=0)
    error: Optional[str] = None


class FactReviewRequest(EventModel):
    """An evidence-linked suggestion for the existing explicit review workflow."""
    source_shot_id: str
    field_group: Literal["facts", "subject", "scene", "spatial", "action", "dynamics", "interaction", "expression", "accompanying"]
    reason: str = Field(min_length=8, max_length=500)
    evidence: List[EvidenceRef] = Field(min_length=1, max_length=6)


class CausalFact(EventModel):
    """A literal saved observation with traceable frame support, not an inference."""
    id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=600)
    source_shot_ids: List[str] = Field(min_length=1)
    evidence: List[EvidenceRef] = Field(min_length=1, max_length=12)
    critical: bool = True
    saved_observation_ids: List[str] = Field(default_factory=list, max_length=12)
    kind: Literal["action", "handoff", "detail", "reveal", "reaction", "state", "context"] = "action"


class PresentationDecision(EventModel):
    source_shot_id: str
    disposition: Literal["preserve", "integrate", "omit"]
    reason: str = Field(min_length=1, max_length=400)
    evidence: List[EvidenceRef] = Field(default_factory=list, max_length=12)


class GenerationPhotography(EventModel):
    """Creative presentation choices, never retroactively claimed source facts."""
    shot_size: Literal["大远景", "远景", "全景", "中景", "近景", "特写", "大特写", "未知"] = "未知"
    lens: Literal["超广角", "广角", "标准", "长焦", "未知"] = "未知"
    camera_movement: Literal["固定", "摇镜", "俯仰", "推近", "拉远", "跟拍", "手持", "升降", "环绕", "变焦", "航拍", "未知"] = "固定"
    composition: str = Field(default="保留已见主体与空间关系", min_length=1, max_length=300)
    intent: str = Field(default="生成摄影编排建议，不代表原片实测", min_length=1, max_length=300)


class GeneratedStage(EventModel):
    id: str = Field(min_length=1)
    semantic_phase: Optional[str] = Field(default=None, min_length=1, max_length=160,
        description="同一目标推进中的语义阶段，如递交与接稳、饮用与反馈；连续子动作共用阶段，必要切角仍独立保留。旧合同可省略。")
    action_summary: Optional[str] = Field(default=None, min_length=1, max_length=260, pattern=r"^[^\r\n]+$",
        description="简洁连贯复述本stage.fact_ids已有可见动作及反馈，不猜品尝/吹气/喘息/疲惫/疾病/心理/因果/喝光结果；风险标签仅在本stage.fact.text明确支持时可用，原不确定性必须保留。不新增主体、道具、结果或对白，不替代fact/evidence。旧合同可省略。")
    source_shot_ids: List[str] = Field(min_length=1)
    fact_ids: List[str] = Field(min_length=1)
    duration_seconds: float = Field(gt=0)
    transition: Literal["initial", "continuation", "cut"] = "continuation"
    photography: Optional[GenerationPhotography] = None


class EventProposal(EventModel):
    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=300)
    source_shot_ids: List[str] = Field(min_length=1)
    generation_subjects: Optional[List[str]] = Field(default=None, min_length=1, max_length=4,
        description="从本事件owned来源保存的primary_subject/visual_facts.people精确选取主要参与者标签，最多4项，不改写标签。")
    generation_objects: Optional[List[str]] = Field(default=None, max_length=6,
        description="从本事件owned来源保存的visual_facts.objects精确选取事件必要道具标签，最多6项，不改写标签，不堆背景家具。")
    causal_facts: List[CausalFact] = Field(min_length=1, max_length=40)
    presentation: List[PresentationDecision] = Field(min_length=1)
    generation_mode: Literal["continuous", "internal_cuts"] = "continuous"
    stages: List[GeneratedStage] = Field(min_length=1, max_length=20)
    confidence: float = Field(default=0.7, ge=0, le=1)
    photography: GenerationPhotography = Field(default_factory=GenerationPhotography)
    continuation_of_anchor_shot_id: Optional[str] = None
    continuation_evidence: List[EvidenceRef] = Field(default_factory=list, max_length=12)
    # A new event goal is not necessarily a new scene. This describes only the
    # boundary immediately before this event, independently of camera cuts.
    scene_continuity: Literal["unknown", "same_scene", "scene_change", "time_elision"] = "unknown"
    scene_evidence: List[EvidenceRef] = Field(default_factory=list, max_length=12)


class RequestEvidenceResponse(EventModel):
    action: Literal["request_evidence"]
    tool_requests: List[ToolRequest] = Field(min_length=1, max_length=2)
    provisional_events: List[EventProposal] = Field(default_factory=list)
    fact_review_requests: List[FactReviewRequest] = Field(default_factory=list, max_length=2)


class FinalizeResponse(EventModel):
    action: Literal["finalize"]
    events: List[EventProposal] = Field(min_length=1)
    fact_review_requests: List[FactReviewRequest] = Field(default_factory=list, max_length=2)


EventResponse = Annotated[Union[RequestEvidenceResponse, FinalizeResponse], Field(discriminator="action")]
EVENT_RESPONSE_ADAPTER = TypeAdapter(EventResponse)


class EventResponseSchema:
    """Provider schema adapter without weakening the existing structured transport."""

    @classmethod
    def model_json_schema(cls):
        return EVENT_RESPONSE_ADAPTER.json_schema()


class SourceOwnership(EventModel):
    source_shot_id: str
    event_id: str


class NarrativeEvent(EventModel):
    id: str
    anchor_shot_id: str
    title: str
    goal: str
    source_shot_ids: List[str]
    generation_subjects: Optional[List[str]] = Field(default=None, min_length=1, max_length=4)
    generation_objects: Optional[List[str]] = Field(default=None, max_length=6)
    initial_state: str
    final_state: str
    causal_facts: List[CausalFact] = Field(default_factory=list)
    presentation: List[PresentationDecision]
    generation_mode: Literal["continuous", "internal_cuts"]
    stages: List[GeneratedStage] = Field(default_factory=list)
    confidence: float
    status: Literal["ready", "fallback"] = "ready"
    window_id: str
    # Historical saved events omitted this field and keep their authored timing.
    timing_policy: Literal["source-faithful", "authored"] = "authored"
    version_hash: str
    warnings: List[str] = Field(default_factory=list)
    photography: GenerationPhotography = Field(default_factory=GenerationPhotography)
    scene_continuity: Literal["unknown", "same_scene", "scene_change", "time_elision"] = "unknown"
    scene_evidence: List[EvidenceRef] = Field(default_factory=list, max_length=12)


class ClipTimelineStage(EventModel):
    id: str
    semantic_phase: Optional[str] = Field(default=None, min_length=1, max_length=160)
    source_shot_ids: List[str]
    fact_ids: List[str]
    generation_start_seconds: float = Field(ge=0)
    generation_end_seconds: float = Field(gt=0, le=12)
    transition: Literal["initial", "continuation", "cut"]
    action: str
    photography: GenerationPhotography = Field(default_factory=GenerationPhotography)


class EventPrompt(EventModel):
    id: str
    prompt_type: Literal["first_frame", "video"]
    platform: str
    positive_prompt: str
    negative_prompt: str = ""
    status: str = "ready"


class GenerationClip(EventModel):
    id: str
    event_id: str
    title: str
    source_shot_ids: List[str] = Field(min_length=1)
    presentation: List[PresentationDecision]
    source_start_time: float
    source_end_time: float
    source_duration_seconds: float
    generation_duration_seconds: float = Field(gt=0, le=12)
    generation_mode: Literal["continuous", "internal_cuts"]
    timeline: List[ClipTimelineStage] = Field(min_length=1)
    first_frame_prompt: EventPrompt
    seedance_prompt: EventPrompt
    status: str
    version_hash: str
    warnings: List[str] = Field(default_factory=list)
    # Renderer diagnostics/settings, separate from copyable generator prose.
    # Optional so committed historical clips remain readable without rewriting.
    delivery: Dict[str, Any] = Field(default_factory=dict)


class WindowUsage(EventModel):
    model_calls: int = 0
    tool_calls: int = 0
    extra_frames: int = 0
    elapsed_seconds: float = 0.0


class WindowAudit(EventModel):
    window_id: str
    source_shot_ids: List[str]
    context_shot_ids: List[str] = Field(default_factory=list)
    status: Literal["ready", "fallback", "cancelled"]
    raw_responses: List[Dict[str, Any]] = Field(default_factory=list)
    tool_results: List[ToolResult] = Field(default_factory=list)
    validation_errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    usage: WindowUsage = Field(default_factory=WindowUsage)
    fact_review_requests: List[FactReviewRequest] = Field(default_factory=list, max_length=2)
    boundary_note: str = "Previous-window context is read-only; events never acquire its sources."


class EventAgentResult(EventModel):
    schema_version: Literal["event-agent.v1"] = "event-agent.v1"
    job_id: str
    run_id: str
    status: Literal["ready", "degraded", "cancelled"]
    config: EventAgentConfig
    windows: List[EventWindow]
    events: List[NarrativeEvent]
    generation_clips: List[GenerationClip]
    frames: List[FrameArtifact] = Field(default_factory=list)
    audit: List[WindowAudit] = Field(default_factory=list)
    raw_responses: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    usage_summary: Dict[str, Any] = Field(default_factory=dict)
    source_coverage: List[SourceOwnership]
    fact_review_requests: List[FactReviewRequest] = Field(default_factory=list)
    version_hash: str
