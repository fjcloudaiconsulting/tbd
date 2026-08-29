"""TBD-404 -- ``scripts/ci/detect-changed-areas.sh`` classifies a PR's diff into
the per-area booleans that gate the work jobs in ``.github/workflows/test.yml``.

WHY THIS EXISTS

A wrong `true` here costs runner seconds on a public repo where they are free.
A wrong `false` skips the suite that would have caught a regression and reports
a GREEN required context. The two errors are not symmetric, so every
uncertainty in the script resolves to `true` and every one of those branches is
fenced below.

⚠ EVERY assertion drives the REAL script against a REAL throwaway git
repository. Nothing here re-implements the path rules; a test that restated
them would pass against a script that had them backwards.

⚠ `git` is NOT installed in the backend dev container, so the git-backed cases
skip there and run in CI (where `actions/checkout` guarantees git). The
event-level cases below need no git and run everywhere, so the module is never
entirely vacuous.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HAVE_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(
    not HAVE_GIT,
    reason="git is not installed in the backend container image; this case runs in CI",
)


def _find_script() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        found = candidate / "scripts" / "ci" / "detect-changed-areas.sh"
        if found.is_file():
            return found
    container_mount = Path("/app/repo-scripts/ci/detect-changed-areas.sh")
    if container_mount.is_file():
        return container_mount
    raise RuntimeError(
        "Could not locate scripts/ci/detect-changed-areas.sh. In the backend "
        "container this needs the ./scripts:/app/repo-scripts:ro mount."
    )


SCRIPT = _find_script()

# A base tree with one file in each area, so every scenario below is a real
# modification of a real tracked file rather than an addition in a vacuum.
BASE_TREE = {
    "README.md": "docs\n",
    "specs/2026-01-01-a-spec.md": "spec\n",
    "backend/app/main.py": "x = 1\n",
    "frontend/components/Thing.tsx": "export const Thing = () => null;\n",
    "frontend/tests/fixtures/report-sources.json": "{}\n",
    "docker-compose.yml": "services: {}\n",
    ".github/workflows/test.yml": "name: Test\n",
    "scripts/ci/await-test-run.sh": "true\n",
    "pfv": "#!/bin/sh\n",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _repo(tmp_path: Path, changes: dict[str, str | None]) -> tuple[Path, str]:
    """Build a repo with BASE_TREE committed, then commit `changes` on top.

    A `None` value deletes the file, so the deletion case is built structurally.
    Returns (repo, base_sha).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for rel, body in BASE_TREE.items():
        _write(repo, rel, body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    if changes:
        for rel, body in changes.items():
            if body is None:
                (repo / rel).unlink()
            else:
                _write(repo, rel, body)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "change")
    return repo, base


def _detect(repo: Path, tmp_path: Path, *, event: str = "pull_request", base: str = "") -> dict:
    out = tmp_path / "gh_output"
    out.write_text("")
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "EVENT_NAME": event,
            "BASE_SHA": base,
            "GITHUB_OUTPUT": str(out),
        },
    )
    assert r.returncode == 0, f"the detector must never fail: {r.stderr}"
    parsed = dict(
        line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
    )
    assert set(parsed) == {"backend", "frontend", "migrations"}, (
        f"the detector must emit exactly the three area outputs, got {sorted(parsed)}. "
        "A missing key evaluates to the empty string in the workflow, which "
        "skips the job AND fails assert-gate.sh -- a permanently red gate."
    )
    return parsed


# ── Event-level branches (no git needed; these run in every environment) ──────


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule", ""])
def test_anything_that_is_not_a_pull_request_runs_everything(event, tmp_path):
    """⚠ On `push: branches: [main]` the answer is EVERYTHING, deliberately.

    That run is the substitute for branch protection's `strict: true` and it is
    what `scripts/ci/await-test-run.sh` gates the release on. Narrowing it
    would make the deploy interlock gate on a partial suite.
    """
    out = _detect(tmp_path, tmp_path, event=event)
    assert out == {"backend": "true", "frontend": "true", "migrations": "true"}


def test_a_pull_request_with_no_base_sha_runs_everything(tmp_path):
    """Fail safe means fail TRUE: an unusable input is never read as "nothing
    changed"."""
    out = _detect(tmp_path, tmp_path, event="pull_request", base="")
    assert out == {"backend": "true", "frontend": "true", "migrations": "true"}


# ── Classification ───────────────────────────────────────────────────────────


@needs_git
def test_a_docs_only_change_runs_nothing(tmp_path):
    """The whole point of the ticket. Measured before TBD-347's own change: 4
    of the last 12 merged PRs were docs/specs-only."""
    repo, base = _repo(tmp_path, {"README.md": "docs v2\n", "specs/2026-01-01-a-spec.md": "spec v2\n"})
    out = _detect(repo, tmp_path, base=base)
    assert out == {"backend": "false", "frontend": "false", "migrations": "false"}


@needs_git
def test_a_backend_change_does_not_run_the_frontend_suite(tmp_path):
    repo, base = _repo(tmp_path, {"backend/app/main.py": "x = 2\n"})
    out = _detect(repo, tmp_path, base=base)
    assert out == {"backend": "true", "frontend": "false", "migrations": "true"}


@needs_git
def test_a_frontend_change_does_not_run_the_backend_shards(tmp_path):
    repo, base = _repo(tmp_path, {"frontend/components/Thing.tsx": "export const Thing = () => 1;\n"})
    out = _detect(repo, tmp_path, base=base)
    assert out == {"backend": "false", "frontend": "true", "migrations": "false"}


@needs_git
def test_a_shared_frontend_fixture_is_a_backend_change_too(tmp_path):
    """⚠ `backend/tests/test_report_sources_frontend_contract.py` and
    `test_period_status_frontend_contract.py` READ `frontend/tests/fixtures/`
    (docker-compose mounts it into the backend service). Classifying that
    directory as frontend-only would let a fixture edit skip the backend
    contract test that exists to catch exactly that drift."""
    repo, base = _repo(tmp_path, {"frontend/tests/fixtures/report-sources.json": '{"a":1}\n'})
    out = _detect(repo, tmp_path, base=base)
    assert out == {"backend": "true", "frontend": "true", "migrations": "true"}


@needs_git
@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/test.yml",
        "scripts/ci/await-test-run.sh",
        "docker-compose.yml",
        "pfv",
    ],
)
def test_an_unclassified_path_runs_everything(path, tmp_path):
    """⚠ The default branch must be EVERYTHING, not nothing. Backend tests
    assert on several repo-root paths (`.do/app.yaml`, `.github/workflows/*`,
    `scripts/ci/*`, `pfv`), and a new top-level path nobody thought about must
    never be silently inert."""
    repo, base = _repo(tmp_path, {path: "changed\n"})
    out = _detect(repo, tmp_path, base=base)
    assert out == {"backend": "true", "frontend": "true", "migrations": "true"}


@needs_git
def test_docs_alongside_code_do_not_dilute_the_code(tmp_path):
    """The inert classification is per FILE, not per PR: one backend file in a
    mostly-prose PR still runs the backend suite."""
    repo, base = _repo(
        tmp_path, {"README.md": "docs v2\n", "backend/app/main.py": "x = 3\n"}
    )
    out = _detect(repo, tmp_path, base=base)
    assert out == {"backend": "true", "frontend": "false", "migrations": "true"}


@needs_git
def test_a_deleted_file_still_counts_as_a_change(tmp_path):
    """`git diff --name-only` lists deletions, and deleting a source file is
    every bit as much a change as editing one."""
    repo, base = _repo(tmp_path, {"frontend/components/Thing.tsx": None})
    out = _detect(repo, tmp_path, base=base)
    assert out["frontend"] == "true"


@needs_git
def test_the_migration_runbook_is_a_backend_change_despite_being_markdown(tmp_path):
    """⚠ `backend/tests/test_rotation_runbook_credential_bindings.py` PARSES
    `infra/MIGRATION.md`, so a prose-only edit to it is a backend change. It is
    matched ABOVE the inert `*.md` pattern on purpose: classified as prose, the
    six shards skip and the fence is disarmed on exactly the docs-only PR that
    drifts the runbook away from `.do/app.yaml` -- the drift it exists to catch.

    This is the same exception `frontend/tests/fixtures/` already carries, for
    the same reason: a test reads the file."""
    repo, base = _repo(tmp_path, {"infra/MIGRATION.md": "runbook v2\n"})
    out = _detect(repo, tmp_path, base=base)
    assert out == {"backend": "true", "frontend": "false", "migrations": "true"}


@needs_git
def test_another_infra_markdown_file_is_still_prose(tmp_path):
    """The exception above is scoped to ONE file, not to `infra/*.md`. No test
    reads the others, and widening it would run the backend suite for every
    infra note."""
    repo, base = _repo(tmp_path, {"infra/NOTES.md": "notes\n"})
    out = _detect(repo, tmp_path, base=base)
    assert out == {"backend": "false", "frontend": "false", "migrations": "false"}


@needs_git
def test_a_nested_markdown_file_is_still_prose(tmp_path):
    """`backend/README.md` must not run the backend suite: the inert patterns
    are checked BEFORE the area prefixes, on purpose."""
    repo, base = _repo(tmp_path, {"backend/NOTES.md": "notes\n"})
    out = _detect(repo, tmp_path, base=base)
    assert out == {"backend": "false", "frontend": "false", "migrations": "false"}


@needs_git
def test_an_empty_diff_is_an_answer_not_an_error(tmp_path):
    repo, base = _repo(tmp_path, {})
    out = _detect(repo, tmp_path, base=base)
    assert out == {"backend": "false", "frontend": "false", "migrations": "false"}


@needs_git
def test_an_unresolvable_base_sha_runs_everything(tmp_path):
    """A shallow clone that never fetched the base commit must not read as
    "nothing changed"."""
    repo, _ = _repo(tmp_path, {"backend/app/main.py": "x = 4\n"})
    out = _detect(repo, tmp_path, base="0" * 40)
    assert out == {"backend": "true", "frontend": "true", "migrations": "true"}


@needs_git
def test_migrations_tracks_backend(tmp_path):
    """`Migration Checks` boots the whole app against real MySQL and hits
    /ready, so its scope is every backend change, not just `backend/alembic/`.
    If that is ever narrowed, narrow it in the script -- not by hand-wiring a
    different output in the workflow."""
    repo, base = _repo(tmp_path, {"backend/app/main.py": "x = 5\n"})
    out = _detect(repo, tmp_path, base=base)
    assert out["migrations"] == out["backend"] == "true"
