"""INF-07: the apex deploy runs a wrangler resolved entirely from a lockfile.

The deploy step holds CLOUDFLARE_API_TOKEN. wrangler-action used to ``npm i wrangler@<version>``
there with no lockfile, so every transitive dependency floated to its newest release on each
deploy. Now ``frontend/apex-worker`` pins wrangler exactly, ``package-lock.json`` pins the tree
with integrity hashes, the job installs it with ``npm ci --ignore-scripts`` and runs that binary
with ``npx --no-install``. In the backend container ``frontend/apex-worker`` and ``.github`` are
read-only mounts (docker-compose.yml).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml


def _find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "ci.yml").exists():
            return candidate
    raise RuntimeError("Could not locate the repo root containing .github/workflows/ci.yml.")


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
WORKER = REPO_ROOT / "frontend" / "apex-worker"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "apex-deploy.yml"


def _package() -> dict:
    return json.loads((WORKER / "package.json").read_text(encoding="utf-8"))


def _lock() -> dict:
    return json.loads((WORKER / "package-lock.json").read_text(encoding="utf-8"))


def _deploy_steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["deploy-worker"]["steps"]


def test_wrangler_is_pinned_exactly_and_the_lockfile_hashes_its_whole_tree():
    version = _package()["devDependencies"]["wrangler"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"wrangler must be an exact version, not {version!r}"
    packages = _lock()["packages"]
    assert packages[""]["devDependencies"] == _package()["devDependencies"], "lockfile is stale"
    assert packages["node_modules/wrangler"]["version"] == version
    unhashed = [name for name, meta in packages.items() if name and not meta.get("integrity")]
    assert not unhashed, f"lockfile entries without an integrity hash: {unhashed[:5]}"


def test_the_deploy_installs_from_the_lockfile_without_scripts_then_runs_that_binary():
    steps = _deploy_steps()
    runs = [step.get("run", "") for step in steps]
    install = next(i for i, step in enumerate(steps) if step.get("run", "").startswith("npm ci"))
    assert "--ignore-scripts" in steps[install]["run"]
    assert steps[install]["working-directory"] == "frontend/apex-worker"

    deploy = next(i for i, step in enumerate(steps) if "CLOUDFLARE_API_TOKEN" in (step.get("env") or {}))
    assert install < deploy
    assert steps[deploy]["run"] == "npx --no-install wrangler deploy"
    assert steps[deploy]["working-directory"] == "frontend/apex-worker"
    # Nothing else in the job may hand the token to a tool that installs wrangler itself.
    assert not any("wrangler-action" in step.get("uses", "") for step in steps)
    assert sum("CLOUDFLARE_API_TOKEN }}" in json.dumps(step) for step in steps) == 1, runs


def test_no_unlocked_wrangler_is_fetched_anywhere_in_the_apex_path():
    sources = [WORKFLOW.read_text(encoding="utf-8"), (WORKER / "package.json").read_text(encoding="utf-8")]
    for text in sources:
        assert not re.search(r"npx\s+(-y|--yes)\b|wrangler@\d", text)
