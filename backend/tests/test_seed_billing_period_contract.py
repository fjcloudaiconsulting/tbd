"""AST-level regression: ``seed.py`` must call ``POST /billing-period``
with a JSON body, not query params.

TBD-232 hardened ``POST /api/v1/settings/billing-period`` from an
unvalidated query parameter (``start_date: datetime.date = None`` in
front of a NOT NULL column, which 500ed at commit) into a Pydantic body.
``backend/seed.py`` holds the **only** callers of that endpoint anywhere
in the tree, and both used ``params={...}``.

The drift is silent. ``seed.py`` guards the first call with
``if r.status_code == 200:`` and prints nothing on failure; the second
checked nothing at all; and ``seed`` appears in no ``.github/workflows/``
file. A missed update yields a demo org with **zero billing periods** and
a cheerful "Seed complete!". The runtime mitigation is
``raise_for_status()`` on both calls; this guard is the source-level half,
so the next drift fails the suite instead of a developer's local demo.

``seed.py`` is one monolithic ``async def main()`` against a hardcoded
``BASE = "http://localhost:8000"`` using a real ``httpx.AsyncClient``,
with the period calls behind ~40 prior network calls — it cannot be
driven in-process, which is why this is a source guard rather than an
integration test.

Modelled on ``tests/test_no_raw_request_client.py``, the established
house pattern for this class of check.
"""
from __future__ import annotations

import ast
from pathlib import Path


SEED_PY = Path(__file__).resolve().parents[1] / "seed.py"

BILLING_PERIOD_PATH = "/api/v1/settings/billing-period"

# Both calls today: the closed-period loop and the current open period.
EXPECTED_CALL_COUNT = 2


def _billing_period_posts() -> list[ast.Call]:
    """Every ``*.post("/api/v1/settings/billing-period", ...)`` call node.

    Matched on the literal first positional argument, so the sibling
    ``/api/v1/settings/billing-period/close`` and
    ``/api/v1/settings/billing-periods/ensure-future`` paths do not
    accidentally count.
    """
    tree = ast.parse(SEED_PY.read_text(encoding="utf-8"), filename=str(SEED_PY))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "post"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == BILLING_PERIOD_PATH:
            calls.append(node)
    return calls


def test_seed_posts_billing_periods_as_json_body():
    calls = _billing_period_posts()
    assert len(calls) == EXPECTED_CALL_COUNT, (
        f"Expected {EXPECTED_CALL_COUNT} POST {BILLING_PERIOD_PATH} call(s) in "
        f"seed.py, found {len(calls)}. If seed.py legitimately gained or lost "
        "one, update EXPECTED_CALL_COUNT — do not delete this guard."
    )

    for call in calls:
        kwargs = {kw.arg for kw in call.keywords if kw.arg}
        assert "json" in kwargs, (
            f"seed.py line {call.lineno}: POST {BILLING_PERIOD_PATH} must pass "
            "the period as `json=` — the endpoint takes a Pydantic body "
            "(schemas.settings.BillingPeriodCreate) and returns 422 for query "
            "params, which seed.py would swallow silently."
        )
        assert "params" not in kwargs, (
            f"seed.py line {call.lineno}: POST {BILLING_PERIOD_PATH} still "
            "passes `params=`. That is the pre-TBD-232 query-param contract; "
            "it now yields a 422 and seeds an org with zero billing periods."
        )


def test_seed_raises_for_status_on_billing_period_posts():
    """Both calls must be followed by a ``raise_for_status()`` so the next
    contract drift is loud rather than a demo org with no periods."""
    source = SEED_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SEED_PY))
    raise_lines = {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "raise_for_status"
    }

    for call in _billing_period_posts():
        # The call spans call.lineno..call.end_lineno; the guard should sit
        # within a couple of lines of its end.
        end = call.end_lineno or call.lineno
        assert any(end < line <= end + 3 for line in raise_lines), (
            f"seed.py line {call.lineno}: POST {BILLING_PERIOD_PATH} has no "
            "`raise_for_status()` immediately after it. Without it a 4xx is "
            "swallowed and `./pfv seed` prints 'Seed complete!' over a demo "
            "org with no billing periods."
        )
