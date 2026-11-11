"""Offline, independently authored storyboard fixtures and honest score limits.

No provider, credentials, database, network client or cache is imported here.
The corpus contains authored script and synthetic state fixtures, not human
adjudicated real-video truth. All hashes cover canonical UTF-8 JSON.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Optional


CORPUS_PATH = Path(__file__).with_name("storyboard_reliability.v1.json")
CORPUS_VERSION = "storyboard-reliability-corpus.v1"
RESPONSES_VERSION = "storyboard-reliability-responses.v1"
PREDICTION_VERSION = "storyboard-reliability-prediction.v1"
CONTENT_ROLES = {"action", "dialogue", "voiceover", "sound"}
RELATIONS = {"same_event", "different_event", "allowed_alternative"}


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def source_hash(case: dict) -> str:
    source = ({"source_text": case["source_text"]} if case["kind"] == "script" else
              {"source_units": case["source_units"], "frames": case["frames"]})
    return content_hash(source)


def _anchor(source: str, text: str, occurrence: Optional[int] = None) -> tuple[int, int]:
    """Find an explicitly authored exact anchor; never run the tested parser."""
    if not text or (occurrence is not None and (not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 0)):
        raise ValueError("source anchor requires nonempty text and a nonnegative occurrence")
    start = -1
    for _ in range((occurrence or 0) + 1):
        start = source.find(text, start + 1)
        if start < 0:
            raise ValueError("authored source anchor not present in source")
    if occurrence is None and source.find(text, start + 1) >= 0:
        raise ValueError("repeated source anchor needs an explicit occurrence")
    return start, start + len(text)


def authored_units(case: dict) -> list[dict]:
    if case["kind"] == "video":
        return [dict(unit) for unit in case["source_units"]]
    result = []
    for unit in case["content_units"]:
        start, end = _anchor(case["source_text"], unit["text"], unit.get("occurrence"))
        result.append({**unit, "start": start, "end": end})
    return result


def authored_coreferences(case: dict) -> list[dict]:
    result = []
    for item in case.get("coreferences", []):
        left, _ = _anchor(case["source_text"], item["context"], item.get("context_occurrence"))
        start, end = _anchor(item["context"], item["mention"], item.get("mention_occurrence"))
        result.append({**item, "start": left + start, "end": left + end})
    return result


def validate_corpus(corpus: dict) -> dict:
    if corpus.get("schema_version") != CORPUS_VERSION:
        raise ValueError("unsupported reliability corpus schema")
    if not isinstance(corpus.get("cases"), list) or not corpus["cases"]:
        raise ValueError("corpus must contain authored cases")
    seen_cases = set()
    for case in corpus["cases"]:
        cid = case.get("id")
        if not cid or cid in seen_cases:
            raise ValueError("case IDs must be nonempty and unique")
        seen_cases.add(cid)
        if case.get("kind") not in {"script", "video"}:
            raise ValueError("case kind must be script or video")
        provenance = case.get("provenance", {})
        if provenance.get("generated_by_tested_pipeline") is not False:
            raise ValueError("tested-pipeline/model output cannot be promoted into gold")
        if not provenance.get("author") or not provenance.get("source_license"):
            raise ValueError("source provenance must identify author and license status")
        if provenance.get("human_review_status") not in {"pending", "reviewed"}:
            raise ValueError("human review status must be explicit")
        units = authored_units(case)
        ids = [unit["id"] for unit in units]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("source unit IDs must be unique and nonempty")
        if case["kind"] == "script":
            characters = {character["id"] for character in case.get("characters", [])}
            previous_end = 0
            for unit in units:
                if unit["start"] < previous_end or unit["role"] not in CONTENT_ROLES:
                    raise ValueError("script content anchors must be ordered and nonoverlapping")
                if unit.get("speaker") is not None and unit["speaker"] not in characters:
                    raise ValueError("gold speaker must reference a declared character")
                if set(unit.get("character_ids", [])) - characters:
                    raise ValueError("gold character attribution must be declared")
                previous_end = unit["end"]
            for item in authored_coreferences(case):
                if item["status"] not in {"resolved", "unresolved", "group"}:
                    raise ValueError("unsupported gold coreference status")
                if item.get("character_id") is not None and item["character_id"] not in characters:
                    raise ValueError("gold coreference references undeclared character")
                if (item["status"] == "resolved") != (item.get("character_id") is not None):
                    raise ValueError("resolved coreference needs one character; ambiguous/group needs none")
            for text in case.get("excluded_source", []):
                _anchor(case["source_text"], text)
        else:
            kind = provenance.get("kind")
            if kind not in {"synthetic-transition-fixture", "human-inspected-real-video"}:
                raise ValueError("video fixture must explicitly distinguish synthetic and real")
            if kind == "synthetic-transition-fixture" and provenance.get("real_video") is not False:
                raise ValueError("synthetic fixture cannot claim real video")
            if kind == "human-inspected-real-video" and (
                provenance.get("human_review_status") != "reviewed" or
                not provenance.get("human_reviewer") or not provenance.get("media_sha256") or
                not provenance.get("authorization_record")
            ):
                raise ValueError("real-video truth requires human reviewer, media hash and authorization record")
            frame_map = {frame["id"]: frame for frame in case.get("frames", [])}
            if not frame_map or len(frame_map) != len(case["frames"]):
                raise ValueError("video frame IDs must be unique and nonempty")
            unit_map = {unit["id"]: unit for unit in units}
            for unit in units:
                if not 0 <= unit["start_time"] < unit["end_time"]:
                    raise ValueError("invalid source time range")
            for frame in frame_map.values():
                unit = unit_map.get(frame["source_id"])
                if unit is None or not unit["start_time"] <= frame["timestamp"] < unit["end_time"]:
                    raise ValueError("frame timestamp/source must lie inside its source range")
            for assertion in case.get("required_facts", []):
                if not assertion.get("frame_ids") or set(assertion["frame_ids"]) - set(frame_map):
                    raise ValueError("required facts need known frame evidence")
                if not assertion.get("source_ids") or set(assertion["source_ids"]) - set(ids):
                    raise ValueError("required facts need known source attribution")
                if any(frame_map[fid]["source_id"] not in assertion["source_ids"] for fid in assertion["frame_ids"]):
                    raise ValueError("fact frame must belong to an attributed source")
        seen_pairs = set()
        for relation in case.get("event_constraints", []):
            left, right = relation["left"], relation["right"]
            pair = frozenset((left, right))
            if left == right or {left, right} - set(ids) or pair in seen_pairs:
                raise ValueError("event constraints must use known distinct source pairs once")
            if relation["relation"] not in RELATIONS or not relation.get("reason"):
                raise ValueError("event constraint needs an authored relation and reason")
            seen_pairs.add(pair)
        # Required relations must admit at least one valid partition. Do not
        # silently publish contradictory independent author assertions.
        parents = {sid: sid for sid in ids}
        def owner(sid):
            while parents[sid] != sid:
                sid = parents[sid]
            return sid
        for relation in case.get("event_constraints", []):
            if relation["relation"] == "same_event":
                parents[owner(relation["left"])] = owner(relation["right"])
        for relation in case.get("event_constraints", []):
            if relation["relation"] == "different_event" and owner(relation["left"]) == owner(relation["right"]):
                raise ValueError("same/different event constraints contradict each other")
    return corpus


def load_corpus(path: Path = CORPUS_PATH) -> dict:
    return validate_corpus(json.loads(Path(path).read_text(encoding="utf-8")))


def corpus_inventory(corpus: dict) -> dict:
    counts = Counter(case["kind"] for case in corpus["cases"])
    synthetic = sum(case["provenance"]["kind"] == "synthetic-transition-fixture" for case in corpus["cases"])
    real = sum(case["provenance"]["kind"] == "human-inspected-real-video" for case in corpus["cases"])
    return {
        "corpus_id": corpus["corpus_id"], "corpus_sha256": content_hash(corpus),
        "case_count": len(corpus["cases"]), "authored_script_cases": counts["script"],
        "synthetic_video_cases": synthetic, "human_inspected_real_video_cases": real,
        "human_review_pending_authored_fixtures": sum(case["provenance"]["human_review_status"] == "pending" for case in corpus["cases"]),
        "manual_pending": dict(corpus.get("inventory", {})),
        "annotation_policy": dict(corpus["annotation_policy"]),
    }


def _rate(passed: int, total: int) -> Optional[float]:
    return passed / total if total else None


def score_events(case: dict, events: Optional[list]) -> dict:
    """Only required relations count; allowed boundaries never change the score."""
    ids = [unit["id"] for unit in authored_units(case)]
    groups = {relation: {"expected": 0, "passed": 0, "missing_or_ambiguous": 0, "violations": []}
              for relation in ("same_event", "different_event")}
    alternatives = []
    assignments: dict[str, list[int]] = {}
    for index, event in enumerate(events or []):
        for sid in event.get("source_ids", []):
            assignments.setdefault(sid, []).append(index)
    for item in case.get("event_constraints", []):
        left, right = assignments.get(item["left"], []), assignments.get(item["right"], [])
        known = len(left) == len(right) == 1
        actual = ("same_event" if left == right else "different_event") if known else "missing_or_ambiguous"
        if item["relation"] == "allowed_alternative":
            alternatives.append({"left": item["left"], "right": item["right"], "observed": actual,
                                 "accepted": known, "scored": False})
            continue
        group = groups[item["relation"]]
        group["expected"] += 1
        if known and actual == item["relation"]:
            group["passed"] += 1
        else:
            group["missing_or_ambiguous"] += int(not known)
            group["violations"].append({"left": item["left"], "right": item["right"],
                                        "observed": actual, "reason": item["reason"]})
    for group in groups.values():
        group["accuracy"] = _rate(group["passed"], group["expected"]) if events is not None else None
    flattened = [sid for event in events or [] for sid in event.get("source_ids", [])]
    unique_known = sum(len(assignments.get(sid, [])) == 1 for sid in ids)
    return {
        "available": events is not None, "event_count_diagnostic": len(events) if events is not None else None,
        "source_coverage": _rate(unique_known, len(ids)) if events is not None else None,
        "source_ownership_complete_unique": flattened == ids if events is not None else None,
        "unknown_source_ids": sorted(set(flattened) - set(ids)),
        "duplicate_source_ids": sorted(sid for sid, owners in assignments.items() if len(owners) > 1),
        "mandatory_same_event": groups["same_event"], "mandatory_different_event": groups["different_event"],
        "allowed_alternatives": alternatives,
        "shot_count_scored": False,
        "limit": "Event constraints express authored goals. Generated shot count is not a quality target.",
    }


def _characters(case: dict, document: dict) -> dict:
    canonical = {name: character["id"] for character in case.get("characters", [])
                 for name in [character["id"], *character.get("aliases", [])]}
    result = dict(canonical)
    for character in document.get("characters", []):
        result[character["id"]] = canonical.get(character.get("name"), character.get("name"))
    return result


def normalize_prediction(case: dict, payload: dict) -> dict:
    """Adapt saved public results/documents; absent evidence stays unavailable."""
    if payload.get("schema_version") == PREDICTION_VERSION:
        return dict(payload)
    if isinstance(payload.get("result"), dict):
        payload = {**payload["result"], **{key: payload[key] for key in ("document", "script_document", "decision") if key in payload}}
    document = payload.get("document") or payload.get("script_document")
    if payload.get("schema_version") == "script-document.v1":
        document = payload
    normalized: dict[str, Any] = {"schema_version": PREDICTION_VERSION, "events": None,
                                  "claims": [], "source_units": None, "coreferences": None,
                                  "adaptation_notes": []}
    source_mapping: dict[str, list[str]] = {}
    if case["kind"] == "script" and isinstance(document, dict):
        character_map = _characters(case, document)
        units = authored_units(case)
        normalized["source_text"] = document.get("source_text")
        normalized["source_units"] = []
        normalized["coreferences"] = []
        normalized["character_profiles"] = [{**character, "source_character_id": character["id"],
                                                "id": character_map.get(character["id"])}
                                               for character in document.get("characters", [])]
        for span in document.get("spans", []):
            source_mapping[span["id"]] = [unit["id"] for unit in units
                                         if span["start"] < unit["end"] and unit["start"] < span["end"]]
            if span.get("role") in CONTENT_ROLES:
                resolved = {character_map.get(span.get("character_id"))} - {None}
                resolved.update(character_map.get(ref.get("character_id")) for ref in span.get("entity_refs", [])
                                if ref.get("character_id"))
                resolved.update(character_map.get(cid, cid) for ref in span.get("entity_refs", [])
                                for cid in ref.get("group_character_ids", []))
                speaker = character_map.get(span.get("character_id"))
                normalized["source_units"].append({"id": span["id"], "start": span["start"], "end": span["end"],
                    "text": span["text"], "role": span["role"], "speaker": speaker,
                    "character_ids": sorted(resolved)})
            for ref in span.get("entity_refs", []):
                group = [character_map.get(cid, cid) for cid in ref.get("group_character_ids", [])]
                normalized["coreferences"].append({"start": ref["start"], "end": ref["end"], "text": ref["text"],
                    "character_id": character_map.get(ref.get("character_id")),
                    "group_character_ids": group,
                    "candidate_character_ids": [character_map.get(cid, cid) for cid in ref.get("candidate_character_ids", [])],
                    "source_span_ids": list(ref.get("source_span_ids", [])), "method": ref.get("method", "unknown"),
                    "status": "group" if group else "resolved" if ref.get("character_id") else "unresolved"})
        if not any("entity_refs" in span for span in document.get("spans", [])):
            normalized["coreferences"] = None
            normalized["adaptation_notes"].append("Saved document has no coreference audit; unavailable, not a pass.")
    raw_events = (payload.get("decision", {}).get("events") if isinstance(payload.get("decision"), dict) else None)
    if raw_events is None:
        raw_events = payload.get("events", payload.get("narrative_events"))
    if isinstance(raw_events, list):
        normalized["events"] = []
        for event in raw_events:
            sids = event.get("source_ids", event.get("source_span_ids", event.get("source_shot_ids", [])))
            if case["kind"] == "script" and source_mapping:
                sids = list(dict.fromkeys(uid for sid in sids for uid in source_mapping.get(sid, [sid])))
            normalized["events"].append({"source_ids": sids, "title": event.get("title", ""), "goal": event.get("goal", "")})
            for assertion in event.get("causal_facts", []):
                normalized["claims"].append(dict(assertion))
            for shot in event.get("shots", []):
                normalized["claims"].extend({"text": text} for text in shot.get("action_steps", []))
    if isinstance(payload.get("claims"), list):
        normalized["claims"].extend(payload["claims"])
    if isinstance(payload.get("coreferences"), list):
        normalized["coreferences"] = payload["coreferences"]
    if payload.get("schema_version") == "storyboard.v1" and case["kind"] == "script" and not source_mapping:
        normalized["adaptation_notes"].append("Public storyboard has no saved script document; source/speaker/coreference scores require a saved document, never a reparse of the gold source.")
    return normalized


def score_script(case: dict, prediction: dict) -> dict:
    units = authored_units(case)
    predicted = prediction.get("source_units")
    if predicted is None:
        return {"available": False, "reason": "No saved source document/attribution supplied; parser output was not reconstructed from gold."}
    source = case["source_text"]
    slice_valid = []
    predicted_positions = set()
    for unit in predicted:
        start, end = unit.get("start"), unit.get("end")
        valid = (isinstance(start, int) and not isinstance(start, bool) and isinstance(end, int) and
                 not isinstance(end, bool) and 0 <= start < end <= len(source) and source[start:end] == unit.get("text"))
        slice_valid.append(valid)
        if valid:
            predicted_positions.update(index for index in range(start, end) if not source[index].isspace())
    expected_positions = {index for unit in units for index in range(unit["start"], unit["end"]) if not source[index].isspace()}
    excluded_positions = set()
    for text in case.get("excluded_source", []):
        start, end = _anchor(source, text)
        excluded_positions.update(range(start, end))
    role_correct = speaker_correct = speaker_total = characters_correct = 0
    details = []
    for expected in units:
        overlapping = [unit for unit, valid in zip(predicted, slice_valid) if valid and
                       unit["start"] < expected["end"] and expected["start"] < unit["end"]]
        role_ok = bool(overlapping) and all(unit.get("role") == expected["role"] for unit in overlapping)
        role_correct += role_ok
        actual_characters = {cid for unit in overlapping for cid in unit.get("character_ids", [])}
        characters_correct += bool(overlapping) and actual_characters == set(expected.get("character_ids", []))
        item = {"source_id": expected["id"], "role_correct": role_ok,
                "character_set_correct": bool(overlapping) and actual_characters == set(expected.get("character_ids", []))}
        if expected["role"] in {"dialogue", "voiceover"}:
            speaker_total += 1
            speaker_ok = bool(overlapping) and all(unit.get("speaker") == expected.get("speaker") for unit in overlapping)
            speaker_correct += speaker_ok
            item.update(speaker_correct=speaker_ok, expected_speaker=expected.get("speaker"),
                        actual_speakers=[unit.get("speaker") for unit in overlapping])
        details.append(item)
    references = prediction.get("coreferences")
    correct = resolved_total = unresolved_total = wrong_resolution = 0
    coreference_details = []
    for item in authored_coreferences(case):
        matches = [ref for ref in references or [] if ref.get("start") == item["start"] and ref.get("end") == item["end"]]
        resolved_total += item["status"] == "resolved"
        unresolved_total += item["status"] != "resolved"
        ok = len(matches) == 1 and matches[0].get("character_id") == item.get("character_id")
        correct += ok
        wrong_resolution += item.get("character_id") is None and any(ref.get("character_id") is not None for ref in matches)
        coreference_details.append({"start": item["start"], "end": item["end"], "expected_character": item.get("character_id"),
                                    "matched": ok if references is not None else None})
    return {
        "available": True, "source_text_unchanged": prediction.get("source_text") == source,
        "exact_source_slice_rate": _rate(sum(slice_valid), len(slice_valid)),
        "content_character_recall": _rate(len(expected_positions & predicted_positions), len(expected_positions)),
        "metadata_content_character_leak": len(excluded_positions & predicted_positions),
        "role_accuracy": _rate(role_correct, len(units)), "speaker_accuracy": _rate(speaker_correct, speaker_total),
        "speaker_expected": speaker_total, "speaker_correct": speaker_correct,
        "character_set_accuracy": _rate(characters_correct, len(units)), "details": details,
        "coreference": {"available": references is not None, "expected": resolved_total + unresolved_total,
            "resolved_expected": resolved_total, "ambiguous_or_group_expected": unresolved_total,
            "correct": correct if references is not None else None,
            "accuracy": _rate(correct, resolved_total + unresolved_total) if references is not None else None,
            "unsafe_ambiguous_resolutions": wrong_resolution if references is not None else None,
            "details": coreference_details},
        "limit": "Exact source and declared identity assertions test authored fixtures; they do not establish general speaker/coreference accuracy.",
    }


def _atom(assertion: dict) -> Optional[tuple]:
    if all(isinstance(assertion.get(key), str) and assertion[key] for key in ("subject", "predicate", "object")) and isinstance(assertion.get("polarity", True), bool):
        return assertion["subject"], assertion["predicate"], assertion["object"], assertion.get("polarity", True)
    return None


def _evidence(assertion: dict, frame_map: dict) -> tuple[set, list]:
    refs = assertion.get("evidence", assertion.get("frame_ids", []))
    ids, invalid = set(), []
    for ref in refs:
        if not isinstance(ref, (str, dict)):
            invalid.append("invalid-evidence-shape")
            continue
        fid = ref if isinstance(ref, str) else ref.get("frame_id", ref.get("id"))
        if not isinstance(fid, str):
            invalid.append("invalid-frame-id")
            continue
        frame = frame_map.get(fid)
        if frame is None:
            invalid.append(fid)
            continue
        ids.add(fid)
        if isinstance(ref, dict) and "timestamp" in ref:
            timestamp = ref["timestamp"]
            if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool) or not abs(timestamp - frame["timestamp"]) <= .001:
                invalid.append(fid)
    return ids, invalid


def _source_evidence_errors(assertion: dict, frame_map: dict, source_ids: set) -> list:
    sources = set(assertion.get("source_ids", assertion.get("source_shot_ids", [])))
    refs, _ = _evidence(assertion, frame_map)
    return sorted((sources - source_ids) | {frame_map[fid]["source_id"] for fid in refs
                                           if frame_map[fid]["source_id"] not in sources})


def score_facts(case: dict, prediction: dict) -> dict:
    claims = prediction.get("claims", [])
    frame_map = {frame["id"]: frame for frame in case.get("frames", [])}
    source_ids = {unit["id"] for unit in authored_units(case)}
    typed = [claim for claim in claims if _atom(claim) is not None]
    texts = [claim.get("text", "") for claim in claims]
    matched = evidence_supported = critical_supported = critical_total = literal_matched = 0
    details = []
    for expected in case.get("required_facts", []):
        matches = [claim for claim in typed if _atom(claim) == _atom(expected)]
        literal = any(expected["text"] == text or expected["text"] in text for text in texts)
        literal_matched += literal
        matched += bool(matches)
        supported = False
        invalid_refs = []
        for claim in matches:
            refs, invalid = _evidence(claim, frame_map)
            invalid_refs.extend(invalid)
            sources = set(claim.get("source_ids", claim.get("source_shot_ids", [])))
            source_errors = _source_evidence_errors(claim, frame_map, source_ids)
            supported |= (not invalid and not source_errors and set(expected["frame_ids"]) <= refs and set(expected["source_ids"]) <= sources)
        evidence_supported += supported
        critical_total += bool(expected.get("critical", True))
        critical_supported += supported and expected.get("critical", True)
        details.append({"fact_id": expected["id"], "atom_matched": bool(matches), "literal_proxy_matched": literal,
                        "required_frame_ids": expected["frame_ids"], "evidence_supported": supported,
                        "invalid_reference_ids": invalid_refs})
    forbidden_atoms = { _atom(item): item for item in case.get("forbidden_claims", []) if _atom(item) is not None }
    forbidden_typed = [forbidden_atoms[_atom(claim)]["id"] for claim in typed if _atom(claim) in forbidden_atoms]
    phrase_hits = []
    for item in case.get("forbidden_claims", []):
        found = [phrase for phrase in item.get("phrases", []) if any(phrase in text for text in texts)]
        if found:
            phrase_hits.append({"id": item["id"], "phrases": found})
    known = {_atom(fact) for fact in case.get("required_facts", [])}
    invalid_refs = [fid for claim in claims for fid in _evidence(claim, frame_map)[1]] if frame_map else []
    invalid_sources = [sid for claim in claims for sid in _source_evidence_errors(claim, frame_map, source_ids)] if frame_map else []
    unverified = sum(_atom(claim) not in known and _atom(claim) not in forbidden_atoms for claim in typed)
    expected_count = len(case.get("required_facts", []))
    return {
        "required": expected_count, "typed_claims": len(typed), "untyped_claims": len(claims) - len(typed),
        "authored_atom_recall": _rate(matched, expected_count) if typed else None,
        "authored_atom_precision": _rate(sum(_atom(claim) in known for claim in typed), len(typed)),
        "atom_with_required_frame_and_source_recall": _rate(evidence_supported, expected_count) if typed else None,
        "critical_evidence_recall": _rate(critical_supported, critical_total) if typed else None,
        "literal_fact_coverage_proxy": _rate(literal_matched, expected_count),
        "forbidden_atom_ids": sorted(set(forbidden_typed)), "forbidden_phrase_proxy_hits": phrase_hits,
        "unverified_typed_claims": unverified, "invalid_evidence_reference_ids": invalid_refs,
        "invalid_source_evidence_reference_ids": invalid_sources,
        "claims_without_frame_evidence": sum(not claim.get("evidence", claim.get("frame_ids", [])) for claim in claims) if frame_map else None,
        "details": details,
        "semantic_acceptance": "not-established",
        "manual_semantic_review_required": True,
        "limit": "Typed atoms score only authored fixture assertions. Literal phrases are a proxy; valid frame IDs/timestamps do not prove visual meaning. Real-video hallucination and semantics require human inspection.",
    }


def score_case(case: dict, payload: dict) -> dict:
    prediction = normalize_prediction(case, payload)
    report = {"case_id": case["id"], "kind": case["kind"], "source_sha256": source_hash(case),
              "response_sha256": content_hash(payload), "provenance": dict(case["provenance"]),
              "events": score_events(case, prediction.get("events")),
              "facts": score_facts(case, prediction), "adaptation_notes": prediction.get("adaptation_notes", [])}
    if case["kind"] == "script":
        report["script"] = score_script(case, prediction)
    return report


def evaluate_responses(corpus: dict, responses: dict) -> dict:
    validate_corpus(corpus)
    if responses.get("schema_version") != RESPONSES_VERSION:
        raise ValueError("saved baseline must use storyboard-reliability-responses.v1")
    if responses.get("corpus_sha256") != content_hash(corpus):
        raise ValueError("saved response corpus hash differs; do not replay against silently changed gold")
    by_id = {case["id"]: case for case in corpus["cases"]}
    reports, seen = [], set()
    for record in responses.get("records", []):
        cid = record.get("case_id")
        if cid not in by_id or cid in seen:
            raise ValueError("saved response case IDs must be known and unique")
        seen.add(cid)
        case = by_id[cid]
        if record.get("source_sha256") and record["source_sha256"] != source_hash(case):
            raise ValueError("saved response source hash differs")
        payload = record.get("prediction", record.get("response"))
        if not isinstance(payload, dict):
            raise ValueError("saved response must contain an object prediction/response")
        reports.append(score_case(case, payload))
    return {
        "schema_version": "storyboard-reliability-report.v1", "corpus": corpus_inventory(corpus),
        "responses_sha256": content_hash(responses), "response_provenance": responses.get("provenance", {}),
        "evaluated_cases": len(reports), "missing_case_ids": sorted(set(by_id) - seen), "cases": reports,
        "offline": True, "model_calls": 0, "network_calls": 0, "cache_used": False,
        "semantic_acceptance": "not-established", "real_video_acceptance": False,
    }


def compare_reports(current: dict, baseline: dict) -> dict:
    if current["corpus"]["corpus_sha256"] != baseline["corpus"]["corpus_sha256"]:
        raise ValueError("comparison requires the same immutable corpus hash")
    paths = [("events", "source_coverage"), ("events", "mandatory_same_event", "accuracy"),
             ("events", "mandatory_different_event", "accuracy"), ("script", "speaker_accuracy"),
             ("script", "role_accuracy"), ("script", "coreference", "accuracy"),
             ("facts", "authored_atom_recall"), ("facts", "critical_evidence_recall")]
    old = {case["case_id"]: case for case in baseline["cases"]}
    changes = []
    for case in current["cases"]:
        previous = old.get(case["case_id"])
        if previous is None:
            continue
        for path in paths:
            new_value, old_value = case, previous
            for key in path:
                new_value = new_value.get(key) if isinstance(new_value, dict) else None
                old_value = old_value.get(key) if isinstance(old_value, dict) else None
            if isinstance(new_value, (int, float)) and isinstance(old_value, (int, float)):
                changes.append({"case_id": case["case_id"], "metric": ".".join(path),
                                "baseline": old_value, "current": new_value, "delta": new_value - old_value})
    return {"corpus_sha256": current["corpus"]["corpus_sha256"], "metric_changes": changes,
            "real_video_acceptance": False, "limit": "Offline fixture deltas are not real-video semantic acceptance."}


def parser_baseline(corpus: dict) -> dict:
    """Fresh local parser execution; no inferred gold events or fabricated claims."""
    from app.script_director.document import parse_script_document

    records = []
    for case in corpus["cases"]:
        if case["kind"] == "script":
            document = parse_script_document(case["source_text"]).model_dump(mode="json")
            records.append({"case_id": case["id"], "source_sha256": source_hash(case), "response": document})
    return {"schema_version": RESPONSES_VERSION, "corpus_sha256": content_hash(corpus), "records": records,
            "provenance": {"provider": "local-parser-only", "model_calls": 0, "cache_used": False,
                           "scope": "Source/speaker/coreference baseline only; no generation/events/video score supplied."}}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inventory", "validate", "evaluate", "parser-baseline"])
    parser.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    parser.add_argument("--responses", type=Path, help="Saved immutable response baseline envelope")
    parser.add_argument("--result-json", type=Path, help="One saved result; requires --case-id")
    parser.add_argument("--document-json", type=Path, help="Saved source document for one script result")
    parser.add_argument("--case-id")
    parser.add_argument("--compare-responses", type=Path, help="Saved previous baseline against the same corpus")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        corpus = load_corpus(args.corpus)
        if args.command in {"inventory", "validate"}:
            result = corpus_inventory(corpus)
        elif args.command == "parser-baseline":
            result = parser_baseline(corpus)
        else:
            if bool(args.responses) == bool(args.result_json):
                raise ValueError("evaluate requires exactly one of --responses or --result-json")
            if args.responses:
                responses = json.loads(args.responses.read_text(encoding="utf-8"))
            else:
                case = next((case for case in corpus["cases"] if case["id"] == args.case_id), None)
                if case is None:
                    raise ValueError("--result-json requires a known --case-id")
                payload = json.loads(args.result_json.read_text(encoding="utf-8"))
                if args.document_json:
                    document = json.loads(args.document_json.read_text(encoding="utf-8"))
                    payload = {"result": payload, "document": document}
                responses = {"schema_version": RESPONSES_VERSION, "corpus_sha256": content_hash(corpus),
                             "records": [{"case_id": case["id"], "response": payload}],
                             "provenance": {"kind": "saved-result-replay", "source_file_sha256": hashlib.sha256(args.result_json.read_bytes()).hexdigest()}}
            result = evaluate_responses(corpus, responses)
            if args.compare_responses:
                baseline = evaluate_responses(corpus, json.loads(args.compare_responses.read_text(encoding="utf-8")))
                result["baseline_comparison"] = compare_reports(result, baseline)
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded + "\n", encoding="utf-8")
            # Do not echo private source scripts/raw responses to the terminal.
            print(json.dumps({"output": str(args.output), "corpus_sha256": content_hash(corpus),
                              "case_count": len(result.get("records", result.get("cases", corpus["cases"])))}, ensure_ascii=False))
        elif args.command == "parser-baseline":
            parser.error("parser-baseline requires --output to avoid printing source content")
        else:
            print(encoded)
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
