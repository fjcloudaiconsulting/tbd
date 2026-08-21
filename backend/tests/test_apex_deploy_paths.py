"""Fence that `apex-deploy.yml`'s trigger paths cover everything the apex ships.

## The defect this exists to stop (TBD-433)

`frontend/scripts/build-apex.sh` decides what the apex bundle contains, via
`ALLOWED_ROUTE_DIRS` and `ALLOWED_APP_FILES`. `.github/workflows/apex-deploy.yml`
decides when the apex is REBUILT. Those two lists have to stay exact
complements, by hand, forever -- and they did not.

`features`, `compare` and `vs` were added to `build-apex.sh` on 2026-06-09
(`0b73a5cc`, #421) and never mirrored into the trigger. For ~72 days a commit
confined to any of them published **nothing** to thebetterdecision.com, while
still triggering a full DigitalOcean redeploy of the authed app -- inverted from
the intent. It was masked by coincidence: the one landing PR that touched
`features/` also touched `privacy/`, so the apex happened to redeploy.

It had also happened once before (`d6e54298` / #466, "redeploy apex on
analytics-only changes"), and was patched by adding two more entries rather than
by making the lists verify each other. Hence this fence.

⚠ `release.yml` lost its own path allowlist in TBD-424, so this is now the
repo's ONLY hand-maintained path list. That makes it the only place this class
of drift can still occur, and the reason the fence lives here rather than
being generalised.

## ⚠ Read the PATHS BLOCK, never the whole file

While fixing this I checked coverage with `grep -q "frontend/app/features/"`
over the whole workflow and it PASSED -- because the string appears in a COMMENT
describing the drift, not in `paths:`. The documentation of the defect made a
naive check for the defect succeed. This module parses the YAML and reads
`on.push.paths`, so a comment can never satisfy it.

⚠ `yaml.safe_load` parses the bare key `on:` as the boolean `True`, not the
string `"on"`. `doc.get("on", {})` silently yields `{}` and every assertion
below would pass vacuously. Read both keys.
"""

import os
import pathlib
import re

import pytest
import yaml


def _find_repo_root(start: pathlib.Path) -> pathlib.Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "apex-deploy.yml").exists() and (
            candidate / "frontend" / "scripts" / "build-apex.sh"
        ).exists():
            return candidate
    return None


REPO_ROOT = _find_repo_root(pathlib.Path(__file__).resolve())

# Skipping is correct in the backend dev container (neither `.github/` nor
# `frontend/scripts/` is mounted there) and FATAL in CI, where pytest runs on a
# plain full checkout. A fence that quietly skips on the runner reports green
# while asserting nothing.
if REPO_ROOT is None and os.environ.get("GITHUB_ACTIONS") == "true":  # pragma: no cover
    raise RuntimeError(
        "apex-deploy.yml / build-apex.sh not found from a CI checkout. These "
        "fences must not be allowed to skip on the runner."
    )

pytestmark = pytest.mark.skipif(
    REPO_ROOT is None,
    reason="workflow + build script are not mounted into the backend container; runs in CI",
)


def _bash_array(name: str, text: str) -> list[str]:
    """Extract a simple quoted bash array literal, e.g. ALLOWED_ROUTE_DIRS=(...)."""
    m = re.search(rf"^{name}=\((.*?)^\)", text, re.MULTILINE | re.DOTALL)
    assert m, f"could not find a {name}=( ... ) array in build-apex.sh"
    return re.findall(r'"([^"]+)"', m.group(1))


def _trigger_paths() -> list[str]:
    doc = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "apex-deploy.yml").read_text()
    )
    # Bare `on:` parses as the boolean True. Read both spellings.
    on = doc.get("on") or doc.get(True)
    assert on, "could not read the `on:` block (the YAML-1.1 `on` -> True trap?)"
    return list(on["push"]["paths"])


def _build_script() -> str:
    return (REPO_ROOT / "frontend" / "scripts" / "build-apex.sh").read_text()


def test_the_two_files_are_shaped_as_this_module_assumes():
    """Positive baseline. Without it a renamed array or a moved key yields empty
    collections and every assertion below passes vacuously."""
    paths = _trigger_paths()
    dirs = _bash_array("ALLOWED_ROUTE_DIRS", _build_script())
    files = _bash_array("ALLOWED_APP_FILES", _build_script())
    assert len(paths) >= 20, f"parsed only {len(paths)} trigger path(s)"
    assert len(dirs) >= 5, f"parsed only {len(dirs)} route dir(s): {dirs}"
    assert len(files) >= 5, f"parsed only {len(files)} app file(s): {files}"
    assert "privacy" in dirs and "docs" in dirs


def test_every_apex_route_dir_triggers_a_redeploy():
    """THE fence. Each dir the apex bundle SHIPS must also REBUILD it.

    Went RED against `main` naming exactly the three that had drifted.
    """
    dirs = _bash_array("ALLOWED_ROUTE_DIRS", _build_script())
    paths = set(_trigger_paths())
    missing = [d for d in dirs if f"frontend/app/{d}/**" not in paths]
    assert not missing, (
        "these route dirs are shipped by build-apex.sh but do NOT trigger an "
        f"apex redeploy, so a change confined to them publishes nothing: {missing}. "
        "Add `frontend/app/<dir>/**` to apex-deploy.yml's `on.push.paths`."
    )


def test_every_apex_app_file_triggers_a_redeploy():
    """Same invariant for the structural app-level files the bundle retains."""
    files = _bash_array("ALLOWED_APP_FILES", _build_script())
    paths = set(_trigger_paths())
    missing = [f for f in files if f"frontend/app/{f}" not in paths]
    assert not missing, (
        "these app files are retained by build-apex.sh but do NOT trigger an "
        f"apex redeploy: {missing}"
    )


def test_the_workflow_still_triggers_on_itself():
    """A change to the trigger list must republish, or a fix to this very file
    does not take effect until something unrelated lands."""
    assert ".github/workflows/apex-deploy.yml" in set(_trigger_paths())
