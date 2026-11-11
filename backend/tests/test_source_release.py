import importlib.util
from pathlib import Path
import subprocess
import zipfile


def release_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "release_source.py"
    spec = importlib.util.spec_from_file_location("release_source", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_includes_current_untracked_source_and_required_private_media_worker():
    release = release_module()
    assert release.distributable("backend/app/media_worker.py")
    assert release.distributable("backend/app/runtime.py")
    assert release.distributable("front/src/components/CanvasVideoPlayer.vue")
    assert release.distributable("backend/requirements.lock")
    assert release.distributable("backend/.env.example")
    assert release.distributable(".github/workflows/ci.yml")


def test_release_never_bundles_credential_storage_media_or_diagnostics():
    release = release_module()
    for path in ["backend/.env", "backend/.env.backup", "backend/data/jobs/task/input.mp4",
                 "backend/evals/results/report.json", "backend/.venv312/lib/code.py",
                 "系统展示/画布.png", "docs/history/old-fix.md", "Prompts/ai-shortfilm-prompts/README.md",
                 "front/node_modules/module/index.ts", "front/public/demo.png", "front/public/other.svg",
                 "front/public/frameforge.svg", "front/public/frameforge-32.png",
                 "front/src/framestage.svg", "front/public/framestage-32.png.bak", "../private.txt", "/private/app.py"]:
        assert not release.distributable(path), path


def test_incomplete_source_without_git_is_rejected(tmp_path):
    release = release_module()
    (tmp_path / "README.md").write_text("example")
    files, problems = release.audit_tree(tmp_path)
    assert [f["path"] for f in files] == ["README.md"]
    assert any(p["path"] == "backend/app/media_worker.py" for p in problems)
    assert {p["path"] for p in problems} >= {
        "front/public/framestage.svg", "front/public/framestage-32.png", "front/public/apple-touch-icon.png",
    }
    assert not release.distributable("系统展示/README.md")
    assert not release.distributable("backend/tests/fixtures/private-cloud-output.json")


def test_verify_head_flags_drift_missing_private_and_uncommitted(tmp_path):
    release = release_module()
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    for name in ("README.md", "LICENSE", "private.bin"):
        (tmp_path / name).write_text("fixture")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    (tmp_path / "README.md").write_text("changed")
    (tmp_path / "LICENSE").unlink()
    (tmp_path / "CONTRIBUTING.md").write_text("pending")
    files, _ = release.audit_tree(tmp_path)
    errors = {p["path"]: p["reason"] for p in release.head_problems(tmp_path, files)}
    assert errors == {"README.md": "working file differs from HEAD", "LICENSE": "committed file missing from working source",
                      "CONTRIBUTING.md": "source is not committed in HEAD", "private.bin": "non-distributable entry tracked in HEAD"}


def test_source_zip_ships_reviewed_icons(tmp_path):
    release = release_module()
    icons = {"front/public/framestage.svg", "front/public/framestage-32.png", "front/public/apple-touch-icon.png"}
    for name in release.REQUIRED | icons:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((release.ROOT / name).read_bytes() if name in icons else b"fixture")
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    (tmp_path / "front/public/demo.png").write_bytes(b"private media")
    manifest, problems = release.audit_tree(tmp_path)
    assert not problems
    assert not release.head_problems(tmp_path, manifest)
    output = tmp_path / "source.zip"
    release.export_zip(tmp_path, output, manifest, git("rev-parse", "HEAD"))
    with zipfile.ZipFile(output) as archive:
        assert {name for name in archive.namelist() if name.startswith("front/public/")} == icons
        for name in icons:
            assert archive.read(name) == (release.ROOT / name).read_bytes()


def test_brand_assets_are_validated_and_scanned(tmp_path):
    release = release_module()
    public = tmp_path / "front/public"
    public.mkdir(parents=True)
    svg = "front/public/framestage.svg"
    small = "front/public/framestage-32.png"
    touch = "front/public/apple-touch-icon.png"
    (tmp_path / small).write_bytes((release.ROOT / touch).read_bytes())
    (tmp_path / touch).write_bytes(b"not a PNG")
    (tmp_path / svg).write_bytes(b"<html/>")
    def errors():
        _, problems = release.audit_tree(tmp_path)
        return {p["path"]: p["reason"] for p in problems}
    reasons = errors()
    assert "32x32" in reasons[small]
    assert "must be a PNG" in reasons[touch]
    assert "SVG root" in reasons[svg]
    (tmp_path / small).write_bytes((release.ROOT / small).read_bytes()[:-4])
    (tmp_path / touch).write_bytes(bytes(256 * 1024 + 1))
    (tmp_path / svg).write_text('<svg xmlns="http://www.w3.org/2000/svg"><!-- sk-' + "A" * 24 + " --></svg>")
    reasons = errors()
    assert "invalid PNG" in reasons[small]
    assert "review limit" in reasons[touch]
    assert "possible credential" in reasons[svg]
