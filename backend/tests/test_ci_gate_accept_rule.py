"""TBD-404 -- ``scripts/ci/assert-gate.sh`` decides what a required aggregate
gate may accept from an upstream job.

WHY THIS EXISTS

`test.yml` now scopes its work jobs to the area they test, so a docs-only PR
skips the six backend shards, `Migration Checks` and the frontend suite. The
two REQUIRED contexts (`Backend Checks`, `Frontend Checks`) still run -- they
must, because a required context that never reports blocks its PR forever --
so they have to be able to accept a `skipped` upstream.

⚠ THE FOOTGUN. GitHub reports `skipped` for two entirely different situations,
and `needs.<job>.result` cannot tell them apart:

    (a) the job's own `if:` was false           -> nothing to test
    (b) an UPSTREAM job failed or was cancelled -> the suite is BROKEN

So the obvious edit -- accept `"skipped"` unconditionally -- turns a genuinely
red suite into a green REQUIRED gate, on the exact gate that exists to stop a
red suite merging. `test_upstream_failure_is_not_excused_by_a_skip` is the
fence for that, and it is the point of the whole ticket.

⚠ EVERY assertion drives the REAL script. Nothing here re-implements the rule;
a test that restated it would pass against a script that had it backwards.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _find_script() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        found = candidate / "scripts" / "ci" / "assert-gate.sh"
        if found.is_file():
            return found
    # In the backend container the repo's scripts/ is mounted read-only at
    # /app/repo-scripts (docker-compose.yml), the same as for
    # test_await_test_run_gate.py.
    container_mount = Path("/app/repo-scripts/ci/assert-gate.sh")
    if container_mount.is_file():
        return container_mount
    raise RuntimeError(
        "Could not locate scripts/ci/assert-gate.sh. In the backend container "
        "this needs the ./scripts:/app/repo-scripts:ro mount."
    )


SCRIPT = _find_script()


def _run(result: str, changed: str, label: str = "Backend shards"):
    return subprocess.run(
        ["bash", str(SCRIPT), result, changed, label],
        capture_output=True,
        text=True,
    )


# ── The accepting half ───────────────────────────────────────────────────────


@pytest.mark.parametrize("changed", ["true", "false"])
def test_a_successful_job_always_passes(changed):
    """A job that ran and passed is accepted regardless of what change
    detection said. The detection output may only ever explain an ABSENCE."""
    r = _run("success", changed)
    assert r.returncode == 0, r.stderr
    assert "passed" in r.stdout


def test_a_skip_is_accepted_only_when_the_area_did_not_change():
    """The new behaviour: this is what makes a docs-only PR mergeable while the
    shards are skipped."""
    r = _run("skipped", "false")
    assert r.returncode == 0, r.stderr
    assert "no changes" in r.stdout


# ── The refusing half ────────────────────────────────────────────────────────


def test_upstream_failure_is_not_excused_by_a_skip():
    """⚠⚠ THE LOAD-BEARING CASE.

    `skipped` + the area DID change is the shape of "an upstream job failed, so
    this job never ran". Accepting `skipped` unconditionally makes that green
    on a required gate. Injecting a blanket accept into assert-gate.sh must
    turn THIS test red -- if it does not, the fence is decorative.
    """
    r = _run("skipped", "true")
    assert r.returncode == 1, (
        "assert-gate.sh accepted a skipped job whose area DID change. That is "
        "the shape of an upstream failure and it must never pass a required "
        "gate."
    )
    assert "skipped" in r.stderr
    assert "upstream" in r.stderr


def test_a_failure_is_never_excused_by_an_unchanged_area():
    """If a job ran at all its verdict is the answer. `changed=false` with a
    real failure means the job ran anyway (a manual dispatch, a re-run, a
    hand-edited `if:`) and still broke."""
    r = _run("failure", "false")
    assert r.returncode == 1
    assert "failure" in r.stderr


def test_a_failure_fails():
    r = _run("failure", "true")
    assert r.returncode == 1


@pytest.mark.parametrize("changed", ["true", "false"])
def test_cancelled_fails_closed(changed):
    """`cancelled` is reachable in normal operation -- a superseded run in the
    concurrency group -- and it means the suite did not pass."""
    r = _run("cancelled", changed)
    assert r.returncode == 1, f"cancelled + changed={changed} was accepted"


@pytest.mark.parametrize("result", ["", "unknown", "SUCCESS", "Skipped", "neutral"])
def test_an_unrecognised_result_fails_closed(result):
    """Fail closed on anything that is not the exact literal. `SUCCESS` and
    `Skipped` are here on purpose: a case-insensitive comparison would be a
    silent widening, and this repo has shipped exactly that defect against a
    DB-backed key before (TBD-322)."""
    r = _run(result, "false")
    assert r.returncode == 1, f"result={result!r} was accepted"


@pytest.mark.parametrize("changed", ["", "no", "FALSE", "0"])
def test_a_skip_needs_the_literal_false_not_merely_not_true(changed):
    """⚠ The settled design said "accept skipped when area-changed != 'true'".
    That is a hole.

    If the `changes` job itself fails or is cancelled,
    `needs.changes.outputs.<area>` evaluates to the EMPTY STRING. Every work
    job's `if:` is then false, every one of them reports `skipped`, and a
    `!= "true"` rule would wave the ENTIRE suite through on a required gate.
    The accept must require change detection to have actually answered.
    """
    r = _run("skipped", changed)
    assert r.returncode == 1, (
        f"a skipped job was accepted with area-changed={changed!r}. Only the "
        "literal 'false' may excuse a skip -- an empty value means change "
        "detection never answered."
    )


# ── Interface ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("args", [[], ["success"], ["success", "false"], ["a", "b", "c", "d"]])
def test_wrong_argument_count_is_a_usage_error_not_a_pass(args):
    """A miswired workflow call (a dropped `${{ }}` that expands to nothing
    would collapse three arguments into fewer) must not exit 0."""
    r = subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True
    )
    assert r.returncode == 2, f"args={args} exited {r.returncode}"
    assert "usage" in r.stderr


def test_the_label_is_reported_so_a_red_gate_names_its_subject():
    """Both aggregates call this script two or three times. A refusal that does
    not say which upstream it is about sends the reader hunting."""
    ok = _run("success", "true", "Migration Checks")
    assert "Migration Checks" in ok.stdout
    bad = _run("failure", "true", "Migration Checks")
    assert "Migration Checks" in bad.stderr
