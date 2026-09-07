"""TBD-499 -- the deploy-drift probe must say WHICH kind of drift it found.

`check-deploy-drift.sh` asserts a deliberate invariant, argued in its own
header: the active deployment's commit == the commit the latest release tag
points at. That invariant is right, and this ticket does not relax it.

What was wrong is that all three ways of violating it emitted the same
sentence -- "a failed deploy, or an auto-rollback" -- and both of those causes
mean production is BEHIND. Measured 2026-09-06: after the TBD-496 history
rewrite a manual ``deploy.yml --ref main`` put production three commits AHEAD
of the tag, and the probe reported an incident that had not happened.

⚠ These tests drive the REAL classifier against REAL git repositories built
per-test. They do not parse the script for an ``if``. A fence that asserts a
branch exists cannot tell whether the branch decides correctly, and this file
exists precisely because the previous wording was decided incorrectly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# ⚠ `git` is NOT installed in the backend dev container, so the ancestry cases
# skip there and run in CI, where `actions/checkout` guarantees git. This
# follows the existing convention in test_ci_change_detection.py. The wiring
# and read-only cases below need no git and run everywhere, so this module is
# never entirely vacuous locally.
HAS_GIT = shutil.which("git") is not None
requires_git = pytest.mark.skipif(HAS_GIT is False, reason="git not installed in this container")


def _find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "test.yml").exists():
            return candidate
    raise RuntimeError("Could not locate repo root")


REPO_ROOT = _find_repo_root(Path(__file__).resolve())


def _resolve_script() -> Path:
    for c in (
        Path("/app/repo-scripts/ci/classify-deploy-drift.sh"),
        REPO_ROOT / "scripts" / "ci" / "classify-deploy-drift.sh",
    ):
        if c.exists():
            return c
    raise FileNotFoundError(
        "classify-deploy-drift.sh not found. In the dev container this needs "
        "the ./scripts:/app/repo-scripts:ro mount."
    )


SCRIPT = _resolve_script()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
@requires_git
def repo(tmp_path: Path) -> Path:
    """A repo with a linear main and one divergent branch.

        A ── B        (main)
         └── C        (side)
    """
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", ".")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "f").write_text("a")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "a")
    (r / "f").write_text("b")
    _git(r, "commit", "-qam", "b")
    _git(r, "branch", "main-line")          # B, the linear tip
    _git(r, "checkout", "-q", "-b", "side", "HEAD~1")
    (r / "g").write_text("c")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "c")            # C, off A -- diverged from B
    return r


def _sha(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", rev)


def _classify(repo: Path, deployed: str, tag: str) -> str:
    return subprocess.run(
        ["bash", str(SCRIPT), deployed, tag],
        cwd=repo, capture_output=True, text=True,
    ).stdout.strip()


@requires_git
def test_identical_commits_are_not_drift(repo: Path) -> None:
    a = _sha(repo, "side~1")
    assert _classify(repo, a, a) == "same"


@requires_git
def test_behind_is_the_failed_deploy_case(repo: Path) -> None:
    """Production older than the tag: a release published that it is not
    serving. This is the ONLY case the original wording described."""
    older = _sha(repo, "side~1")
    newer = _sha(repo, "side")
    assert _classify(repo, older, newer) == "behind"


@requires_git
def test_ahead_is_not_a_failed_deploy(repo: Path) -> None:
    """Production newer than the tag: unreleased code, usually a manual
    `deploy.yml --ref main`. Reporting this as a rollback is the defect."""
    tag = _sha(repo, "side~1")
    deployed = _sha(repo, "side")
    assert _classify(repo, deployed, tag) == "ahead"


@requires_git
def test_diverged_is_distinguished_from_both(repo: Path) -> None:
    """Neither is an ancestor of the other: the most serious case, and the one
    previously indistinguishable from the two benign ones."""
    side_tip = _sha(repo, "side")
    other_line = _sha(repo, "main-line")
    assert _classify(repo, side_tip, other_line) == "diverged"


@requires_git
def test_a_commit_missing_from_the_repo_is_unknown_not_diverged(repo: Path) -> None:
    """⚠ The case that produced GitHub issue #745.

    A history rewrite orphans the SHA the deployment platform recorded, so the
    deployed commit can be absent from the repository entirely. Classifying
    that as `diverged` would report the most serious state for the most benign
    cause -- the code is fine, only its name is gone.
    """
    missing = "0" * 40
    assert _classify(repo, missing, _sha(repo, "side")) == "unknown"
    assert _classify(repo, _sha(repo, "side"), missing) == "unknown"


@requires_git
def test_missing_arguments_do_not_crash_the_probe(repo: Path) -> None:
    """The probe must never fail closed on a malformed input; it reports."""
    assert _classify(repo, "", "") == "unknown"


def test_the_probe_delegates_to_this_classifier() -> None:
    """The wiring, so the behaviour above is the behaviour production gets."""
    for c in (
        Path("/app/repo-scripts/ci/check-deploy-drift.sh"),
        REPO_ROOT / "scripts" / "ci" / "check-deploy-drift.sh",
    ):
        if c.exists():
            probe = c.read_text()
            break
    else:
        pytest.fail("check-deploy-drift.sh not found")

    assert "classify-deploy-drift.sh" in probe, (
        "check-deploy-drift.sh no longer calls the classifier, so these tests "
        "would pass while the probe decides on its own logic"
    )
    for state in ("behind", "ahead", "unknown"):
        assert f"{state})" in probe, f"probe has no branch for the {state!r} case"


def test_the_probe_is_still_read_only() -> None:
    """TBD-425: a deploy-based liveness probe would have DESTROYED working
    credentials in the 2026-08-20 incident rather than detecting drift sooner.
    This ticket changes reporting only."""
    for c in (
        Path("/app/repo-scripts/ci/check-deploy-drift.sh"),
        REPO_ROOT / "scripts" / "ci" / "check-deploy-drift.sh",
    ):
        if c.exists():
            lines = [
                ln for ln in c.read_text().splitlines()
                if not ln.lstrip().startswith("#")
            ]
            break
    body = "\n".join(lines)
    for forbidden in ("doctl apps create", "doctl apps update", "doctl apps delete",
                      "app_action", "git push"):
        assert forbidden not in body, (
            f"the drift probe must stay READ-ONLY (TBD-425); found {forbidden!r}"
        )
