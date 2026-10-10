"""Regression guards for the release workflow (release.yml) and the apex gate.

Production deploys happen in fjcloudaiconsulting/aws-infra (a Renovate bump of
the vX.Y.Z image tag). What this repo owns, and these fences pin, is that a
release is cut only for a commit whose post-merge suite passed, exactly once,
by release-please, followed by the GHCR promote and smoke of that version.
"""
from pathlib import Path

import pytest


def _find_repo_root(start: Path) -> Path:
    """Walk upward from `start` until `.github/workflows/release.yml` is found.

    `Path(__file__).parents[2]` works from a host checkout but resolves to `/`
    inside the backend container (this file lives at `/app/tests/`, with the
    repo's `.github` mounted at `/app/.github`). Walking upward handles both.
    """
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "release.yml").exists():
            return candidate
    raise RuntimeError(
        "Could not locate repo root containing .github/workflows/release.yml. "
        "Run these tests from a checked-out repo."
    )


REPO_ROOT = _find_repo_root(Path(__file__).resolve())


# ── TBD-391: the release interlock is actually wired ────────────────────────
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
    """The gate must run BEFORE `release`, not after it.

    release-please cuts an immutable git tag and publishes a GitHub Release.
    Under semantic-release, measured on PR #654, that happened 7m41s before the
    post-merge suite reported, so gating any later job would still leave a
    published release for a commit whose suite then goes red.
    """
    jobs = _yaml(RELEASE_WORKFLOW)["jobs"]
    assert "await-tests" in jobs, "release.yml lost its await-tests gate"
    assert "await-tests" in (jobs["release"].get("needs") or []), (
        "`release` must depend on `await-tests`. Gating a later job leaves "
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


def test_release_workflow_has_exactly_the_gated_release_jobs():
    """Nothing in release.yml deploys (production is the aws-infra bump PR),
    and smoke boots what promote retagged. A new job here must be added to
    this set deliberately, together with its own gate."""
    jobs = _yaml(RELEASE_WORKFLOW)["jobs"]
    assert set(jobs) == {"await-tests", "release", "promote", "release-smoke"}
    needs = jobs["release-smoke"]["needs"]
    assert "promote" in ([needs] if isinstance(needs, str) else needs)


def test_promote_retags_exactly_the_images_test_yml_publishes():
    """INFRA-147. aws-infra deploys tbd/<image>:vX.Y.Z for each of these, so
    each must be published as sha-<7> by ci.yml AND retagged by promote.
    Wrong implementations: an image published but never promoted (no vX.Y.Z,
    the deploy pins a tag that does not exist), promoted but never published
    (promote fails the release), or mcp built from the backend Dockerfile."""
    test_jobs = _yaml(REPO_ROOT / ".github" / "workflows" / "ci.yml")["jobs"]
    matrix = test_jobs["backend-images"]["strategy"]["matrix"]["include"]
    published = {e["image"] for e in matrix} | {test_jobs["frontend-image"]["with"]["image"]}
    promoted = set(_yaml(RELEASE_WORKFLOW)["jobs"]["promote"]["with"]["images"].split())
    assert promoted == published
    assert promoted >= {"backend", "frontend", "migrations", "mcp"}
    assert test_jobs["backend-images"]["with"]["file"] == "${{ matrix.file }}"
    # Only mcp names a file; a `file` on backend/migrations publishes another image under their name.
    assert {e["image"]: e.get("file") for e in matrix} == {
        "backend": None,
        "migrations": None,
        "mcp": "backend/Dockerfile.mcp",
    }


def test_release_runs_are_serialised_and_never_cancelled():
    """TBD-391 / INFRA-42. One Release run at a time, and never
    `cancel-in-progress`: a run cancelled after release-please tagged leaves a
    release whose images were never promoted. (Superseded PENDING runs are
    still dropped by GitHub; the next run's merged-release-PR await covers
    them.)"""
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


def _index_of(steps, predicate, label):
    for i, step in enumerate(steps):
        if predicate(step):
            return i
    raise AssertionError(f"no step matching {label} in the job")


# ---------------------------------------------------------------------------
# TBD-424 defect 2 -- release.yml must have NO trigger-level `paths:` filter,
# and the removal must not be "fixed" by loosening the gated jobs' condition or by
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
        "through the `release_created` condition on `promote`. TBD-424 defect 2."
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
        "through the `release_created` condition on `promote`. TBD-424."
    )


def test_release_workflow_does_not_do_its_own_change_detection():
    """F5 (TBD-424). The rejected alternative, banned explicitly.

    `ci.yml`'s detector (scripts/ci/detect-changed-areas.sh) is
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
        "release path (TBD-424): on ci.yml it can only ever ADD work, here "
        "it could silently SUPPRESS the run that tags a merged release PR."
    )
