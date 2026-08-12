"""TBD-391 -- ``scripts/ci/await-test-run.sh`` is the interlock that stops a
deploy shipping before the post-merge suite reports.

WHY THIS FENCE EXISTS AT ALL, AND WHY IT IS SHAPED LIKE THIS

The ticket's DoD asks that a FAILING post-merge suite be shown to block the
deploy. Taken literally that is impossible before merge: ``release.yml``
triggers only on ``push: branches: [main]``, so no feature branch can ever
produce a Release run to observe.

The design answer was to put the decision in a SCRIPT rather than inline YAML,
which turns the central claim into something testable offline. That is the
whole reason the gate is a shell script and not six lines of ``run:``.

⚠ EVERY assertion below drives the REAL script with a stubbed ``gh`` on PATH.
Nothing here re-implements the decision logic; a test that restated the rules
would pass against a script that had them backwards.

⚠ The GitHub API states covered here are the ones nobody exercises by accident
and where "fails open" would be invisible: ``cancelled`` (reachable whenever a
pending post-merge run is superseded in its concurrency group), ``timed_out``,
a run that never appears, and an API error such as a 403 from a missing
``actions: read`` permission.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

def _find_script() -> Path:
    """Locate `scripts/ci/await-test-run.sh` in either layout.

    On a bare CI runner the whole repo is checked out, so walking upward from
    this file finds it at `REPO_ROOT/scripts/ci`. Inside the backend container
    only `backend/` is mounted at `/app`, and `/app/scripts` is already
    `backend/scripts` — so the repo-root `scripts/` gets its own read-only
    mount at `/app/repo-scripts` (see docker-compose.yml).

    Raises rather than skipping: a skip here would make the gate's central
    fence silently absent in whichever environment happened to lack the path,
    which is exactly how a fence becomes decoration.
    """
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        found = candidate / "scripts" / "ci" / "await-test-run.sh"
        if found.is_file():
            return found
    container_mount = Path("/app/repo-scripts/ci/await-test-run.sh")
    if container_mount.is_file():
        return container_mount
    raise RuntimeError(
        "Could not locate scripts/ci/await-test-run.sh. In the backend "
        "container this needs the ./scripts:/app/repo-scripts:ro mount; a "
        "container built before that mount existed shows this module red. "
        "Run `docker compose up -d --force-recreate backend` once."
    )


SCRIPT = _find_script()

FULL_SHA = "1af0b388fd27a4b621d9761a6a3ef1d153f0704c"


def _runs_payload(*runs: str) -> str:
    return '{"total_count": %d, "workflow_runs": [%s]}' % (len(runs), ",".join(runs))


def _run(status: str, conclusion: str | None, started: str = "2026-08-12T18:30:38Z") -> str:
    concl = "null" if conclusion is None else f'"{conclusion}"'
    return (
        f'{{"status": "{status}", "conclusion": {concl}, '
        f'"run_started_at": "{started}"}}'
    )


def _invoke(tmp_path: Path, payload: str | None, *, exit_code: int = 0, sha: str = FULL_SHA):
    """Run the REAL script with `gh` stubbed to emit `payload`.

    `exit_code` non-zero models the API call itself failing -- a 403 from a
    missing `actions: read` scope is the realistic case, and it must not be
    mistaken for "the suite passed".
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "gh"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            cat <<'PAYLOAD'
            {payload if payload is not None else ""}
            PAYLOAD
            exit {exit_code}
            """
        )
    )
    stub.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["GH_REPO"] = "fjcloudaiconsulting/tbd"
    # Keep the wall clock out of the test: one poll, then the deadline.
    env["AWAIT_TEST_POLL_SECONDS"] = "0"
    env["AWAIT_TEST_TIMEOUT_SECONDS"] = "0"

    return subprocess.run(
        ["bash", str(SCRIPT), sha],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_script_exists_and_is_executable():
    """Positive baseline. Without it, a wrong path would make every assertion
    below fail for the wrong reason and read as 'the gate is strict'."""
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


def test_success_allows_the_deploy():
    """THE OVER-REACH CONTROL. Without this, a script that returned 1
    unconditionally would pass every other assertion in this file while
    permanently blocking all deploys."""
    with pytest.MonkeyPatch.context():
        pass
    res = _invoke(Path("/tmp"), _runs_payload(_run("completed", "success")))
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "cancelled", "timed_out", "action_required", "neutral", "skipped"],
)
def test_any_non_success_conclusion_blocks_the_deploy(tmp_path, conclusion):
    """THE HEADLINE FENCE, and the DoD's central claim.

    Every one of these means "the suite did not pass". `cancelled` is the one
    that matters most in practice: a PENDING post-merge run is cancelled when
    a newer merge supersedes it in the concurrency group, so it is reachable
    on any burst of merges -- and treating it as success would silently ship
    a commit whose suite never ran at all.
    """
    res = _invoke(tmp_path, _runs_payload(_run("completed", conclusion)))
    assert res.returncode == 1, (
        f"conclusion={conclusion!r} must block the deploy; "
        f"got exit {res.returncode}\n{res.stdout}{res.stderr}"
    )


def test_api_error_fails_closed_within_the_timeout(tmp_path):
    """A 403 from a missing `actions: read` permission is the realistic
    version of this, and it is the failure that would look exactly like a
    working gate. It must NOT be read as success."""
    res = _invoke(tmp_path, "", exit_code=1)
    assert res.returncode == 1, res.stdout + res.stderr


def test_missing_run_fails_closed_rather_than_shipping(tmp_path):
    """No Test run for this SHA. The script waits, then fails closed at the
    deadline. Kills treating an absent run as 'nothing to wait for'."""
    res = _invoke(tmp_path, _runs_payload())
    assert res.returncode == 1, res.stdout + res.stderr


def test_in_progress_does_not_ship_early(tmp_path):
    """A still-running suite must never be mistaken for a passing one."""
    res = _invoke(tmp_path, _runs_payload(_run("in_progress", None)))
    assert res.returncode == 1, res.stdout + res.stderr


def test_newest_run_wins_so_a_rerun_can_unblock(tmp_path):
    """A re-run of a red suite should be able to unblock a deploy without a
    force-push. Ordering is by `run_started_at`, so the newest wins.

    ⚠ THE ARRAY ORDER IS LOAD-BEARING AND WAS WRONG ONCE. In both fixtures the
    run that SHOULD win is listed LAST, so a script that took `workflow_runs[0]`
    picks the wrong one and this goes red. An earlier revision listed the
    winner first in both cases, which meant `runs[0]` produced the right answer
    by accident and the mutant survived -- measured, not theorised. If you
    reorder these, re-run the injection.
    """
    # Newest is SUCCESS and is listed second: `runs[0]` would read the failure.
    payload = _runs_payload(
        _run("completed", "failure", started="2026-08-12T18:00:00Z"),
        _run("completed", "success", started="2026-08-12T19:00:00Z"),
    )
    res = _invoke(tmp_path, payload)
    assert res.returncode == 0, res.stdout + res.stderr

    # Mirror: newest is FAILURE and is listed second, so `runs[0]` would read
    # the older success and ship a commit whose latest suite is red.
    payload = _runs_payload(
        _run("completed", "success", started="2026-08-12T18:00:00Z"),
        _run("completed", "failure", started="2026-08-12T19:00:00Z"),
    )
    res = _invoke(tmp_path, payload)
    assert res.returncode == 1, res.stdout + res.stderr


def test_abbreviated_sha_fails_fast_and_distinctly(tmp_path):
    """The `?head_sha=` query matches ONLY a full 40-character SHA; an
    abbreviated one returns total_count 0 silently. `${{ github.sha }}` is
    always full, so this guards a hand-run verification command -- and it
    exits 2, distinct from the gate's own 1, so it cannot be misread as
    'the suite failed'."""
    res = _invoke(tmp_path, _runs_payload(_run("completed", "success")), sha="1af0b38")
    assert res.returncode == 2, res.stdout + res.stderr
