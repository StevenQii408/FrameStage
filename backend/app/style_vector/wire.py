"""Bounded local repair of style JSON; never invent missing evidence/verdicts."""
from __future__ import annotations

import json

from pydantic import ValidationError


def parse_style_response(raw, schema, *, array_key, trace):
    value = json.loads(raw)
    if isinstance(value, list):
        value = {array_key: value}
        trace["repairs"] = [f"wrapped-array:{array_key}"]
    if not isinstance(value, dict):
        raise ValueError("画风响应须为 JSON 对象或可明确归属的数组")
    fields = schema.model_fields
    unknown = set(value) - set(fields)
    if unknown:
        raise ValueError("画风响应含未知顶层字段：" + ",".join(sorted(unknown)))
    parsed = {}
    rejected = []
    for key, items in value.items():
        if not isinstance(items, list):
            raise ValueError(f"画风响应 {key} 必须为数组")
        # Validate each record against the original strict model so one bad
        # record cannot discard unrelated, valid observations.
        annotation = fields[key].annotation
        item_schema = annotation.__args__[0]
        limit = {"decisions": 20, "features": 24, "recipes": 3}[key]
        if len(items) > limit:
            raise ValueError(f"画风响应 {key} 超过 {limit} 项预算")
        parsed[key] = []
        for index, item in enumerate(items):
            try:
                parsed[key].append(item_schema.model_validate(item).model_dump())
            except ValidationError as exc:
                rejected.append({"field": key, "index": index,
                                 "errors": exc.errors(include_input=False, include_context=False, include_url=False)})
    trace["rejected_records"] = rejected
    if rejected and not any(parsed.values()):
        raise ValueError("画风响应没有通过校验的有效记录")
    return schema.model_validate(parsed)


def error_details(exc):
    if isinstance(exc, ValidationError):
        return exc.errors(include_input=False, include_context=False, include_url=False)
    return str(exc)[:500]
