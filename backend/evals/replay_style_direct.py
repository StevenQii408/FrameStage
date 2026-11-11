"""Export saved style results using SQLite read-only access; never run providers."""
import argparse
import json
from pathlib import Path
import sqlite3


def export_saved(database, mapping):
    """Mapping: samples [{id, media_sha256, analysis_id}]; retain bound source hash."""
    path = Path(database).resolve()
    if not path.is_file():
        raise ValueError("数据库不存在")
    rows = mapping.get("samples", [])
    if not rows or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("映射为空或样本ID重复")
    output = []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        for item in rows:
            row = conn.execute("SELECT source_json,result_json FROM style_analyses WHERE id=?", (item["analysis_id"],)).fetchone()
            if not row or not row[1]:
                raise ValueError("分析不存在或未保存结果：" + item["analysis_id"])
            source, result = json.loads(row[0]), json.loads(row[1])
            hashes = {a.get("source_sha256") for a in source.get("assets", []) if a.get("source_sha256")}
            if item["media_sha256"] not in hashes:
                raise ValueError("保存证据缺少原素材哈希或与人工素材不一致，不能人为改标来源")
            for asset in result.get("assets", []):
                asset.pop("path", None)
                asset.pop("original_path", None)
            result["replay_source"] = "database"
            output.append({"id": item["id"], "media_sha256": item["media_sha256"], "prediction": result})
    return {"schema_version": "style-direct-replay.v1", "new_paid_requests": 0, "samples": output}


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读导出已有画风分析供离线人工回放")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("回放输出已存在，请使用新路径")
    result = export_saved(args.database, json.loads(args.mapping.read_text(encoding="utf8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({"sample_count": len(result["samples"]), "new_paid_requests": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
