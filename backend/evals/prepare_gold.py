"""Prepare external-media inventory only, never label truth or scene boundaries."""
import argparse
import json
from pathlib import Path

from app.services.video_processor import probe_video
from evals.manifest import sha256_file


def inventory(media_dir, filenames):
    root = media_dir.resolve()
    clips = []
    for index, filename in enumerate(filenames, 1):
        path = (root / filename).resolve()
        if not path.is_relative_to(root):
            raise ValueError("素材只能使用外置目录内相对路径")
        metadata = probe_video(path)
        clips.append({"id": f"clip-{index:03d}", "file": filename,
                      "sha256": sha256_file(path), "media_metadata": metadata,
                      "annotation_status": "pending-human", "shots": []})
    return {"schema_version": "storyboard-eval-draft.v1", "corpus_id": "flash-golden-draft",
            "target_clips": 10, "target_shots_approximately": 25,
            "note": "不是有效评测 manifest。人工填入镜头、视觉真值与证据后转换为 storyboard-eval.v1；不得把模型预测当真值。",
            "clips": clips}


def main(argv=None):
    parser = argparse.ArgumentParser(description="从外置素材建立匿名 SHA-256 清单，不自动标注")
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--files", nargs="+", required=True, help="外置目录内相对文件名")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    draft = inventory(args.media_dir, args.files)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

