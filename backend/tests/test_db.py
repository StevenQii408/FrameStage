import sqlite3

from app.db import Database


def test_database_migrates_provider_columns_with_legacy_marker(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE jobs (
              id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
              progress INTEGER NOT NULL DEFAULT 0, video_filename TEXT, video_path TEXT,
              input_type TEXT NOT NULL DEFAULT 'video', script_text TEXT NOT NULL DEFAULT '',
              config_json TEXT NOT NULL, error TEXT, result_version TEXT NOT NULL DEFAULT 'storyboard.v1',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            "INSERT INTO jobs (id,status,stage,config_json,created_at,updated_at) VALUES ('old','completed','completed','{}','now','now')"
        )

    database = Database(path)
    database.init()
    row = database.fetchone("SELECT provider, model, segmentation_profile, raw_shot_count, narrative_shot_count, script_plan_json, script_raw_response_json, planning_warnings_json FROM jobs WHERE id = 'old'")
    assert dict(row) == {
        "provider": "legacy-unknown", "model": "legacy-unknown", "segmentation_profile": "legacy-v1",
        "raw_shot_count": 0, "narrative_shot_count": 0,
        "script_plan_json": None, "script_raw_response_json": "[]", "planning_warnings_json": "[]",
    }
    assert database.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_shots'") is not None
    shot_columns = {item["name"] for item in database.fetchall("PRAGMA table_info(shots)")}
    raw_columns = {item["name"] for item in database.fetchall("PRAGMA table_info(raw_shots)")}
    assert {"cut_reason", "source_edit_times_json", "generation_duration_seconds", "scene_reason"} <= shot_columns
    assert "generation_shot_ids_json" in raw_columns
