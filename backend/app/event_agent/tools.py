"""Window-scoped evidence tools. No model-selected filesystem paths or mutations."""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from pydantic import Field, model_validator

from app.schemas import FrameArtifact
from app.services.video_processor import _frame_metrics
from app.services.visual_tools import CompareRequest, CropRequest, ToolModel


class BoundaryRequest(ToolModel):
    timestamp: float = Field(ge=0)
    radius: float = Field(default=0.3, gt=0, le=1)


class IntervalRequest(ToolModel):
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    max_frames: int = Field(default=3, ge=1, le=6)

    @model_validator(mode="after")
    def interval(self):
        if self.end_time <= self.start_time or self.end_time - self.start_time > 16:
            raise ValueError("取证区间必须递增且不超过16秒")
        return self


class NeighborRequest(ToolModel):
    pass


class EventWindowToolRegistry:
    input_models = {
        "inspect_boundary": BoundaryRequest,
        "sample_interval_frames": IntervalRequest,
        "compare_frames": CompareRequest,
        "crop_frame": CropRequest,
        "get_neighbor_context": NeighborRequest,
    }

    def __init__(self, window, shots, frames, *, video_path, artifact_dir,
                 max_extra_frames=6, deadline=None, register_frame: Optional[Callable] = None):
        self.window = window
        self.shots = sorted(shots, key=lambda shot: shot.start_time)
        self.scope_start = getattr(window, "context_start_time", None)
        self.scope_start = window.start_time if self.scope_start is None else self.scope_start
        self.scope_ids = set(window.source_shot_ids) | set(getattr(window, "context_shot_ids", []))
        self.frames = {frame.id: frame for frame in frames
                       if self.scope_start <= frame.timestamp < window.end_time
                       and frame.shot_id in self.scope_ids}
        self.video_path = Path(video_path) if video_path else None
        self.artifact_dir = Path(artifact_dir)
        self.max_extra_frames = min(6, max(0, max_extra_frames))
        self.deadline = deadline
        self.register_frame = register_frame
        self.extra_frames = 0
        self.calls = 0

    def _remaining(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError("事件窗口取证时间预算耗尽")

    def _reserve(self, count):
        if self.extra_frames + count > self.max_extra_frames:
            raise ValueError("事件窗口附加图片预算耗尽")

    def _interval(self, start, end):
        if not (self.scope_start <= start < end <= self.window.end_time):
            raise ValueError("取证区间超出当前事件窗口")

    def _frame(self, frame_id):
        if frame_id not in self.frames:
            raise ValueError("帧ID不属于当前事件窗口")
        return self.frames[frame_id]

    def call(self, request):
        from app.event_agent.models import ToolResult

        started = time.monotonic()
        self.calls += 1  # Failed tools also consume the allowance.
        try:
            self._remaining()
            if self.calls > 2:
                raise ValueError("事件窗口工具调用预算耗尽")
            if request.name not in self.input_models:
                raise ValueError("工具不在允许列表中")
            args = self.input_models[request.name].model_validate(request.arguments)
            output, frames = getattr(self, "_" + request.name)(args)
            self._remaining()
            return ToolResult(name=request.name, status="success", output=output,
                              frames=frames, elapsed_seconds=time.monotonic() - started)
        except Exception as exc:
            return ToolResult(name=request.name, status="error", output={}, frames=[],
                              elapsed_seconds=time.monotonic() - started, error=str(exc))

    def _decode(self, timestamps):
        import cv2

        if not self.video_path or not self.video_path.is_file():
            raise ValueError("来源视频不可用")
        capture = cv2.VideoCapture(str(self.video_path))
        result = []
        try:
            if not capture.isOpened():
                raise ValueError("不能解码来源视频")
            for timestamp in timestamps:
                self._remaining()
                if not self.scope_start <= timestamp < self.window.end_time:
                    raise ValueError("取证时间超出当前事件窗口")
                capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
                ok, image = capture.read()
                if not ok:
                    raise ValueError("请求时间附近没有可解码帧")
                actual = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
                # Some backends return an unavailable/zero PTS. Do not claim it
                # is an exact timestamp: the output keeps its timestamp basis.
                basis = "decoder-pts"
                if not (self.scope_start <= actual < self.window.end_time) or (actual == 0 and timestamp > 0.1):
                    raise ValueError("解码器未返回有效时间戳，不能发布虚假的精确证据")
                result.append((actual, image, basis))
            return result
        finally:
            capture.release()

    def _store(self, image, timestamp, kind, *, parent=None, bbox=None):
        import cv2

        self._reserve(1)
        owner = next((shot for shot in self.shots if shot.id in self.scope_ids
                      and shot.start_time <= timestamp < shot.end_time), None)
        if owner is None:
            raise ValueError("证据时间不属于任何当前来源镜头")
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_id = f"{owner.id}-agent-{kind}-{uuid.uuid4().hex[:12]}"
        path = self.artifact_dir / f"{artifact_id}.jpg"
        height, width = image.shape[:2]
        scale = min(1.0, 768 / max(width, height))
        if scale < 1:
            image = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))))
        if not cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 90]):
            raise ValueError("不能保存补充证据帧")
        frame = FrameArtifact(id=artifact_id, shot_id=owner.id, timestamp=timestamp,
                              path=str(path), thumbnail_path=str(path),
                              width=image.shape[1], height=image.shape[0],
                              artifact_kind=f"event-agent-{kind}", parent_frame_id=parent,
                              bbox=list(bbox) if bbox else None, selected=False)
        self.extra_frames += 1
        self.frames[frame.id] = frame
        if self.register_frame:
            self.register_frame(frame)
        return frame

    def _sample_interval_frames(self, request):
        self._interval(request.start_time, request.end_time)
        self._reserve(request.max_frames)
        timestamps = [request.start_time + (request.end_time - request.start_time) * (i + 0.5) / request.max_frames
                      for i in range(request.max_frames)]
        decoded = self._decode(timestamps)
        frames = [self._store(image, actual, "sample") for actual, image, _ in decoded]
        return {"timestamp_basis": "decoder-pts", "interval": [request.start_time, request.end_time]}, frames

    def _inspect_boundary(self, request):
        import cv2

        left = max(self.scope_start, request.timestamp - request.radius)
        right = min(self.window.end_time, request.timestamp + request.radius)
        self._interval(left, right)
        if not left < request.timestamp < right:
            raise ValueError("边界必须位于窗口内部")
        self._reserve(2)
        offsets = (-1, -0.5, -1 / 6, 1 / 6, 0.5, 1)
        timestamps = [max(left, min(right - 1e-6, request.timestamp + request.radius * offset)) for offset in offsets]
        decoded = self._decode(timestamps)
        curve = [{"first_time": a[0], "last_time": b[0], **_frame_metrics(a[1], b[1])}
                 for a, b in zip(decoded, decoded[1:])]
        frames = [self._store(decoded[i][1], decoded[i][0], "boundary") for i in (2, 3)]
        brightness = [float(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).mean()) for _, image, _ in decoded]
        return {"metrics_version": "boundary-evidence.v1", "timestamp_basis": "decoder-pts",
                "curve": curve, "brightness": brightness,
                "note": "指标只提示视觉变化；global SSIM近似、HSV、pHash及边缘变化均不证明叙事事件边界"}, frames

    def _compare_frames(self, request):
        import cv2
        import numpy as np

        frames = [self._frame(request.first_frame_id), self._frame(request.last_frame_id)]
        if frames[0].id == frames[1].id or abs(frames[0].timestamp - frames[1].timestamp) < 1e-6:
            raise ValueError("比较需要不同时间的两张帧；同一帧或同时间裁剪不提供动作变化证据")
        gray = []
        for frame in frames:
            image = cv2.imread(frame.path, cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise ValueError("证据图片不可读取")
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
        return {"first_frame_id": frames[0].id, "last_frame_id": frames[1].id,
                "brightness_change": float(gray[1].mean() - gray[0].mean()),
                "mean_absolute_difference": float(np.mean(cv2.absdiff(*gray))),
                "feature_flow_x": dx, "feature_flow_y": dy, "tracked_features": count,
                "note": "稀疏光流仅作为局部位移线索，不能证明动作身份或运镜类型"}, []

    def _crop_frame(self, request):
        import cv2

        parent = self._frame(request.frame_id)
        self._reserve(1)
        # Prefer original resolution; a crop is evidence, not an invented frame.
        decoded = self._decode([parent.timestamp])
        actual, image, _ = decoded[0]
        x1, y1, x2, y2 = request.bbox
        height, width = image.shape[:2]
        crop = image[int(y1 * height):max(int(y1 * height) + 1, int(y2 * height)),
                     int(x1 * width):max(int(x1 * width) + 1, int(x2 * width))]
        if crop.size == 0:
            raise ValueError("裁剪区域为空")
        frame = self._store(crop, actual, "crop", parent=parent.id, bbox=request.bbox)
        return {"parent_frame_id": parent.id, "bbox": list(request.bbox), "timestamp_basis": "decoder-pts"}, [frame]

    def _get_neighbor_context(self, request):
        entries = []
        for shot in self.shots:
            if shot.id not in self.scope_ids:
                continue
            entries.append({"shot_id": shot.id, "start_time": shot.start_time, "end_time": shot.end_time,
                            "scene_group_id": shot.scene_group_id, "subject_action": shot.action,
                            "initial_state": shot.action_details.initial_state,
                            "final_state": shot.action_details.final_state,
                            "frame_ids": [frame.id for frame in self.frames.values() if frame.shot_id == shot.id]})
        return {"shots": entries, "note": "已保存的来源观察是上下文；缺失字段仍为未知"}, []
