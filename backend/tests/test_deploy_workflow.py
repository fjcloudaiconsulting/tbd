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

REPO_ROOT = Path(__file__).resolve().parents[2]
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
    assert "alembic upgrade head" in spec, "PRE_DEPLOY job must run alembic"


def test_migrate_job_binds_database_url():
    spec = APP_SPEC.read_text()
    migrate_block = spec[spec.index("name: migrate"):]
    assert "DATABASE_URL" in migrate_block, (
        "Migrate job must declare DATABASE_URL — App Platform does not "
        "auto-inherit secrets to PRE_DEPLOY jobs."
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
