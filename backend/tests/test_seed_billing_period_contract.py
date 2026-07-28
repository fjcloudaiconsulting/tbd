"""AST-level regression: ``seed.py`` must call ``POST /billing-period``
with a JSON body, not query params.

TBD-232 hardened ``POST /api/v1/settings/billing-period`` from an
unvalidated query parameter (``start_date: datetime.date = None`` in
front of a NOT NULL column, which 500ed at commit) into a Pydantic body.
``backend/seed.py`` holds the **only** callers of that endpoint anywhere
in the tree, and both used ``params={...}``.

The drift is silent. ``seed.py`` guarded the first call with
``if r.status_code == 200:`` and printed nothing on failure; the second
checked nothing at all; and ``seed`` appears in no ``.github/workflows/``
file. A missed update yields a demo org with **zero billing periods** and
a cheerful "Seed complete!". The runtime mitigation is that both calls
route their response through ``seed.billing_period_outcome``, which ends
in ``raise_for_status()``; this guard is the source-level half, so the
next drift fails the suite instead of a developer's local demo.

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

import httpx
import pytest

import seed


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


def _raise_for_status_calls(node: ast.AST) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "raise_for_status"
    ]


def _status_check_lines(tree: ast.Module) -> set[int]:
    """Line numbers of calls that make a non-2xx loud.

    Two shapes count:

    * a bare ``r.raise_for_status()``;
    * a call to a module-level ``seed.py`` helper whose own body calls
      ``raise_for_status()``.

    The second shape exists because a bare ``raise_for_status()`` is now
    wrong here. TBD-232 gave ``POST /billing-period`` a duplicate-start
    pre-flight that answers 409 ``billing_period_exists``, and ``seed.py``
    is documented as a repeatable dataset whose period dates are
    deterministic for a given ``today`` — so a bare ``raise_for_status()``
    aborts every same-day re-run before recurring, budgets, forecast plans
    and reports are seeded. The calls must tolerate that one status and
    stay loud on everything else, which means delegating to a helper.

    Requiring the helper to itself contain a ``raise_for_status()`` is what
    keeps this guard honest: a helper that merely logs and continues does
    not satisfy it.
    """
    tolerant_helpers = {
        fn.name
        for fn in tree.body
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _raise_for_status_calls(fn)
    }

    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "raise_for_status":
            lines.add(node.lineno)
        elif isinstance(func, ast.Name) and func.id in tolerant_helpers:
            lines.add(node.lineno)
    return lines


def test_seed_checks_status_on_billing_period_posts():
    """Both calls must hand their response to something that raises on an
    unexpected status, so the next contract drift is loud rather than a demo
    org with no periods."""
    source = SEED_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SEED_PY))
    check_lines = _status_check_lines(tree)

    for call in _billing_period_posts():
        # The call spans call.lineno..call.end_lineno; the guard should sit
        # within a couple of lines of its end.
        end = call.end_lineno or call.lineno
        assert any(end < line <= end + 3 for line in check_lines), (
            f"seed.py line {call.lineno}: POST {BILLING_PERIOD_PATH} does not "
            "hand its response to `raise_for_status()` or to a seed.py helper "
            "that ends in one. Without that a 4xx is swallowed and "
            "`./pfv seed` prints 'Seed complete!' over a demo org with no "
            "billing periods."
        )


def test_seed_tolerates_duplicate_billing_period_conflict():
    """The status check must not be a bare ``raise_for_status()``.

    ``./pfv seed`` is re-runnable by contract and its period start dates are
    deterministic, so a same-day second run gets 409 ``billing_period_exists``
    on every POST. A bare ``raise_for_status()`` turns that into an
    ``httpx.HTTPStatusError`` out of ``main()`` and steps 6+ (recurring,
    budgets, forecast plans, reports) never run.
    """
    source = SEED_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SEED_PY))

    tolerant = [
        fn
        for fn in tree.body
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _raise_for_status_calls(fn)
        and "billing_period_exists" in ast.get_source_segment(source, fn)
    ]
    assert tolerant, (
        "seed.py has no helper that both calls `raise_for_status()` and names "
        "the `billing_period_exists` conflict code. Without one, a repeat "
        "`./pfv seed` on the same day aborts at step 5."
    )

    helper_names = {fn.name for fn in tolerant}
    for call in _billing_period_posts():
        end = call.end_lineno or call.lineno
        used = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in helper_names
            and end < node.lineno <= end + 3
        }
        assert used, (
            f"seed.py line {call.lineno}: POST {BILLING_PERIOD_PATH} does not "
            f"route its response through one of {sorted(helper_names)}, so a "
            "duplicate start date on a re-run aborts the whole seed."
        )


# ── seed.billing_period_outcome (TBD-239 §3) ─────────────────────────────
#
# TBD-239 gave POST /billing-period a second conflict code,
# `billing_period_overlap`. The helper is called directly here rather than
# through the AST, because "absorbs the code" is a runtime property.


def _response(status: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload if payload is not None else {},
        request=httpx.Request("POST", f"http://localhost:8000{BILLING_PERIOD_PATH}"),
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("billing_period_exists", "exists"),
        ("billing_period_overlap", "overlaps"),
    ],
)
def test_billing_period_outcome_absorbs_both_conflict_codes(code, expected):
    """`./pfv seed` is re-runnable by contract.

    Seed's dates are deterministic for a given `today`, but `today` moves:
    a re-run on a later day shifts the whole window, so a period that was
    posted yesterday is now *contained* by the one being posted today. That
    answers 409 `billing_period_overlap`, not `billing_period_exists`, and
    a helper that absorbs only the latter aborts the seed at step 5.
    """
    assert (
        seed.billing_period_outcome(
            _response(409, {"detail": "...", "code": code})
        )
        == expected
    )


def test_billing_period_outcome_still_raises_on_other_statuses():
    """The contract-drift guard has to survive the widening: a 422 from a
    changed request shape must not be swallowed into a cheerful
    "Seed complete!" over an org with no billing periods."""
    with pytest.raises(httpx.HTTPStatusError):
        seed.billing_period_outcome(_response(422, {"detail": "nope"}))
    with pytest.raises(httpx.HTTPStatusError):
        seed.billing_period_outcome(_response(409, {"code": "something_else"}))


def test_billing_period_outcome_reports_creation_on_2xx():
    assert seed.billing_period_outcome(_response(200, {"id": 1})) == "created"
