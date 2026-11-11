import json
from functools import lru_cache
from pathlib import Path
from typing import Dict

from pydantic import BaseModel


class PromptProfile(BaseModel):
    id: str
    version: int
    platform: str
    prompt_type: str
    purpose: str
    required_contract_fields: list[str]
    forbid_invented_unknowns: bool = True
    first_frame_only: bool = False


@lru_cache(maxsize=1)
def profiles() -> Dict[str, PromptProfile]:
    path = Path(__file__).with_name("profiles.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    parsed = [PromptProfile.model_validate(item) for item in payload["profiles"]]
    return {item.id: item for item in parsed}


def profile(profile_id: str) -> PromptProfile:
    try:
        return profiles()[profile_id]
    except KeyError as exc:
        raise ValueError(f"未知 Prompt Profile：{profile_id}") from exc


@lru_cache(maxsize=1)
def style_rules() -> dict:
    """Reviewed local guidance, not arbitrary live Markdown ingestion."""
    payload = json.loads(Path(__file__).with_name("style-rules.v1.json").read_text(encoding="utf-8"))
    if payload.get("schema_version") != "style-rules.v1" or not payload.get("observation_guidance"):
        raise ValueError("画风规则版本或指导文本无效")
    if not isinstance(payload.get("sources"), list) or not payload["sources"]:
        raise ValueError("画风规则必须记录来源与固定摘要")
    return payload


@lru_cache(maxsize=1)
def action_rules() -> dict:
    """Versioned reviewed supplement, not automatic ingestion of user Markdown."""
    payload = json.loads(Path(__file__).with_name("action-rules.v1.json").read_text(encoding="utf-8"))
    if payload.get("schema_version") != "action-delivery.v1" or not payload.get("observation_guidance") or not payload.get("sources"):
        raise ValueError("动作交付规则版本、来源或指导文本无效")
    return payload
