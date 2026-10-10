"""Regression guards for the `release` job in ci.yml and the apex gate.

Production deploys happen in fjcloudaiconsulting/aws-infra (a Renovate bump of
the vX.Y.Z image tag). What this repo owns, and these fences pin, is that a
release is cut only on a push to main, after both gates, through the shared
release workflow (release-please, GHCR promote and smoke of that version).
"""
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    """Walk upward from `start` until `.github/workflows/ci.yml` is found.

    `Path(__file__).parents[2]` works from a host checkout but resolves to `/`
    inside the backend container (this file lives at `/app/tests/`, with the
    repo's `.github` mounted at `/app/.github`). Walking upward handles both.
    """
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "ci.yml").exists():
            return candidate
    raise RuntimeError(
        "Could not locate repo root containing .github/workflows/ci.yml. "
        "Run these tests from a checked-out repo."
    )


REPO_ROOT = _find_repo_root(Path(__file__).resolve())


CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
APEX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "apex-deploy.yml"


def _yaml(path: Path) -> dict:
    import yaml

    doc = yaml.safe_load(path.read_text())
    # Positive baseline: an empty or mis-parsed document would make every
    # assertion below pass vacuously.
    assert isinstance(doc, dict) and doc.get("jobs"), f"failed to parse {path}"
    return doc


def test_release_job_calls_the_shared_workflow_after_both_gates_on_main_only():
    """INFRA-159. release-please, the release-commit gate, promote and smoke
    all live in the shared release.yml@v1; this job only wires it in. Running
    it on a PR would try to tag and needs the `release` environment."""
    job = _yaml(CI_WORKFLOW)["jobs"]["release"]
    assert job["uses"] == "fjcloudaiconsulting/.github/.github/workflows/release.yml@v1"
    assert job["secrets"] == "inherit", "the App secrets live in environment `release`"
    assert _normalise_expr(job["if"]) == (
        "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )
    assert set(job["needs"]) == {"backend", "frontend"}, (
        "release must wait for both required gates, which already need every work job"
    )
    perms = job["permissions"]
    assert perms == {
        "contents": "read",
        "checks": "read",
        "pull-requests": "read",
        "packages": "write",
    }
    assert job["with"]["health-url"] == "http://localhost:8000/health"


def test_release_promotes_exactly_the_images_ci_yml_publishes():
    """INFRA-147. aws-infra deploys tbd/<image>:vX.Y.Z for each of these, so
    each must be published as sha-<7> by ci.yml AND retagged by promote.
    Wrong implementations: an image published but never promoted (no vX.Y.Z,
    the deploy pins a tag that does not exist), promoted but never published
    (promote fails the release), or mcp built from the backend Dockerfile."""
    jobs = _yaml(CI_WORKFLOW)["jobs"]
    matrix = jobs["backend-images"]["strategy"]["matrix"]["include"]
    published = {e["image"] for e in matrix} | {jobs["frontend-image"]["with"]["image"]}
    promoted = set(jobs["release"]["with"]["images"].split())
    assert promoted == published
    assert promoted >= {"backend", "frontend", "migrations", "mcp"}
    assert jobs["backend-images"]["with"]["file"] == "${{ matrix.file }}"
    # Only mcp names a file; a `file` on backend/migrations publishes another image under their name.
    assert {e["image"]: e.get("file") for e in matrix} == {
        "backend": None,
        "migrations": None,
        "mcp": "backend/Dockerfile.mcp",
    }


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
    `workflow_dispatch` arm in `deploy-worker`'s `if:` is what keeps the
    documented stale-deploy recovery working. Without it the recovery path
    silently does nothing.
    """
    jobs = _yaml(APEX_WORKFLOW)["jobs"]
    assert "await-tests" in jobs, "apex-deploy.yml lost its await-tests gate"
    assert "await-tests" in (jobs["deploy-worker"].get("needs") or [])
    assert "workflow_dispatch" in (jobs["await-tests"].get("if") or ""), (
        "the gate must skip on manual dispatch, which is the recovery path"
    )
    guard = jobs["deploy-worker"].get("if") or ""
    assert "workflow_dispatch" in guard, (
        "deploy-worker needs an explicit dispatch arm: `needs:` on a "
        "SKIPPED gate would otherwise skip the manual recovery deploy too."
    )


def _normalise_expr(raw) -> str:
    """Strip `${{ }}` wrapping and collapse whitespace in a workflow `if:`."""
    text = " ".join(str(raw or "").split())
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
        text = " ".join(text.split())
    return text
