"""Local read-only directing tools. No hidden network calls or plan mutation."""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from app.script_director.plan_models import ToolRequest


GRAMMAR = {
    "space": "需要交代新空间、人物位置、危险来源或逃离路线时考虑全景；不是每场强制大全景。",
    "dialogue": "按信息、权力与情绪变化切换，不按每句对白反打；说话时可保留听者反应或双人镜头。",
    "detail": "关键线索、决定或反应可用特写；物体出现不必自动特写。",
    "angle": "低角度可表现力量或压迫，也可属于人物视点；机位角度不得与俯仰运镜混淆。",
    "axis": "默认保持轴线、视线、左右位置与运动方向；跨轴须明确重新建立空间，不为规则堆叠无用镜头。",
    "action": "动作按因果顺序连续；首帧停在动作前，道具持有者与上一镜结束状态一致。",
    "sound": "对白、旁白与画外声音独立保存；枪声不是人物中枪或死亡的证据，不添加原文没有的射手或结局。",
    "timing": "时间为建议表演时长，不是原视频时间戳；对白和停顿决定预算，不按字数机械切镜。",
}


class ToolResult(BaseModel):
    name: str
    arguments: Dict[str, Any]
    result: Dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: float = 0
    error: str = ""


def estimate_dialogue_seconds(text: str) -> float:
    """Explicit planning heuristic, never described as measured speech/audio."""
    length = len(re.sub(r"[\s\W_]", "", text, flags=re.UNICODE))
    pauses = len(re.findall(r"[，。？！；…,.!?;]", text))
    return round(max(0.5, length / 4.0 + min(2.0, pauses * 0.18)), 2)


def split_dialogue_ranges(span, maximum_seconds=12.0):
    """Lossless ranges, preferring punctuation; not four equal time segments."""
    result, start = [], 0
    while start < len(span.text):
        end = start + 1
        while end <= len(span.text) and estimate_dialogue_seconds(span.text[start:end]) <= maximum_seconds:
            end += 1
        end = min(len(span.text), max(start + 1, end - 1))
        if end < len(span.text):
            boundary = max((pos + 1 for pos in range(start, end) if span.text[pos] in "，。！？；,.!?;"), default=end)
            if boundary > start:
                end = boundary
        result.append((start, end))
        start = end
    return result


class ScriptToolRegistry:
    def __init__(self, document, previous_state=None):
        self.document = document
        self.spans = {span.id: span for span in document.spans}
        self.characters = {character.id: character for character in document.characters}
        self.previous_state = previous_state or {}
        self.audit: List[dict] = []

    def run(self, request: ToolRequest) -> ToolResult:
        start = time.perf_counter()
        result = ToolResult(name=request.name, arguments=request.model_dump())
        try:
            if any(sid not in self.spans for sid in request.span_ids):
                raise ValueError("工具请求引用了不存在的原文片段")
            if any(cid not in self.characters for cid in request.character_ids):
                raise ValueError("工具请求引用了不存在的人物")
            if request.name == "lookup_film_grammar":
                ids = request.rule_ids or list(GRAMMAR)
                if any(rid not in GRAMMAR for rid in ids):
                    raise ValueError("未知电影语言规则")
                result.result = {"version": "film-grammar.v1", "rules": {rid: GRAMMAR[rid] for rid in ids}}
            elif request.name == "get_character_profile":
                result.result = {"characters": [self.characters[cid].model_dump() for cid in request.character_ids]}
            elif request.name == "get_scene_state":
                result.result = {"scenes": [scene.model_dump() for scene in self.document.scenes],
                    "previous_directed_state": self.previous_state,
                    "note": "原文场景与人物设定；尚未发生的道具交接不提前视为完成"}
            elif request.name == "get_source_span":
                result.result = {"spans": [self.spans[sid].model_dump() for sid in request.span_ids]}
            else:
                result.result = {"timing_origin": "estimated-performance", "characters_per_second": 4.0,
                    "estimates": [{"span_id": sid, "seconds": estimate_dialogue_seconds(self.spans[sid].text)} for sid in request.span_ids]}
        except (ValueError, KeyError) as exc:
            result.error = str(exc)
        result.elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
        self.audit.append(result.model_dump())
        return result
