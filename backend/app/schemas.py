from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.perception_models import ActionPhase, EvidenceRef, PerceptionRecord, VisualObservationV2


class ProviderHealth(BaseModel):
    provider: str
    model: str
    quantization: str = "n/a"
    ready: bool
    estimated_memory_mb: int = 0
    max_images_per_shot: int = 4
    max_context_tokens: int = 4096
    max_new_tokens: int = 512
    timeout_seconds: int = 180
    detail: Optional[str] = None


class SequenceStrategy(BaseModel):
    """Local derived layer; does not alter the frozen source shot plan."""

    target_min_seconds: float = Field(default=6.0, gt=0, le=12)
    target_max_seconds: float = Field(default=10.0, gt=0, le=12)
    hard_max_seconds: float = Field(default=12.0, gt=0, le=12)
    max_source_shots: int = Field(default=4, ge=1, le=4)
    max_event_steps: int = Field(default=6, ge=1, le=8)
    minimum_confidence: float = Field(default=0.8, ge=0, le=1)


class JobConfig(BaseModel):
    script_strategy: Literal["director-v3", "legacy-v2"] = "director-v3"
    event_grouping_policy: Literal["legacy-pairs-v1", "event-agent-v2", "script-director-v3"] = "legacy-pairs-v1"
    max_video_duration_seconds: int = 600
    max_video_size_mb: int = 2048
    max_shots: int = 120
    frames_per_shot: int = 4
    max_frames_per_shot: int = 4
    frame_max_long_edge: int = 768
    jpeg_quality: int = 80
    vlm_max_context_tokens: int = 4096
    vlm_max_new_tokens: int = 512
    script_max_new_tokens: int = 2048
    vlm_timeout_seconds: int = 180
    provider_concurrency: int = 1
    segmentation_profile: str = "source-boundaries-v4"
    narrative_min_seconds: float = 1.2
    narrative_max_seconds: float = 5.0
    max_raw_shots_per_narrative: int = 4
    sequence_strategy: Optional[SequenceStrategy] = None


class FrameArtifact(BaseModel):
    id: str
    shot_id: str
    timestamp: float
    path: str
    thumbnail_path: str
    width: int = 0
    height: int = 0
    blur_score: Optional[float] = None
    brightness_score: Optional[float] = None
    selected: bool = True
    artifact_kind: str = "keyframe"
    parent_frame_id: Optional[str] = None
    bbox: Optional[List[float]] = None


class SpatialEvidence(BaseModel):
    foreground: str = "未知"
    midground: str = "未知"
    background: str = "未知"
    relationships: str = "未知"
    observable: bool = True


class ActionDetails(BaseModel):
    primary_subject: str = "未知"
    initial_state: str = "未知"
    steps: List[str] = Field(default_factory=list)
    interaction_target: str = "无"
    final_state: str = "未知"
    emotion_change: str = "未知"
    evidence: List[str] = Field(default_factory=list)
    accompanying_states: List[str] = Field(default_factory=list)
    action_phases: List[ActionPhase] = Field(default_factory=list)
    step_evidence: List[List[EvidenceRef]] = Field(default_factory=list)


class VisualFacts(BaseModel):
    objects: List[str] = Field(default_factory=list)
    people: List[str] = Field(default_factory=list)
    scene: str = "未知"
    evidence: List[str] = Field(default_factory=list)
    uncertainties: List[str] = Field(default_factory=list)


class VisualStyleEvidence(BaseModel):
    medium: str = "未知"
    character_design: str = "未知"
    rendering: str = "未知"
    color_palette: str = "未知"
    texture: str = "未知"
    visible_effects: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)
    uncertain_techniques: List[str] = Field(default_factory=list)


class ShotContract(BaseModel):
    timing_origin: Literal["source-video", "script-performance"] = "source-video"
    purpose: str = "复现当前镜头的可见内容"
    first_frame_state: str = "未知"
    primary_action: str = "未知"
    end_state: str = "未知"
    continuity_anchors: List[str] = Field(default_factory=list)
    shot_size: str = "未知"
    lens: str = "未知"
    camera_movement: str = "未知"
    lighting: str = "未知"
    composition: str = "未知"
    source_edit_times: List[float] = Field(default_factory=list)
    generation_duration_seconds: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    editorial_note: str = ""
    aspect_ratio: str = "待设定"
    observed_style: str = "未知"
    style_profile_version: str = ""
    style_profile_hash: str = ""
    generation_suggestions: Dict[str, str] = Field(default_factory=dict)
    delivery: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_source_duration(self):
        # Source observations may span a long uncut take. Provider/clip limits
        # belong to the generation plan, never to the source/evidence contract.
        return self


class VisionObservation(BaseModel):
    script_direction: Optional[Dict[str, Any]] = None
    subject_action: str
    shot_size: str
    lens: str = Field(description="视觉焦段估计：超广角、广角、标准、长焦或未知")
    camera_movement: str
    lighting_vibe: str
    composition: str = ""
    lighting_type: str = "未知"
    lighting_detail: str = ""
    spatial_evidence: SpatialEvidence = Field(default_factory=SpatialEvidence)
    action_details: ActionDetails = Field(default_factory=ActionDetails)
    visual_style_evidence: VisualStyleEvidence = Field(default_factory=VisualStyleEvidence)
    visual_facts: VisualFacts = Field(default_factory=VisualFacts)
    confidence: float = Field(default=0.5, ge=0, le=1)
    raw_response: Optional[str] = None
    analysis_warnings: List[str] = Field(default_factory=list)
    analysis_review: Dict[str, Any] = Field(default_factory=dict)
    analysis_span_observations: List[Dict[str, Any]] = Field(default_factory=list)
    perception_record: Optional[PerceptionRecord] = Field(default=None, exclude=True)


class StaticVisionObservation(BaseModel):
    """Strict first-stage output for facts visible in individual frames."""

    primary_subject: str
    shot_size: Literal["大远景", "远景", "全景", "中景", "近景", "特写", "大特写", "未知"]
    lens: Literal["超广角", "广角", "标准", "长焦", "未知"]
    lighting_vibe: str
    composition: str
    lighting_type: Literal["自然柔光", "硬质方向光", "高调", "低调", "逆光", "实景光源", "体积光", "混合", "未知"] = "未知"
    lighting_detail: str = ""
    spatial_evidence: SpatialEvidence = Field(default_factory=SpatialEvidence)
    visual_style_evidence: VisualStyleEvidence = Field(default_factory=VisualStyleEvidence)
    visual_facts: VisualFacts = Field(default_factory=VisualFacts)
    confidence: float = Field(default=0.5, ge=0, le=1)


class MotionVisionObservation(BaseModel):
    """Strict second-stage output for changes across chronological frames."""

    subject_action: str
    camera_movement_type: Literal[
        "固定",
        "摇镜",
        "俯仰",
        "推近",
        "拉远",
        "跟拍",
        "手持",
        "升降",
        "环绕",
        "变焦",
        "航拍",
        "复合运动",
        "未知",
    ]
    camera_movement_detail: str
    primary_subject: str = "未知"
    initial_state: str = "未知"
    action_steps: List[str] = Field(default_factory=list, min_length=0, max_length=3)
    interaction_target: str = "无"
    final_state: str = "未知"
    emotion_change: str = "未知"
    visible_evidence: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class ScriptBeat(BaseModel):
    """A traceable, mostly-atomic unit extracted before model planning."""

    id: str
    text: str
    scene_hint: str = "scene-001"
    scene_label: str = ""
    paragraph_hint: str = "paragraph-001"


class ScriptShotPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_beat_ids: List[str] = Field(min_length=1)
    scene_group_id: str = "scene-001"
    primary_subject: str
    first_frame_state: str
    action_steps: List[str] = Field(min_length=1, max_length=4)
    interaction_target: str = "无"
    end_state: str
    emotion_change: str = "无明显变化"
    shot_size: Literal["大远景", "远景", "全景", "中景", "近景", "特写", "大特写", "未知"]
    lens: Literal["超广角", "广角", "标准", "长焦", "未知"]
    camera_movement_type: Literal[
        "固定", "摇镜", "俯仰", "推近", "拉远", "跟拍", "手持", "升降", "环绕", "变焦", "航拍", "复合运动", "未知"
    ]
    camera_movement_detail: str = ""
    lighting_type: Literal["自然柔光", "硬质方向光", "高调", "低调", "逆光", "实景光源", "体积光", "混合", "未知"] = "未知"
    lighting_detail: str = ""
    composition: str
    foreground: str = "无明确前景"
    midground: str = "主要主体"
    background: str = "剧本场景"
    spatial_relationships: str = "主体与环境关系按剧本文本建立"
    continuity_anchors: List[str] = Field(default_factory=list)
    duration_seconds: Literal[4.0, 4.5, 5.0] = 4.5
    confidence: float = Field(default=0.72, ge=0, le=1)
    warnings: List[str] = Field(default_factory=list)


class ScriptPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shots: List[ScriptShotPlan] = Field(min_length=1)


class ScriptPlanResult(BaseModel):
    plan: ScriptPlan
    raw_responses: List[str] = Field(default_factory=list)
    rule_reconciled: bool = False


class PromptBundle(BaseModel):
    id: str
    shot_id: str
    prompt_type: str
    platform: str
    version: int = 1
    positive_prompt: str
    negative_prompt: str
    status: str = "ready"
    template_id: str = "cinematic-shot-v1"
    template_version: int = 1
    warnings: List[str] = Field(default_factory=list)
    contract: Optional[ShotContract] = None


class RawShot(BaseModel):
    id: str
    number: int
    start_time: float
    end_time: float
    duration: float
    boundary_metrics: Dict[str, Any] = Field(default_factory=dict)
    decision: str = "keep"
    reason: str = ""
    narrative_shot_id: Optional[str] = None
    generation_shot_ids: List[str] = Field(default_factory=list)


class ActionAtom(BaseModel):
    """A traceable, at-most-five-second action unit."""

    id: str
    number: int
    start_time: float
    end_time: float
    duration: float
    source_raw_shot_ids: List[str] = Field(default_factory=list)
    frame_ids: List[str] = Field(default_factory=list)
    primary_subject: str = "未知"
    initial_state: str = "未知"
    action: str = "未知"
    interaction_target: str = "无"
    final_state: str = "未知"
    confidence: float = Field(default=0.5, ge=0, le=1)
    warnings: List[str] = Field(default_factory=list)
    generation_shot_id: Optional[str] = None


class SceneGroup(BaseModel):
    id: str
    order: int
    title: str
    location: Optional[str] = None
    time_of_day: Optional[str] = None
    shot_ids: List[str] = Field(default_factory=list)
    cover_frame_ids: List[str] = Field(default_factory=list)
    start_time: float
    end_time: float
    warning_count: int = 0
    grouping_mode: str = "semantic"


class Shot(BaseModel):
    script_direction: Optional[Dict[str, Any]] = None
    timing_origin: Literal["source-video", "script-performance"] = "source-video"
    visual_observation_v2: Optional[VisualObservationV2] = None
    id: str
    number: int
    start_time: float
    end_time: float
    duration: float
    time: str
    source_text: str = ""
    action: str = ""
    shot_size: str = ""
    lens: str = Field(default="", description="视觉焦段估计；保留 lens 字段以兼容 storyboard.v1")
    camera_movement: str = ""
    lighting_vibe: str = ""
    composition: str = ""
    confidence: float = 0.0
    status: str = "queued"
    frame_ids: List[str] = Field(default_factory=list)
    prompts: List[PromptBundle] = Field(default_factory=list)
    analysis_warnings: List[str] = Field(default_factory=list)
    analysis_review: Dict[str, Any] = Field(default_factory=dict)
    analysis_span_observations: List[Dict[str, Any]] = Field(default_factory=list)
    action_details: ActionDetails = Field(default_factory=ActionDetails)
    spatial_evidence: SpatialEvidence = Field(default_factory=SpatialEvidence)
    visual_style_evidence: VisualStyleEvidence = Field(default_factory=VisualStyleEvidence)
    visual_facts: VisualFacts = Field(default_factory=VisualFacts)
    source_raw_shot_ids: List[str] = Field(default_factory=list)
    source_action_atom_ids: List[str] = Field(default_factory=list)
    scene_group_id: Optional[str] = None
    cut_reason: str = ""
    source_edit_times: List[float] = Field(default_factory=list)
    generation_duration_seconds: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    scene_reason: str = ""

    @model_validator(mode="after")
    def validate_source_duration(self):
        return self


class JobStatus(BaseModel):
    id: str
    status: str
    stage: str
    progress: int = 0
    video_filename: Optional[str] = None
    input_type: str = "video"
    script_text: str = ""
    provider: Optional[str] = None
    model: Optional[str] = None
    segmentation_profile: str = "legacy-v1"
    style_strategy: str = "legacy-v1"
    raw_shot_count: int = 0
    narrative_shot_count: int = 0
    error: Optional[str] = None
    result_version: str = "storyboard.v1"
    created_at: str
    updated_at: str
    config: JobConfig


class MontageStateLinkage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_final_state: str = "未知"
    right_initial_state: str = "未知"
    compatible: Optional[bool] = None
    evidence: List[str] = Field(default_factory=list)


class MontageContextEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["subject", "prop", "location"]
    value: str = Field(min_length=1, max_length=160)
    shot_id: str
    source_field: Literal["primary_subject", "people", "objects", "scene", "initial_state", "final_state", "interaction_target"]
    quote: str = Field(min_length=2, max_length=500)


class MontageRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_shot_id: str
    right_shot_id: str
    observed_edit_fact: str
    relation_type: Literal["连续动作", "反应镜头", "视觉呼应", "时间省略", "场景转换", "未知"]
    possible_narrative_intent: str = "待判断"
    evidence: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    scene_change: bool = False
    merge_eligible: Optional[bool] = None
    event_goal: str = ""
    boundary_role: Literal["action-detail", "continuation", "independent-reaction", "reveal", "unknown"] = "unknown"
    reason: str = ""
    state_linkage: Optional[MontageStateLinkage] = None
    shared_subjects: List[str] = Field(default_factory=list)
    shared_prop: str = ""
    shared_location: str = ""
    context_evidence: List[MontageContextEvidence] = Field(default_factory=list, max_length=12)


class MontageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relations: List[MontageRelation]


class NarrativeEvent(BaseModel):
    id: str
    goal: str
    source_shot_ids: List[str] = Field(min_length=1)
    source_raw_shot_ids: List[str] = Field(default_factory=list)
    primary_subject: str = "未知"
    participants: List[str] = Field(default_factory=list)
    interaction_target: str = "未知"
    location: str = "未知"
    initial_state: str = "未知"
    final_state: str = "未知"
    evidence: List[str] = Field(default_factory=list)
    status: str = "ready"


class SequenceTimelineSegment(BaseModel):
    id: Optional[str] = None
    semantic_phase: Optional[str] = Field(default=None, min_length=1, max_length=160)
    source_shot_id: str
    source_shot_ids: Optional[List[str]] = Field(default=None, min_length=1, max_length=120)
    fact_ids: List[str] = Field(default_factory=list)
    photography: Optional[Dict[str, Any]] = None
    source_start_time: float
    source_end_time: float
    generation_start_seconds: float = Field(ge=0)
    generation_end_seconds: float = Field(gt=0, le=12)
    shot_size: str = "未知"
    lens: str = "未知"
    camera_movement: str = "未知"
    composition: str = ""
    action: str = "未知"
    action_steps: List[str] = Field(default_factory=list)
    initial_state: str = "未知"
    final_state: str = "未知"
    source_edit_times: List[float] = Field(default_factory=list)
    generation_edit_times: List[float] = Field(default_factory=list)
    transition: Literal["initial", "cut", "continuation"] = "initial"
    cut_reason: str = ""


class GenerationSequenceContract(BaseModel):
    """Generation contract; its budget is independent of source shot boundaries."""

    purpose: str
    first_frame_state: str
    end_state: str
    generation_duration_seconds: float = Field(gt=0, le=12)
    source_shot_ids: List[str] = Field(min_length=1, max_length=120)
    source_edit_times: List[float] = Field(default_factory=list)
    continuity_anchors: List[str] = Field(default_factory=list)
    editorial_note: str = "保留来源切镜和景别变化；来源时间与生成时间分别记录"
    aspect_ratio: str = "待设定"
    resolution: str = "1080p"
    orientation: str = "待设定"
    style_profile_version: str = ""
    style_profile_hash: str = ""
    observed_style: str = "未知"
    delivery: Dict[str, Any] = Field(default_factory=dict)


class SequencePrompt(BaseModel):
    id: str
    prompt_type: Literal["first_frame", "video"]
    platform: str
    version: int = 1
    positive_prompt: str
    negative_prompt: str = ""
    status: str = "ready"
    contract: GenerationSequenceContract


class GenerationSequence(BaseModel):
    id: str
    event_id: str
    title: str
    main_action: str
    scene_group_id: Optional[str] = None
    cover_frame_id: Optional[str] = None
    source_shot_ids: List[str] = Field(min_length=1, max_length=120)
    source_raw_shot_ids: List[str] = Field(default_factory=list)
    source_start_time: float
    source_end_time: float
    source_duration_seconds: float
    generation_duration_seconds: float = Field(gt=0, le=12)
    timeline: List[SequenceTimelineSegment] = Field(min_length=1, max_length=20)
    generation_mode: Optional[Literal["continuous", "internal_cuts"]] = None
    presentation: List[Dict[str, Any]] = Field(default_factory=list)
    causal_facts: List[Dict[str, Any]] = Field(default_factory=list)
    photography: Optional[Dict[str, Any]] = None
    version_hash: Optional[str] = None
    first_frame_prompt: SequencePrompt
    seedance_prompt: SequencePrompt
    status: str = "ready"
    version: int = 1
    build_hash: str
    warnings: List[str] = Field(default_factory=list)


class StoryboardResult(BaseModel):
    schema_version: str = "storyboard.v1"
    job: JobStatus
    shots: List[Shot] = Field(default_factory=list)
    frames: List[FrameArtifact] = Field(default_factory=list)
    evidence_frames: List[FrameArtifact] = Field(default_factory=list)
    prompts: List[PromptBundle] = Field(default_factory=list)
    raw_shots: List[RawShot] = Field(default_factory=list)
    action_atoms: List[ActionAtom] = Field(default_factory=list)
    scene_groups: List[SceneGroup] = Field(default_factory=list)
    montage_relations: List[MontageRelation] = Field(default_factory=list)
    narrative_events: Optional[List[NarrativeEvent]] = None
    generation_sequences: Optional[List[GenerationSequence]] = None
    sequence_metadata: Optional[Dict[str, Any]] = None
    style_profile: Optional[Dict[str, Any]] = None
    style_observation: Optional[Dict[str, Any]] = None
    visual_bible: Optional[Dict[str, Any]] = None
    style_analysis: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class JobCreated(BaseModel):
    job_id: str
    status: str
    input_type: str = "video"


class Event(BaseModel):
    event_id: str
    type: str
    job_id: str
    sequence: int
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: str
