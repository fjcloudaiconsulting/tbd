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

import json
import re
import shlex
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

# ⚠ Read, never restated: `.github/branch-protection/main.json` is the
# recorded branch-protection posture (TBD-420) and names the required contexts.
REQUIRED_CHECKS = json.loads(
    (REPO_ROOT / ".github" / "branch-protection" / "main.json").read_text()
)["required_status_checks"]
REQUIRED_CONTEXTS = set(REQUIRED_CHECKS["contexts"])

# The change-detection areas each gate may accept a skip against. A frontend
# job scoped to (and asserted with) the backend area is self-consistent, and
# only this pin can see that it no longer tests on frontend PRs.
GATE_AREAS = {"backend": {"backend", "migrations"}, "frontend": {"frontend"}}


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
    assert JOBS[gate].get("if") == "${{ always() }}", (
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


_RESULT_ARG = re.compile(r"\$\{\{ needs\.([A-Za-z0-9_-]+)\.result \}\}")
_AREA_ARG = re.compile(r"\$\{\{ needs\.changes\.outputs\.([A-Za-z0-9_-]+) \}\}")
_IF_AREA = re.compile(r"\$\{\{ needs\.changes\.outputs\.([A-Za-z0-9_-]+) == 'true' \}\}")
_SHELL_OPERATORS = set("();<>|&")
# The only keys an assert step may carry. `if:`, `continue-on-error:`,
# `shell:`, `env:` and `working-directory:` can each stop the step failing the
# gate while it still looks like an assertion.
_ASSERT_STEP_KEYS = {"name", "run"}


def _gate_mismatches(jobs: dict, gate: str, areas: set[str]) -> list[str]:
    """Every way `gate`'s `needs:` and its assert-gate.sh calls disagree.

    Tokenizes each step's `run:` as shell rather than searching it: a
    `needs.<job>.result` inside an `echo`, or a call followed by `|| true`,
    asserts nothing and must not count.
    """
    problems = []
    asserted = {}
    for step in jobs[gate]["steps"]:
        run = str(step.get("run", ""))
        # Cheap prefix check first: other steps carry heredocs whose
        # apostrophes are not valid shell quoting on their own.
        if run.split()[:2] != ["bash", GATE_SCRIPT]:
            continue
        extra = sorted(set(step) - _ASSERT_STEP_KEYS)
        if extra:
            problems.append(f"{GATE_SCRIPT} step {step.get('name')!r} carries {extra}, so it may never fail the gate")
            continue
        try:
            lexer = shlex.shlex(run, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            tokens = []
        match = _RESULT_ARG.fullmatch(tokens[2]) if len(tokens) == 5 else None
        if not match or any(t and set(t) <= _SHELL_OPERATORS for t in tokens):
            problems.append(f"unparseable {GATE_SCRIPT} call: {tokens or run!r}")
            continue
        asserted[match.group(1)] = tokens[3]

    needs = set(jobs[gate].get("needs") or [])
    if DETECTOR not in needs:
        problems.append(f"`{DETECTOR}` is not in needs:, so every area argument is empty")
    for job in sorted(needs - set(asserted)):
        problems.append(f"`{job}` is in needs: but its result is never asserted")
    for job in sorted(set(asserted) - needs):
        problems.append(f"`{job}` is asserted but not in needs: (always empty)")

    # The skip-accept argument must be the area the job itself is scoped to,
    # and that area must be one this gate owns. Asserting `frontend-static`
    # against the BACKEND output would wave its skip through on a
    # frontend-only PR where it never ran.
    for job, area_arg in sorted(asserted.items()):
        if job not in needs:
            continue
        if job == DETECTOR:
            if area_arg != "true":
                problems.append(f"`{DETECTOR}` asserted with area {area_arg!r}; it has no area, use 'true'")
            continue
        scoped = _IF_AREA.fullmatch(str(jobs[job].get("if", "")))
        passed = _AREA_ARG.fullmatch(area_arg)
        if not scoped:
            problems.append(f"`{job}` has an if: this fence cannot read an area from: {jobs[job].get('if')!r}")
        elif not passed or passed.group(1) != scoped.group(1):
            problems.append(f"`{job}` asserted with area {area_arg!r}, but it is scoped to {scoped.group(1)!r}")
        elif scoped.group(1) not in areas:
            problems.append(f"`{job}` is scoped to {scoped.group(1)!r}; gate `{gate}` owns only {sorted(areas)}")
    return problems


@pytest.mark.parametrize("gate", GATES)
def test_every_job_a_gate_needs_has_its_result_asserted(gate):
    """⚠ TBD-422. `needs:` membership alone does not gate anything.

    `test_every_job_is_wired_into_one_of_the_two_gates` (and its in-workflow
    twin) only prove a job is in a gate's `needs:`. A job listed there whose
    result no step passes to assert-gate.sh goes red while the required gate
    stays GREEN, with every other fence in this file passing. So `needs:`
    (including `changes`, which both gates assert with a literal `true`) must
    equal the set of jobs asserted by plain, un-neutered steps, each with the
    job's own area, drawn from the areas this gate owns.
    """
    assert set(GATE_AREAS) == set(GATES)
    problems = _gate_mismatches(JOBS, gate, GATE_AREAS[gate])
    assert not problems, f"gate `{gate}`:\n  " + "\n  ".join(problems)
    assert len(JOBS[gate]["needs"]) >= 3, f"gate `{gate}` needs only {JOBS[gate]['needs']}"


_FE = "${{ needs.changes.outputs.frontend }}"
_FE_JOB = {"needs": ["changes"], "if": "${{ needs.changes.outputs.frontend == 'true' }}"}


def _assert_step(job: str, area: str, tail: str = "") -> dict:
    return {"name": job, "run": f'bash {GATE_SCRIPT} "${{{{ needs.{job}.result }}}}" "{area}" "x"{tail}'}


_GOOD_STEPS = [_assert_step("changes", "true"), _assert_step("a", _FE), _assert_step("b", _FE)]
_GOOD = {"changes": {}, "a": _FE_JOB, "b": _FE_JOB, "g": {"needs": ["changes", "a", "b"], "steps": _GOOD_STEPS}}


def _with(steps=None, needs=None, **jobs) -> dict:
    gate = {"needs": needs or ["changes", "a", "b"], "steps": steps or _GOOD_STEPS}
    return {**_GOOD, **jobs, "g": gate}


_BROKEN = {
    "missing assert": (_with(steps=_GOOD_STEPS[:2]), "`b` is in needs: but its result is never asserted"),
    "asserted, not needed": (_with(needs=["changes", "a"]), "`b` is asserted but not in needs:"),
    "echo only": (_with(steps=[*_GOOD_STEPS[:2], {"run": 'echo "${{ needs.b.result }}"'}]), "`b` is in needs:"),
    "wrong area": (_with(steps=[*_GOOD_STEPS[:2], _assert_step("b", "${{ needs.changes.outputs.backend }}")]), "scoped to 'frontend'"),
    "spaced || true": (_with(steps=[*_GOOD_STEPS[:2], _assert_step("b", _FE, " || true")]), "unparseable"),
    "glued ||true": (_with(steps=[*_GOOD_STEPS[:2], _assert_step("b", _FE, "||true")]), "unparseable"),
    "glued ;true": (_with(steps=[*_GOOD_STEPS[:2], _assert_step("b", _FE, ";true")]), "unparseable"),
    "backgrounded &": (_with(steps=[*_GOOD_STEPS[:2], _assert_step("b", _FE, " &")]), "unparseable"),
    # Five tokens exactly, so only the operator check can see it.
    "backgrounded, no label": (_with(steps=[*_GOOD_STEPS[:2], {"run": f'bash {GATE_SCRIPT} "${{{{ needs.b.result }}}}" "{_FE}" &'}]), "unparseable"),
    "no detector in needs": (_with(needs=["a", "b"], steps=_GOOD_STEPS[1:]), "`changes` is not in needs:"),
    "extra argument line": (_with(steps=[*_GOOD_STEPS[:2], _assert_step("b", _FE, "\ntrue")]), "unparseable"),
    "step if:": (_with(steps=[*_GOOD_STEPS[:2], {**_assert_step("b", _FE), "if": False}]), "carries ['if']"),
    "step continue-on-error": (_with(steps=[*_GOOD_STEPS[:2], {**_assert_step("b", _FE), "continue-on-error": True}]), "carries ['continue-on-error']"),
    "step shell:": (_with(steps=[*_GOOD_STEPS[:2], {**_assert_step("b", _FE), "shell": "sh {0}"}]), "carries ['shell']"),
    ".outputs.x as result": (_with(steps=[*_GOOD_STEPS[:2], {"run": f'bash {GATE_SCRIPT} "${{{{ needs.b.outputs.x }}}}" "{_FE}" "x"'}]), "unparseable"),
    "changes with an area": (_with(steps=[_assert_step("changes", _FE), *_GOOD_STEPS[1:]]), "`changes` asserted with area"),
    "unreadable work if:": (_with(b={"needs": ["changes"], "if": "${{ always() }}"}), "cannot read an area"),
    "self-consistent foreign area": (
        _with(b={"needs": ["changes"], "if": "${{ needs.changes.outputs.backend == 'true' }}"},
              steps=[*_GOOD_STEPS[:2], _assert_step("b", "${{ needs.changes.outputs.backend }}")]),
        "gate `g` owns only ['frontend']",
    ),
}


def test_the_needs_vs_asserted_checker_accepts_the_correct_shape():
    assert _gate_mismatches(_GOOD, "g", {"frontend"}) == []


@pytest.mark.parametrize("case", sorted(_BROKEN))
def test_the_needs_vs_asserted_checker_rejects_each_broken_shape(case):
    """Anti-vacuity: every shape above must be reported, by the reason that
    names it. A neutered step is also reported as leaving its job unasserted."""
    jobs, expected = _BROKEN[case]
    problems = _gate_mismatches(jobs, "g", {"frontend"})
    assert any(expected in problem for problem in problems), problems


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
    failure again, from the other direction. Compared against the recorded
    posture file, not a copy of it."""
    assert REQUIRED_CONTEXTS == {"Backend Checks", "Frontend Checks"}
    assert {c["context"] for c in REQUIRED_CHECKS["checks"]} == REQUIRED_CONTEXTS
    assert {JOBS[gate]["name"] for gate in GATES} == REQUIRED_CONTEXTS


def test_no_other_job_claims_a_required_context_name():
    """⚠ Two check-runs with the same name is how the rejected mirrored-workflow
    design failed: a real red result can be overwritten by a stub. The split
    introduced a second frontend job, so pin that it took a different name."""
    required = REQUIRED_CONTEXTS
    assert len(required) == 2, required
    for job, spec in JOBS.items():
        if job in GATES:
            continue
        assert str(spec.get("name", job)) not in required, (
            f"job `{job}` is named {spec.get('name')!r}, colliding with a "
            "required status-check context."
        )
