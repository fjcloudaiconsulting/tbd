"""TBD-242 — the Python side of the period-status cross-language contract.

`frontend/lib/billingPeriodStatus.ts` carries a TypeScript port of
:func:`app.services.billing_service.period_status`. Two implementations of one
normatively-ordered five-branch partition can drift, and the kernel docstring
says exactly why that is dangerous: revision 1's unordered predicates were
non-disjoint, and "two implementers writing the `if/elif` in different orders
would both have satisfied it while producing different canonical answers."

This test and its vitest twin (`frontend/tests/lib/period-status-contract.test.ts`)
read ONE shared fixture. If the Python classifier changes, this test goes red
here, in the backend suite, immediately.

⚠ **This test FAILS; it does not regenerate.** That distinction is the whole
point. The repo's only other cross-language sync mechanism
(`scripts/regen_feature_catalog_fixture.py`) is explicitly manual and is
pinned by no backend test, so a Python-side change can drift past it silently
while the TypeScript guard stays green. Do not "fix" a failure here by making
the test rewrite the fixture — regenerate deliberately, and review the diff,
because a changed vector means the wire meaning of a status changed.

⚠ **The vector set must keep its branch-order-discriminating cases.** Without
an inverted row whose `start_date > today` (branch 1 vs branch 3) and an open
row whose `start_date > today` (branch 2 vs branch 3), a reordered port still
passes every vector and the guard is vacuous — the defect class this
programme has already produced six times. Both are asserted explicitly below.
"""
from __future__ import annotations

import datetime
import json
import pathlib
from typing import get_args

import pytest

from app.services.billing_service import PeriodStatus, RosterRow, period_status

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend"
    / "tests"
    / "fixtures"
    / "period-status-vectors.json"
)

REGEN = "python backend/scripts/gen_period_status_vectors.py"


def _load() -> dict:
    if not FIXTURE.exists():  # pragma: no cover - guards a moved fixture
        pytest.fail(f"contract fixture missing at {FIXTURE}; regenerate with: {REGEN}")
    return json.loads(FIXTURE.read_text())


def test_every_vector_matches_the_python_classifier() -> None:
    data = _load()
    today = datetime.date.fromisoformat(data["today"])

    mismatches = []
    for i, vec in enumerate(data["vectors"]):
        end = vec["end_date"]
        row = RosterRow(
            id=i + 1,
            start_date=datetime.date.fromisoformat(vec["start_date"]),
            end_date=datetime.date.fromisoformat(end) if end else None,
        )
        actual = period_status(row, today=today)
        if actual != vec["status"]:
            mismatches.append(
                f"  ({vec['start_date']}, {end}) -> fixture {vec['status']!r}, "
                f"python {actual!r}"
            )

    assert not mismatches, (
        "period_status no longer agrees with the frontend contract fixture:\n"
        + "\n".join(mismatches)
        + f"\n\nIf this change is intended, regenerate with: {REGEN}\n"
        "and update frontend/lib/billingPeriodStatus.ts to match."
    )


def test_fixture_status_vocabulary_is_exhaustive() -> None:
    """Catches a SIXTH branch being added to the Literal without a vector."""
    data = _load()
    assert sorted(data["statuses"]) == sorted(get_args(PeriodStatus)), (
        "PeriodStatus members changed; the frontend union in "
        "frontend/lib/billingPeriodStatus.ts must change too. Regenerate with: "
        f"{REGEN}"
    )


def test_vectors_discriminate_branch_order() -> None:
    """Without these the guard is vacuous against a reordered port."""
    data = _load()
    today = data["today"]
    vectors = data["vectors"]

    inverted_future = [
        v
        for v in vectors
        if v["end_date"] is not None
        and v["end_date"] < v["start_date"]
        and v["start_date"] > today
    ]
    assert inverted_future, (
        "no vector with an INVERTED row starting after today — branch 1 "
        "(`invalid`) could be reordered below branch 3 (`upcoming`) undetected"
    )
    assert all(v["status"] == "invalid" for v in inverted_future)

    open_future = [
        v for v in vectors if v["end_date"] is None and v["start_date"] > today
    ]
    assert open_future, (
        "no vector with an OPEN row starting after today — branch 2 (`open`) "
        "could be reordered below branch 3 (`upcoming`) undetected"
    )
    assert all(v["status"] == "open" for v in open_future)


def test_fixture_is_reproducible_from_the_generator() -> None:
    """The committed fixture must BE the generator's output, byte for byte.

    ⚠ This exists because it once was not. The fixture shipped claiming
    "GENERATED ... do not hand-edit" while carrying 14 vectors to the
    generator's 13, in a different order and a layout ``json.dumps`` cannot
    produce. Every value happened to be correct, so nothing was red — and
    anyone running the documented regenerate command to fix a failure would
    have silently DROPPED a vector.

    A provenance banner that no test enforces is decoration. This is the test
    that makes it true.
    """
    from scripts.gen_period_status_vectors import build

    assert _load() == build(), (
        "the committed fixture is not the generator's output. Do not hand-edit "
        f"it — run: {REGEN}"
    )


def test_every_status_is_exercised_by_at_least_one_vector() -> None:
    data = _load()
    covered = {v["status"] for v in data["vectors"]}
    missing = set(get_args(PeriodStatus)) - covered
    assert not missing, f"no vector exercises: {sorted(missing)}. Regenerate: {REGEN}"
