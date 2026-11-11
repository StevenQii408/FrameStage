"""Bounded, typed local evidence tools. Tools never mutate analysis entities."""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.providers.base import FrameInput, ShotContext
from app.services.video_processor import _run, probe_video


class ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PerceptionBudget(ToolModel):
    max_steps: int = Field(default=6, ge=3, le=6)
    max_extra_frames: int = Field(default=6, ge=0, le=6)
    max_crops: int = Field(default=4, ge=0, le=4)
    max_supplemental_calls: int = Field(default=1, ge=0, le=1)
    max_corrections: int = Field(default=1, ge=0, le=1)


class FramesRequest(ToolModel):
    pass


class HighResolutionRequest(ToolModel):
    frame_id: str


class CropRequest(ToolModel):
    frame_id: str
    bbox: Tuple[float, float, float, float]

    @model_validator(mode="after")
    def valid_bbox(self):
        x1, y1, x2, y2 = self.bbox
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise ValueError("Normalized bbox is invalid")
        return self


class MotionSampleRequest(ToolModel):
    count: int = Field(default=2, ge=1, le=6)


class CompareRequest(ToolModel):
    first_frame_id: str
    last_frame_id: str


class ToolFrame(ToolModel):
    id: str
    timestamp: float
    width: int
    height: int
    artifact_kind: str
    parent_frame_id: Optional[str] = None
    bbox: Optional[Tuple[float, float, float, float]] = None


class Comparison(ToolModel):
    brightness_change: float
    mean_absolute_difference: float
    feature_flow_x: float
    feature_flow_y: float
    tracked_features: int
    note: str = "视觉指标仅作为补证据线索，不是摄影技法或动作事实"


class ToolResult(ToolModel):
    frames: List[ToolFrame] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    comparison: Optional[Comparison] = None


class ToolBudgetExceeded(RuntimeError):
    pass


class LocalToolRegistry:
    """Scoped to one shot; file inputs can only refer to registered frame IDs."""

    input_models = {
        "get_shot_frames": FramesRequest, "get_neighbor_shots": FramesRequest,
        "get_high_res_frame": HighResolutionRequest, "crop_frame": CropRequest,
        "sample_motion_peak_frames": MotionSampleRequest, "compare_frames": CompareRequest,
        "get_video_metadata": FramesRequest,
    }

    def __init__(self, context: ShotContext, frames: List[FrameInput], budget=None, deadline=None):
        self.context = context
        self.deadline = deadline
        self.budget = budget or PerceptionBudget()
        self.frames = {frame.id: frame for frame in frames}
        self.base_ids = list(self.frames)
        self.audit: List[Dict] = []
        self.extra_frames = 0
        self.crops = 0
        self.exposed_neighbors = set()

    def catalogue(self) -> List[FrameInput]:
        return list(self.frames.values())

    def _remaining(self):
        if self.deadline is None:
            return 30.0
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("本地工具单镜时间预算耗尽")
        return remaining

    def call(self, name: str, payload: Dict, *, step_allowance: int = 2) -> ToolResult:
        started = time.monotonic()
        entry = {"tool": name, "input": payload, "status": "failed", "output": None, "error": None}
        self.audit.append(entry)
        try:
            self._remaining()
            if len(self.audit) > min(step_allowance, self.budget.max_steps):
                raise ToolBudgetExceeded("工具执行步数超过本镜预算")
            if name not in self.input_models:
                raise ValueError("Unknown local tool")
            request = self.input_models[name].model_validate(payload)
            entry["input"] = request.model_dump()
            result = getattr(self, "_" + name)(request)
            self._remaining()
            entry["output"] = result.model_dump()
            entry["status"] = "success"
            return result
        except Exception as exc:
            entry["error"] = str(exc)
            raise
        finally:
            entry["elapsed_seconds"] = round(time.monotonic() - started, 4)

    def _reserve(self, count: int, crops=0):
        if self.extra_frames + count > self.budget.max_extra_frames or self.crops + crops > self.budget.max_crops:
            raise ToolBudgetExceeded("附加帧或裁剪预算已耗尽")

    def _frame(self, frame_id) -> FrameInput:
        if frame_id not in self.frames:
            raise ValueError("Frame ID is outside this tool context")
        return self.frames[frame_id]

    def _video(self) -> Path:
        path = Path(self.context.video_path or "")
        if not self.context.video_path or not path.is_file():
            raise ValueError("原视频不可用，不能获取原分辨率证据")
        return path

    def _output(self, kind):
        if not self.context.artifact_dir:
            raise ValueError("未配置持久化证据目录")
        root = Path(self.context.artifact_dir)
        root.mkdir(parents=True, exist_ok=True)
        artifact_id = f"{self.context.shot_id}-evidence-{kind}-{uuid.uuid4().hex[:10]}"
        return artifact_id, root / f"{artifact_id}.jpg"

    def _store(self, artifact_id, path, timestamp, kind, parent=None, bbox=None):
        with Image.open(path) as image:
            width, height = image.size
        frame = FrameInput(artifact_id, timestamp, str(path), width, height, kind, parent, bbox)
        if self.context.register_frame:
            self.context.register_frame(frame)
        self.frames[artifact_id] = frame
        self.extra_frames += 1
        self.crops += int(kind == "crop")
        return ToolFrame(**{key: getattr(frame, key) for key in ToolFrame.model_fields})

    def _describe(self, frame):
        width, height = frame.width, frame.height
        if not width or not height:
            with Image.open(frame.path) as image:
                width, height = image.size
        return ToolFrame(id=frame.id, timestamp=frame.timestamp, width=width, height=height,
                         artifact_kind=frame.artifact_kind, parent_frame_id=frame.parent_frame_id, bbox=frame.bbox)

    def _get_shot_frames(self, request):
        return ToolResult(frames=[self._describe(self.frames[item]) for item in self.base_ids])

    def _get_neighbor_shots(self, request):
        neighbors = self.context.neighbor_frames
        fresh = [frame for frame in neighbors if frame.id not in self.frames]
        self._reserve(len(fresh))
        for frame in fresh:
            self.frames[frame.id] = frame
        self.extra_frames += len(fresh)
        return ToolResult(frames=[self._describe(frame) for frame in neighbors],
                          metadata={"usage": "相邻镜头只作上下文，不作为本镜视觉事实"})

    def _get_high_res_frame(self, request):
        source = self._frame(request.frame_id)
        self._reserve(1)
        artifact_id, output = self._output("highres")
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{source.timestamp:.6f}",
              "-i", str(self._video()), "-frames:v", "1", "-q:v", "2", "-y", str(output)], timeout=self._remaining())
        return ToolResult(frames=[self._store(artifact_id, output, source.timestamp, "highres", source.id)])

    def _crop_frame(self, request):
        source = self._frame(request.frame_id)
        self._reserve(1, 1)
        artifact_id, output = self._output("crop")
        with Image.open(source.path) as image:
            x1, y1, x2, y2 = request.bbox
            box = (int(x1 * image.width), int(y1 * image.height),
                   min(image.width, max(int(x2 * image.width), int(x1 * image.width) + 1)),
                   min(image.height, max(int(y2 * image.height), int(y1 * image.height) + 1)))
            image.crop(box).convert("RGB").save(output, quality=92)
        return ToolResult(frames=[self._store(artifact_id, output, source.timestamp, "crop", source.id, request.bbox)])

    def _sample_motion_peak_frames(self, request):
        """Rank at most six probe frames; publish only the requested peaks."""
        import cv2
        import numpy as np

        video = self._video()
        self._reserve(request.count)
        capture = cv2.VideoCapture(str(video))
        candidates = []
        previous = None
        try:
            for index in range(6):
                self._remaining()
                timestamp = self.context.start_time + (self.context.end_time - self.context.start_time) * (index + 0.5) / 6
                capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
                ok, image = capture.read()
                if not ok:
                    continue
                gray = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (160, 90))
                score = float(np.mean(cv2.absdiff(gray, previous))) if previous is not None else 0
                candidates.append((score, timestamp))
                previous = gray
        finally:
            capture.release()
        peaks = sorted(sorted(candidates, reverse=True)[:request.count], key=lambda item: item[1])
        if len(peaks) != request.count:
            raise ValueError("运动采样不足，保留已有证据")
        result = []
        for score, timestamp in peaks:
            artifact_id, output = self._output("motion")
            _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.6f}",
                  "-i", str(video), "-frames:v", "1", "-vf",
                  "scale=w='min(768,iw)':h='min(768,ih)':force_original_aspect_ratio=decrease",
                  "-q:v", "3", "-y", str(output)], timeout=self._remaining())
            result.append(self._store(artifact_id, output, timestamp, "motion"))
        return ToolResult(frames=result, metadata={"ranking": "相邻缩略帧亮度绝对差；不代表语义动作边界"})

    def _compare_frames(self, request):
        import cv2
        import numpy as np

        frames = [self._frame(request.first_frame_id), self._frame(request.last_frame_id)]
        gray = []
        for frame in frames:
            image = cv2.imread(frame.path, cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise ValueError("无法读取比较帧")
            gray.append(cv2.resize(image, (320, 180)))
        features = cv2.goodFeaturesToTrack(gray[0], maxCorners=100, qualityLevel=0.02, minDistance=7)
        dx = dy = 0.0
        count = 0
        if features is not None:
            moved, status, _ = cv2.calcOpticalFlowPyrLK(gray[0], gray[1], features, None)
            if moved is not None and status is not None:
                delta = (moved - features)[status.reshape(-1) == 1].reshape(-1, 2)
                if len(delta):
                    dx, dy = map(float, np.median(delta, axis=0))
                    count = len(delta)
        return ToolResult(comparison=Comparison(
            brightness_change=float(gray[1].mean() - gray[0].mean()),
            mean_absolute_difference=float(np.mean(cv2.absdiff(*gray))),
            feature_flow_x=dx, feature_flow_y=dy, tracked_features=count,
        ))

    def _get_video_metadata(self, request):
        return ToolResult(metadata=probe_video(self._video(), timeout=self._remaining()))
