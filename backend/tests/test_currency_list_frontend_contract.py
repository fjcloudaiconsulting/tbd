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


def _currencies_ts() -> str:
    """Locate the frontend list in the container and on a plain checkout."""
    candidates = [
        Path("/app/frontend/lib/currencies.ts"),
        Path(__file__).resolve().parents[2] / "frontend" / "lib" / "currencies.ts",
    ]
    for p in candidates:
        if p.exists():
            return p.read_text()
    pytest.skip(f"frontend/lib/currencies.ts not reachable from {candidates}")


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
    src = _currencies_ts()
    common = re.findall(r'\["([A-Z]{3})",\s*"[^"]+"\]', src)
    assert common, "COMMON_CURRENCIES not found, or its shape changed"
    missing = sorted(set(common) - set(ISO_4217_CURRENCIES))
    assert not missing, f"common currencies the backend rejects: {missing}"


def test_the_parser_would_notice_an_empty_array():
    """⚠ Control on this file's own instrument.

    If the regex stopped matching, `_parse_array` would return `[]` and the
    equality test above would report all 170 as "accepted but hidden" — loud.
    But a subtler break returning a partial list would look like a small,
    plausible diff. Pin the size so the instrument itself is fenced.
    """
    ts = _parse_array(_currencies_ts(), "ALL_CURRENCIES")
    assert len(ts) == len(ISO_4217_CURRENCIES) == 170, (
        f"frontend={len(ts)}, backend={len(ISO_4217_CURRENCIES)}. If a currency "
        "was deliberately added or removed, update the expected count here in "
        "the same commit."
    )
