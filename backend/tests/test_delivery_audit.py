from evals.delivery_audit import audit_result


def result():
    return {"job": {"id": "fixture", "input_type": "script", "status": "completed"},
            "shots": [{"id": "s1", "status": "ready", "action_details": {"steps": ["开门"]}},
                      {"id": "s2", "status": "ready", "action_details": {"steps": ["进入"]}}],
            "generation_sequences": [{"source_shot_ids": ["s1", "s2"], "generation_duration_seconds": 6,
                                      "status": "ready"}]}


def test_structure_does_not_claim_semantic_accuracy():
    report = audit_result(result())
    assert report["structural_pass"] and report["semantic_accuracy"] is None
    assert report["human_review_required"] and report["model_calls"] == 0


def test_duplicate_ownership_and_invalid_reference_fail_audit():
    value = result()
    value["generation_sequences"][0]["source_shot_ids"] = ["s1", "s1"]
    value["shots"][0]["visual_observation_v2"] = {"evidence": [{"frame_id": "absent", "timestamp": .5}]}
    report = audit_result(value)
    assert not report["structural_pass"] and report["duplicate_source_ids"] == ["s1"]
    assert report["missing_source_ids"] == ["s2"] and report["invalid_evidence_frame_ids"] == ["absent"]
