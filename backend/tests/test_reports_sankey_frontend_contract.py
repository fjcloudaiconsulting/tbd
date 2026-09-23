"""TBD-552 — the frontend half of the Sankey filter whitelist must match the
backend's ``_SANKEY_SUPPORTED_FILTER_FIELDS`` exactly.

``useSankeyQuery.ts`` inverts ``buildSankeyBody``'s strip from a ``txn_type``
deny-list to a keep-by-allowlist (RULING 4): it must fail CLOSED on every
future catalog filter, which only holds if its allowlist is the SAME set the
backend accepts. A backend field missing from the frontend list is silently
stripped from every request (a dead control); a frontend field missing from
the backend list 422s on the endpoint the moment anyone tries it.

Same shape as ``test_currency_list_frontend_contract.py``: parsed, never
grepped, reuses its repo-root walk, and FAILS rather than SKIPS when the
frontend file — or the array in it — is unreachable. A skip reads as a pass
and leaves this fence permanently dead exactly when a rename or a shape
change breaks it.

⚠ INTEGRATION NOTE for whoever lands the frontend half: this test expects an
array literal of Sankey filter field strings named
``SANKEY_SUPPORTED_FILTER_FIELDS`` in ``frontend/lib/reports/useSankeyQuery.ts``
(``const`` or ``export const``), e.g.::

    const SANKEY_SUPPORTED_FILTER_FIELDS = ["date", "amount", "category_id",
      "account_id", "status", "tag_name"];

If the frontend array is named or shaped differently, update ``_ARRAY_NAME``
/ ``_parse_array`` below in the same commit rather than loosening the match.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.sankey_service import _SANKEY_SUPPORTED_FILTER_FIELDS

_ARRAY_NAME = "SANKEY_SUPPORTED_FILTER_FIELDS"


def _find_repo_root(start: Path) -> Path:
    """Walk up for the repo root instead of assuming a fixed depth.

    ⚠ NOT ``parents[2]`` — see ``test_currency_list_frontend_contract.py``,
    which documents why a fixed depth is a SKIP-shaped failure rather than a
    CI-gated one.
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


def _use_sankey_query_ts() -> str:
    """Read the frontend file, in the container or on a plain checkout.

    ⚠⚠ FAILS; NEVER SKIPS. A skip is absent exactly when it matters — see
    ``test_currency_list_frontend_contract.py`` for the same rule in the same
    words. ``docker-compose.yml`` already mounts ``./frontend/lib`` read-only
    into the backend container (for that same fence), so no compose change is
    needed here.
    """
    candidates = [
        Path("/app/frontend/lib/reports/useSankeyQuery.ts"),
        _find_repo_root(Path(__file__).resolve())
        / "frontend"
        / "lib"
        / "reports"
        / "useSankeyQuery.ts",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text()
    pytest.fail(
        "frontend/lib/reports/useSankeyQuery.ts is unreachable, so the drift "
        "fence between the Sankey filter strip and _SANKEY_SUPPORTED_FILTER_FIELDS "
        f"did not run. Looked in: {[str(c) for c in candidates]}. Inside the "
        "backend container this means the read-only ./frontend/lib mount in "
        "docker-compose.yml is missing or severed — "
        "`docker compose up -d --force-recreate backend`. If the file was "
        "renamed, update this path in the same commit."
    )


def _parse_array(source: str, name: str) -> list[str]:
    """Extract the string entries of a named array, exported or local."""
    m = re.search(
        rf"(?:export\s+)?const {name}[^=]*=\s*\[(.*?)\];", source, re.S
    )
    assert m, (
        f"{name} not found in useSankeyQuery.ts, or its shape changed. See the "
        "integration note at the top of this file for the expected shape."
    )
    return re.findall(r'"([a-z_]+)"', m.group(1))


def test_sankey_frontend_filter_allowlist_matches_backend_exactly():
    """Both directions. Either half alone fails open."""
    frontend = set(_parse_array(_use_sankey_query_ts(), _ARRAY_NAME))
    backend = {f.value for f in _SANKEY_SUPPORTED_FILTER_FIELDS}

    frontend_only = sorted(frontend - backend)
    backend_only = sorted(backend - frontend)

    problems = []
    if frontend_only:
        problems.append(
            "the frontend keeps field(s) the backend will 422 on: "
            f"{frontend_only}"
        )
    if backend_only:
        problems.append(
            "the backend supports field(s) the frontend strips before they "
            f"ever reach the endpoint: {backend_only}"
        )
    assert not problems, "\n".join(problems)
