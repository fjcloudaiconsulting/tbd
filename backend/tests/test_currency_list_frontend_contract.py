"""The frontend currency list must match the backend's (TBD-325).

`frontend/lib/currencies.ts` duplicates `ISO_4217_CURRENCIES` so the account
picker can render without a round-trip. Duplication without a fence is drift,
and this repo has been bitten by exactly that before — the hand-written
report-sources fixture disagreed with production in 16 places, one of them a
live right/wrong split.

⚠ THIS TEST FAILS RATHER THAN REGENERATING. If it is red, one side gained or
lost a currency. Read the diff and decide which is correct. Auto-syncing the
frontend from the backend would restore precisely the silent drift it exists
to stop: a currency the picker offers but the API rejects is a user-visible
422 on a valid-looking choice, and one the API accepts but the picker omits is
an org that can never be created through the UI.

⚠ Parsed, never grepped. A bare `grep '"USD"'` over the file is satisfied by
the string appearing in a comment or in the COMMON_CURRENCIES label table.
This extracts the ALL_CURRENCIES array specifically.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.currency_service import ISO_4217_CURRENCIES

# Pinned so a currency cannot be added or removed without an explicit, reviewed
# edit here. ⚠ Adding is the dangerous direction: currency is immutable
# post-create, so a code that should not be offered locks an org to it forever.
EXPECTED_CURRENCY_COUNT = 156


def _find_repo_root(start: Path) -> Path:
    """Walk up for the repo root instead of assuming a fixed depth.

    ⚠ NOT ``parents[2]``. That happens to be correct today from
    ``backend/tests/``, but it is silently wrong the moment this file moves one
    directory deeper (``backend/tests/contracts/``, a plausible tidy-up) — and
    the failure is a SKIP, not a red. Same walk as
    ``test_period_status_frontend_contract.py``, which documents that a fixed
    depth is developer-gated rather than CI-gated: on a plain checkout with
    ``working-directory: backend`` the wrong depth still resolves, so a
    regression reaches ``main`` with every check green.
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


def _currencies_ts() -> str:
    """Read the frontend list, in the container or on a plain checkout.

    ⚠⚠ THIS FAILS; IT NEVER SKIPS. A guard that skips is absent exactly when it
    matters and fails OPEN — rename ``currencies.ts`` and update only the
    frontend import, and a skipping version of this file would go green while
    the drift fence was permanently dead. That is the sibling of every
    green-and-worthless test this repo has shipped, and
    ``test_period_status_frontend_contract.py`` states the same rule in the
    same words.
    """
    candidates = [
        Path("/app/frontend/lib/currencies.ts"),
        _find_repo_root(Path(__file__).resolve()) / "frontend" / "lib" / "currencies.ts",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text()
    pytest.fail(
        "frontend/lib/currencies.ts is unreachable, so the drift fence between "
        "the picker and ISO_4217_CURRENCIES did not run. Looked in: "
        f"{[str(c) for c in candidates]}. Inside the backend container this "
        "means the read-only ./frontend/lib mount in docker-compose.yml is "
        "missing or severed — `docker compose up -d --force-recreate backend`. "
        "If the file was renamed, update this path in the same commit."
    )


def _parse_array(source: str, name: str) -> list[str]:
    """Extract the string entries of a named exported array.

    Anchored on the declaration and terminated at the closing bracket, so a
    later array in the same file cannot bleed in.
    """
    m = re.search(
        rf"export const {name}:[^=]*=\s*\[(.*?)\];", source, re.S
    )
    assert m, f"{name} not found in currencies.ts, or its shape changed"
    return re.findall(r'"([A-Z]{3})"', m.group(1))


def test_frontend_all_currencies_matches_the_backend_set_exactly():
    """Both directions. Either half alone fails open."""
    ts = _parse_array(_currencies_ts(), "ALL_CURRENCIES")
    front, back = set(ts), set(ISO_4217_CURRENCIES)

    offered_but_rejected = sorted(front - back)
    accepted_but_hidden = sorted(back - front)

    problems = []
    if offered_but_rejected:
        problems.append(
            "the picker OFFERS currencies the API REJECTS — a user picking one "
            f"gets a 422 on a valid-looking choice: {offered_but_rejected}"
        )
    if accepted_but_hidden:
        problems.append(
            "the API ACCEPTS currencies the picker does NOT offer — those orgs "
            f"cannot be created through the UI at all: {accepted_but_hidden}"
        )
    assert not problems, "\n".join(problems)


def test_the_frontend_list_has_no_duplicates():
    """A duplicated line renders the same option twice.

    Compared against the parsed list rather than the set used above, because
    that comparison would silently absorb a repeat.
    """
    ts = _parse_array(_currencies_ts(), "ALL_CURRENCIES")
    dupes = sorted({c for c in ts if ts.count(c) > 1})
    assert not dupes, f"duplicated entries in ALL_CURRENCIES: {dupes}"


def test_every_common_currency_is_also_in_the_full_list():
    """The picker's first optgroup must not offer anything the second lacks.

    COMMON_CURRENCIES is a hand-curated shortlist with display names. If a code
    there is not in ALL_CURRENCIES it is either a typo or a currency the
    backend rejects, and it renders as a broken option either way.
    """
    # ⚠ Scoped to the COMMON_CURRENCIES array, not the whole file. A file-wide
    # scan for the [code, name] shape would silently absorb any second such
    # array added later, and would then be asserting about the wrong list.
    src = _currencies_ts()
    block = re.search(
        r"export const COMMON_CURRENCIES:[^=]*=\s*\[(.*?)\];", src, re.S
    )
    assert block, "COMMON_CURRENCIES not found, or its shape changed"
    common = re.findall(r'\["([A-Z]{3})",\s*"[^"]+"\]', block.group(1))
    assert common, "COMMON_CURRENCIES matched but yielded no [code, name] pairs"

    # Compared against ALL_CURRENCIES, which is what the picker actually renders
    # from. Comparing against the backend set would be equivalent only because
    # the first test forces the two equal — an indirection that breaks the
    # moment that test is weakened.
    all_codes = set(_parse_array(src, "ALL_CURRENCIES"))
    missing = sorted(set(common) - all_codes)
    assert not missing, f"common currencies absent from ALL_CURRENCIES: {missing}"


def test_the_parser_would_notice_an_empty_array():
    """⚠ Control on this file's own instrument.

    If the regex stopped matching, `_parse_array` would return `[]` and the
    equality test above would report all 170 as "accepted but hidden" — loud.
    But a subtler break returning a partial list would look like a small,
    plausible diff. Pin the size so the instrument itself is fenced.
    """
    ts = _parse_array(_currencies_ts(), "ALL_CURRENCIES")
    assert len(ts) == len(ISO_4217_CURRENCIES) == EXPECTED_CURRENCY_COUNT, (
        f"frontend={len(ts)}, backend={len(ISO_4217_CURRENCIES)}. If a currency "
        "was deliberately added or removed, update the expected count here in "
        "the same commit."
    )
