from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Tuple

from app.schemas import ProviderHealth, ScriptBeat, ScriptPlanResult, VisionObservation


@dataclass
class FrameInput:
    id: str
    timestamp: float
    path: str
    width: int = 0
    height: int = 0
    artifact_kind: str = "keyframe"
    parent_frame_id: Optional[str] = None
    bbox: Optional[Tuple[float, float, float, float]] = None


@dataclass
class ShotContext:
    shot_id: str
    number: int
    start_time: float
    end_time: float
    script_text: str
    global_style: str = ""
    video_path: Optional[str] = None
    artifact_dir: Optional[str] = None
    neighbor_frames: List[FrameInput] = field(default_factory=list)
    register_frame: Optional[Callable[[FrameInput], None]] = field(default=None, repr=False)
    source_edit_times: List[float] = field(default_factory=list)
    report_perception_progress: Optional[Callable[[str, str], None]] = field(default=None, repr=False)


class ScriptPlanError(ValueError):
    def __init__(self, message: str, raw_responses: List[str]):
        super().__init__(message)
        self.raw_responses = raw_responses


class VisionProvider(Protocol):
    def plan_script_director(self, prompt: str, *, schema, max_tokens: int) -> str:
        ...

    def analyze_event_window(self, prompt, frames, *, phase, schema, timeout_seconds, max_tokens) -> str:
        ...

    def analyze_style(self, prompt, frames, *, phase, schema, timeout_seconds, max_tokens) -> str:
        ...

    def propose_style(self, prompt: str, frames: List[FrameInput]) -> str:
        ...

    def health(self) -> ProviderHealth:
        ...

    def analyze_shot(self, frames: List[FrameInput], context: ShotContext) -> VisionObservation:
        ...

    def summarize_video(self, frames: List[FrameInput]) -> str:
        ...

    def group_raw_shots(self, frames: List[FrameInput], protected_after: List[int]) -> List[List[int]]:
        ...

    def group_scenes(self, frames: List[FrameInput]) -> List[int]:
        ...

    def plan_script(self, beats: List[ScriptBeat], target_beat_ids: List[str]) -> ScriptPlanResult:
        ...

    def unload(self) -> None:
        ...
