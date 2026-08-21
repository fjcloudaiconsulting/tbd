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
        PROBE: (REPO_ROOT / PROBE).read_text(),
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
