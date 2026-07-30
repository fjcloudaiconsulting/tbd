"""Regenerate the TBD-242 period-status cross-language contract fixture.

    cd backend && python -m scripts.gen_period_status_vectors

⚠ **That is the only invocation that works, and it must run on the HOST.**
A script PATH puts ``backend/scripts`` on ``sys.path[0]`` and does not add the
CWD, so both ``python backend/scripts/gen_period_status_vectors.py`` and
``cd backend && python scripts/gen_period_status_vectors.py`` raise
``ModuleNotFoundError: No module named 'app'``. And inside the backend
container the fixture directory is mounted read-only, by design — the mount
exists so the contract test can READ the fixture, not so a container can
rewrite a repo file.

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

def _grid() -> list[tuple[str, str | None]]:
    """A DENSE sweep around `today`, not a hand-picked sample.

    The hand-picked list below pins 14 enumerated inputs; a Python-side
    predicate change affecting any triple outside them is invisible to both
    suites. The grid turns "14 enumerated inputs" into several hundred at zero
    maintenance cost: every ordered pair of offsets in a window around `today`,
    INCLUDING inverted pairs (`end < start`), plus far-past/far-future anchors
    so the unbounded branches stay covered.
    """
    offsets = [-400, -3, -2, -1, 0, 1, 2, 3, 400]
    out: list[tuple[str, str | None]] = []
    for so in offsets:
        start = (TODAY + datetime.timedelta(days=so)).isoformat()
        out.append((start, None))  # branch 2 across the whole window
        for eo in offsets:
            out.append((start, (TODAY + datetime.timedelta(days=eo)).isoformat()))
    return out


#: (start_date, end_date). Ordered by the branch each is meant to exercise.
#: These are the NAMED cases; `_grid()` is appended to them. Do not delete a
#: named case to silence a failure — two of them are structurally asserted by
#: `test_vectors_discriminate_branch_order`.
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


def _find_repo_root(start: pathlib.Path) -> pathlib.Path:
    """Walk upward from `start` until a directory holding both
    `.github/workflows/deploy.yml` and `.do/app.yaml` is found.

    Same marker walk as `tests/test_deploy_workflow.py`, and here for the same
    reason: `parents[2]` is correct only from a host checkout and resolves to
    `/` when this file sits at `/app/scripts/…` inside the backend container.
    """
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "deploy.yml").exists() and (
            candidate / ".do" / "app.yaml"
        ).exists():
            return candidate
    raise RuntimeError(
        "Could not locate repo root containing .github/workflows/deploy.yml "
        "and .do/app.yaml. Run this generator from a checked-out repo."
    )


OUT = (
    _find_repo_root(pathlib.Path(__file__).resolve())
    / "frontend"
    / "tests"
    / "fixtures"
    / "period-status-vectors.json"
)


def build() -> dict:
    return {
        "_comment": (
            "TBD-242 cross-language contract. GENERATED from backend "
            "period_status; do not hand-edit. Regenerate on the host with: "
            "cd backend && python -m scripts.gen_period_status_vectors"
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
            for i, (start, end) in enumerate(CASES + _grid(), start=1)
        ],
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {len(build()['vectors'])} vectors to {OUT}")
