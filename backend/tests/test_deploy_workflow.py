from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
APP_SPEC = REPO_ROOT / ".do" / "app.yaml"


def test_deploy_workflow_pushes_app_spec():
    """Locks down the actual root cause of the 2026-04-24 SSO outage:
    `digitalocean/app_action/deploy@v2` only redeploys against the live
    spec unless `app_spec_location` is set, so PR #79's migrate PRE_DEPLOY
    job never reached production. If this test fails, the workflow has
    regressed to the silent-drift state."""
    workflow = DEPLOY_WORKFLOW.read_text()
    assert "app_spec_location: .do/app.yaml" in workflow, (
        "deploy.yml must pass app_spec_location to digitalocean/app_action/deploy "
        "so the repo's .do/app.yaml is pushed on every deploy."
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
