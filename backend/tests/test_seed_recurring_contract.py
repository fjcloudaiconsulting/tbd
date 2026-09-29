"""TBD-573: seed step 6 must not report recurring templates it never created.

``POST /api/v1/recurring`` refuses a ``next_due_date`` before the start of the
CURRENT billing cycle, which the server derives from the wall clock
(``recurring_service.validate_frontier``). The seed derives ``next_due_date``
from the anchor, so with a pinned past anchor every template is refused. The
seed used to ignore the response and print ``Created 5`` over zero rows.

``seed.recurring_outcome`` now classifies each response: created, skipped
(that one refusal, which a past anchor cannot avoid without reading the clock
``main`` is fenced against), or raise (anything else is contract drift).
"""
from __future__ import annotations

import ast
import datetime
from pathlib import Path

import httpx
import pytest

import seed
from app.services import recurring_service
from app.services.exceptions import ValidationError

SEED_PY = Path(__file__).resolve().parents[1] / "seed.py"


def _resp(status: int, detail: str | None = None) -> httpx.Response:
    req = httpx.Request("POST", "http://seed.test/api/v1/recurring")
    body = {"detail": detail} if detail is not None else {"id": 1}
    return httpx.Response(status, json=body, request=req)


class _NoOrgDb:
    async def scalar(self, _stmt):
        return None  # frontier_lower_bound falls back to cycle day 1


async def _real_frontier_message() -> str:
    with pytest.raises(ValidationError) as exc:
        await recurring_service.validate_frontier(
            _NoOrgDb(), 1, datetime.date(2026, 4, 1), today=datetime.date(2026, 9, 29),
        )
    return str(exc.value)


# fence — kills: a matcher that drifted from the server's wording, which would
# turn every past-anchor seed into a crash (or, if broadened, a silent skip).
# The message is produced by the real service function, not copied here.
@pytest.mark.asyncio
async def test_the_real_frontier_refusal_is_classified_as_skipped():
    assert seed.recurring_outcome(_resp(400, await _real_frontier_message())) == "skipped"


def test_success_is_created():
    assert seed.recurring_outcome(_resp(201)) == "created"


# fence — kills: skipping every 400 (or every error), which hides contract
# drift exactly like the swallowed 422 billing_period_outcome guards against.
@pytest.mark.parametrize("status,detail", [
    (400, "Category must be an expense category"),
    (422, "field required"),
    (404, "Account not found"),
    (500, "boom"),
])
def test_any_other_failure_raises(status, detail):
    with pytest.raises(httpx.HTTPStatusError):
        seed.recurring_outcome(_resp(status, detail))


def _main_fn() -> ast.AsyncFunctionDef:
    tree = ast.parse(SEED_PY.read_text(encoding="utf-8"))
    return next(
        fn for fn in tree.body if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "main"
    )


# fence — the on-switch. Kills: the helper existing but the recurring POST
# still discarding its response.
def test_main_classifies_every_recurring_post():
    main_fn = _main_fn()
    posts = [
        n for n in ast.walk(main_fn)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "post"
        and n.args and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "/api/v1/recurring"
    ]
    assert len(posts) == 1
    # The name the POST's response is bound to, then: is THAT name classified?
    # Kills recurring_outcome(<some other response>) as well as no call at all.
    bound = {
        t.id for n in ast.walk(main_fn) if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Await) and n.value.value is posts[0]
        for t in n.targets if isinstance(t, ast.Name)
    }
    assert bound, "the recurring POST's response is not bound to a name"
    classified = [
        n for n in ast.walk(main_fn)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "recurring_outcome"
        and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id in bound
    ]
    assert classified, "main() posts recurring templates without classifying that response"


# fence — kills: the old `Created {len(rec_defs)}` line, and any other count of
# what was ATTEMPTED (len(rec_outcomes), a loop counter) printed as created.
def test_main_reports_only_created_templates_as_created():
    lines = [
        n for n in ast.walk(_main_fn())
        if isinstance(n, ast.JoinedStr) and "recurring templates" in ast.unparse(n)
    ]
    assert len(lines) == 1, [ast.unparse(n) for n in lines]
    counted = [v.value for v in lines[0].values if isinstance(v, ast.FormattedValue)]
    assert len(counted) == 1 and _is_count_of_created(counted[0]), (
        f"line {lines[0].lineno}: the printed count must be <outcomes>.count('created')"
    )


def _is_count_of_created(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "count"
        and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "created"
    )
