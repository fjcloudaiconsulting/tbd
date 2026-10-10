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

import json
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


_RUN_IDS = iter(range(1000, 10**6))


def _jobs(backend: tuple[str, str | None], frontend: tuple[str, str | None] = ("completed", "success"), *extra: dict) -> list[dict]:
    """The jobs of one ci.yml run. INFRA-159: the gate reads the two required
    gate jobs, not the run's own conclusion (which now includes release)."""
    jobs = [
        {"id": 1, "name": "Backend Checks", "status": backend[0], "conclusion": backend[1]},
        {"id": 2, "name": "Frontend Checks", "status": frontend[0], "conclusion": frontend[1]},
        {"id": 3, "name": "Detect Changes", "status": "completed", "conclusion": "success"},
    ]
    return jobs + list(extra)


def _runs_payload(*runs: str) -> str:
    return '{"total_count": %d, "workflow_runs": [%s]}' % (len(runs), ",".join(runs))


def _run(
    status: str,
    conclusion: str | None,
    started: str = "2026-08-12T18:30:38Z",
    *,
    event: str = "push",
    branch: str = "main",
) -> str:
    """A workflow run whose two gate jobs mirror `status`/`conclusion`."""
    concl = "null" if conclusion is None else f'"{conclusion}"'
    run_id = next(_RUN_IDS)
    _GATES_BY_RUN[run_id] = _jobs((status, conclusion))
    return (
        f'{{"id": {run_id}, "status": "{status}", "conclusion": {concl}, '
        f'"run_started_at": "{started}", "event": "{event}", "head_branch": "{branch}"}}'
    )


_GATES_BY_RUN: dict[int, list[dict]] = {}


def _invoke(
    tmp_path: Path,
    payload: str | None,
    *,
    exit_code: int = 0,
    sha: str = FULL_SHA,
    jobs_by_run: dict[int, list[dict]] | None = None,
    jobs_exit_code: int = 0,
):
    """Run the REAL script with `gh` stubbed to emit `payload`.

    `exit_code` non-zero models the API call itself failing -- a 403 from a
    missing `actions: read` scope is the realistic case, and it must not be
    mistaken for "the suite passed".
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "runs.json").write_text(payload if payload is not None else "")
    for run_id, jobs in (jobs_by_run if jobs_by_run is not None else _GATES_BY_RUN).items():
        (tmp_path / f"jobs-{run_id}.json").write_text(json.dumps({"jobs": jobs}))
    stub = bin_dir / "gh"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            # $1 is `api`, $2 the endpoint. The jobs endpoint carries the run id.
            if [[ "$2" =~ /actions/runs/([0-9]+)/jobs ]]; then
              cat "{tmp_path}/jobs-${{BASH_REMATCH[1]}}.json" 2>/dev/null || echo '{{"jobs": []}}'
              exit {jobs_exit_code}
            fi
            cat "{tmp_path}/runs.json"
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


def test_success_allows_the_deploy(tmp_path):
    """THE OVER-REACH CONTROL. Without this, a script that returned 1
    unconditionally would pass every other assertion in this file while
    permanently blocking all deploys."""
    res = _invoke(tmp_path, _runs_payload(_run("completed", "success")))
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
    # Exit 1 alone would also pass a script that fails at once on "absent",
    # which would block every release whose push run is not listed yet.
    assert "no Test run" in res.stdout and "timed out" in res.stderr, res.stdout + res.stderr


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


@pytest.mark.parametrize(
    ("event", "branch"),
    [
        ("pull_request", "feature"),
        # A fork PR from a branch named `main`: only the event check drops it.
        ("pull_request", "main"),
        ("workflow_dispatch", "main"),
        ("push", "feature"),
    ],
)
def test_only_the_push_run_on_main_counts(tmp_path, event, branch):
    """INFRA-96. The release commit's push run on `main` is the one that
    builds the sha-<7> images `promote` retags, so it is the only run the
    gate may read. Any other run on the same sha is ignored, even when newer.

    ⚠ The run that must be IGNORED is the NEWEST and listed LAST, so a script
    without the event/branch filter picks it ("newest run wins") and goes red.
    """
    # Fail-open direction: a red push run must not be masked by a newer
    # green run of another event or branch.
    payload = _runs_payload(
        _run("completed", "failure", started="2026-08-12T18:00:00Z"),
        _run("completed", "success", started="2026-08-12T19:00:00Z", event=event, branch=branch),
    )
    res = _invoke(tmp_path, payload)
    assert res.returncode == 1, res.stdout + res.stderr

    # Mirror: a green push run must not be blocked by a newer red one.
    payload = _runs_payload(
        _run("completed", "success", started="2026-08-12T18:00:00Z"),
        _run("completed", "failure", started="2026-08-12T19:00:00Z", event=event, branch=branch),
    )
    res = _invoke(tmp_path, payload)
    assert res.returncode == 0, res.stdout + res.stderr


def test_a_green_non_push_run_alone_fails_closed(tmp_path):
    """Only a pull_request run exists for the sha: that is "no push run yet",
    so the gate waits and fails closed at the deadline instead of shipping."""
    payload = _runs_payload(
        _run("completed", "success", event="pull_request", branch="feature"),
    )
    res = _invoke(tmp_path, payload)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "no Test run" in res.stdout and "timed out" in res.stderr, res.stdout + res.stderr


def _single_run(jobs: list[dict], run_conclusion: str | None = "failure"):
    """One push run on main with the given jobs. The RUN's own conclusion is
    deliberately unrelated to the gates unless a test says otherwise."""
    run_id = next(_RUN_IDS)
    run = (
        f'{{"id": {run_id}, "status": "completed", "conclusion": "{run_conclusion}", '
        '"run_started_at": "2026-08-12T18:30:38Z", "event": "push", "head_branch": "main"}'
    )
    return _runs_payload(run), {run_id: jobs}


def test_release_side_failure_with_green_gates_still_allows_the_deploy(tmp_path):
    """INFRA-159, the regression. release/promote/smoke now live in ci.yml, so
    the push run's conclusion is `failure` when the shared release-commit gate
    (or promote, or smoke) is red. The landing deploy depends on the tests
    only and must pass."""
    extra = [
        {"id": 90, "name": "Release / release", "status": "completed", "conclusion": "failure"},
        {"id": 91, "name": "Release / promote / promote", "status": "completed", "conclusion": "skipped"},
    ]
    payload, jobs = _single_run(_jobs(("completed", "success"), ("completed", "success"), *extra))
    res = _invoke(tmp_path, payload, jobs_by_run=jobs)
    assert res.returncode == 0, res.stdout + res.stderr


def test_release_side_job_still_running_does_not_delay_the_deploy(tmp_path):
    extra = [{"id": 90, "name": "Release / smoke / smoke", "status": "in_progress", "conclusion": None}]
    payload, jobs = _single_run(_jobs(("completed", "success"), ("completed", "success"), *extra), None)
    res = _invoke(tmp_path, payload, jobs_by_run=jobs)
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize("which", ["backend", "frontend"])
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_either_gate_failing_blocks_even_if_the_other_is_green(tmp_path, which, conclusion):
    bad = ("completed", conclusion)
    good = ("completed", "success")
    jobs = _jobs(bad, good) if which == "backend" else _jobs(good, bad)
    payload, jobs_by_run = _single_run(jobs, "success")
    res = _invoke(tmp_path, payload, jobs_by_run=jobs_by_run)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "timed out" not in res.stderr, "must fail on the conclusion, not by timing out"


@pytest.mark.parametrize("pending", [("in_progress", None), ("queued", None)])
def test_a_pending_gate_waits_then_fails_closed(tmp_path, pending):
    payload, jobs = _single_run(_jobs(("completed", "success"), pending), None)
    res = _invoke(tmp_path, payload, jobs_by_run=jobs)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "timed out" in res.stderr, res.stdout + res.stderr


def test_missing_gate_jobs_time_out_fail_closed(tmp_path):
    """A run exists but neither gate has a job yet (or one is missing)."""
    payload, jobs = _single_run([], None)
    res = _invoke(tmp_path, payload, jobs_by_run=jobs)
    assert res.returncode == 1 and "timed out" in res.stderr, res.stdout + res.stderr

    only_backend = [{"id": 1, "name": "Backend Checks", "status": "completed", "conclusion": "success"}]
    payload, jobs = _single_run(only_backend, None)
    res = _invoke(tmp_path, payload, jobs_by_run=jobs)
    assert res.returncode == 1 and "timed out" in res.stderr, res.stdout + res.stderr


def test_a_rerun_gate_job_wins_by_highest_id(tmp_path):
    """A repeated gate name takes the highest job id: the re-run that
    unblocks, and (mirror) a later red one that must not be masked."""
    older_red = {"id": 0, "name": "Backend Checks", "status": "completed", "conclusion": "failure"}
    payload, jobs = _single_run(_jobs(("completed", "success"), ("completed", "success"), older_red), None)
    assert _invoke(tmp_path, payload, jobs_by_run=jobs).returncode == 0

    newer_red = {"id": 99, "name": "Backend Checks", "status": "completed", "conclusion": "failure"}
    payload, jobs = _single_run(_jobs(("completed", "success"), ("completed", "success"), newer_red), None)
    assert _invoke(tmp_path, payload, jobs_by_run=jobs).returncode == 1


def test_jobs_api_error_fails_closed(tmp_path):
    payload, jobs = _single_run(_jobs(("completed", "success")))
    res = _invoke(tmp_path, payload, jobs_by_run=jobs, jobs_exit_code=1)
    assert res.returncode == 1, res.stdout + res.stderr
