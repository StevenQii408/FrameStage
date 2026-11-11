"""Explicit style application changes style, not saved script delivery grammar."""

import json

import pytest

from app.db import utc_now
from app.services.prompt_service import build_prompts
from test_script_prompt_delivery import observation as script_observation
from test_style_direct import client, finished, image_bytes


@pytest.mark.parametrize("workflow", ["direct", "legacy", "vector"])
@pytest.mark.parametrize("legacy", [True, False], ids=["saved-v1", "current-v2"])
def test_style_refresh_preserves_saved_script_template_and_non_style_body(client, workflow, legacy):
    service = client.app.state.job_service
    job_id, shot_id = "script-refresh", "script-refresh-shot"
    now = utc_now()
    service.db.insert_job(dict(id=job_id, status="completed", stage="completed", progress=100,
        input_type="script", config={"style_strategy": "legacy-v1"}, created_at=now, updated_at=now))
    visual = script_observation()
    service.db.execute("""INSERT INTO shots(id,job_id,number,start_time,end_time,duration,time,
        generation_duration_seconds,observation_json) VALUES(?,?,?,?,?,?,?,?,?)""",
        (shot_id, job_id, 1, 0, 4, 4, "00:00", 4, visual.model_dump_json()))
    original = build_prompts(shot_id, visual, template_id="seedance-script-v1" if legacy else None)
    for prompt in original:
        service.db.execute("""INSERT INTO prompts(id,job_id,shot_id,prompt_type,platform,version,
            positive_prompt,negative_prompt,status,template_id,template_version,warnings_json,contract_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (prompt.id, job_id, shot_id, prompt.prompt_type,
            prompt.platform, prompt.version, prompt.positive_prompt, prompt.negative_prompt, prompt.status,
            prompt.template_id, prompt.template_version, json.dumps(prompt.warnings), prompt.contract.model_dump_json()))

    if workflow == "direct":
        created = client.post("/api/style/analyses", files={"image": ("frame.png", image_bytes(), "image/png")})
        assert created.status_code == 202
        analysis = finished(client, created.json()["analysis_id"])
        response = client.post(f"/api/jobs/{job_id}/style/apply", json={
            "analysis_id": analysis["analysis_id"], "expected_version": analysis["result"]["profile"]["version"]})
        assert response.status_code == 200, response.text
    elif workflow == "legacy":
        from app.services.style_service import StyleObservation, build_profile
        from app.services.style_workflow import confirm_style
        profile = build_profile(StyleObservation(mode="creative"), [
            "medium.two-d-animation", "style_family.pop-art", "line_language.bold-black-outline",
            "character_design.expressive-limbs", "palette.primary-four-color", "shading.duotone-halftone",
            "material.paper-fiber", "composition.clean-negative-space",
            "graphic_motif.handdrawn-speedlines", "motion_language.exaggerated-pose",
        ])
        service.db.update_job(job_id, style_profile_json=profile.model_dump_json())
        confirm_style(service, job_id)
    else:
        from app.style_vector.models import ProjectVisualBible
        from app.style_vector.workflow import confirm_visual_bible
        bible = ProjectVisualBible(global_core_terms=["medium.three-d-animation"])
        service.db.update_job(job_id, visual_bible_json=bible.model_dump_json())
        confirm_visual_bible(service, job_id)

    after = service.db.fetchall("SELECT * FROM prompts WHERE job_id=?", (job_id,))
    assert {item["template_id"] for item in after} == {prompt.template_id for prompt in original}
    before_by_type = {prompt.prompt_type: prompt for prompt in original}
    for saved in after:
        before = before_by_type[saved["prompt_type"]]
        updated = json.loads(saved["contract_json"])
        assert updated["observed_style"] != before.contract.observed_style
        assert saved["positive_prompt"].replace(updated["observed_style"], "<STYLE>") == before.positive_prompt.replace(before.contract.observed_style, "<STYLE>")
        if saved["prompt_type"] == "video":
            assert "无台词\n别删这句。" in saved["positive_prompt"]
            assert saved["positive_prompt"].count("甲敲门") == 2
    video = next(item for item in after if item["prompt_type"] == "video")
    assert ("拟音／画外声音" in video["positive_prompt"]) is legacy
    revisions = service.db.fetchall("SELECT payload_json FROM prompt_revisions WHERE job_id=?", (job_id,))
    assert {json.loads(item["payload_json"])["positive_prompt"] for item in revisions} == {prompt.positive_prompt for prompt in original}
    shot = service.db.fetchone("SELECT start_time,end_time,observation_json FROM shots WHERE id=?", (shot_id,))
    assert (shot["start_time"], shot["end_time"]) == (0, 4)
    assert shot["observation_json"] == visual.model_dump_json()
