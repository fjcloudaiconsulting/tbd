"""Regression guards for the App Platform deploy contract.

These tests lock down the four operational invariants we've now broken
multiple times in production:

  1. The deploy workflow MUST push the repo's spec on every run
     (`app_spec_location` set; `app_name` absent — v2 prefers app_name and
     silently drops the file otherwise).
  2. The spec MUST declare a PRE_DEPLOY migrate job so long migrations
     don't gate uvicorn's port-bind on the serving probe budget.
  3. The migrate job MUST bind DATABASE_URL — App Platform does not
     auto-inherit secrets across components, so a fresh migrate job with
     no DATABASE_URL crashes alembic on first deploy (2026-04-25 incident).
  4. The backend service MUST declare every SECRET it reads — App Platform
     removes any SECRET not in the spec on push, which previously dropped
     JWT_SECRET_KEY to its placeholder default and crashlooped backend
     (2026-04-25 incident).
"""
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    """Walk upward from `start` until a directory containing both
    `.github/workflows/deploy.yml` and `.do/app.yaml` is found.

    `Path(__file__).parents[2]` worked when these tests ran from a host
    checkout but resolved to `/` inside the backend container (where the
    test file lives at `/app/tests/test_deploy_workflow.py`). Walking
    upward is robust to either layout.
    """
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "deploy.yml").exists() and (
            candidate / ".do" / "app.yaml"
        ).exists():
            return candidate
    raise RuntimeError(
        "Could not locate repo root containing .github/workflows/deploy.yml "
        "and .do/app.yaml. Run these tests from a checked-out repo."
    )


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
APP_SPEC = REPO_ROOT / ".do" / "app.yaml"


def _deploy_step(workflow: str) -> str:
    start = workflow.index("digitalocean/app_action/deploy")
    rest = workflow[start:]
    next_step = rest.find("\n      - ")
    return rest if next_step < 0 else rest[:next_step]


def test_deploy_workflow_pushes_app_spec():
    workflow = DEPLOY_WORKFLOW.read_text()
    assert "app_spec_location: .do/app.yaml" in workflow, (
        "deploy.yml must pass app_spec_location so the file actually deploys."
    )
    step = _deploy_step(workflow)
    assert "app_name:" not in step, (
        "deploy.yml must NOT pass app_name on the deploy step — v2 prefers "
        "app_name and silently ignores app_spec_location (deploy/main.go: "
        "createSpec). Drop app_name; the action picks the app up via the "
        "spec file's top-level `name:` field."
    )


def test_app_spec_declares_predeploy_migrate_job():
    spec = APP_SPEC.read_text()
    assert "kind: PRE_DEPLOY" in spec, "spec must declare PRE_DEPLOY migrate"
    # The PRE_DEPLOY job invokes the structured-logging migrate wrapper
    # (backend/scripts/migrate.py), which drives alembic from outside via
    # the Python API + per-revision subprocess.
    assert "scripts/migrate.py" in spec, (
        "PRE_DEPLOY job must run the migrate wrapper (backend/scripts/migrate.py)"
    )


def test_migrate_job_binds_database_url():
    spec = APP_SPEC.read_text()
    migrate_block = spec[spec.index("name: migrate"):]
    assert "DATABASE_URL" in migrate_block, (
        "Migrate job must declare DATABASE_URL — App Platform does not "
        "auto-inherit secrets to PRE_DEPLOY jobs."
    )


def test_app_spec_declares_custom_domain():
    """The `app_spec_location` workflow path strips anything not in the
    file — same trap as missing SECRET envs. Without the `domains:` block
    the custom domain falls off the live app, Cloudflare's origin TLS
    handshake fails, and the public site goes dark even though backend
    and frontend components are healthy. (Hit on PR #89 merge,
    2026-04-25.)"""
    spec = APP_SPEC.read_text()
    assert "domain: app.thebetterdecision.com" in spec, (
        "Spec must declare app.thebetterdecision.com as a domain — "
        "anything not in this file is removed from the live app on push."
    )
    assert "type: PRIMARY" in spec, (
        "Custom domain must be marked PRIMARY for ingress routing."
    )


def test_backend_service_declares_all_required_secrets():
    """Every SECRET the backend reads at boot MUST appear in the backend
    service block. Missing-from-spec equals removed-from-live on push,
    and a backend without JWT_SECRET_KEY crashloops at import time."""
    spec = APP_SPEC.read_text()
    services_idx = spec.index("services:")
    jobs_idx = spec.find("\njobs:", services_idx)
    services_block = spec[services_idx:jobs_idx if jobs_idx > 0 else len(spec)]
    backend_idx = services_block.index("- name: backend")
    next_service = services_block.find("\n  - name:", backend_idx + 1)
    backend_block = services_block[backend_idx:next_service if next_service > 0 else len(services_block)]

    required = [
        "DATABASE_URL",
        "REDIS_URL",
        "JWT_SECRET_KEY",
        "MFA_ENCRYPTION_KEY",
        "MAILGUN_API_KEY",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ]
    missing = [k for k in required if f"key: {k}" not in backend_block]
    assert not missing, (
        f"Backend service is missing required secret declarations: {missing}. "
        "Any SECRET not in this spec will be removed on next deploy. "
        "Pull the encrypted EV[...] value from `doctl apps spec get` and add it."
    )


# ── TBD-391: the deploy interlock is actually wired ─────────────────────────
#
# `scripts/ci/await-test-run.sh` is fenced for its DECISION logic in
# test_await_test_run_gate.py. These fences pin the other half: that the
# workflows actually consult it, and that the one workflow which must NOT be
# gated still is not. Covering the script alone would certify a gate nothing
# calls.

RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
APEX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "apex-deploy.yml"


def _yaml(path: Path) -> dict:
    import yaml

    doc = yaml.safe_load(path.read_text())
    # Positive baseline: an empty or mis-parsed document would make every
    # assertion below pass vacuously.
    assert isinstance(doc, dict) and doc.get("jobs"), f"failed to parse {path}"
    return doc


def test_release_gates_semantic_release_on_the_post_merge_suite():
    """The gate must run BEFORE `release`, not between `release` and `deploy`.

    semantic-release cuts an immutable git tag and publishes a GitHub Release.
    Measured on PR #654 it did so 7m41s before the post-merge suite reported,
    so gating only the deploy would still leave a published release for a
    commit whose suite then goes red.
    """
    jobs = _yaml(RELEASE_WORKFLOW)["jobs"]
    assert "await-tests" in jobs, "release.yml lost its await-tests gate"
    assert "await-tests" in (jobs["release"].get("needs") or []), (
        "`release` must depend on `await-tests`. Gating only `deploy` leaves "
        "the tag and GitHub Release published for untested code."
    )
    # The wait needs `actions: read` to list runs; without it the API 403s and
    # the gate fails closed for a reason that looks exactly like a working gate.
    assert (jobs["await-tests"].get("permissions") or {}).get("actions") == "read"


def test_apex_gates_its_deploy_but_never_the_manual_recovery():
    """Same interlock on the landing surface, with the dispatch bypass intact.

    ⚠ `needs:` on a SKIPPED job skips the dependent by default, so the explicit
    `workflow_dispatch` arm in `build-and-deploy`'s `if:` is what keeps the
    documented stale-deploy recovery working. Without it the recovery path
    silently does nothing.
    """
    jobs = _yaml(APEX_WORKFLOW)["jobs"]
    assert "await-tests" in jobs, "apex-deploy.yml lost its await-tests gate"
    assert "await-tests" in (jobs["build-and-deploy"].get("needs") or [])
    assert "workflow_dispatch" in (jobs["await-tests"].get("if") or ""), (
        "the gate must skip on manual dispatch, which is the recovery path"
    )
    guard = jobs["build-and-deploy"].get("if") or ""
    assert "workflow_dispatch" in guard, (
        "build-and-deploy needs an explicit dispatch arm: `needs:` on a "
        "SKIPPED gate would otherwise skip the manual recovery deploy too."
    )


def test_manual_deploy_workflow_is_deliberately_ungated():
    """Pins the boundary from the OTHER side. `deploy.yml` is the escape hatch
    used when the gate itself is wrong; gating it would remove the recovery at
    exactly the moment it is needed."""
    jobs = _yaml(REPO_ROOT / ".github" / "workflows" / "deploy.yml")["jobs"]
    assert "await-tests" not in jobs, (
        "deploy.yml must stay ungated — it is the recovery path for a broken "
        "gate. See scripts/ci/await-test-run.sh."
    )


# ---------------------------------------------------------------------------
# TBD-425 -- the app-spec secret drift guard must be wired, and wired BEFORE
# the deploy. The script being correct is worth nothing if it runs after the
# spec has already been pushed.
# ---------------------------------------------------------------------------

GUARD_SCRIPT = "assert-app-spec-secrets-synced.sh"
DEPLOY_ACTION = "digitalocean/app_action/deploy"


def _deploy_steps(workflow_path):
    import yaml

    doc = yaml.safe_load(workflow_path.read_text())
    return doc["jobs"]["deploy"]["steps"]


def _index_of(steps, predicate, label):
    for i, step in enumerate(steps):
        if predicate(step):
            return i
    raise AssertionError(f"no step matching {label} in the deploy job")


import pytest


@pytest.mark.parametrize("workflow", ["release.yml", "deploy.yml"])
def test_secret_drift_guard_runs_before_the_spec_is_pushed(workflow):
    """⚠ ORDER IS THE WHOLE POINT. `app_action/deploy@v2` pushes the committed
    `.do/app.yaml` as the authoritative spec, so a guard that runs afterwards
    reports on damage already done. On 2026-08-20 that push replaced
    production's database and redis credentials with stale committed blobs.
    """
    steps = _deploy_steps(REPO_ROOT / ".github" / "workflows" / workflow)

    guard = _index_of(
        steps, lambda s: GUARD_SCRIPT in str(s.get("run", "")), GUARD_SCRIPT
    )
    deploy = _index_of(
        steps, lambda s: DEPLOY_ACTION in str(s.get("uses", "")), DEPLOY_ACTION
    )

    assert guard < deploy, (
        f"{workflow}: the secret-drift guard is at step {guard} but the deploy "
        f"is at {deploy}. The guard must run BEFORE the spec is pushed, or it "
        "only ever reports damage that has already happened."
    )


@pytest.mark.parametrize("workflow", ["release.yml", "deploy.yml"])
def test_secret_drift_guard_has_doctl_available(workflow):
    """The guard reads the live spec. Without doctl it exits 2 and the deploy
    fails for a confusing reason instead of a clear one."""
    steps = _deploy_steps(REPO_ROOT / ".github" / "workflows" / workflow)
    setup = _index_of(
        steps, lambda s: "action-doctl" in str(s.get("uses", "")), "action-doctl"
    )
    guard = _index_of(
        steps, lambda s: GUARD_SCRIPT in str(s.get("run", "")), GUARD_SCRIPT
    )
    assert setup < guard, f"{workflow}: doctl is installed after the guard runs"


def test_the_automatic_deploy_path_cannot_bypass_the_guard():
    """⚠ `deploy.yml` is the documented break-glass and MAY override the guard.
    `release.yml` is the automatic path and MUST NOT -- an override there would
    make every merge able to overwrite production's secrets silently, which is
    the failure this guard exists to stop.
    """
    steps = _deploy_steps(REPO_ROOT / ".github" / "workflows" / "release.yml")
    guard = steps[
        _index_of(steps, lambda s: GUARD_SCRIPT in str(s.get("run", "")), GUARD_SCRIPT)
    ]
    env = guard.get("env") or {}
    assert "ALLOW_SECRET_DRIFT" not in env, (
        "release.yml's drift guard accepts ALLOW_SECRET_DRIFT. The automatic "
        "deploy path must never be able to skip it; only the manual "
        "break-glass (deploy.yml) may."
    )


def test_the_break_glass_override_is_opt_in_and_defaults_to_false():
    import yaml

    doc = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text())
    triggers = doc.get(True, doc.get("on"))
    inputs = (triggers or {}).get("workflow_dispatch", {}).get("inputs", {})
    assert "allow_secret_drift" in inputs, (
        "deploy.yml is the break-glass path and must expose a deliberate "
        "override, or a genuine emergency is blocked by this guard."
    )
    assert inputs["allow_secret_drift"].get("default") is False, (
        "the override must DEFAULT to false; an emergency path that skips the "
        "guard by default is not a guard."
    )


# ---------------------------------------------------------------------------
# TBD-424 defect 2 -- release.yml must have NO trigger-level `paths:` filter,
# and the removal must not be "fixed" by loosening the deploy condition or by
# reintroducing the same question as in-workflow change detection.
#
# Why the filter went: `.releaserc.json` answers "should this merge ship?" by
# commit INTENT, and since 1f246cbe its suppressions actually suppress (see
# test_release_rules_ordering.py). The paths filter answered the same question
# by a wrong proxy -- file paths -- and was a second, unfenced gate. Measured
# over the last ~100 merges it changed zero release outcomes.
# ---------------------------------------------------------------------------


def _triggers(path: Path) -> dict:
    """Return a workflow's `on:` trigger block.

    ⚠⚠ `yaml.safe_load` parses the BARE key `on:` as the YAML 1.1 boolean
    `True`, not the string `"on"`. A fence written as
    `doc.get("on", {}).get("push", {})` therefore silently gets `{}` and then
    PASSES WHILE ASSERTING NOTHING. Read both keys, and assert the result is
    non-empty so a mis-parse is loud instead of vacuous.
    """
    doc = _yaml(path)
    triggers = doc.get("on")
    if triggers is None:
        triggers = doc.get(True)
    assert isinstance(triggers, dict) and triggers, (
        f"{path.name}: could not read the `on:` block. Remember yaml.safe_load "
        "parses the bare key `on:` as the boolean True, not the string 'on'."
    )
    return triggers


def _normalise_expr(raw) -> str:
    """Strip `${{ }}` wrapping and collapse whitespace in a workflow `if:`."""
    text = " ".join(str(raw or "").split())
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
        text = " ".join(text.split())
    return text


def test_release_workflow_has_no_trigger_level_paths_filter():
    """F1 (TBD-424). The teaching fence: it names the correct place to suppress.

    A `paths:` filter here decides shippability from file paths. That is a
    proxy for the real question, and it is the WRONG proxy: it cannot tell a
    `chore(frontend):` apart from a `feat(frontend):`, and it silently
    misattributes a suppressed merge's commits to whatever merge next happens
    to touch an allowlisted path. `.releaserc.json` answers the real question
    by commit type/scope and is fenced by test_release_rules_ordering.py.

    ⚠ `paths-ignore` is checked too: it is the same gate spelled inversely and
    would otherwise walk straight past a fence that only looked for `paths`.
    """
    push = _triggers(RELEASE_WORKFLOW).get("push")
    assert isinstance(push, dict) and push, "release.yml lost its push trigger"
    offenders = [k for k in ("paths", "paths-ignore") if k in push]
    assert not offenders, (
        f"release.yml's `on.push` reintroduced {offenders}. Do not suppress "
        "releases by file path — a path filter cannot tell shipping intent "
        "from a chore, and the commits it skips are silently attributed to a "
        "later merge. Suppress in `.releaserc.json` instead (add a "
        '`{"type"/"scope": ..., "release": false}` rule AFTER every rule that '
        "grants a real release type -- see test_release_rules_ordering.py), "
        "and let the `new_release_published` condition on `deploy` gate the "
        "ship. TBD-424 defect 2."
    )


def test_release_workflow_push_trigger_is_only_branch_scoped():
    """F2 (TBD-424). The invariant, stated positively.

    F1 bans the two narrowings we know about; this bans every narrowing,
    including forms nobody has thought of yet. Both are kept on purpose --
    F1 is the one whose failure message teaches.
    """
    push = _triggers(RELEASE_WORKFLOW).get("push")
    assert set(push) == {"branches"}, (
        f"release.yml's `on.push` keys are {sorted(push)}; the only permitted "
        "trigger-level narrowing is `branches`. Every push to main must start "
        "a Release run; what ships is decided by .releaserc.json and by the "
        "`new_release_published` condition on `deploy`. TBD-424."
    )


def test_release_deploy_still_gates_solely_on_new_release_published():
    """F3 (TBD-424). The dangerous wrong fix.

    Removing the paths filter AND loosening this condition turns release.yml
    into deploy-on-every-merge -- a production push for every docs typo. The
    filter's removal is only safe BECAUSE this condition is the gate.
    """
    deploy = _yaml(RELEASE_WORKFLOW)["jobs"]["deploy"]
    condition = _normalise_expr(deploy.get("if"))
    assert condition == "needs.release.outputs.new_release_published == 'true'", (
        f"release.yml's `deploy` job guard is now {condition!r}. It must stay "
        "exactly `needs.release.outputs.new_release_published == 'true'`: with "
        "the trigger-level paths filter gone (TBD-424) this condition is the "
        "ONLY thing standing between a docs-only merge and a production "
        "deploy. Widening it -- or adding an `||` arm -- ships everything."
    )


def test_release_workflow_does_not_do_its_own_change_detection():
    """F5 (TBD-424). The rejected alternative, banned explicitly.

    `test.yml`'s detector (scripts/ci/detect-changed-areas.sh) is
    VERDICT-NEUTRAL: it fails TRUE on any uncertainty and structurally cannot
    turn a red suite green. The same detector on the release side would be
    VERDICT-CHANGING -- it could veto a release semantic-release decided to
    cut, a silent UNDER-release, a failure mode this pipeline has never had.
    semantic-release's own commit analysis IS the change detection here.
    """
    # ⚠ Scans the PARSED steps, not the raw file: the `on:` block deliberately
    # NAMES detect-changed-areas.sh in the comment explaining why it must not
    # be used here, and a raw-text fence would forbid its own rationale.
    offenders = []
    for name, job in _yaml(RELEASE_WORKFLOW)["jobs"].items():
        for step in job.get("steps") or []:
            body = f"{step.get('run', '')} {step.get('uses', '')}"
            if "detect-changed-areas" in body:
                offenders.append(f"{name}:{step.get('name', '?')}")
    assert not offenders, (
        f"release.yml invokes detect-changed-areas.sh in {offenders}. "
        "In-workflow change detection was deliberately rejected for the "
        "release path (TBD-424): on test.yml it can only ever ADD work, here "
        "it could silently SUPPRESS a release semantic-release decided to "
        "cut. Let .releaserc.json decide."
    )


# ---------------------------------------------------------------------------
# TBD-424 defect 4 -- somebody must be told when a release was PUBLISHED but
# never DEPLOYED.
#
# `smoke-tests` has `needs: deploy`, so a FAILED deploy SKIPS it and
# notify-smoke-failure.sh never runs. We had a notifier for "deployed but not
# serving" and none at all for "did not deploy at all" -- the louder of the
# two, because it leaves a three-way divergence: an immutable published tag,
# a production app still running PRE-tag code, and a `main` that is neither.
# ---------------------------------------------------------------------------

NOTIFIER_JOB = "notify-undeployed-release"


def test_release_notifies_when_a_published_release_did_not_deploy():
    """F4 (TBD-424). Pins the three things that make this notifier fire at all.

    ⚠ `if: failure()` would NOT work: when `deploy` is skipped or cancelled the
    job's result is not `failure`, and `always()` is what keeps the job itself
    alive past a failed upstream.
    ⚠ Hanging it off `smoke-tests` reproduces the exact hole it closes --
    `smoke-tests` is skipped precisely when the deploy failed.
    ⚠ `cancelled` stays IN scope deliberately (no `!cancelled()`): a deploy
    cancelled mid-push is the loudest case of all, DO may be half-rolled.
    """
    jobs = _yaml(RELEASE_WORKFLOW)["jobs"]
    assert NOTIFIER_JOB in jobs, (
        f"release.yml has no `{NOTIFIER_JOB}` job. A failed deploy skips "
        "`smoke-tests`, so notify-smoke-failure.sh never runs and a published "
        "tag that never reached production is announced to nobody. TBD-424."
    )
    job = jobs[NOTIFIER_JOB]

    needs = job.get("needs") or []
    needs = [needs] if isinstance(needs, str) else list(needs)
    assert "release" in needs and "deploy" in needs, (
        f"`{NOTIFIER_JOB}` must depend on both `release` and `deploy`; got "
        f"{needs}."
    )
    assert "smoke-tests" not in needs, (
        f"`{NOTIFIER_JOB}` must NOT depend on `smoke-tests`. That job is "
        "SKIPPED whenever the deploy failed, which is the exact hole this "
        "notifier exists to close."
    )

    condition = _normalise_expr(job.get("if"))
    for fragment in (
        "always()",
        "needs.release.outputs.new_release_published == 'true'",
        "needs.deploy.result != 'success'",
    ):
        assert fragment in condition, (
            f"`{NOTIFIER_JOB}`'s `if:` is {condition!r} and is missing "
            f"{fragment!r}. Without `always()` the job is skipped along with "
            "its failed upstream; without the `new_release_published` arm it "
            "fires on every no-op release run; and `failure()` alone misses a "
            "SKIPPED or CANCELLED deploy, which is most of the failure space."
        )
    assert "!cancelled()" not in condition, (
        f"`{NOTIFIER_JOB}` must NOT exclude cancelled runs. A deploy cancelled "
        "mid-push can leave DO half-rolled with the tag already published -- "
        "the loudest case, not one to stay quiet about."
    )

    assert (job.get("permissions") or {}).get("issues") == "write", (
        f"`{NOTIFIER_JOB}` needs `permissions: issues: write` to open or "
        "comment the alert issue. Job-level permissions REPLACE the "
        "workflow-level block (which grants `issues: read`), so omitting it "
        "makes the notifier 403 exactly when it is needed."
    )


def test_the_undeployed_release_notifier_is_wired_into_both_deploy_paths():
    """F4b (TBD-424). Half-wiring a guard into one deploy path only is the
    shape the parametrized secret-drift fences above already exist to prevent.

    `deploy.yml` is the manual escape hatch and has no `release` job, so its
    arm gates on the deploy result alone -- but the same script must run, or
    an operator's break-glass deploy can fail into silence.
    """
    jobs = _yaml(REPO_ROOT / ".github" / "workflows" / "deploy.yml")["jobs"]
    assert NOTIFIER_JOB in jobs, (
        f"deploy.yml has no `{NOTIFIER_JOB}` job. The manual deploy path fails "
        "into silence: `smoke-tests` is skipped when `deploy` fails."
    )
    job = jobs[NOTIFIER_JOB]
    condition = _normalise_expr(job.get("if"))
    assert "always()" in condition and "needs.deploy.result" in condition, (
        f"deploy.yml's `{NOTIFIER_JOB}` guard is {condition!r}; it must use "
        "`always()` plus a `needs.deploy.result` test."
    )
    assert (job.get("permissions") or {}).get("issues") == "write"

    steps = job.get("steps") or []
    assert any(
        "notify-undeployed-release.sh" in str(s.get("run", "")) for s in steps
    ), "deploy.yml's notifier job must run scripts/notify-undeployed-release.sh"
