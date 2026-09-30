"""TBD-580: ``transactions_set_category``.

Driven through the real ``registry.invoke`` / ``confirm_action`` on the
file-backed SQLite world of ``test_actions`` (org A: one settled, reportable
expense "SPOTIFY PREMIUM" in category ``c1``; ``c2`` is a second expense
category). F-A3 and F-A4 for this tool run in ``test_actions`` with every
other write tool.

Fences: F-P6 F-P10. Every fence names the wrong implementation it kills.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy import func, select, update

from app.agent import registry
from app.agent.registry import ToolContext, ToolError
from app.models import Category
from app.models.category import CategoryType
from app.models.category_rule import CategoryRule, RuleSource
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.routers import agent as agent_router
from app.services.category_rules_service import normalize_description
from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    AUTO, P_START, _confirm, _invoke, _refused, _row, engine, factory, w,
)

TOOL = "transactions_set_category"
TOKEN = normalize_description("SPOTIFY PREMIUM")


async def _stage(f, a, *, tx=None, cat=None, **kw):
    return await _invoke(f, a["member"], TOOL,
                         {"transaction_id": tx or a["tx"], "category_id": cat or a["c2"]}, **kw)


async def _tx(f, tx_id) -> Transaction:
    async with f() as db:
        return await db.get(Transaction, tx_id)


async def _set(f, model, row_id, **values):
    async with f() as db:
        await db.execute(update(model).where(model.id == row_id).values(**values))
        await db.commit()


async def _rules(f) -> list[tuple[str, int, int]]:
    async with f() as db:
        rows = (await db.execute(select(
            CategoryRule.normalized_token, CategoryRule.category_id, CategoryRule.match_count,
        ))).all()
        return [tuple(r) for r in rows]


async def _add(f, *objs):
    async with f() as db:
        db.add_all(objs)
        await db.commit()
        return [o.id for o in objs]


def _sibling(a, **kw) -> Transaction:
    return Transaction(
        org_id=a["org"], account_id=a["acct"], category_id=a["c1"], description="RENT",
        amount=Decimal("500.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.PENDING, date=P_START + datetime.timedelta(days=1), **kw,
    )


async def _make_series(f, a) -> tuple[int, int]:
    """Put ``a["tx"]`` in a recurring series with a pending sibling."""
    [tpl] = await _add(f, RecurringTransaction(
        org_id=a["org"], account_id=a["acct"], category_id=a["c1"], description="SPOTIFY PREMIUM",
        amount=Decimal("9.99"), type="expense", frequency=Frequency.MONTHLY,
        next_due_date=P_START + datetime.timedelta(days=40),
    ))
    [sib] = await _add(f, _sibling(a, recurring_id=tpl))
    await _set(f, Transaction, a["tx"], recurring_id=tpl)
    return tpl, sib


async def _make_pair(f, a) -> int:
    """Link ``a["tx"]`` with a partner both ways (a transfer pair)."""
    [partner] = await _add(f, _sibling(a, linked_transaction_id=a["tx"]))
    await _set(f, Transaction, a["tx"], linked_transaction_id=partner)
    return partner


# ── F-P6: the rule disclosure ─────────────────────────────────────────────

async def test_fp6_preview_lists_the_rule_for_a_reportable_row_and_writes_nothing(factory, w):
    """FENCE F-P6 (disclosure). Wrong implementation: a preview listing only
    the transaction change while confirm also upserts the org's rule."""
    a = w["A"]
    await _add(factory, CategoryRule(
        org_id=a["org"], normalized_token=TOKEN, raw_description_seen="SPOTIFY PREMIUM",
        category_id=a["c1"], match_count=3, source=RuleSource("user_edit"),
    ))
    out = await _stage(factory, a)
    assert out["requires_confirmation"] is True
    assert out["changes"] == [
        {"entity": "transactions", "id": a["tx"], "field": "category_id",
         "before": a["c1"], "after": a["c2"], "currency": None},
        {"entity": "category_rules", "id": TOKEN, "field": "category_id",
         "before": a["c1"], "after": a["c2"], "currency": None},
    ]
    assert out["warnings"], "the rule write is called out"
    assert out["context"]["description"] == {"untrusted": "SPOTIFY PREMIUM"}
    assert (await _tx(factory, a["tx"])).category_id == a["c1"]
    assert await _rules(factory) == [(TOKEN, a["c1"], 3)]


async def test_fp6_confirm_learns_the_disclosed_rule(factory, w):
    """GUARD. The confirm path runs ``update_transaction`` with learning on."""
    a = w["A"]
    out = await _confirm(factory, a["member"], (await _stage(factory, a))["action_id"])
    assert out["status"] == "done"
    assert out["result"]["category_id"] == a["c2"]
    assert out["result"]["rule_learned"] is True
    assert await _rules(factory) == [(TOKEN, a["c2"], 1)]


async def test_fp6_non_reportable_row_omits_the_rule_and_learns_none(factory, w):
    """FENCE F-P6 (gate). Wrong implementation: disclosing a rule for a row
    ``update_transaction`` will not learn from (a skipped import row), so the
    preview lists a write that never happens."""
    a = w["A"]
    await _set(factory, Transaction, a["tx"], reconciliation_state="skipped")
    out = await _stage(factory, a)
    assert [c["entity"] for c in out["changes"]] == ["transactions"]
    assert not out["warnings"]
    done = await _confirm(factory, a["member"], out["action_id"])
    assert done["result"]["rule_learned"] is False
    assert await _rules(factory) == []


async def test_fp6_rule_drift_alone_is_stale(factory, w):
    """FENCE F-P6 (fingerprint). Wrong implementation: a fingerprint over
    the transaction change only; the rule it would overwrite changed."""
    a = w["A"]
    out = await _stage(factory, a)
    await _add(factory, CategoryRule(
        org_id=a["org"], normalized_token=TOKEN, raw_description_seen="x",
        category_id=a["c1"], match_count=1, source=RuleSource("user_edit"),
    ))
    err = await _refused(_confirm(factory, a["member"], out["action_id"]))
    assert err.code == "preview_stale"
    assert (await _tx(factory, a["tx"])).category_id == a["c1"]
    assert (await _row(factory, out["action_id"])).status.value == "stale"


async def test_fp6_description_edit_between_preview_and_confirm_is_stale(factory, w):
    """FENCE F-P6 (rule id). Wrong implementation: a fingerprint over the
    ``before`` values only; a new description has no rule either, so only
    the change id (the normalized token) moves."""
    a = w["A"]
    out = await _stage(factory, a)
    await _set(factory, Transaction, a["tx"], description="NETFLIX STANDARD")
    err = await _refused(_confirm(factory, a["member"], out["action_id"]))
    assert err.code == "preview_stale"
    assert await _rules(factory) == []


async def test_auto_previews_and_learns_no_rule(factory, w):
    """GUARD (F-A4 has the fence). An ``agent:auto`` principal lists no rule
    and writes none, on a row that would teach one on the confirm path."""
    a = w["A"]
    out = await _stage(factory, a, api_token_id=a["t1"], **AUTO)
    assert out["status"] == "done"
    assert [c["entity"] for c in out["changes"]] == ["transactions"]
    assert out["result"]["rule_learned"] is False
    assert (await _tx(factory, a["tx"])).category_id == a["c2"]
    assert await _rules(factory) == []


# ── F-P10: series and linked rows ─────────────────────────────────────────

async def test_fp10_series_and_linked_rows_are_refused_at_preview(factory, w):
    """FENCE F-P10 (preview). Wrong implementation: a tool calling
    ``update_transaction`` without the refusal, which silently writes the
    partner, the template and every pending sibling."""
    a = w["A"]
    await _make_series(factory, a)
    err = await _refused(_stage(factory, a))
    assert (err.code, err.data) == ("unsupported_in_v1", {"reason": "recurring_series"})

    await _set(factory, Transaction, a["tx"], recurring_id=None)
    await _make_pair(factory, a)
    err = await _refused(_stage(factory, a))
    assert (err.code, err.data) == ("unsupported_in_v1", {"reason": "linked_transaction"})
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(CategoryRule)) == 0
    assert agent_router._STATUS["unsupported_in_v1"] == 422


async def test_fp10_row_joining_a_series_after_preview_fails_the_confirm(factory, w):
    """FENCE F-P10 (re-preview). Wrong implementation: refusing at preview
    only; confirm then rewrites the template and the pending sibling."""
    a = w["A"]
    out = await _stage(factory, a)
    tpl, sib = await _make_series(factory, a)
    err = await _refused(_confirm(factory, a["member"], out["action_id"]))
    assert err.code == "unsupported_in_v1"
    row = await _row(factory, out["action_id"])
    assert (row.status.value, row.error_code) == ("failed", "unsupported_in_v1")
    assert (await _tx(factory, a["tx"])).category_id == a["c1"]
    assert (await _tx(factory, sib)).category_id == a["c1"]
    async with factory() as db:
        assert (await db.get(RecurringTransaction, tpl)).category_id == a["c1"]


async def test_fp10_row_linked_after_preview_fails_the_confirm(factory, w):
    """FENCE F-P10 (re-preview). Wrong implementation: refusing at preview
    only; confirm then mirrors the category onto the partner."""
    a = w["A"]
    out = await _stage(factory, a)
    partner = await _make_pair(factory, a)
    err = await _refused(_confirm(factory, a["member"], out["action_id"]))
    assert err.code == "unsupported_in_v1"
    assert (await _row(factory, out["action_id"])).status.value == "failed"
    assert (await _tx(factory, a["tx"])).category_id == a["c1"]
    assert (await _tx(factory, partner)).category_id == a["c1"]


async def test_fp10_execute_refuses_on_its_own(factory, w):
    """FENCE F-P10 (execute). The re-preview and the execute are two
    transactions; a pair landing between them must still be refused. Wrong
    implementation: an execute that trusts the re-preview."""
    a = w["A"]
    partner = await _make_pair(factory, a)
    spec = registry.get_tool(TOOL)
    async with factory() as db:
        user = await db.get(User, a["member"])
        ctx = ToolContext(db=db, user=user, org_id=a["org"], channel="in_app", api_token_id=None)
        err = await _refused(registry.call_mapped(
            spec.execute, ctx, spec.args(transaction_id=a["tx"], category_id=a["c2"]),
        ))
    assert (err.code, err.data) == ("unsupported_in_v1", {"reason": "linked_transaction"})
    assert (await _tx(factory, a["tx"])).category_id == a["c1"]
    assert (await _tx(factory, partner)).category_id == a["c1"]


# ── other refusals ────────────────────────────────────────────────────────

async def test_refusals(factory, w):
    a, b = w["A"], w["B"]
    assert (await _refused(_stage(factory, a, cat=a["c1"]))).code == "no_change"
    # Another org's transaction, and another org's category.
    assert (await _refused(_stage(factory, a, tx=b["tx"]))).code == "not_found"
    assert (await _refused(_stage(factory, a, cat=b["c2"]))).code == "invalid_arguments"
    # A category of the wrong type for an expense.
    [income] = await _add(factory, Category(org_id=a["org"], name="Salary", type=CategoryType.INCOME))
    assert (await _refused(_stage(factory, a, cat=income))).code == "invalid_arguments"
    await _set(factory, Transaction, a["tx"], is_manual_adjustment=True)
    assert (await _refused(_stage(factory, a))).code == "invalid_arguments"
    assert (await _tx(factory, a["tx"])).category_id == a["c1"]
