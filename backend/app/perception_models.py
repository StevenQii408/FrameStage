"""Evidence-bearing internal IR; storyboard.v1 remains its compatibility projection."""
from __future__ import annotations

from typing import Annotated, Dict, Generic, List, Literal, Optional, Tuple, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class EvidenceRef(EvidenceModel):
    frame_id: str = Field(min_length=1)
    timestamp: float = Field(ge=0, strict=True)
    bbox: Optional[Tuple[float, float, float, float]] = None
    crop_artifact_id: Optional[str] = None
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_region(self):
        if self.bbox is not None:
            x1, y1, x2, y2 = self.bbox
            if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
                raise ValueError("bbox must be normalized [x1,y1,x2,y2] within the image")
        return self


T = TypeVar("T")


class ObservedField(EvidenceModel, Generic[T]):
    value: T
    observable: bool = Field(strict=True)
    confidence: float = Field(ge=0, le=1, strict=True)
    evidence: List[EvidenceRef]
    uncertainties: List[str]

    @model_validator(mode="after")
    def require_evidence(self):
        unknown = self.value in ("未知", "", None) if isinstance(self.value, str) or self.value is None else not self.value
        if self.observable and isinstance(self.value, str) and unknown:
            raise ValueError("An unknown string cannot be observable")
        if self.observable and not self.evidence:
            raise ValueError("Every observable claim, including absence, requires frame evidence")
        if not self.observable and not unknown:
            raise ValueError("An unobservable value must be unknown or an empty list")
        if not self.observable and self.confidence != 0:
            raise ValueError("An unobservable field has zero evidence confidence")
        return self


ShotSize = Literal["大远景", "远景", "全景", "中景", "近景", "特写", "大特写", "未知"]
Lens = Literal["超广角", "广角", "标准", "长焦", "未知"]
Movement = Literal["固定", "摇镜", "俯仰", "推近", "拉远", "跟拍", "手持", "升降", "环绕", "变焦", "航拍", "复合运动", "未知"]
Lighting = Literal["自然柔光", "硬质方向光", "高调", "低调", "逆光", "实景光源", "体积光", "混合", "未知"]


def unknown_field(value="未知", reason="画面证据不足") -> dict:
    return dict(value=value, observable=False, confidence=0.0, evidence=[], uncertainties=[reason])


class VisualFactsV2(EvidenceModel):
    primary_subject: ObservedField[str]
    objects: ObservedField[List[str]]
    people: ObservedField[List[str]]
    scene: ObservedField[str]
    foreground: ObservedField[str]
    midground: ObservedField[str]
    background: ObservedField[str]
    relationships: ObservedField[str]


class StaticPhotographyV2(EvidenceModel):
    shot_size: ObservedField[ShotSize]
    lens: ObservedField[Lens]
    lighting_type: ObservedField[Lighting]
    lighting_detail: ObservedField[str]
    lighting_vibe: ObservedField[str]
    composition: ObservedField[str]


class PhotographyV2(StaticPhotographyV2):
    camera_movement_type: ObservedField[Movement]
    camera_movement_detail: ObservedField[str]


class DynamicsV2(EvidenceModel):
    subject_action: ObservedField[str]
    initial_state: ObservedField[str]
    action_steps: ObservedField[List[str]]
    interaction_target: ObservedField[str]
    final_state: ObservedField[str]
    emotion_change: ObservedField[str]
    accompanying_states: ObservedField[List[str]] = Field(default_factory=lambda: unknown_field([], "未记录伴随状态"), validate_default=True)
    action_phases: List["ActionPhase"] = Field(default_factory=list)
    step_evidence: List[List[EvidenceRef]] = Field(default_factory=list)

    @model_validator(mode="after")
    def limit_steps(self):
        if len(self.action_steps.value) > 4:
            raise ValueError("At most four chronological action steps")
        if len(self.accompanying_states.value) > 2:
            raise ValueError("At most two accompanying states")
        return self


class ActionPhase(EvidenceModel):
    """Semantic grouping of observed steps; times are generation suggestions only."""
    description: str = Field(min_length=2, max_length=180)
    source_step_indices: List[Annotated[int, Field(strict=True, ge=1, le=4)]] = Field(min_length=1, max_length=4)
    evidence: List[EvidenceRef] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, strict=True)
    generation_start_seconds: Optional[float] = Field(default=None, ge=0, strict=True)
    generation_end_seconds: Optional[float] = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def validate_phase(self):
        if any(isinstance(index, bool) or not 1 <= index <= 4 for index in self.source_step_indices):
            raise ValueError("Phase source steps must be numbered 1–4")
        if self.source_step_indices != sorted(set(self.source_step_indices)):
            raise ValueError("Phase source steps must be unique and ordered")
        start, end = self.generation_start_seconds, self.generation_end_seconds
        if (start is None) != (end is None) or (start is not None and end <= start):
            raise ValueError("Generation timing must provide one positive interval or neither endpoint")
        return self


DynamicsV2.model_rebuild()


class ArtisticEffectsV2(EvidenceModel):
    medium: ObservedField[str] = Field(default_factory=lambda: unknown_field(reason="旧版本未记录媒介"))
    character_design: ObservedField[str] = Field(default_factory=lambda: unknown_field(reason="旧版本未记录角色造型"))
    rendering: ObservedField[str] = Field(default_factory=lambda: unknown_field(reason="旧版本未记录渲染表现"))
    color_palette: ObservedField[str]
    motion_signature: ObservedField[str] = Field(default_factory=lambda: unknown_field(reason="旧版本未记录动态画风"))
    texture: ObservedField[str]
    visible_effects: ObservedField[List[str]]
    uncertain_techniques: List[str]


class Interpretation(EvidenceModel):
    description: str = Field(min_length=1)
    evidence: List[EvidenceRef] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class StaticObservationV2(EvidenceModel):
    facts: VisualFactsV2
    photography: StaticPhotographyV2
    artistic_effects: ArtisticEffectsV2
    uncertainties: List[str]


class MotionObservationV2(EvidenceModel):
    dynamics: DynamicsV2
    motion_signature: ObservedField[str] = Field(default_factory=lambda: unknown_field(reason="动态画风证据不足"))
    camera_movement_type: ObservedField[Movement]
    camera_movement_detail: ObservedField[str]
    uncertainties: List[str]


class VisualObservationV2(EvidenceModel):
    schema_version: Literal["visual-observation.v2"] = "visual-observation.v2"
    facts: VisualFactsV2
    dynamics: DynamicsV2
    photography: PhotographyV2
    artistic_effects: ArtisticEffectsV2
    uncertainties: List[str]
    interpretations: List[Interpretation] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class PerceptionStageRecord(EvidenceModel):
    """Execution health is independent of whether a valid field is unknown."""
    state: Literal["succeeded", "partial", "failed", "deferred"] = "deferred"
    error: Optional[Dict] = None
    input_frames: List[Dict] = Field(default_factory=list)
    attempts: int = Field(default=0, ge=0)
    schema_status: Literal["passed", "partial", "failed", "not_run"] = "not_run"
    schema_enforcement: str = "local-compact-fields"
    usage: List[Dict] = Field(default_factory=list)
    unknown_fields: List[str] = Field(default_factory=list)


class PerceptionRecord(EvidenceModel):
    observation: VisualObservationV2
    raw_responses: List[Dict] = Field(default_factory=list)
    normalized_stages: Dict = Field(default_factory=dict)
    trace: List[Dict] = Field(default_factory=list)
    tools: List[Dict] = Field(default_factory=list)
    budgets: Dict = Field(default_factory=dict)
    supplemental_call_used: bool = False
    correction_used: bool = False
    elapsed_seconds: float = 0
    stages: Dict[str, PerceptionStageRecord] = Field(default_factory=dict)
    input_frames: List[Dict] = Field(default_factory=list)
    revisions: List[Dict] = Field(default_factory=list)
    source_revision: Optional[Union[str, int]] = None
    analysis_span_observations: List[Dict] = Field(default_factory=list)


def iter_observed_fields(model):
    if isinstance(model, ObservedField):
        yield model
    elif isinstance(model, BaseModel):
        for name in type(model).model_fields:
            yield from iter_observed_fields(getattr(model, name))


def validate_evidence(model, frames) -> None:
    """Reject invented IDs, moved timestamps and unresolved crop artifacts."""
    catalogue = {frame.id: frame for frame in frames}
    def references_in(value):
        if isinstance(value, EvidenceRef):
            yield value
        elif isinstance(value, BaseModel):
            for name in type(value).model_fields:
                yield from references_in(getattr(value, name))
        elif isinstance(value, list):
            for item in value:
                yield from references_in(item)
    references = references_in(model)
    for ref in references:
        frame = catalogue.get(ref.frame_id)
        if frame is None or abs(frame.timestamp - ref.timestamp) > 0.002:
            raise ValueError(f"Invalid evidence frame/time: {ref.frame_id}")
        if ref.crop_artifact_id:
            crop = catalogue.get(ref.crop_artifact_id)
            if crop is None or getattr(crop, "artifact_kind", "") != "crop":
                raise ValueError(f"Unresolved crop artifact: {ref.crop_artifact_id}")
            if abs(crop.timestamp - ref.timestamp) > 0.002:
                raise ValueError("Crop and source timestamp differ")
            if ref.frame_id not in (crop.id, getattr(crop, "parent_frame_id", None)):
                raise ValueError("Crop does not originate from the cited frame")


def empty_stage(stage, reason):
    def fill(cls):
        values = {}
        for name, field in cls.model_fields.items():
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, ObservedField):
                value = [] if name in ("objects", "people", "action_steps", "visible_effects", "accompanying_states") else "未知"
                values[name] = unknown_field(value, reason)
            elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
                values[name] = fill(annotation)
            else:
                values[name] = [reason] if name in ("uncertainties", "uncertain_techniques") else []
        return values
    return stage.model_validate(fill(stage))


def merge_stages(static, motion, warnings=None) -> VisualObservationV2:
    static = static or empty_stage(StaticObservationV2, "静态阶段失败")
    motion = motion or empty_stage(MotionObservationV2, "动态阶段失败")
    return VisualObservationV2(
        facts=static.facts, dynamics=motion.dynamics,
        photography=PhotographyV2(**static.photography.model_dump(),
                                  camera_movement_type=motion.camera_movement_type,
                                  camera_movement_detail=motion.camera_movement_detail),
        artistic_effects=ArtisticEffectsV2(**{**static.artistic_effects.model_dump(), "motion_signature": motion.motion_signature}),
        uncertainties=list(dict.fromkeys([*static.uncertainties, *motion.uncertainties])),
        interpretations=[], warnings=list(warnings or []),
    )


def key_fields(v2: VisualObservationV2):
    return [v2.facts.primary_subject, v2.facts.scene, v2.dynamics.subject_action,
            v2.photography.shot_size, v2.photography.lens,
            v2.photography.camera_movement_type, v2.photography.lighting_vibe]


def observation_review(v2: VisualObservationV2, record=None):
    """Field-scoped readiness; an unknown lens never erases a visible action.

    Request/schema health and visual uncertainty are deliberately independent.
    A valid observation of a held state is a usable action, too.
    """
    generic_actions = {"人物移动", "主体移动", "表情变化", "画面变化", "人物表情变化"}
    static_missing = [f"facts.{name}" for name in ("primary_subject", "scene")
                      if not getattr(v2.facts, name).observable]
    action = v2.dynamics.subject_action
    action_ready = action.observable and action.value.strip("。 ") not in generic_actions
    dynamic_missing = [] if action_ready else ["dynamics.subject_action"]
    endpoint_missing = [f"dynamics.{name}" for name in ("initial_state", "final_state")
                        if not getattr(v2.dynamics, name).observable]
    issues = []
    for scope, missing in (("static", static_missing), ("dynamic", dynamic_missing)):
        for path in missing:
            issues.append({"scope": scope, "severity": "review", "code": "core_evidence_missing",
                           "field": path, "message": "该字段缺少有效证据，其他有效字段已保留"})
    for path in endpoint_missing:
        issues.append({"scope": "dynamic", "severity": "info", "code": "endpoint_evidence_missing",
                       "field": path, "message": "起止状态证据不足，未用中间代表帧补造首尾状态"})
    stages = record.stages if isinstance(record, PerceptionRecord) else record.get("stages", {}) if isinstance(record, dict) else {}
    for name, stage in stages.items():
        data = stage.model_dump() if isinstance(stage, PerceptionStageRecord) else stage
        if data.get("state") in ("failed", "deferred") and data.get("error"):
            error = data["error"]
            issues.append({"scope": "dynamic" if name == "motion" else "static", "severity": "review",
                           "code": error.get("category", "stage_unavailable"), "kind": error.get("kind"),
                           "message": error.get("message", "该阶段未完成，其他有效阶段已保留")})
    responses = record.raw_responses if isinstance(record, PerceptionRecord) else record.get("raw_responses", []) if isinstance(record, dict) else []
    def final_field(path):
        parts = path.split(".")
        if path.startswith("camera_movement_"):
            parts = ["photography", path]
        elif path == "motion_signature":
            parts = ["artistic_effects", path]
        current = v2
        for part in parts:
            current = getattr(current, part, None)
        return current
    seen = {issue.get("field") for issue in issues}
    for response in responses:
        for repair in response.get("field_repairs", []):
            path = repair.get("field", "")
            field = final_field(path)
            if repair.get("repair") != "field_only_unknown" or path in response.get("resolved_fields", []) or path in seen or (field and getattr(field, "observable", False)):
                continue
            scope = "dynamic" if path.startswith("dynamics.") else "static" if path.startswith("facts.") else "style" if path.startswith("artistic_effects.") or path == "motion_signature" else "photography"
            issues.append({"scope": scope, "severity": "info", "code": "field_structure_invalid",
                           "field": path, "message": "该字段结构或证据引用无效，其他有效字段已保留"})
            seen.add(path)
    spans = record.analysis_span_observations if isinstance(record, PerceptionRecord) else record.get("analysis_span_observations", []) if isinstance(record, dict) else []
    pending_spans = [span for span in spans if span.get("status") != "ready"]
    if pending_spans:
        dynamic_missing.append("analysis_span_coverage")
        action_ready = False
        issues.append({"scope": "dynamic", "severity": "review", "code": "analysis_span_coverage_pending",
                       "message": f"{len(pending_spans)} 个分析区间待完成，有效区间结果已保留",
                       "spans": [{"start_time": span["start_time"], "end_time": span["end_time"]} for span in pending_spans]})
    return {"version": "perception-review.v2",
            "static": {"status": "pending_review" if static_missing else "ready", "missing_fields": static_missing},
            "dynamic": {"status": "ready" if action_ready else "pending_review", "missing_fields": dynamic_missing,
                        "endpoint_missing_fields": endpoint_missing},
            "video_ready": not static_missing and action_ready and not endpoint_missing,
            "issues": issues}


def project_v1(v2: VisualObservationV2, raw_response=None, partial_failure=False):
    from app.schemas import ActionDetails, SpatialEvidence, VisionObservation, VisualFacts, VisualStyleEvidence

    def value(field):
        return field.value if field.observable else ([] if isinstance(field.value, list) else "未知")

    def descriptions(field):
        return [f"{ref.frame_id} | {ref.timestamp:.3f}s：{ref.description}" for ref in field.evidence]

    facts, dynamics, photo, style = v2.facts, v2.dynamics, v2.photography, v2.artistic_effects
    movement = value(photo.camera_movement_type)
    detail = value(photo.camera_movement_detail)
    camera = f"{movement}：{detail}" if movement != "未知" and detail != "未知" else movement
    fields = key_fields(v2)
    confidence = min((field.confidence for field in fields if field.observable), default=0)
    if sum(not field.observable for field in fields) >= 2:
        confidence = min(confidence, 0.5)
    if partial_failure:
        confidence = min(confidence, 0.3)
    projection = VisionObservation(
        subject_action=value(dynamics.subject_action), shot_size=value(photo.shot_size),
        lens=value(photo.lens), camera_movement=camera, lighting_vibe=value(photo.lighting_vibe),
        composition=value(photo.composition), lighting_type=value(photo.lighting_type),
        lighting_detail=value(photo.lighting_detail), confidence=confidence,
        spatial_evidence=SpatialEvidence(
            foreground=value(facts.foreground), midground=value(facts.midground),
            background=value(facts.background), relationships=value(facts.relationships),
            observable=any(field.observable for field in (facts.foreground, facts.midground, facts.background)),
        ),
        action_details=ActionDetails(
            primary_subject=value(facts.primary_subject), initial_state=value(dynamics.initial_state),
            steps=value(dynamics.action_steps), interaction_target=value(dynamics.interaction_target),
            final_state=value(dynamics.final_state), emotion_change=value(dynamics.emotion_change),
            evidence=descriptions(dynamics.subject_action),
            accompanying_states=value(dynamics.accompanying_states),
            action_phases=dynamics.action_phases, step_evidence=dynamics.step_evidence,
        ),
        visual_facts=VisualFacts(objects=value(facts.objects), people=value(facts.people),
                                 scene=value(facts.scene), evidence=descriptions(facts.scene),
                                 uncertainties=[*v2.uncertainties, *facts.scene.uncertainties]),
        visual_style_evidence=VisualStyleEvidence(
            medium=value(style.medium), character_design=value(style.character_design),
            rendering=value(style.rendering),
            color_palette=value(style.color_palette), texture=value(style.texture),
            visible_effects=value(style.visible_effects), evidence=list(dict.fromkeys(
                item for field in (style.medium, style.character_design, style.rendering,
                                   style.color_palette, style.texture, style.visible_effects)
                for item in descriptions(field))),
            uncertain_techniques=style.uncertain_techniques,
        ),
        raw_response=raw_response, analysis_warnings=v2.warnings,
    )
    if "analysis_review" in VisionObservation.model_fields:
        projection.analysis_review = observation_review(v2)
    return projection
