"""Regenerate the TBD-242 period-status cross-language contract fixture.

    python backend/scripts/gen_period_status_vectors.py

EXPLICIT and MANUAL, never invoked from a test — same convention as
``regen_feature_catalog_fixture.py``. Unlike that one, the fixture this writes
IS pinned by a backend test (``tests/test_period_status_frontend_contract.py``),
so forgetting to run this turns the backend suite red instead of letting the
Python and TypeScript classifiers drift apart in silence.

Regenerating is a deliberate act: a changed vector means the wire meaning of a
status changed, and ``frontend/lib/billingPeriodStatus.ts`` must change with it.
Review the diff.

⚠ **Do not drop a case to make a failure go away.** Two cases are load-bearing
and asserted structurally by the contract test: an inverted row starting after
today (branch 1 vs 3) and an open row starting after today (branch 2 vs 3).
Without them a port with reordered branches passes every remaining vector.
"""
from __future__ import annotations

import datetime
import json
import pathlib
from typing import get_args

from app.services.billing_service import PeriodStatus, RosterRow, period_status

#: Frozen so the fixture is reproducible. Chosen to sit strictly inside
#: 2026-07-01..2026-07-31 so containment cases have room on both sides.
TODAY = datetime.date(2026, 7, 29)

#: (start_date, end_date). Ordered by the branch each is meant to exercise.
CASES: list[tuple[str, str | None]] = [
    # branch 2 — open
    ("2026-07-01", None),
    ("2020-01-01", None),  # LAPSED open row: still `open`, never `past`
    ("2999-01-01", None),  # open AND future -> discriminates branch 2 vs 3
    ("2026-07-30", None),  # open, starting TOMORROW -> branch 2 vs 3, tight
    # branch 1 — invalid
    ("2026-07-20", "2026-07-10"),
    ("2999-01-01", "1999-01-01"),  # inverted AND future -> branch 1 vs 3
    ("2026-07-30", "2026-07-29"),
    # branch 3 — upcoming
    ("2026-07-30", "2026-08-29"),
    # branch 4 — current_by_calendar (both bounds inclusive)
    ("2026-07-01", "2026-07-31"),
    ("2026-07-29", "2026-07-29"),
    ("2026-07-29", "2026-08-28"),  # starts exactly today
    ("2026-06-29", "2026-07-29"),  # ends exactly today
    # branch 5 — past
    ("2026-06-01", "2026-06-30"),
    ("2026-06-01", "2026-07-28"),  # ended yesterday
]

OUT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend"
    / "tests"
    / "fixtures"
    / "period-status-vectors.json"
)


def build() -> dict:
    return {
        "_comment": (
            "TBD-242 cross-language contract. GENERATED from backend "
            "period_status; do not hand-edit. Regenerate: python "
            "backend/scripts/gen_period_status_vectors.py"
        ),
        "today": TODAY.isoformat(),
        "statuses": sorted(get_args(PeriodStatus)),
        "vectors": [
            {
                "start_date": start,
                "end_date": end,
                "status": period_status(
                    RosterRow(
                        id=i,
                        start_date=datetime.date.fromisoformat(start),
                        end_date=datetime.date.fromisoformat(end) if end else None,
                    ),
                    today=TODAY,
                ),
            }
            for i, (start, end) in enumerate(CASES, start=1)
        ],
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {len(build()['vectors'])} vectors to {OUT}")
