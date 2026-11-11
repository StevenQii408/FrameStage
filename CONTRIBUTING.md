# Contributing

Keep changes scoped and preserve historical task data. New strategies and Prompt rules must have explicit versions; reading old results must not silently re-analyze or re-render them.

## Development environment

Use Python 3.12+ for development, Node.js 22 and installed `ffmpeg` / `ffprobe`. CI exercises Python 3.12 and 3.13; older virtual environments are not supported by the release dependency set.

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
python -m pytest -q -p no:cacheprovider
python -m app.style_library check
cd ../front
npm ci
npm run typecheck
npm test
npm run test:layout
npm run build
```

No API Key is needed for these checks. Tests use temporary databases, explicit Mock providers and isolated authentication configuration. Paid/live tests must remain opt-in; never run them automatically for a pull request or expose cloud credentials to untrusted PR code. Do not add production data or private `.env` files to fixtures.

To inspect the UI without inference, start the regular backend with `STORYBOARD_PROVIDER=mock` and run the frontend. Browser writes still use the local session bootstrap. QA scripts that change test providers must run only against an explicitly isolated temporary backend; see `front/README.md`.

## Rebuilding Python locks

Install the `uv` dependency tool separately and run the following from `backend/`. Resolution accesses the package index, not a model API; it requires no cloud Key. Record the `uv --version` used in a dependency-update PR.

```bash
uv pip compile pyproject.toml --python-version 3.12 --universal \
  --constraint requirements.constraints --generate-hashes \
  --no-emit-index-url --no-header --no-annotate \
  --output-file requirements.lock
uv pip compile pyproject.toml --python-version 3.12 --universal --extra dev \
  --constraint requirements.constraints --generate-hashes \
  --no-emit-index-url --no-header --no-annotate \
  --output-file requirements-dev.lock
```

`requirements.constraints` contains reviewed compatibility/security boundaries; update it deliberately when a fix requires a newer release. Do not create locks with a machine-wide `pip freeze`, remove hashes to bypass an installation error, or embed private index credentials. Reinstall the resulting locks in a fresh environment and run the full checks before committing both runtime and development locks.

## What a useful change includes

- Explain the user-visible trigger, resulting behavior and validation performed.
- Cover the failure boundary being fixed: cancellation, stale revisions, retry budgets, duplicate events, transaction aborts or source ownership where relevant.
- Keep local edits and remote task state separate. Undo must not erase another task's new server identity or delivered assets.
- Preserve exact source time separately from generation timing. Do not reintroduce equal four-part action timelines or fixed source-shot duration limits.
- Keep facts, uncertain observations and authored staging distinct. Source ID coverage is not proof of narrative fidelity.
- Use existing dependency and module boundaries. Do not remove legacy readers until a tested migration or an explicit compatibility policy exists.

## Models, prompts and knowledge

Production defaults remain `qwen3.8-flash`, with no hidden model upgrade or local MLX fallback. Model/tool retries share their declared budgets. Tools return evidence; only validated application code commits business results.

Edit authored cinema style sources under `Prompts/cinema_style_library/`, then run these commands from the backend directory:

```bash
python -m app.style_library build
python -m app.style_library check
```

Do not bulk-copy images or text from repositories with unclear licensing. Record source and license boundaries for substantive third-party material. Optional studio or artist references describe a creative resemblance, not proven authorship of the input.

## Evaluation policy

Unit and Mock tests measure implementation behavior. Human-reviewed real clips measure semantic quality. Keep these results separate.

- Never generate human gold labels by copying the model prediction, parser output or production result being evaluated.
- Use self-owned or explicitly licensed media with hashes and documented provenance.
- Human labels need an actual reviewer and evidence. Blank, unsigned or automatically drafted reviews are pending, not a successful benchmark.
- Evaluate missed/added facts, action order, event grouping, style confusion and uncertainty as well as JSON validity.
- Store raw paid responses and private media outside the release. Publish only sanitized, permitted aggregate reports.

## Release review

Use the release export tooling and inspect its manifest before publication. Verify a clean export installs and runs without the original machine's private data, credentials, global Python packages or absolute paths. Public CI uses least-privilege read permissions and no model keys. Dependency updates must update lockfiles and pass the same checks; do not use a blanket audit force-fix without reviewing compatibility.

The root MIT license covers project-authored code and documentation. It does not relicense uploaded media, external assets or third-party source. See `SECURITY.md` for the local API and disclosure policy.
# 发布前检查

```bash
python scripts/secret_scan.py --history
python scripts/release_source.py --verify-head
python scripts/release_source.py --output release-artifacts/frameforge-source.zip
```

不带参数时检查工作区候选文件；`--verify-head` 和 `--output` 要求公开源文件全部与当前提交一致，必需文件完整，提交中没有私有文件。ZIP 内清单记录提交和每个文件的 SHA-256。演示目录、原始云端响应、凭据、运行数据和本地历史文档不发布。只发布已检查的 main 分支，不镜像推送私有 refs。
