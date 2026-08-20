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
