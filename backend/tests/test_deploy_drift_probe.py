"""Fences for the scheduled deploy-drift probe (TBD-434).

⚠⚠ The probe must be READ-ONLY, and that is not a style preference. TBD-425 was
caused BY a deploy: `app_action/deploy@v2` pushes the committed `.do/app.yaml`
as authoritative and overwrote live database and redis credentials. A probe that
"checks liveness" by deploying would have destroyed working credentials sooner.
`test_the_probe_never_mutates_the_app` is the fence that keeps that true.

⚠ `yaml.safe_load` parses the bare key `on:` as the boolean `True`, not the
string `"on"`. `doc.get("on", {})` silently yields `{}` and every trigger
assertion would pass vacuously. Read both spellings.
"""

import os
import pathlib

import pytest
import yaml


def _find_repo_root(start: pathlib.Path) -> pathlib.Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "deploy-drift-probe.yml").exists():
            return candidate
    return None


REPO_ROOT = _find_repo_root(pathlib.Path(__file__).resolve())

if REPO_ROOT is None and os.environ.get("GITHUB_ACTIONS") == "true":  # pragma: no cover
    raise RuntimeError(
        "deploy-drift-probe.yml not found from a CI checkout; these fences must "
        "not be allowed to skip on the runner."
    )

pytestmark = pytest.mark.skipif(
    REPO_ROOT is None,
    reason="workflow tree is not mounted into the backend container; runs in CI",
)

WORKFLOW = "deploy-drift-probe.yml"
PROBE = "scripts/ci/check-deploy-drift.sh"
NOTIFIER = "scripts/notify-deploy-drift.sh"


def _repo_script(relative: str) -> pathlib.Path:
    """Locate a repo-ROOT script in either layout.

    ⚠ `REPO_ROOT / "scripts/..."` is wrong inside the backend container and
    right on a CI runner, which is why these fences were green in CI and red in
    every dev container. REPO_ROOT resolves to `/app` (the `.github` mount puts
    the workflow there), but `/app/scripts` is already `backend/scripts` -- the
    repo-root `scripts/` has its own read-only mount at `/app/repo-scripts`.
    Same resolver as test_await_test_run_gate.py, and raising rather than
    skipping for the same reason: a skip would make the probe's central fence
    silently absent in whichever environment lacked the path.
    """
    direct = REPO_ROOT / relative
    if direct.is_file():
        return direct
    container_mount = pathlib.Path("/app/repo-scripts") / relative.split("scripts/", 1)[1]
    if container_mount.is_file():
        return container_mount
    raise RuntimeError(
        f"Could not locate {relative}. In the backend container this needs the "
        "./scripts:/app/repo-scripts:ro mount; a container built before that "
        "mount existed shows this module red. Run "
        "`docker compose up -d --force-recreate backend` once."
    )

# `doctl` verbs that CHANGE the app. None may appear in the probe path.
MUTATING = (
    "apps update",
    "apps create",
    "apps create-deployment",
    "apps delete",
    "apps restart",
    "app_action",
    "apps propose",
)


def _doc() -> dict:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / WORKFLOW).read_text())


def _triggers() -> dict:
    doc = _doc()
    on = doc.get("on") or doc.get(True)
    assert on, "could not read the `on:` block (the YAML-1.1 `on` -> True trap?)"
    return on


def _steps() -> list[dict]:
    return list(_doc()["jobs"]["probe"]["steps"])


def test_the_workflow_is_shaped_as_this_module_assumes():
    """Positive baseline; without it a rename empties every collection below and
    the fences pass while asserting nothing."""
    doc = _doc()
    assert "probe" in doc.get("jobs", {}), f"jobs were {list(doc.get('jobs', {}))}"
    assert len(_steps()) >= 4, f"parsed only {len(_steps())} step(s)"
    assert _triggers(), "no triggers parsed"


def test_the_probe_never_mutates_the_app():
    """THE fence. Read-only is the entire design (see the module docstring).

    Scans both the workflow and the probe script, because a mutating call could
    be added in either.
    """
    blobs = {
        WORKFLOW: (REPO_ROOT / ".github" / "workflows" / WORKFLOW).read_text(),
        PROBE: _repo_script(PROBE).read_text(),
    }
    offenders = []
    for name, text in blobs.items():
        for verb in MUTATING:
            # The docstrings deliberately NAME app_action while explaining the
            # danger, so only flag it outside comment lines.
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if verb in stripped:
                    offenders.append(f"{name}: {stripped[:90]}")
    assert not offenders, (
        "the drift probe must be READ-ONLY -- TBD-425 was caused BY a deploy "
        f"overwriting live secrets. Mutating call(s) found: {offenders}"
    )


def test_it_runs_on_a_schedule_and_can_be_run_by_hand():
    triggers = _triggers()
    assert "schedule" in triggers, "the probe must run on a schedule; that is its point"
    assert triggers["schedule"], "schedule block is empty"
    assert "workflow_dispatch" in triggers, "must be runnable by hand during an incident"


def test_it_self_tests_when_the_probe_itself_changes():
    """A PR editing the probe should exercise it, not wait for the next cron."""
    paths = set((_triggers().get("push") or {}).get("paths") or [])
    for required in (f".github/workflows/{WORKFLOW}", PROBE, NOTIFIER):
        assert required in paths, f"{required} does not self-test the probe"


def test_tags_are_fetched():
    """The probe compares the deployed commit against the LATEST RELEASE TAG.
    A shallow clone has no tags, so it would exit 2 on every run."""
    checkout = next(s for s in _steps() if str(s.get("uses", "")).startswith("actions/checkout"))
    assert str(checkout.get("with", {}).get("fetch-depth")) == "0", (
        "the probe needs tags; a depth-1 clone has none"
    )


def test_the_notifier_only_fires_on_actual_drift():
    """Anti-alarm-fatigue. A notifier that opens an issue on every green run is
    ignored within a week, which makes the whole ticket worthless."""
    step = next(s for s in _steps() if NOTIFIER in str(s.get("run", "")))
    cond = str(step.get("if", ""))
    assert "drifted" in cond and "true" in cond, (
        f"the notifier must be gated on the drift verdict; its `if` was {cond!r}"
    )
    assert "always()" not in cond, "gating on always() would alarm on every run"


def test_the_probe_job_can_open_issues():
    perms = _doc()["jobs"]["probe"].get("permissions") or {}
    assert perms.get("issues") == "write", (
        "job-level permissions REPLACE the workflow-level block, so `issues: write` "
        f"must be spelled out on the job; got {perms}"
    )


def test_the_probe_actually_checks_secret_spec_drift():
    """TBD-434 DoD 2: the probe must ALSO run assert-app-spec-secrets-synced.sh.

    The commit comparison answers "is production running the released code?". It
    cannot see the other half of the TBD-425 failure: the committed
    `.do/app.yaml` disagreeing with the live app's secrets, which is invisible
    until the next deploy OVERWRITES production's working credentials.

    ⚠ THIS FENCE MUST NOT BE SATISFIABLE BY A COMMENT. The first cut of the
    probe *mentioned* the guard in its drift-report text ("check
    assert-app-spec-secrets-synced.sh first") while never invoking it, and a
    naive `grep -c` on the file returned 1 and looked delivered. Same shape as
    the TBD-433 trap where a whole-file grep was satisfied by the comment
    documenting the defect. So: strip comment lines, then assert.
    """
    text = _repo_script(PROBE).read_text()
    code = [
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    invocations = [l for l in code if "assert-app-spec-secrets-synced" in l]
    assert invocations, (
        "the probe does not INVOKE assert-app-spec-secrets-synced.sh in any "
        "non-comment line, so secret-spec drift -- the actual TBD-425 "
        "mechanism -- goes unchecked"
    )
    # And it must be executed, not merely named in a string.
    assert any('"$SPEC_GUARD"' in l or "SPEC_GUARD=" in l for l in code), (
        "the guard is named but never executed"
    )


def test_a_missing_secret_guard_fails_loud_rather_than_passing():
    """If the guard is absent or non-executable the probe must report drift, not
    a partial all-clear. A monitor that silently checks less than it claims is
    worse than no monitor."""
    code = _repo_script(PROBE).read_text()
    assert "secret drift is UNCHECKED" in code, (
        "the probe must fail loud when it cannot run the secret guard"
    )
