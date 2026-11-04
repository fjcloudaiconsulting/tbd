"""TBD-664 PR A0: staged args keep only the fields the caller set (W13).

``propose`` stored ``args.model_dump(mode="json")``, so every optional field
came back SET at confirm; ``update_transaction`` honours an explicit null
(``model_fields_set``), so a description-only edit would have cleared a pending
row's expected settlement date. No shipped tool has an optional field yet, so a
scratch write tool over ``PUT /transactions/{id}`` stands in.

Fence: T-A12. Wrong implementation: storing the dump without ``exclude_unset``.
"""
from __future__ import annotations

import datetime

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

from app.agent import registry
from app.agent.registry import Change, Preview, ToolError, ToolSpec
from app.models.agent_pending_action import AgentPendingAction
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import Role
from app.schemas.transaction import TransactionUpdate
from app.services import transaction_service
from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    P_START, _confirm, _invoke, engine, factory, w,
)

FIELDS = ("description", "settled_date")


class _EditArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_id: int
    description: str | None = None
    settled_date: datetime.date | None = None


async def _preview(ctx, args):
    tx = await ctx.db.scalar(
        select(Transaction).where(Transaction.id == args.transaction_id, Transaction.org_id == ctx.org_id)
        .execution_options(populate_existing=True)
    )
    changes = []
    for f in FIELDS:
        if f not in args.model_fields_set:
            continue
        old, new = getattr(tx, f), getattr(args, f)
        if old != new:
            iso = lambda v: v.isoformat() if isinstance(v, datetime.date) else v  # noqa: E731
            changes.append(Change("transactions", tx.id, f, iso(old), iso(new)))
    if not changes:
        raise ToolError("no_change", "nothing to change")
    return Preview(summary="edit", changes=changes, warnings=[], context={})


async def _execute(ctx, args):
    body = TransactionUpdate(**args.model_dump(include=set(FIELDS), exclude_unset=True))
    out = await transaction_service.update_transaction(
        ctx.db, ctx.org_id, args.transaction_id, body, learn=False
    )
    return transaction_service.to_response(out).model_dump(mode="json")


@pytest.fixture
def edit_tool():
    registry.register(ToolSpec(
        name="scratch_edit", risk="write", args=_EditArgs, product_area=None, min_role=Role.MEMBER,
        mirrors_route=("PUT", "/api/v1/transactions/{transaction_id}"), description="t",
        preview=_preview, execute=_execute,
    ))
    yield "scratch_edit"
    registry._TOOLS.pop("scratch_edit", None)


async def test_ta12_a_description_only_edit_keeps_the_pending_rows_settled_date(factory, w, edit_tool):
    """FENCE T-A12. Wrong implementation: ``args.model_dump(mode="json")`` at
    propose. Confirm then re-validates with ``settled_date: null`` SET, its
    re-preview names a clearing the caller never asked for (``preview_stale``),
    and a surviving execute would clear the date."""
    a = w["A"]
    async with factory() as db:
        await db.execute(update(Transaction).where(Transaction.id == a["tx"]).values(
            status=TransactionStatus.PENDING, settled_date=P_START))
        await db.commit()
    staged = await _invoke(factory, a["member"], edit_tool,
                           {"transaction_id": a["tx"], "description": "RENAMED"})
    async with factory() as db:
        row = await db.get(AgentPendingAction, staged["action_id"])
    assert row.args_json == {"transaction_id": a["tx"], "description": "RENAMED"}
    done = await _confirm(factory, a["member"], staged["action_id"])
    assert done["status"] == "done"
    async with factory() as db:
        tx = await db.get(Transaction, a["tx"])
    assert (tx.description, tx.settled_date) == ("RENAMED", P_START)
