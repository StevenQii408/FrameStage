"""Review/export the CURRENT source tree without publishing private media or data.

Uses tracked and reviewed untracked candidates; never stages, commits or pushes.
The default source archive excludes historical scratch reports and demo pixels
whose redistribution rights have not been established.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import tempfile
from xml.etree import ElementTree
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
BRAND_SVG = "front/public/framestage.svg"
BRAND_PNG_SIZES = {"front/public/framestage-32.png": (32, 32), "front/public/apple-touch-icon.png": (180, 180)}
BRAND_ASSETS = {BRAND_SVG, *BRAND_PNG_SIZES}
TOP_FILES = {"README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md",
             "PROJECT_STATUS.md", "DEVELOPMENT_ROADMAP.md", "THIRD_PARTY_REFERENCES.md",
             ".gitignore", ".gitattributes", ".nvmrc"}
SOURCE_EXTENSIONS = {".py", ".ts", ".mjs", ".vue", ".css", ".html", ".json", ".md", ".toml", ".yml", ".yaml", ".lock", ".constraints"}
PRIVATE_PARTS = {".git", "node_modules", "__pycache__", ".pytest_cache", "data", "models", "eval-media", "eval-results", "results", "dist"}
SECRET_PATTERNS = [re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"), re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
                   re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")]


def distributable(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(part in PRIVATE_PARTS or part.startswith((".venv", "data-")) for part in path.parts):
        return False
    if any(part.startswith(".env") and part != ".env.example" for part in path.parts):
        return False
    if name in TOP_FILES or name in BRAND_ASSETS or name in {"backend/LICENSE", "backend/.env.example"}:
        return True
    if name.startswith("docs/history/"):
        return False
    if name.startswith("Prompts/ai-shortfilm-prompts/"):
        return False
    if name.startswith("系统展示/") or (name.startswith("backend/tests/fixtures/") and path.suffix != ".py"):
        return False
    prefixes = ("backend/app/", "backend/tests/", "backend/evals/", "front/src/", "front/tests/", "front/scripts/", "Prompts/", "scripts/", "docs/", ".github/")
    if name.startswith(prefixes):
        return path.suffix in SOURCE_EXTENSIONS
    if path.parent.as_posix() in ("front", "backend"):
        return path.suffix in SOURCE_EXTENSIONS
    return False


# Small explicit boot/test contract; the full file set is separately checked against HEAD.
REQUIRED = set("""README.md LICENSE SECURITY.md CONTRIBUTING.md THIRD_PARTY_REFERENCES.md
.github/workflows/ci.yml scripts/release_source.py scripts/secret_scan.py scripts/check_backend_wheel.py
backend/.env.example backend/LICENSE backend/pyproject.toml backend/requirements.lock
backend/requirements-dev.lock backend/app/start_cloud.py backend/app/main.py backend/app/config.py
backend/app/db.py backend/app/runtime.py backend/app/media_worker.py backend/app/providers/bailian.py
backend/app/generation/cloud/provider.py backend/app/script_director/engine.py
backend/app/style_direct/cinema_library.v1.json backend/app/style_vector/catalog.v2.json
backend/app/prompt_profiles/profiles.json backend/evals/storyboard_reliability.v1.json
backend/tests/style_protocol_samples.py backend/tests/fixtures/workspace_qa_app.py backend/tests/test_smoke.py
Prompts/cinema_style_library/sources.v1.json Prompts/cinema_style_library/taxonomy.v1.json
front/index.html front/package.json front/package-lock.json front/tsconfig.json front/vite.config.ts
front/src/main.ts front/src/App.vue front/src/router.ts front/src/sceneLayout.ts""".split()) | BRAND_ASSETS


def brand_asset_problem(name, data):
    if len(data) > 256 * 1024:
        return "brand icon exceeds 256 KiB review limit"
    if name == BRAND_SVG:
        try:
            root = ElementTree.fromstring(data.decode("utf-8"))
        except (UnicodeDecodeError, ElementTree.ParseError):
            return "brand icon must be a UTF-8 SVG"
        if root.tag != "{http://www.w3.org/2000/svg}svg":
            return "brand icon must have an SVG root element"
        return None

    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "brand icon must be a PNG"
    expected = BRAND_PNG_SIZES[name]
    offset = 8
    has_image_data = False
    while offset + 12 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, offset)
        end = offset + 8 + length
        if end + 4 > len(data) or zlib.crc32(data[offset + 4:end]) != int.from_bytes(data[end:end + 4], "big"):
            return "invalid PNG chunk"
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                return "missing PNG header"
            if struct.unpack_from(">II", data, offset + 8) != expected:
                return f"brand PNG must measure {expected[0]}x{expected[1]} pixels"
        if kind == b"IDAT" and length:
            has_image_data = True
        offset = end + 4
        if kind == b"IEND":
            if not length and has_image_data and offset == len(data):
                return None
            break
    return "incomplete or invalid PNG"


def audit_tree(root=ROOT):
    root = Path(root)
    if (root / ".git").exists():
        result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                cwd=root, capture_output=True)
        if result.returncode:
            return [], [{"reason": "cannot read Git source inventory"}]
        candidates = sorted(set(filter(None, result.stdout.decode().split("\0"))))
    else:
        # GitHub/source ZIP downloads have no .git. Apply the same allowlist,
        # pruning private/heavy directories before descending into the tree.
        candidates = []
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = sorted(name for name in names if name not in PRIVATE_PARTS
                              and not name.startswith((".venv", "data-"))
                              and not (Path(directory) / name).is_symlink())
            candidates.extend((Path(directory) / name).relative_to(root).as_posix() for name in files)
        candidates.sort()
    manifest, problems = [], []
    for name in candidates:
        if not distributable(name):
            continue
        path = root / name
        if path.is_symlink():
            problems.append({"path": name, "reason": "source symlink requires explicit review"})
            continue
        if not path.is_file():  # A tracked deletion is not shipped.
            continue
        data = path.read_bytes()
        if name in BRAND_ASSETS:
            issue = brand_asset_problem(name, data)
            if issue:
                problems.append({"path": name, "reason": issue})
        if len(data) > 2 * 1024 * 1024:
            problems.append({"path": name, "reason": "source file exceeds 2 MB review limit"})
        text = data.decode("utf-8", errors="replace")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            problems.append({"path": name, "reason": "possible credential; value intentionally omitted"})
        if re.search(r"/" + r"Users/[^/\s]+/", text):
            problems.append({"path": name, "reason": "personal absolute path requires sanitization"})
        manifest.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    for missing in sorted(REQUIRED - {f["path"] for f in manifest}):
        problems.append({"path": missing, "reason": "required release file missing"})
    return manifest, problems


def head_problems(root, manifest, revision="HEAD"):
    if not (Path(root) / ".git").exists():
        return [{"reason": "HEAD verification requires a Git checkout"}]
    tree = subprocess.run(["git", "ls-tree", "-rz", revision], cwd=root, capture_output=True)
    if tree.returncode:
        return [{"reason": "cannot inspect committed HEAD"}]
    files = {f["path"]: f for f in manifest}
    seen, problems = set(), []
    for row in filter(None, tree.stdout.split(b"\0")):
        header, name = row.split(b"\t", 1)
        mode, kind, object_id = header.decode().split()
        name = name.decode()
        seen.add(name)
        if not distributable(name) or kind != "blob" or mode not in {"100644", "100755"}:
            problems.append({"path": name, "reason": "non-distributable entry tracked in HEAD"})
        elif name not in files:
            problems.append({"path": name, "reason": "committed file missing from working source"})
        else:
            data = (Path(root) / name).read_bytes()
            digest = hashlib.new("sha1" if len(object_id) == 40 else "sha256")
            digest.update(f"blob {len(data)}\0".encode()); digest.update(data)
            if digest.hexdigest() != object_id or hashlib.sha256(data).hexdigest() != files[name]["sha256"]:
                problems.append({"path": name, "reason": "working file differs from HEAD"})
    problems.extend({"path": name, "reason": "source is not committed in HEAD"} for name in sorted(files.keys() - seen))
    return problems


def export_zip(root, output, manifest, commit):
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False, suffix=".tmp") as temp:
        tmp = Path(temp.name)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            def write(name, data):
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
            for item in manifest:
                data = (root / item["path"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != item["sha256"]:
                    raise ValueError(f"source changed during export: {item['path']}")
                write(item["path"], data)
            write("RELEASE_MANIFEST.json", json.dumps({"schema_version": "source-release.v2",
                  "commit": commit, "files": manifest}, ensure_ascii=False, indent=2))
        if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip() != commit:
            raise ValueError("HEAD changed during export")
        tmp.replace(output)
    finally:
        tmp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-head", action="store_true", help="Reject missing, private or uncommitted source")
    parser.add_argument("--output", type=Path, help="Write a reproducible archive of the verified commit")
    args = parser.parse_args(argv)
    manifest, problems = audit_tree()
    revision = None
    if args.verify_head or args.output:
        result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        revision = result.stdout.strip() if result.returncode == 0 else None
        if revision:
            problems.extend(head_problems(ROOT, manifest, revision))
        else:
            problems.append({"reason": "release requires a committed Git checkout"})
    if problems:
        print(json.dumps({"ready": False, "problems": problems}, ensure_ascii=False, indent=2))
        return 1
    if args.output:
        output = args.output.resolve()
        if output.suffix != ".zip":
            parser.error("--output must be a .zip file")
        export_zip(ROOT, output, manifest, revision)
    print(json.dumps({"ready": True, "source_files": len(manifest), "source_bytes": sum(item["bytes"] for item in manifest),
                      "output": str(args.output.resolve()) if args.output else None,
                      "excludes": ["credentials", "local databases", "model outputs", "demo media", "historical scratch documents"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
