"""Verify the installed backend payload against the public source checkout."""
from pathlib import Path
import sys
import zipfile

from release_source import audit_tree


def check(wheel, root):
    files, problems = audit_tree(root.parent)
    if problems:
        raise ValueError(f"cannot verify wheel against incomplete or unaudited source ({len(problems)} problems)")
    wanted = {f["path"][len("backend/"):]: root.parent / f["path"] for f in files
              if f["path"].startswith(("backend/app/", "backend/evals/"))
              and Path(f["path"]).suffix in {".py", ".json"}}
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError("duplicate archive entries")
        for name in names:
            prefix, _, _ = name.partition("/")
            if prefix in {"app", "evals"}:
                if name not in wanted and name != "app/prompt_profiles/README.md":
                    raise ValueError(f"unexpected package data: {name}")
            elif not prefix.endswith(".dist-info"):
                raise ValueError(f"unexpected top-level entry: {name}")
        for name, source in wanted.items():
            if archive.read(name) != source.read_bytes():
                raise ValueError(f"wheel differs from source: {name}")
        licenses = [n for n in names if n.endswith("/LICENSE") and n.partition("/")[0].endswith(".dist-info")]
        if not licenses or not any(archive.read(n) == (root / "LICENSE").read_bytes() for n in licenses):
            raise ValueError("missing or incorrect project license")
    print(f"verified {len(wanted)} Python/JSON files and project license in {wheel.name}")


if __name__ == "__main__":
    check(Path(sys.argv[1]), Path(__file__).resolve().parents[1] / "backend")
