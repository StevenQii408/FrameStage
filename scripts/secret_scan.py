"""Scan source candidates and Git history without printing credential values."""
import argparse
import json
from pathlib import Path
import subprocess

from release_source import ROOT, SECRET_PATTERNS, audit_tree


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="scan commits reachable from HEAD")
    parser.add_argument("--all-refs", action="store_true", help="also scan private/local refs")
    args = parser.parse_args(argv)
    manifest, problems = audit_tree()
    checked = 0
    if args.history or args.all_refs:
        # cat-file batch reads repository objects; no checkout, external command
        # execution, upload, or changes to commits are performed.
        objects = subprocess.check_output(["git", "rev-list", "--objects", "--all" if args.all_refs else "HEAD"], cwd=ROOT).decode().splitlines()
        for row in objects:
            object_id, _, name = row.partition(" ")
            kind = subprocess.check_output(["git", "cat-file", "-t", object_id], cwd=ROOT).strip()
            if kind != b"blob":
                continue
            data = subprocess.check_output(["git", "cat-file", "blob", object_id], cwd=ROOT)
            checked += 1
            if b"\0" in data:
                continue
            if any(pattern.search(data.decode("utf-8", errors="replace")) for pattern in SECRET_PATTERNS):
                problems.append({"path": name or "unnamed-history-blob", "object": object_id[:12],
                                 "reason": "possible credential in history; value omitted"})
    print(json.dumps({"ready": not problems, "source_files": len(manifest), "history_blobs": checked,
                      "problems": problems, "scope": "known key/private-key patterns; not a formal security audit"}, ensure_ascii=False, indent=2))
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
