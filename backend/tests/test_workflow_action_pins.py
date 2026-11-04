"""INF-06: every action and reusable workflow in .github/workflows is pinned to a commit SHA.

A tag (``@v1``) is a pointer its owner, or anyone who takes over that repo, can move to new code,
and that code then runs with this repo's tokens and inherited secrets. Org-owned workflows are no
exception. Local ``./`` paths are part of this checkout, and ``docker://`` images need a digest.

Known gap: this checks one level deep. The pinned org ``release.yml`` still calls
``promote-release.yml@v1`` and ``smoke.yml@v1``, so nested org-workflow calls are still at @v1;
pin them in fjcloudaiconsulting/.github, then bump tbd's pin to that release.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


def _find_repo_root(start: Path) -> Path:
    """The checkout on a host, /app (where .github is mounted) in the backend container."""
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "ci.yml").exists():
            return candidate
    raise RuntimeError("Could not locate the repo root containing .github/workflows/ci.yml.")


WORKFLOWS = _find_repo_root(Path(__file__).resolve()) / ".github" / "workflows"

PINNED = re.compile(r"[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}")
PINNED_IMAGE = re.compile(r"docker://\S+@sha256:[0-9a-f]{64}")


def _uses() -> list[tuple[str, str]]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for name, job in (yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}).items():
            if "uses" in job:
                found.append((f"{path.name}:{name}", str(job["uses"])))
            for step in job.get("steps") or []:
                if "uses" in step:
                    found.append((f"{path.name}:{name}", str(step["uses"])))
    return found


def _pinned(uses: str) -> bool:
    return uses.startswith("./") or bool(PINNED.fullmatch(uses) or PINNED_IMAGE.fullmatch(uses))


def test_the_scan_sees_the_org_workflows_and_third_party_actions():
    """Non-vacuous: a parse that found nothing would pass the pin check."""
    refs = {uses.split("@")[0] for _, uses in _uses()}
    assert {
        "fjcloudaiconsulting/.github/.github/workflows/release.yml",
        "fjcloudaiconsulting/.github/.github/workflows/build-image.yml",
        "fjcloudaiconsulting/.github/.github/workflows/pr-title.yml",
        "fjcloudaiconsulting/.github/actions/uv-sync",
        "actions/checkout",
    } <= refs


def test_every_uses_is_pinned_to_a_commit_sha():
    unpinned = [(where, uses) for where, uses in _uses() if not _pinned(uses)]
    assert not unpinned, f"pin these to a full commit SHA (with a `# vX.Y.Z` comment): {unpinned}"


def test_the_check_refuses_tags_branches_and_short_shas():
    for uses in (
        "fjcloudaiconsulting/.github/.github/workflows/release.yml@v1",
        "actions/checkout@main",
        "actions/checkout@3d3c42e",
        "docker://alpine:3",
    ):
        assert not _pinned(uses), uses
    assert _pinned("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1")
    assert _pinned("./.github/actions/local")
