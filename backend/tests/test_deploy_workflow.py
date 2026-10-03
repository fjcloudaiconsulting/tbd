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

import pytest


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


def test_release_gates_release_please_on_the_post_merge_suite():
    """The gate must run BEFORE `release`, not between `release` and `deploy`.

    release-please cuts an immutable git tag and publishes a GitHub Release.
    Under semantic-release, measured on PR #654, that happened 7m41s before the
    post-merge suite reported, so gating only the deploy would still leave a
    published release for a commit whose suite then goes red.
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


def test_release_awaits_the_tests_of_the_commit_it_tags():
    """INFRA-42. release-please tags the merge commit of the merged release PR,
    which is not this run's commit when a merge landed on top (or the release
    commit's own run was dropped from the concurrency group). await-tests only
    proved THIS commit, so the release job must also await that commit's Test
    run, BEFORE release-please can tag it. That run also publishes the sha-<7>
    images `promote` retags.
    """
    job = _yaml(RELEASE_WORKFLOW)["jobs"]["release"]
    steps = job["steps"]
    wait = _index_of(
        steps, lambda s: "await-test-run.sh" in str(s.get("run", "")), "await-test-run.sh"
    )
    tag = _index_of(
        steps,
        lambda s: "googleapis/release-please-action@" in str(s.get("uses", "")),
        "release-please-action",
    )
    assert wait < tag, "the release commit's tests must be awaited before release-please runs"
    run = str(steps[wait]["run"])
    for fragment in ("autorelease: pending", "--state merged", "mergeCommit", "set -euo pipefail"):
        assert fragment in run, (
            f"the wait must target the merged release PR's merge commit, the one "
            f"release-please tags, and fail the job; missing {fragment!r}"
        )
    assert "|| true" not in run and "|| :" not in run, "a swallowed wait gates nothing"
    assert '[ "$sha" = "$GITHUB_SHA" ]' in run, (
        "only this run's own commit (already awaited by await-tests) may be skipped"
    )
    perms = job.get("permissions") or {}
    assert perms.get("actions") == "read", (
        "listing workflow runs needs `actions: read`; without it the wait 403s"
    )
    assert perms.get("pull-requests") == "read", "`gh pr list` needs `pull-requests: read`"


def test_deploy_awaits_the_tagged_commits_tests_before_pushing_the_spec():
    """INFRA-42. A release PR merged between the release job's lookup and
    release-please's own query is tagged unawaited; the deploy re-checks the
    tagged commit before DO is touched."""
    steps = _deploy_steps(RELEASE_WORKFLOW)
    wait = _index_of(
        steps, lambda s: "await-test-run.sh" in str(s.get("run", "")), "await-test-run.sh"
    )
    deploy = _index_of(
        steps, lambda s: DEPLOY_ACTION in str(s.get("uses", "")), DEPLOY_ACTION
    )
    assert wait < deploy
    assert "needs.release.outputs.tag_name" in str(steps[wait].get("env", {}).get("TAG"))
    job = _yaml(RELEASE_WORKFLOW)["jobs"]["deploy"]
    assert (job.get("permissions") or {}).get("actions") == "read"


def test_release_please_runs_with_the_release_app_token():
    """RELEASE_CONTRACT.md section 2. A release PR opened with GITHUB_TOKEN
    triggers no CI, so its required checks never report and it cannot merge."""
    job = _yaml(RELEASE_WORKFLOW)["jobs"]["release"]
    assert job.get("environment") == "release", "the App secrets live in environment `release`"
    steps = job["steps"]
    rp = steps[
        _index_of(
            steps,
            lambda s: "googleapis/release-please-action@" in str(s.get("uses", "")),
            "release-please-action",
        )
    ]
    assert rp["with"]["token"] == "${{ steps.app.outputs.token }}"
    app = next(s for s in steps if s.get("id") == "app")
    assert "actions/create-github-app-token@" in app["uses"]
    assert app["with"].get("permission-workflows") == "write", (
        "without workflows: write the tag is refused when a later main commit "
        "changed a workflow file (INFRA-78)"
    )


@pytest.mark.parametrize(
    ("job", "workflow"),
    [("promote", "promote-release"), ("release-smoke", "smoke")],
)
def test_ghcr_promote_and_smoke_run_once_per_release(job, workflow):
    """INFRA-42. The GHCR side of a release: retag the release commit's images
    (never rebuild) and boot them, only when release-please cut a release."""
    jobs = _yaml(RELEASE_WORKFLOW)["jobs"]
    assert jobs[job]["uses"] == (
        f"fjcloudaiconsulting/.github/.github/workflows/{workflow}.yml@v1"
    )
    assert _normalise_expr(jobs[job].get("if")) == (
        "needs.release.outputs.release_created == 'true'"
    )
    assert jobs[job]["with"]["version"] == "${{ needs.release.outputs.version }}"


def test_do_deploy_does_not_wait_on_the_ghcr_side():
    """DO builds from source, so a GHCR promote or smoke failure must not hold
    production back (INFRA-42 design); the smoke must boot promoted images."""
    jobs = _yaml(RELEASE_WORKFLOW)["jobs"]
    assert jobs["deploy"]["needs"] == "release"
    assert "promote" in jobs["release-smoke"]["needs"]


def test_release_runs_are_serialised_and_never_cancelled():
    """TBD-391 / INFRA-42. One Release run at a time, so two DO spec pushes
    never interleave, and never `cancel-in-progress`: a run cancelled after
    release-please tagged, or mid-deploy, leaves a release DO never got or a
    half-rolled app. (Superseded PENDING runs are still dropped by GitHub;
    the next run's merged-release-PR await covers them.)"""
    concurrency = _yaml(RELEASE_WORKFLOW)["concurrency"]
    assert concurrency["group"] == "release-deploy"
    assert concurrency["cancel-in-progress"] is False


def test_version_txt_matches_the_release_please_manifest():
    """build-image bakes version.txt; promote checks the label against the
    tag release-please derives from the manifest. Out of step, every release
    fails promote."""
    import json

    manifest = json.loads((REPO_ROOT / ".release-please-manifest.json").read_text())
    assert manifest == {".": (REPO_ROOT / "version.txt").read_text().strip()}


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
# Why the filter went (semantic-release era): commit intent answered "should
# this merge ship?" and the paths filter answered it again by a wrong proxy,
# file paths. Since INFRA-42 the answer is the owner merging the release-please
# PR, and every push must reach release-please.
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
    to touch an allowlisted path. Since INFRA-42 the real question is answered
    by the owner merging the release-please PR.

    ⚠ `paths-ignore` is checked too: it is the same gate spelled inversely and
    would otherwise walk straight past a fence that only looked for `paths`.
    """
    push = _triggers(RELEASE_WORKFLOW).get("push")
    assert isinstance(push, dict) and push, "release.yml lost its push trigger"
    offenders = [k for k in ("paths", "paths-ignore") if k in push]
    assert not offenders, (
        f"release.yml's `on.push` reintroduced {offenders}. Do not suppress "
        "releases by file path — a path filter cannot tell shipping intent "
        "from a chore, and a skipped push can leave a merged release PR "
        "untagged. What ships is decided by merging the release-please PR, "
        "through the `release_created` condition on `deploy`. TBD-424 defect 2."
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
        "a Release run; what ships is decided by merging the release-please PR, "
        "through the `release_created` condition on `deploy`. TBD-424."
    )


def test_release_deploy_still_gates_solely_on_release_created():
    """F3 (TBD-424). The dangerous wrong fix.

    Removing the paths filter AND loosening this condition turns release.yml
    into deploy-on-every-merge -- a production push for every docs typo. The
    filter's removal is only safe BECAUSE this condition is the gate. It is
    also the "exactly once per release" rule: release-please reports
    `release_created` only in the run that creates the tag.
    """
    deploy = _yaml(RELEASE_WORKFLOW)["jobs"]["deploy"]
    condition = _normalise_expr(deploy.get("if"))
    assert condition == "needs.release.outputs.release_created == 'true'", (
        f"release.yml's `deploy` job guard is now {condition!r}. It must stay "
        "exactly `needs.release.outputs.release_created == 'true'`: with "
        "the trigger-level paths filter gone (TBD-424) this condition is the "
        "ONLY thing standing between a docs-only merge and a production "
        "deploy. Widening it -- or adding an `||` arm -- ships everything."
    )


def test_release_workflow_does_not_do_its_own_change_detection():
    """F5 (TBD-424). The rejected alternative, banned explicitly.

    `test.yml`'s detector (scripts/ci/detect-changed-areas.sh) is
    VERDICT-NEUTRAL: it fails TRUE on any uncertainty and structurally cannot
    turn a red suite green. The same detector on the release side would be
    VERDICT-CHANGING -- it could skip the run that tags a merged release PR,
    a silent UNDER-release, a failure mode this pipeline has never had.
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
        "it could silently SUPPRESS the run that tags a merged release PR."
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
        "needs.release.outputs.release_created == 'true'",
        "needs.deploy.result != 'success'",
    ):
        assert fragment in condition, (
            f"`{NOTIFIER_JOB}`'s `if:` is {condition!r} and is missing "
            f"{fragment!r}. Without `always()` the job is skipped along with "
            "its failed upstream; without the `release_created` arm it "
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


def _notifier_fires(condition: str, release: str, created: str, deploy: str) -> bool:
    """Evaluate the notifier's `if:` for one outcome. Only the operators and
    contexts it uses are supported; anything else fails the eval loudly."""
    expr = (
        condition.replace("always()", "True")
        .replace("&&", " and ")
        .replace("||", " or ")
        .replace("needs.release.outputs.release_created", repr(created))
        .replace("needs.release.result", repr(release))
        .replace("needs.deploy.result", repr(deploy))
    )
    return eval(expr, {"__builtins__": {}}, {"True": True})


@pytest.mark.parametrize(
    ("release", "created", "deploy", "fires"),
    [
        ("success", "true", "failure", True),  # released, deploy failed
        ("success", "true", "cancelled", True),
        ("success", "true", "success", False),  # released and deployed
        ("success", "", "skipped", False),  # ordinary merge
        ("skipped", "", "skipped", False),  # await-tests red: its own red run
        ("failure", "", "skipped", True),  # INFRA-42: died after publishing?
        ("cancelled", "", "skipped", True),
    ],
)
def test_the_notifier_fires_for_every_undeployed_outcome(release, created, deploy, fires):
    """INFRA-42. release-please can publish the GitHub Release and then fail
    (PR comment, relabel): no `release_created`, `deploy` skips, and a re-run
    throws DuplicateReleaseError. That version would never reach DO, silently,
    so a failed release job must raise the alarm too, without firing on an
    ordinary merge."""
    condition = _normalise_expr(_yaml(RELEASE_WORKFLOW)["jobs"][NOTIFIER_JOB].get("if"))
    assert _notifier_fires(condition, release, created, deploy) is fires, condition


def test_the_notifier_names_a_failed_release_job_instead_of_a_manual_deploy(tmp_path):
    """The alarm must say what happened: with no tag and a failed release job
    it points at the Releases page, not at a manual deploy.yml run."""
    import os
    import subprocess

    log = tmp_path / "gh.log"
    stub = tmp_path / "gh"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{log}"\nexit 0\n')
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GH_TOKEN": "x", "GH_REPO": "o/r", "RUN_ID": "1", "SHA": "abc",
        "REF_NAME": "main", "ACTOR": "a", "DEPLOY_RESULT": "skipped",
        "RELEASE_TAG": "", "RELEASE_RESULT": "failure",
    }
    script = REPO_ROOT / "scripts" / "notify-undeployed-release.sh"
    done = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    body = log.read_text()
    assert "issue" in body and "create" in body, body
    assert "may already have published a GitHub Release" in body
    assert "manual `deploy.yml` run" not in body


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


# ── TBD-371: the founder-count exclusion list must never be plaintext here ───


_EXCLUSION_KEY = "FOUNDER_COUNT_EXCLUDE_USERNAMES"


def _all_env_entries(doc) -> list[dict]:
    """Every `envs:` entry anywhere in the spec (services, jobs, workers).

    Walks rather than indexing a known path: the founder-count variable is a
    RUN_AND_BUILD_TIME var on the api service today, but a future spec that
    also set it on `jobs.migrate` -- which already binds its own REDIS_URL --
    must be covered too, not silently skipped.
    """
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "envs" and isinstance(value, list):
                    found.extend(e for e in value if isinstance(e, dict))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return found


def test_founder_count_exclusion_is_never_a_plaintext_value():
    """The smoke-account username must not be published in the app spec.

    `.do/app.yaml` is source. It previously carried the production
    post-deploy smoke account username as a plaintext `value:` -- an account
    `docs/operations/DEPLOYMENT.md` requires to have NO MFA -- so the repo named the weakest
    authenticated account in production (TBD-371).

    Two accepted shapes:

      * `type: SECRET` with an `EV[...]` ciphertext (the real fix; only
        DigitalOcean can mint that blob, so it lands out of band), or
      * an EMPTY `value:`, the interim state.

    Anything else means a username -- or some other real identifier -- has
    been pasted back in.

    ⚠ This fence names no username. Asserting "the literal is absent" would
    have to EMBED the literal to search for it, republishing the exact string
    the ticket exists to unpublish. It asserts the shape instead, so it also
    catches a DIFFERENT account being pasted in later.
    """
    import yaml

    doc = yaml.safe_load(APP_SPEC.read_text())
    entries = _all_env_entries(doc)

    # Positive baseline: a mis-parsed spec would make the assertions below
    # pass vacuously, which is how this class of fence usually dies.
    assert len(entries) > 5, (
        f"parsed only {len(entries)} env entries from {APP_SPEC} — the spec "
        "did not parse as expected, so this fence proves nothing"
    )

    matches = [e for e in entries if e.get("key") == _EXCLUSION_KEY]

    assert matches, (
        f"{_EXCLUSION_KEY} is missing from {APP_SPEC}. Do not delete this key "
        "to 'clean it up': the committed spec is authoritative on every "
        "deploy, so the key must exist here for the value to survive. "
        "Deleting it silently drops the exclusion list and inflates the "
        "public founder count (TBD-371)."
    )

    for entry in matches:
        value = entry.get("value", "")
        if entry.get("type") == "SECRET":
            assert isinstance(value, str) and value.startswith("EV["), (
                f"{_EXCLUSION_KEY} is typed SECRET but its value is not an "
                "EV[...] ciphertext. A SECRET carrying plaintext publishes it "
                "just as loudly as a plain value does."
            )
            continue
        assert value == "", (
            f"{_EXCLUSION_KEY} carries a non-empty plaintext value in "
            f"{APP_SPEC}. That file is source, so this publishes the account "
            "it names — and the production smoke account has no MFA "
            "(TBD-371). Set it as a SECRET in the DO console, sync the "
            'EV[...] blob back here, and replace `value: ""` with '
            "`type: SECRET`."
        )
