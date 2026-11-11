"""Source-preserving screenplay document models.

Offsets are Python character offsets into the unchanged source, not byte offsets.
Metadata stays in the document even when it does not enter event coverage.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


SpanRole = Literal[
    "character_definition", "scene_heading", "action", "dialogue",
    "parenthetical", "voiceover", "sound", "note",
]
CONTENT_ROLES = frozenset({"action", "dialogue", "voiceover", "sound"})


class ScriptDocumentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScriptEntityReference(ScriptDocumentModel):
    """An exact source token and the evidence for its conservative identity."""
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)
    kind: Literal["mention", "pronoun"]
    character_id: Optional[str] = None
    group_character_ids: List[str] = Field(default_factory=list)
    candidate_character_ids: List[str] = Field(default_factory=list)
    source_span_ids: List[str] = Field(default_factory=list)
    method: Literal["explicit", "same-scene-context", "unknown"] = "unknown"


class ScriptAttributionAudit(ScriptDocumentModel):
    span_id: str
    character_id: Optional[str] = None
    speaker: str = "unknown"
    method: Literal["explicit", "same-scene-context", "unknown"]
    source_span_ids: List[str] = Field(default_factory=list)
    candidate_character_ids: List[str] = Field(default_factory=list)
    reason: str


class ScriptSpan(ScriptDocumentModel):
    id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = Field(min_length=1)
    role: SpanRole
    scene_id: Optional[str] = None
    character_id: Optional[str] = None
    speaker: Optional[str] = None
    parenthetical: Optional[str] = None
    source_refs: List[str] = Field(default_factory=list)
    entity_refs: List[ScriptEntityReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_offsets(self):
        if self.end <= self.start or self.end - self.start != len(self.text):
            raise ValueError("span offsets must match the exact source text length")
        return self


class ScriptCharacter(ScriptDocumentModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: List[str] = Field(default_factory=list)
    description: str = ""
    source_span_ids: List[str] = Field(default_factory=list)


class ScriptScene(ScriptDocumentModel):
    id: str = Field(min_length=1)
    heading: str = ""
    source_span_ids: List[str] = Field(default_factory=list)


class ScriptDocument(ScriptDocumentModel):
    schema_version: Literal["script-document.v1"] = "script-document.v1"
    source_text: str
    spans: List[ScriptSpan] = Field(default_factory=list)
    characters: List[ScriptCharacter] = Field(default_factory=list)
    scenes: List[ScriptScene] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    attribution_audit: List[ScriptAttributionAudit] = Field(default_factory=list)

    @property
    def content_spans(self) -> List[ScriptSpan]:
        return [span for span in self.spans if span.role in CONTENT_ROLES]

    @model_validator(mode="after")
    def valid_source_references(self):
        span_ids = {span.id for span in self.spans}
        character_ids = {character.id for character in self.characters}
        scene_ids = {scene.id for scene in self.scenes}
        if len(span_ids) != len(self.spans):
            raise ValueError("duplicate script span id")
        if len(character_ids) != len(self.characters) or len(scene_ids) != len(self.scenes):
            raise ValueError("duplicate character or scene id")
        previous_end = 0
        for span in self.spans:
            if span.start < previous_end or self.source_text[span.start:span.end] != span.text:
                raise ValueError("script spans must be ordered, non-overlapping, exact source slices")
            if self.source_text[previous_end:span.start].strip():
                raise ValueError("script spans must preserve every non-whitespace source character")
            if span.scene_id is not None and span.scene_id not in scene_ids:
                raise ValueError("script span references an unknown scene")
            if span.character_id is not None and span.character_id not in character_ids:
                raise ValueError("script span references an unknown character")
            if set(span.source_refs) - span_ids:
                raise ValueError("script span has unknown source references")
            for ref in span.entity_refs:
                if not span.start <= ref.start < ref.end <= span.end or self.source_text[ref.start:ref.end] != ref.text:
                    raise ValueError("entity references must be exact source tokens within their span")
                if ((ref.character_id and ref.character_id not in character_ids)
                        or set([*ref.candidate_character_ids, *ref.group_character_ids]) - character_ids):
                    raise ValueError("entity reference has an unknown character")
                if set(ref.source_span_ids) - span_ids:
                    raise ValueError("entity reference has unknown source references")
            previous_end = span.end
        if self.source_text[previous_end:].strip():
            raise ValueError("script spans must preserve every non-whitespace source character")
        for item in [*self.characters, *self.scenes]:
            if set(item.source_span_ids) - span_ids:
                raise ValueError("character or scene has unknown source references")
        for audit in self.attribution_audit:
            if audit.span_id not in span_ids or set(audit.source_span_ids) - span_ids:
                raise ValueError("attribution audit has unknown source references")
            if (audit.character_id and audit.character_id not in character_ids) or set(audit.candidate_character_ids) - character_ids:
                raise ValueError("attribution audit has an unknown character")
        return self
