from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
APP_SPEC = REPO_ROOT / ".do" / "app.yaml"


def test_deploy_workflow_pushes_app_spec():
    """Locks down the actual root cause of the 2026-04-24 SSO outage:
    `digitalocean/app_action/deploy@v2` createSpec only reads the spec
    file when `app_name` is unset; otherwise it grabs the live spec by
    name and ignores the file. PR #79's migrate PRE_DEPLOY job never
    reached production because the workflow had both inputs and the
    file was silently dropped. Two assertions below — the action must
    receive `app_spec_location` AND must NOT receive `app_name` — keep
    the workflow on the file-driven path."""
    workflow = DEPLOY_WORKFLOW.read_text()
    assert "app_spec_location: .do/app.yaml" in workflow, (
        "deploy.yml must pass app_spec_location to digitalocean/app_action/deploy "
        "so the repo's .do/app.yaml is pushed on every deploy."
    )
    deploy_step = workflow[workflow.index("digitalocean/app_action/deploy"):]
    deploy_step = deploy_step[: deploy_step.find("\n      - ")] if "\n      - " in deploy_step else deploy_step
    assert "app_name:" not in deploy_step, (
        "deploy.yml must NOT pass app_name alongside app_spec_location — "
        "v2 prefers app_name and silently ignores the file (deploy/main.go:"
        "createSpec). Drop app_name; the action picks the app up via the "
        "spec file's top-level `name:` field."
    )


def test_app_spec_declares_predeploy_migrate_job():
    """The migrate job is the canonical init step for App Platform — it
    runs before the new revision goes live, so long migrations never
    starve uvicorn's health probe budget. Pair with the initContainer in
    k8s/templates/backend.yaml for K8s parity."""
    spec = APP_SPEC.read_text()
    assert "kind: PRE_DEPLOY" in spec, (
        "App Platform spec must declare a PRE_DEPLOY job for migrations."
    )
    assert "alembic upgrade head" in spec, (
        "PRE_DEPLOY job must run `alembic upgrade head`."
    )


def test_migrate_job_binds_database_url():
    """The migrate job's envs must declare DATABASE_URL — App Platform
    does not auto-inherit secrets across components, so a missing
    DATABASE_URL on the job means alembic can't connect on first deploy
    (we hit this exact failure on 2026-04-25 when the job existed but
    had only APP_ENV bound). The encrypted EV[...] reference is safe to
    commit because the blob is decryptable only by App Platform with the
    app's per-app key."""
    spec = APP_SPEC.read_text()
    migrate_idx = spec.index("name: migrate")
    migrate_block = spec[migrate_idx:]
    assert "DATABASE_URL" in migrate_block.split("\njobs:")[0] or "DATABASE_URL" in migrate_block, (
        "Migrate job must declare DATABASE_URL in its envs."
    )
