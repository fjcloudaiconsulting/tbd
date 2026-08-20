"""TBD-404 -- `.github/workflows/test.yml` must stay wired the way the scoped
CI design requires.

`test_ci_gate_accept_rule.py` fences the RULE (as behaviour, by executing
`scripts/ci/assert-gate.sh`). `test_ci_change_detection.py` fences the
CLASSIFIER (by executing it against real git repositories). This module fences
the WIRING between them, which is the part neither script can see:

  * a work job that loses its `if:` silently stops being scoped (harmless), and
    a work job that gains a WRONG `if:` silently stops running (not harmless);
  * an aggregate that loses `always()` becomes a skipped required check, which
    branch protection reads as PASSING;
  * an aggregate that re-inlines `== 'skipped'` gives the accept rule a second
    implementation that the behavioural fence cannot see.

⚠ The standing ban on a trigger-level `paths:` filter (TBD-347, load-bearing
for deploys since TBD-391) was held by a comment until now. It is a test here.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "test.yml").exists():
            return candidate
    raise RuntimeError("Could not locate repo root containing .github/workflows/test.yml")


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test.yml"
WORKFLOW = yaml.safe_load(WORKFLOW_PATH.read_text())
JOBS = WORKFLOW["jobs"]

# The two jobs whose `name:` is a REQUIRED status check on `main`.
GATES = ("backend", "frontend")
DETECTOR = "changes"
GATE_SCRIPT = "scripts/ci/assert-gate.sh"


def _work_jobs() -> list[str]:
    return sorted(set(JOBS) - set(GATES) - {DETECTOR})


def _steps(job: str) -> list[dict]:
    return JOBS[job]["steps"]


def test_the_trigger_has_no_paths_filter():
    """⚠ THE STANDING BAN (TBD-347), now executable.

    A required context that never reports blocks its PR forever on "Expected —
    waiting for status to be reported", and since TBD-391 it also silently
    stops production deploys: `scripts/ci/await-test-run.sh` waits for a
    concluded `Test` run on the merge commit, one 25-minute timeout at a time.

    In-workflow change detection (this ticket) is the sanctioned alternative
    precisely because the jobs still start.
    """
    # ⚠ YAML 1.1 parses the bare key `on` as the BOOLEAN True. Reading
    # `doc["on"]` raises KeyError, and a `.get("on", {})` would silently return
    # an empty mapping and pass vacuously.
    trigger = WORKFLOW.get(True, WORKFLOW.get("on"))
    assert isinstance(trigger, dict) and trigger, (
        f"could not parse the workflow trigger; got {trigger!r}"
    )
    assert "pull_request" in trigger and "push" in trigger, (
        f"expected pull_request and push triggers, found {sorted(trigger)}"
    )
    for event, config in trigger.items():
        if not isinstance(config, dict):
            continue
        for banned in ("paths", "paths-ignore"):
            assert banned not in config, (
                f"`{event}:` gained a `{banned}:` filter. That is permanently "
                "banned here: the jobs would stop existing, the required "
                "contexts would never report, and the deploy interlock in "
                "scripts/ci/await-test-run.sh would time out instead of "
                "finding a concluded run. Gate the JOBS on the `changes` "
                "outputs instead."
            )


def test_the_detector_job_emits_every_area_the_workflow_reads():
    """A missing output evaluates to the empty string: the work job skips AND
    assert-gate.sh refuses the skip, so the gate is permanently red. Loud, but
    only after it ships."""
    outputs = JOBS[DETECTOR]["outputs"]
    assert set(outputs) == {"backend", "frontend", "migrations"}, (
        f"`changes` emits {sorted(outputs)}; the workflow reads backend, "
        "frontend and migrations."
    )


def test_the_detector_uses_no_third_party_action():
    """⚠ These outputs gate two REQUIRED status checks. A marketplace action
    here would put an unpinned third-party supply chain on the repo's merge
    gate, which is why the design says `git diff` and not `dorny/paths-filter`.
    """
    for step in _steps(DETECTOR):
        uses = str(step.get("uses", ""))
        if not uses:
            continue
        assert uses.startswith("actions/"), (
            f"`changes` uses the third-party action {uses!r}. Change detection "
            "gates the required contexts; keep it to first-party actions plus "
            "scripts/ci/detect-changed-areas.sh."
        )


def test_the_detector_checks_out_full_history():
    """⚠ Fails SILENTLY without this. The default depth-1 clone has no base
    commit, the diff fails, and the detector fails SAFE — every area true. The
    build stays green and the scoping simply never does anything."""
    checkouts = [s for s in _steps(DETECTOR) if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkouts, "`changes` must check the repository out to diff it"
    assert any(str(s.get("with", {}).get("fetch-depth")) == "0" for s in checkouts), (
        "`changes` must check out with `fetch-depth: 0` or the PR's base "
        "commit is absent and every diff falls back to 'everything changed'."
    )


@pytest.mark.parametrize("job", _work_jobs())
def test_every_work_job_is_gated_on_change_detection(job):
    """The scoping itself. A work job with no `if:` runs on every docs PR,
    which is the state this ticket exists to leave."""
    condition = str(JOBS[job].get("if", ""))
    assert "needs.changes.outputs" in condition, (
        f"job `{job}` has `if: {condition!r}`, which does not read a "
        "`needs.changes.outputs.<area>` value. Every work job in this "
        "workflow must be scoped to the area it tests."
    )
    assert DETECTOR in (JOBS[job].get("needs") or []), (
        f"job `{job}` reads `needs.changes.outputs` but does not list "
        "`changes` in its `needs:`; the expression would be empty and the job "
        "would never run."
    )


def test_there_is_more_than_one_work_job():
    """Anti-vacuity floor for the parametrisation above: if a rename made
    `_work_jobs()` empty, the whole scoping fence would silently collect zero
    cases and pass."""
    jobs = _work_jobs()
    assert len(jobs) >= 3, f"expected at least 3 work jobs, parsed {jobs}"


@pytest.mark.parametrize("gate", GATES)
def test_both_gates_always_run(gate):
    """⚠ A required check that is itself SKIPPED is read as PASSING by branch
    protection. The whole reason `Frontend Checks` was split into an aggregate
    plus `frontend-work` is so that the required name can never be the thing
    that skips."""
    assert "always()" in str(JOBS[gate].get("if", "")), (
        f"gate `{gate}` must be `if: ${{{{ always() }}}}`. Without it the "
        "required context skips whenever an upstream skips, and branch "
        "protection treats a skipped required check as a pass."
    )


@pytest.mark.parametrize("gate", GATES)
def test_gates_route_every_result_through_the_shared_script(gate):
    """⚠ ONE implementation of the accept rule, or the behavioural fence in
    test_ci_gate_accept_rule.py is guarding a copy nobody runs."""
    checked = 0
    for step in _steps(gate):
        run = str(step.get("run", ""))
        if ".result }}" not in run:
            continue
        checked += 1
        assert GATE_SCRIPT in run, (
            f"a step in `{gate}` reads a job result without calling "
            f"{GATE_SCRIPT}:\n{run}\nThe accept rule must have exactly one "
            "implementation."
        )
    assert checked >= 2, (
        f"gate `{gate}` only inspects {checked} upstream result(s); expected "
        "at least the detector plus its own work job(s)."
    )


@pytest.mark.parametrize("gate", GATES)
def test_gates_do_not_reimplement_the_skip_rule_inline(gate):
    """⚠ THE FOOTGUN, guarded at the wiring level.

    The trivially available "fix" for a gate that goes red on a docs PR is to
    add `|| [ "$result" = "skipped" ]` right here. Unconditionally accepting
    `skipped` turns a genuinely broken suite into a green REQUIRED gate,
    because GitHub also reports `skipped` when an UPSTREAM job failed.
    """
    for step in _steps(gate):
        run = str(step.get("run", ""))
        assert "skipped" not in run, (
            f"a step in `{gate}` mentions 'skipped' inline:\n{run}\n"
            f"Do not re-implement the accept rule here — call {GATE_SCRIPT}, "
            "which accepts a skip ONLY when change detection reported the "
            "literal `false` for that area."
        )


def test_every_job_is_wired_into_one_of_the_two_gates():
    """Mirrors the in-workflow wiring guard so the shards catch it too.

    ⚠ The union of BOTH gates' `needs:` is deliberate (TBD-404): `frontend-work`
    hangs off the frontend gate, not the backend one. It is NOT an allowlist
    widening — the exemption still covers only the two gate jobs, which cannot
    depend on themselves.
    """
    wired = set()
    for gate in GATES:
        wired |= set(JOBS[gate].get("needs") or [])
    unwired = sorted(set(JOBS) - wired - set(GATES))
    assert not unwired, (
        f"job(s) {unwired} are not depended on by either required gate. They "
        "would report an unrequired context: red, and the PR merges anyway."
    )


def test_the_required_context_names_are_unchanged():
    """Branch protection pins these two strings. Renaming either turns the
    required check into one that never reports — the permanently-blocked-PR
    failure again, from the other direction."""
    assert JOBS["backend"]["name"] == "Backend Checks"
    assert JOBS["frontend"]["name"] == "Frontend Checks"


def test_no_other_job_claims_a_required_context_name():
    """⚠ Two check-runs with the same name is how the rejected mirrored-workflow
    design failed: a real red result can be overwritten by a stub. The split
    introduced a second frontend job, so pin that it took a different name."""
    required = {"Backend Checks", "Frontend Checks"}
    for job, spec in JOBS.items():
        if job in GATES:
            continue
        assert str(spec.get("name", job)) not in required, (
            f"job `{job}` is named {spec.get('name')!r}, colliding with a "
            "required status-check context."
        )
