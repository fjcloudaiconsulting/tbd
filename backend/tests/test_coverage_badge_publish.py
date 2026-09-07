"""TBD-500 -- the coverage badge must never be able to fail a required check,
and its README URL must be the versionless gist form.

Two properties are fenced here, both of which fail SILENTLY if broken:

1. **The publish steps cannot redden CI.** They are decoration. Each carries
   ``continue-on-error: true`` AND is gated to pushes on ``main``, and the
   script itself exits 0 on every error path. If a future edit drops the
   ``continue-on-error`` and the gist token expires, ``Backend Checks`` -- a
   required status check -- starts failing for a badge. Nothing else would
   catch that until it blocked a merge.

2. **The README uses the VERSIONLESS gist raw URL.** ``GET /gists/{id}``
   returns a ``raw_url`` containing a revision SHA that changes on every
   update. A badge built from that URL renders correctly forever while
   reporting the FIRST value it ever saw -- a green badge that has stopped
   tracking reality, which is worse than no badge. The versionless form
   ``/<gist-id>/raw/<filename>`` always serves the current revision.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


def _find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "test.yml").exists():
            return candidate
    raise RuntimeError("Could not locate repo root containing .github/workflows/test.yml")


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
WORKFLOW = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "test.yml").read_text())


def _resolve(*candidates: Path) -> Path:
    """First existing path wins.

    In the dev container `scripts/` is mounted at /app/repo-scripts (because
    /app/scripts is already the backend's own scripts dir) and README.md has
    its own mount. On a bare CI runner the whole repo is checked out, so the
    repo-root path is the one that exists. Trying both keeps this fence armed
    in BOTH places rather than silently skipping in one.
    """
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        "none of these exist: "
        + ", ".join(str(c) for c in candidates)
        + ". In the dev container this needs the ./README.md:/app/README.md:ro "
        "and ./scripts:/app/repo-scripts:ro mounts."
    )


README_PATH = _resolve(Path("/app/README.md"), REPO_ROOT / "README.md")
BADGE_SCRIPT = _resolve(
    Path("/app/repo-scripts/ci/update-coverage-badge.sh"),
    REPO_ROOT / "scripts" / "ci" / "update-coverage-badge.sh",
)
JOBS = WORKFLOW["jobs"]

# (job, step name) for every step that publishes a badge.
PUBLISH_STEPS = (
    ("backend", "Publish backend coverage badge"),
    ("frontend-work", "Publish frontend coverage badge"),
)


def _step(job: str, name: str) -> dict:
    for step in JOBS[job]["steps"]:
        if step.get("name") == name:
            return step
    pytest.fail(
        f"{job!r} has no step named {name!r}. If the badge publish was renamed or "
        f"removed, update PUBLISH_STEPS in this file deliberately."
    )


@pytest.mark.parametrize(("job", "name"), PUBLISH_STEPS)
def test_publish_step_cannot_fail_the_job(job: str, name: str) -> None:
    """A badge is decoration and must never redden a required status check."""
    step = _step(job, name)
    assert step.get("continue-on-error") is True, (
        f"{job}/{name!r} lost `continue-on-error: true`. `Backend Checks` and "
        f"`Frontend Checks` are required on main, so an expired gist token would "
        f"then block every merge for the sake of a badge."
    )


@pytest.mark.parametrize(("job", "name"), PUBLISH_STEPS)
def test_publish_step_only_runs_on_main(job: str, name: str) -> None:
    """The badge tracks `main`. Publishing from a PR would make it report
    whatever branch last ran, which is worse than stale.

    `workflow_dispatch` is allowed alongside `push` so the pipeline can be
    verified on demand rather than only by merging something. The branch gate
    is what keeps the badge honest, and it must survive.
    """
    condition = _step(job, name).get("if", "")
    assert "github.ref == 'refs/heads/main'" in condition, (
        f"{job}/{name!r} lost its main-only gate; the badge would report "
        f"whichever branch ran last"
    )
    assert "github.event_name" in condition, f"{job}/{name!r} has no event gate at all"
    assert "pull_request" not in condition, (
        f"{job}/{name!r} would publish from a pull request"
    )


def test_a_skipped_badge_update_is_visible() -> None:
    """A soft failure that nobody can see is a frozen badge.

    The script exits 0 on every error path so it can never redden a required
    check. That is correct, and it is exactly why the skip must announce
    itself: measured 2026-09-07, the first run after the secret was added had
    started before it existed, both steps reported `success`, and nothing was
    written. Without an annotation that state is indistinguishable from a
    working badge.
    """
    script = BADGE_SCRIPT.read_text()
    assert "::warning" in script, (
        "the soft-fail path must emit a ::warning:: annotation, or a skipped "
        "update is invisible in the Actions UI and the badge silently freezes"
    )
    # The annotation has to be inside the helper every guard routes through,
    # not bolted onto one branch.
    helper = script[script.index("warn()") : script.index("}", script.index("warn()"))]
    assert "::warning" in helper, "the annotation must live in warn(), not one call site"
    assert "exit 0" in helper, "warn() must still exit 0"


def test_readme_badges_use_the_versionless_gist_url() -> None:
    """A `raw_url` from the API pins a revision SHA and freezes the badge."""
    readme = README_PATH.read_text()
    decoded_readme = readme.replace("%2F", "/").replace("%3A", ":")
    endpoints = re.findall(r"gist\.githubusercontent\.com/[^)\s]*", decoded_readme)
    assert endpoints, "no gist-backed coverage badges found in README.md"

    for url in endpoints:
        decoded = url
        # Versionless: <user>/<gist-id>/raw/<filename>. Versioned inserts a
        # 40-char revision SHA between `raw` and the filename.
        assert not re.search(r"/raw/[0-9a-f]{40}/", decoded), (
            f"README badge pins a gist revision SHA and will freeze at its first "
            f"value: {decoded}. Use the versionless form /<gist-id>/raw/<filename>."
        )
        assert re.search(r"/raw/tbd-coverage-(backend|frontend)\.json$", decoded), (
            f"unexpected gist badge target: {decoded}"
        )


def test_badge_script_exits_zero_on_every_error_path() -> None:
    """Belt and braces with `continue-on-error`: the script itself fails soft.

    Parsed rather than executed -- the script's only side effect is a network
    PATCH, and a test that could reach it would be a test that can rewrite a
    gist.
    """
    script = BADGE_SCRIPT.read_text()

    assert "warn()" in script and "exit 0" in script, (
        "the script's error helper must exit 0; a badge failure must not "
        "propagate to the workflow"
    )
    # Every guard routes through warn(), which exits 0.
    for guard in ("GIST_TOKEN", "must be a bare number", "must be 'backend' or 'frontend'"):
        assert guard in script, f"missing soft-fail guard: {guard}"

    assert "set -uo pipefail" in script, "expected `set -uo pipefail`"
    assert "set -e" not in script.replace("set -uo pipefail", ""), (
        "`set -e` would defeat the soft-fail contract"
    )
