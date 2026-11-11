import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from evals.manifest import ManifestError, load_manifest, resolve_clips
from evals.run import render_markdown, run_benchmark
from evals.scoring import match_boundaries, normalize_lens, normalize_movement, normalize_shot_size, score_observation, score_transitions


def labels():
    return {
        "subject_action": {"observable": True, "value": "主体沿道路缓慢移动", "keywords": ["移动"]},
        "shot_size": {"observable": True, "value": "全景"},
        "lens": {"observable": True, "value": "广角"},
        "camera_movement": {"observable": True, "value": "跟拍"},
        "lighting_vibe": {"observable": True, "value": "自然光"},
    }


def manifest_payload(filename: str, sha256: str):
    return {
        "schema_version": "storyboard-eval.v1",
        "corpus_id": "test-corpus",
        "clips": [
            {
                "id": "clip-smoke",
                "file": filename,
                "sha256": sha256,
                "tags": ["smoke"],
                "shots": [
                    {"number": 1, "start_time": 0.0, "end_time": 1.0, "transition_in": "start", "labels": labels()}
                ],
            }
        ],
    }


def test_manifest_validates_and_resolves_only_hashed_relative_media(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    video = media / "clip.mp4"
    video.write_bytes(b"fixture")
    digest = hashlib.sha256(b"fixture").hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(manifest_payload("clip.mp4", digest)), encoding="utf-8")

    loaded = load_manifest(manifest)
    assert resolve_clips(loaded, media)[0]["_path"] == video

    loaded["clips"][0]["sha256"] = "0" * 64
    with pytest.raises(ManifestError, match="SHA-256"):
        resolve_clips(loaded, media)


def test_manifest_rejects_absolute_paths_and_invalid_taxonomy(tmp_path):
    payload = manifest_payload("/tmp/private.mp4", "0" * 64)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestError, match="相对路径"):
        load_manifest(manifest)

    payload["clips"][0]["file"] = "clip.mp4"
    payload["clips"][0]["shots"][0]["labels"]["lens"]["value"] = "50mm"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestError, match="视觉焦段"):
        load_manifest(manifest)


def test_boundary_matching_and_transition_types():
    score = match_boundaries([1.12, 3.8, 8.0], [1.0, 4.0], tolerance=0.3)
    assert (score["tp"], score["fp"], score["fn"]) == (2, 1, 0)
    assert score["recall"] == 1.0

    transitions = score_transitions(
        [1.12, 3.8, 9.0],
        [{"time": 1.0, "type": "hard_cut"}, {"time": 4.0, "type": "dissolve"}],
        tolerance=0.3,
    )
    assert transitions["by_type"]["hard_cut"] == {"expected": 1, "detected": 1}
    assert transitions["by_type"]["dissolve"] == {"expected": 1, "detected": 1}
    assert transitions["fp"] == 1


def test_taxonomy_normalization_and_confidence_rule():
    assert normalize_shot_size("medium close-up") == "中近景"
    assert normalize_lens("estimated 28mm wide angle") == "广角"
    assert normalize_movement("slow lateral tracking") == "跟拍"

    prediction = {
        "subject_action": "未知",
        "shot_size": "中景",
        "lens": "未知",
        "camera_movement": "跟拍",
        "lighting_vibe": "自然光",
        "confidence": 0.8,
    }
    score = score_observation(labels(), prediction)
    assert score["unknown_count"] == 2
    assert score["confidence_rule_pass"] is False
    prediction["confidence"] = 0.5
    assert score_observation(labels(), prediction)["confidence_rule_pass"] is True


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for the eval integration test")
def test_mock_benchmark_generates_layered_report(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    video = media / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=12",
            "-t", "1", "-pix_fmt", "yuv420p", "-y", str(video),
        ],
        check=True,
    )
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(manifest_payload("clip.mp4", digest)), encoding="utf-8")
    monkeypatch.setenv("STORYBOARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STORYBOARD_MIN_AVAILABLE_MEMORY_GB", "0")

    report = run_benchmark(manifest, media, tmp_path / "results", "mock")

    assert report["schema_version"] == "storyboard-eval-report.v1"
    assert report["summary"]["frames"]["generated"] == 4
    assert report["summary"]["model"]["parse_rate"] == 1.0
    assert report["clips"][0]["model_metrics"][0]["prediction"]["subject_action"]
    assert "核心指标" in render_markdown(report)
