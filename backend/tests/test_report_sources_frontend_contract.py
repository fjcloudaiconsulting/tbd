"""The frontend's report-source fixture must match the real catalog.

TBD-381. The frontend now derives every widget's number format AND every filter
control's visibility from `GET /api/v1/reports/sources`. Its tests need a
catalog to render against, and the first version of that fixture was written by
hand -- which put it a source-edit away from certifying the wrong behaviour.

⚠ This is not hypothetical. The hand-written fixture disagreed with production
in sixteen places, and one was a live right/wrong split: it omitted
`credit_utilization`'s `sum(credit_limit)` measure entirely, so that KPI
resolved to `currency` in the app and to `number` under test -- EUR 45,000.00 on
screen, 45000 in the assertion. It also omitted the whole `accounts` source,
which would have made any future `accounts` filter test STRUCTURALLY vacuous:
an unknown source hits `sourceSupportsField`'s deliberate "unknown -> allow
everything" branch, so such a test asserts every control is present and passes
no matter what the code does.

⚠ FAIL, DO NOT REGENERATE. If this test is red a source changed. Re-run the
generator, read the diff, and confirm the frontend still formats and gates
correctly -- then commit the new fixture. Auto-regenerating in CI would restore
exactly the silent drift this exists to stop.

    docker compose exec -T backend python -m scripts.regen_report_sources_fixture --stdout \\
      > frontend/tests/fixtures/report-sources.json

Modelled on the two fixtures that already work this way:
`test_period_status_frontend_contract.py` and
`test_feature_catalog_frontend_contract.py`.

⚠ The fixture lives on the frontend side and reaches this container through the
read-only mount in `docker-compose.yml`. A backend image built before that mount
existed shows this test red; `docker compose up -d --force-recreate backend`
picks it up.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from scripts.regen_report_sources_fixture import build_catalog

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "frontend"
    / "tests"
    / "fixtures"
    / "report-sources.json"
)


def _load() -> list[dict]:
    if not FIXTURE.exists():  # pragma: no cover - environment problem, not a defect
        pytest.skip(
            f"{FIXTURE} not reachable. On a bare checkout it is at "
            "frontend/tests/fixtures/; in the container it arrives via the "
            "read-only mount in docker-compose.yml."
        )
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_matches_the_live_catalog():
    """Byte-for-byte on the structure the router serializes."""
    assert _load() == build_catalog(), (
        "frontend/tests/fixtures/report-sources.json is stale. A report source "
        "changed its measures, filters or dimensions. Re-run "
        "`python -m scripts.regen_report_sources_fixture --stdout`, READ THE "
        "DIFF, and confirm the frontend still formats and gates correctly "
        "before committing. Do not regenerate blindly."
    )


def test_every_source_is_in_the_fixture():
    """A missing source is worse than a wrong one: it is silently permissive.

    `sourceSupportsField` returns "allow everything" for an unknown dataset --
    a deliberate, load-bearing bias so a cold cache cannot strip every filter.
    The cost is that a frontend test naming a source the fixture omits asserts
    nothing at all while passing.
    """
    fixture_keys = {s["key"] for s in _load()}
    live_keys = {s["key"] for s in build_catalog()}
    assert fixture_keys == live_keys, (
        f"sources missing from the fixture: {sorted(live_keys - fixture_keys)}; "
        f"unknown to the backend: {sorted(fixture_keys - live_keys)}"
    )


def test_a_field_never_carries_two_formats_within_one_source():
    """The precondition for the frontend resolver's field-only backstop.

    `lib/reports/widget-format.ts` resolves an exact (agg, field) pair first and
    falls back to a field-only match. That fallback is documented as "a backstop,
    not a guess" ON THE GROUNDS that no field maps to two formats -- a claim
    verified once by hand and, until now, guarded by nothing. If a source ever
    publishes `sum(x) -> currency` and `avg(x) -> percent`, the backstop starts
    silently picking whichever row comes first.
    """
    for source in build_catalog():
        by_field: dict[str, set[str]] = {}
        for measure in source["measures"]:
            by_field.setdefault(measure["field"], set()).add(measure["format"])
        conflicts = {f: fmts for f, fmts in by_field.items() if len(fmts) > 1}
        assert not conflicts, (
            f"source {source['key']!r} publishes a field under two formats: "
            f"{conflicts}. The frontend's field-only fallback cannot resolve "
            "that unambiguously -- either make the formats agree, or delete the "
            "fallback and require an exact (agg, field) match."
        )
