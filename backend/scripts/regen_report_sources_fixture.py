"""Regenerate the report-source catalog fixture the frontend tests format against.

TBD-381. The frontend derives every widget's number format, and now every filter
control's visibility, from `GET /api/v1/reports/sources`. Its tests therefore
need a catalog fixture -- and a HAND-WRITTEN one is the stale-fixture failure
mode this repo already tracks. The first version of that fixture disagreed with
production in 16 places, one of them a live right/wrong split: `sum(credit_limit)`
resolves to `currency` against the real catalog and to `number` against the
fixture, so a credit-limit KPI rendered EUR 45,000.00 in production and 45000
under test. The tests agreed with themselves and disagreed with the app.

Same shape as the two fixtures that already solve this:
  * scripts/gen_period_status_vectors.py -> period-status-vectors.json
  * scripts/regen_feature_catalog_fixture.py -> feature-catalog.json

Run it ON THE HOST, as a module, from `backend/`:

    cd backend && python -m scripts.regen_report_sources_fixture

⚠ A script path (`python scripts/regen_...py`) puts `backend/scripts` on
sys.path and fails to import `app` -- the same trap the period-status generator
documents.

The committed JSON is asserted, never regenerated, by
`backend/tests/test_report_sources_frontend_contract.py`. If that test is red,
a source changed: re-run this, read the diff, and make sure the frontend still
formats correctly -- do not regenerate blindly.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import sys

from app.reports.sources import all_sources

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend"
    / "tests"
    / "fixtures"
    / "report-sources.json"
)


def build_catalog() -> list[dict]:
    """Serialize the catalog exactly as `GET /reports/sources` does.

    Mirrors `routers/reports.py`'s `dataclasses.asdict` over the same three
    accessor METHODS, so the fixture cannot drift from the wire shape without
    the router drifting too.
    """
    return [
        {
            "key": s.key,
            "label": s.label,
            "dimensions": [dataclasses.asdict(d) for d in s.dimensions()],
            "measures": [dataclasses.asdict(m) for m in s.measures()],
            "filters": [
                {**dataclasses.asdict(f), "ops": list(f.ops)} for f in s.filters()
            ],
        }
        for s in all_sources()
    ]


def main() -> None:
    payload = json.dumps(build_catalog(), indent=2) + "\n"

    # `--stdout` exists because the app's dependencies live in the backend
    # CONTAINER while the fixture lives on the HOST side of a read-only mount
    # (`./frontend/tests/fixtures:/app/frontend/tests/fixtures:ro`). So the
    # normal invocation is:
    #
    #   docker compose exec -T backend python -m scripts.regen_report_sources_fixture --stdout \
    #     > frontend/tests/fixtures/report-sources.json
    #
    # Writing directly works only where `app` is importable AND the fixture
    # path is writable, i.e. a host with the backend deps installed.
    if "--stdout" in sys.argv:
        sys.stdout.write(payload)
        return

    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(payload, encoding="utf-8")
    print(f"wrote {FIXTURE}")


if __name__ == "__main__":
    main()
