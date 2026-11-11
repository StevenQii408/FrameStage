"""Scorer regressions use author assertions; never paid APIs or model-as-gold."""
from copy import deepcopy
import json
import socket

import pytest

from evals.storyboard_reliability import (
    CORPUS_PATH, PREDICTION_VERSION, RESPONSES_VERSION, authored_coreferences,
    authored_units, compare_reports, content_hash, corpus_inventory,
    evaluate_responses, load_corpus, main, normalize_prediction, score_case,
    score_events, source_hash, validate_corpus,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("reliability tests must never access the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def corpus():
    return load_corpus()


def case(corpus, cid):
    return next(item for item in corpus["cases"] if item["id"] == cid)


def authored_assertions_for_scorer_test(item, groups=None):
    """An oracle *scorer test*, not a pipeline response/acceptance baseline."""
    prediction = {"schema_version": PREDICTION_VERSION,
                  "events": [{"source_ids": group} for group in groups] if groups is not None else None,
                  "claims": [{**fact, "evidence": list(fact["frame_ids"])} for fact in item["required_facts"]]}
    if item["kind"] == "script":
        prediction.update(source_text=item["source_text"], source_units=authored_units(item),
                          coreferences=authored_coreferences(item))
    return prediction


def envelope(corpus, item, prediction):
    return {"schema_version": RESPONSES_VERSION, "corpus_sha256": content_hash(corpus),
            "provenance": {"kind": "scorer-oracle-test-only"},
            "records": [{"case_id": item["id"], "source_sha256": source_hash(item), "prediction": prediction}]}


def test_corpus_provenance_never_claims_real_manual_gold(corpus):
    inventory = corpus_inventory(corpus)
    assert inventory["authored_script_cases"] == 10
    assert inventory["synthetic_video_cases"] == 10
    assert inventory["human_inspected_real_video_cases"] == 0
    assert inventory["human_review_pending_authored_fixtures"] == 20
    assert inventory["manual_pending"]["existing_draft_pending_human"] == 4
    assert inventory["manual_pending"]["real_video_manual_pending"] == 10
    assert inventory["annotation_policy"]["not_model_output_as_gold"]
    assert all(not item["provenance"]["generated_by_tested_pipeline"] for item in corpus["cases"])


def test_json_schema_is_saved_and_uses_the_same_contract(corpus):
    schema = json.loads(CORPUS_PATH.with_name("storyboard_reliability.schema.json").read_text())
    assert schema["properties"]["schema_version"]["const"] == corpus["schema_version"]
    assert schema["$defs"]["provenance"]["properties"]["generated_by_tested_pipeline"] == {"const": False}
    try:
        import jsonschema
    except ImportError:
        return
    jsonschema.Draft202012Validator(schema).validate(corpus)


@pytest.mark.parametrize("cid", ["script-01-handoff", "script-03-alias", "script-07-split-speaker"])
def test_authored_source_speaker_and_coreference_oracle(corpus, cid):
    item = case(corpus, cid)
    report = score_case(item, authored_assertions_for_scorer_test(item))
    assert report["script"]["speaker_accuracy"] == 1
    assert report["script"]["coreference"]["accuracy"] == 1
    assert report["script"]["exact_source_slice_rate"] == 1
    assert report["script"]["metadata_content_character_leak"] == 0
    assert report["events"]["available"] is False
    assert report["facts"]["semantic_acceptance"] == "not-established"


def test_ambiguous_pronoun_fabricated_identity_is_scored_as_unsafe(corpus):
    item = case(corpus, "script-02-ambiguous")
    prediction = authored_assertions_for_scorer_test(item)
    prediction["coreferences"][0]["character_id"] = "林岚"
    prediction["claims"] = [{"text": "林岚抬手敲门"}]
    report = score_case(item, prediction)
    assert report["script"]["coreference"]["unsafe_ambiguous_resolutions"] == 1
    assert report["script"]["coreference"]["accuracy"] == .5
    assert report["facts"]["forbidden_phrase_proxy_hits"][0]["id"] == "no-resolve-she"


def test_wrong_speaker_and_offset_cannot_receive_full_credit(corpus):
    item = case(corpus, "script-01-handoff")
    prediction = authored_assertions_for_scorer_test(item)
    prediction["source_units"][2]["speaker"] = "何青"
    prediction["source_units"][0]["start"] += 1
    report = score_case(item, prediction)["script"]
    assert report["speaker_accuracy"] == 0
    assert report["exact_source_slice_rate"] == .75
    assert report["content_character_recall"] < 1


def test_metadata_is_not_an_action_content_unit(corpus):
    item = case(corpus, "script-01-handoff")
    prediction = authored_assertions_for_scorer_test(item)
    text = "何青（32岁，管理员）"
    start = item["source_text"].index(text)
    prediction["source_units"].append({"start": start, "end": start + len(text), "text": text,
                                       "role": "action", "speaker": None, "character_ids": ["何青"]})
    assert score_case(item, prediction)["script"]["metadata_content_character_leak"] == len(text)


def test_allowed_event_boundary_accepts_both_partitions_without_shot_target(corpus):
    item = case(corpus, "video-02-drink-feedback")
    joined = score_events(item, [{"source_ids": ["s1", "s2", "s3"]}])
    split = score_events(item, [{"source_ids": ["s1", "s2"]}, {"source_ids": ["s3"]}])
    for result in [joined, split]:
        assert result["mandatory_same_event"]["accuracy"] == 1
        assert result["allowed_alternatives"][0]["accepted"] is True
        assert result["allowed_alternatives"][0]["scored"] is False
        assert result["shot_count_scored"] is False
    assert joined["event_count_diagnostic"] != split["event_count_diagnostic"]


def test_required_different_event_does_not_reward_everything_merged(corpus):
    item = case(corpus, "video-05-scene-change")
    all_joined = score_events(item, [{"source_ids": ["s1", "s2", "s3"]}])
    assert all_joined["mandatory_same_event"]["accuracy"] == 1
    assert all_joined["mandatory_different_event"]["accuracy"] == 0


def test_missing_duplicate_or_unknown_ownership_is_not_a_pass(corpus):
    item = case(corpus, "video-01-handoff")
    result = score_events(item, [{"source_ids": ["s1", "s2"]}, {"source_ids": ["s2", "foreign"]}])
    assert result["duplicate_source_ids"] == ["s2"]
    assert result["unknown_source_ids"] == ["foreign"]
    assert result["source_coverage"] == pytest.approx(1 / 3)
    assert result["mandatory_same_event"]["accuracy"] == 0
    assert result["source_ownership_complete_unique"] is False


@pytest.mark.parametrize("kind,prefix", [("script", "u"), ("video", "s")])
@pytest.mark.parametrize("count,index", [(12, 8), (13, 9)])
def test_twelve_and_thirteen_events_use_the_same_rules(corpus, kind, prefix, count, index):
    item = case(corpus, f"{kind}-0{index}-events-{count}")
    prediction = [{"source_ids": [f"{prefix}{i}"]} for i in range(1, count + 1)]
    result = score_events(item, prediction)
    assert result["source_ownership_complete_unique"] is True
    assert result["mandatory_different_event"]["expected"] == count - 1
    assert result["mandatory_different_event"]["accuracy"] == 1
    assert result["shot_count_scored"] is False


def test_typed_fact_requires_both_transition_frames_and_source_attribution(corpus):
    item = case(corpus, "video-01-handoff")
    prediction = authored_assertions_for_scorer_test(item, [["s1", "s2", "s3"]])
    good = score_case(item, prediction)["facts"]
    assert good["authored_atom_recall"] == 1
    assert good["critical_evidence_recall"] == 1
    prediction["claims"][2]["evidence"] = ["f3"]
    partial = score_case(item, prediction)["facts"]
    assert partial["authored_atom_recall"] == 1
    assert partial["critical_evidence_recall"] == pytest.approx(2 / 3)
    prediction["claims"][0]["source_ids"] = ["s3"]
    assert score_case(item, prediction)["facts"]["critical_evidence_recall"] == pytest.approx(1 / 3)


def test_wrong_frame_id_or_timestamp_is_evidence_error(corpus):
    item = case(corpus, "video-01-handoff")
    prediction = authored_assertions_for_scorer_test(item)
    prediction["claims"][0]["evidence"] = [{"frame_id": "f1", "timestamp": 9}]
    prediction["claims"][1]["evidence"] = ["foreign-frame"]
    report = score_case(item, prediction)["facts"]
    assert report["invalid_evidence_reference_ids"] == ["f1", "foreign-frame"]
    assert report["critical_evidence_recall"] == pytest.approx(1 / 3)


def test_foreign_source_and_source_frame_mismatch_fail_evidence_support(corpus):
    item = case(corpus, "video-01-handoff")
    prediction = authored_assertions_for_scorer_test(item)
    prediction["claims"][0]["source_ids"].append("foreign")
    prediction["claims"][1]["evidence"].append("f1")
    report = score_case(item, prediction)["facts"]
    assert report["invalid_source_evidence_reference_ids"] == ["foreign", "s1"]
    assert report["critical_evidence_recall"] == pytest.approx(1 / 3)


def test_repeated_exact_source_anchor_requires_explicit_occurrence(corpus):
    changed = deepcopy(corpus)
    item = changed["cases"][0]
    item["source_text"] += "\n我去开门。"
    with pytest.raises(ValueError, match="explicit occurrence"):
        validate_corpus(changed)
    item["content_units"][2]["occurrence"] = 0
    validate_corpus(changed)


def test_contradictory_event_gold_is_rejected(corpus):
    changed = deepcopy(corpus)
    item = case(changed, "video-01-handoff")
    item["event_constraints"].append({"left": "s1", "right": "s3", "relation": "different_event", "reason": "contradictory fixture"})
    with pytest.raises(ValueError, match="contradict"):
        validate_corpus(changed)


def test_forbidden_atoms_and_phrase_checks_never_equal_real_semantic_acceptance(corpus):
    item = case(corpus, "video-02-drink-feedback")
    prediction = authored_assertions_for_scorer_test(item)
    forbidden = item["forbidden_claims"][0]
    prediction["claims"].append({"subject": forbidden["subject"], "predicate": forbidden["predicate"],
                                  "object": forbidden["object"], "text": forbidden["text"], "evidence": ["f3"]})
    report = score_case(item, prediction)["facts"]
    assert report["forbidden_atom_ids"] == ["exhaustion"]
    assert report["forbidden_phrase_proxy_hits"]
    assert report["authored_atom_precision"] == pytest.approx(2 / 3)
    assert report["semantic_acceptance"] == "not-established"
    assert report["manual_semantic_review_required"] is True


def test_literal_output_is_a_proxy_and_missing_typed_semantics_remains_unavailable(corpus):
    item = case(corpus, "video-01-handoff")
    prediction = {"schema_version": PREDICTION_VERSION, "events": None,
                  "claims": [{"text": fact["text"], "evidence": fact["frame_ids"]} for fact in item["required_facts"]]}
    report = score_case(item, prediction)["facts"]
    assert report["literal_fact_coverage_proxy"] == 1
    assert report["authored_atom_recall"] is None
    assert report["critical_evidence_recall"] is None
    assert report["semantic_acceptance"] == "not-established"


def test_model_output_as_gold_and_unreviewed_real_video_are_rejected(corpus):
    forged = deepcopy(corpus)
    forged["cases"][0]["provenance"]["generated_by_tested_pipeline"] = True
    with pytest.raises(ValueError, match="promoted"):
        validate_corpus(forged)
    forged = deepcopy(corpus)
    forged["cases"][10]["provenance"]["kind"] = "human-inspected-real-video"
    with pytest.raises(ValueError, match="human reviewer"):
        validate_corpus(forged)


def test_saved_response_replay_and_baseline_delta_require_exact_corpus_hash(corpus):
    item = case(corpus, "video-05-scene-change")
    previous = authored_assertions_for_scorer_test(item, [["s1", "s2", "s3"]])
    current = authored_assertions_for_scorer_test(item, [["s1", "s2"], ["s3"]])
    previous_report = evaluate_responses(corpus, envelope(corpus, item, previous))
    current_report = evaluate_responses(corpus, envelope(corpus, item, current))
    comparison = compare_reports(current_report, previous_report)
    assert next(change["delta"] for change in comparison["metric_changes"]
                if change["metric"] == "events.mandatory_different_event.accuracy") == 1
    assert current_report["model_calls"] == current_report["network_calls"] == 0
    assert current_report["cache_used"] is False
    assert current_report["real_video_acceptance"] is False
    forged = envelope(corpus, item, current)
    forged["corpus_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="corpus hash"):
        evaluate_responses(corpus, forged)


def test_public_result_without_saved_document_cannot_claim_attribution_pass(corpus):
    item = case(corpus, "script-01-handoff")
    output = normalize_prediction(item, {"schema_version": "storyboard.v1", "shots": [], "narrative_events": []})
    assert output["source_units"] is None
    assert score_case(item, {"schema_version": "storyboard.v1"})["script"]["available"] is False


def test_saved_document_adapter_keeps_canonical_speaker_and_pronoun_audit(corpus):
    item = case(corpus, "script-01-handoff")
    expected = authored_units(item)
    refs = authored_coreferences(item)
    document = {"schema_version": "script-document.v1", "source_text": item["source_text"],
                "characters": [{"id": "char-a", "name": "何青"}, {"id": "char-b", "name": "杜川"}],
                "spans": [{"id": f"span-{i}", "start": unit["start"], "end": unit["end"], "text": unit["text"],
                           "role": unit["role"], "character_id": "char-b" if unit["speaker"] else None,
                           "entity_refs": [{**ref, "text": ref["mention"], "character_id": "char-b"} for ref in refs
                                           if unit["start"] <= ref["start"] < unit["end"]]}
                          for i, unit in enumerate(expected)]}
    prediction = normalize_prediction(item, {"document": document, "decision": {"events": [
        {"source_span_ids": ["span-0", "span-1"]}, {"source_span_ids": ["span-2", "span-3"]}]}})
    assert prediction["source_units"][2]["speaker"] == "杜川"
    assert prediction["coreferences"][0]["character_id"] == "杜川"
    assert prediction["events"][0]["source_ids"] == ["u1", "u2"]


def test_saved_group_members_preserve_profiles_refs_and_ambiguous_singular(corpus):
    item = case(corpus, "script-02-ambiguous")
    units = authored_units(item)
    refs = authored_coreferences(item)
    document = {
        "schema_version": "script-document.v1", "source_text": item["source_text"],
        "characters": [
            {"id": "doctor-1", "name": "林岚", "aliases": [], "description": "30岁，医生", "source_span_ids": ["profile-1"]},
            {"id": "reporter-1", "name": "方雨", "aliases": [], "description": "28岁，记者", "source_span_ids": ["profile-2"]}],
        "spans": [{"id": f"content-{i}", "start": unit["start"], "end": unit["end"], "text": unit["text"],
                   "role": unit["role"], "character_id": None, "entity_refs": []}
                  for i, unit in enumerate(units)],
    }
    for name, identifier in [("林岚", "doctor-1"), ("方雨", "reporter-1")]:
        start = item["source_text"].index(name, units[0]["start"])
        document["spans"][0]["entity_refs"].append({"start": start, "end": start + len(name), "text": name,
            "character_id": identifier, "group_character_ids": [], "candidate_character_ids": [],
            "source_span_ids": ["content-0"], "method": "explicit"})
    document["spans"][1]["entity_refs"] = [{"start": refs[0]["start"], "end": refs[0]["end"], "text": "她",
        "character_id": None, "group_character_ids": [], "candidate_character_ids": ["doctor-1", "reporter-1"],
        "source_span_ids": ["content-0"], "method": "unknown"}]
    document["spans"][3]["entity_refs"] = [{"start": refs[1]["start"], "end": refs[1]["end"], "text": "两人",
        "character_id": None, "group_character_ids": ["doctor-1", "reporter-1"], "candidate_character_ids": [],
        "source_span_ids": ["content-0"], "method": "same-scene-context"}]
    prediction = normalize_prediction(item, document)
    assert prediction["source_units"][3]["character_ids"] == ["方雨", "林岚"]
    assert prediction["source_units"][3]["speaker"] is None
    group = next(ref for ref in prediction["coreferences"] if ref["text"] == "两人")
    assert group["character_id"] is None and group["status"] == "group"
    assert group["group_character_ids"] == ["林岚", "方雨"]
    assert group["source_span_ids"] == ["content-0"]
    ambiguous = next(ref for ref in prediction["coreferences"] if ref["text"] == "她")
    assert ambiguous["character_id"] is None and ambiguous["group_character_ids"] == []
    assert ambiguous["candidate_character_ids"] == ["林岚", "方雨"]
    assert prediction["source_units"][1]["character_ids"] == []
    assert prediction["character_profiles"][0] == {"id": "林岚", "source_character_id": "doctor-1", "name": "林岚",
        "aliases": [], "description": "30岁，医生", "source_span_ids": ["profile-1"]}
    report = score_case(item, document)["script"]
    assert report["character_set_accuracy"] == 1
    assert report["coreference"]["accuracy"] == 1
    assert report["coreference"]["unsafe_ambiguous_resolutions"] == 0


def test_cli_offline_replay_writes_report_without_source_content(corpus, tmp_path, capsys):
    item = case(corpus, "video-01-handoff")
    saved = tmp_path / "baseline.json"
    saved.write_text(json.dumps(envelope(corpus, item, authored_assertions_for_scorer_test(item, [["s1", "s2", "s3"]]))))
    output = tmp_path / "report.json"
    assert main(["evaluate", "--responses", str(saved), "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["evaluated_cases"] == 1
    assert len(report["missing_case_ids"]) == 19
    assert "source_text" not in capsys.readouterr().out
