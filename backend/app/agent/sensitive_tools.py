"""The sensitive transaction tools (TBD-664 PR B): ``transactions_update_money``,
``transactions_create``, ``transactions_delete`` and ``transactions_transfer``.

They move money, so each is ``sensitive``: never auto-applied, and never
confirmed by an agent. ``actions.confirm`` refuses an MCP confirm with
``requires_browser_confirmation``; the person applies the change in the app.

Each is route-bound (``app.agent.route_tool``): execute runs the REST handler.
Each preview names every row the confirm changes (fenced by
``tests/agent/test_agent_honesty.py``): every balance it moves as an
``accounts.balance_delta``, a learned category rule, a cascaded transfer leg,
the rows a delete demotes or unlinks, and the import-batch counters.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.agent.registry import Change, Preview, ToolContext, ToolError, register
from app.agent.route_tool import CascadeDisclosure, route_tool
from app.models import Account, Category, Transaction
from app.models.category_rule import CategoryRule
from app.models.import_batch import ImportBatch, ImportBatchStatus
from app.models.tag import TransactionTag
from app.models.transaction import TransactionStatus, TransactionType
from app.models.user import Role
from app.routers import transactions as transactions_router
from app.schemas.transaction import TransferCreate
from app.services import transaction_service
from app.services.category_rules_service import normalize_description
from app.services.exceptions import ValidationError
from app.services.reconciliation_service import PENDING_STATES
from app.services.transaction_filters import contributes_to_cached_balance

MONEY = "Moves money: the account balances listed here change."
CONFIRM = "Returns a preview; nothing changes until the user confirms it in the app."
SETTLED = TransactionStatus.SETTLED
ROUTER = transactions_router.router


def _signed(amount: Decimal, tx_type: TransactionType | str) -> Decimal:
    return amount if TransactionType(tx_type) == TransactionType.INCOME else -amount


def _iso(d: datetime.date | None) -> str | None:
    return d.isoformat() if d is not None else None


def _add(deltas: dict[int, Decimal], account_id: int, amount: Decimal) -> None:
    deltas[account_id] = deltas.get(account_id, Decimal("0")) + amount


async def _currencies(ctx: ToolContext, account_ids) -> dict[int, str]:
    rows = await ctx.db.execute(
        select(Account.id, Account.currency)
        .where(Account.id.in_(list(account_ids)), Account.org_id == ctx.org_id)
    )
    return dict(rows.all())


async def _balance_changes(ctx: ToolContext, deltas: dict[int, Decimal]) -> list[Change]:
    """One ``balance_delta`` per account that moves, in account id order."""
    cur = await _currencies(ctx, deltas)
    return [
        Change("accounts", aid, "balance_delta", None, f"{d:.2f}", cur.get(aid))
        for aid, d in sorted(deltas.items()) if d
    ]


def _priced(changes: list[Change], currency_of: dict) -> list[Change]:
    """Every ``amount`` change carries its row's currency (row id -> code)."""
    # Built directly, not by ``dataclasses.replace`` (fenced app-wide by
    # tests/test_complete_roster_single_construction_site.py).
    return [
        Change(c.entity, c.id, c.field, c.before, c.after, currency_of.get(c.id))
        if c.field == "amount" else c
        for c in changes
    ]


async def _rule_change(ctx: ToolContext, description: str, category_id: int) -> list[Change]:
    """The category rule ``create_transaction`` learns from a manual create:
    keyed by the normalized description, most recent pick wins."""
    token = normalize_description(description)
    if not token:
        return []
    current = await ctx.db.scalar(
        select(CategoryRule.category_id)
        .where(CategoryRule.org_id == ctx.org_id, CategoryRule.normalized_token == token)
        .execution_options(populate_existing=True)
    )
    return [Change("category_rules", {"untrusted": token}, "category_id", current, category_id)]


RULE = "Also updates the organization's categorization rule for this description."


# ── transactions_update_money ─────────────────────────────────────────────
#
# The four ``PUT /transactions/{id}`` fields ``transactions_update`` leaves
# out because they move ``accounts.balance``. Refused here, as the service
# would write beyond the row: a linked row (a transfer leg mirrors its amount
# to the partner; a matched import row's amount is outside the balance), and
# an account move inside a recurring series (it moves the template and every
# pending sibling).

def _money_after(tx: Transaction, args) -> tuple:
    """(status, type, amount, account_id, settled_date) once the update runs,
    resolved as ``update_transaction`` resolves them with no settled_date sent."""
    sent = args.model_fields_set
    status = TransactionStatus(args.status) if "status" in sent else tx.status
    tx_type = TransactionType(args.type) if "type" in sent else tx.type
    amount = args.amount if "amount" in sent else tx.amount
    account_id = args.account_id if "account_id" in sent else tx.account_id
    if status == SETTLED:
        settled = tx.settled_date if tx.status == SETTLED else datetime.date.today()
    else:
        settled = None if tx.status == SETTLED else tx.settled_date
    return status, tx_type, amount, account_id, settled


async def _money_check(ctx: ToolContext, tx: Transaction, args) -> None:
    if tx.is_manual_adjustment:
        raise ValidationError("Manual balance adjustments cannot be edited")
    if tx.linked_transaction_id is not None:
        raise ToolError(
            "unsupported_in_v1",
            "the amount, account, type or status of a linked transaction cannot be changed here",
            data={"reason": "linked_transaction"},
        )
    status, tx_type, amount, account_id, settled = _money_after(tx, args)
    if account_id != tx.account_id:
        if tx.recurring_id is not None:
            raise ToolError(
                "unsupported_in_v1",
                "moving a transaction in a recurring series moves the series; edit the series instead",
                data={"reason": "recurring_series"},
            )
        await transaction_service.validate_account(ctx.db, account_id, ctx.org_id)
    if tx_type != tx.type:
        await transaction_service.validate_category_for_type(ctx.db, tx.category_id, ctx.org_id, tx_type)
    if settled is not None and settled < tx.date:
        raise ValidationError("settled_date must be on or after date")


async def _money_present(ctx: ToolContext, tx: Transaction, args, changes: list[Change]) -> Preview:
    status, tx_type, amount, account_id, settled = _money_after(tx, args)
    cur = await _currencies(ctx, {tx.account_id, account_id})
    changes = _priced(changes, {tx.id: cur.get(account_id)})
    fields = [c.field for c in changes]
    if settled != tx.settled_date:
        changes.append(Change("transactions", tx.id, "settled_date", _iso(tx.settled_date), _iso(settled)))
    deltas: dict[int, Decimal] = {}
    # Unlinked (checked above), so False only for a reverted import row,
    # whose amount is outside the balance before and after.
    if contributes_to_cached_balance(tx, None):
        if tx.status == SETTLED:
            _add(deltas, tx.account_id, -_signed(tx.amount, tx.type))
        if status == SETTLED:
            _add(deltas, account_id, _signed(amount, tx_type))
    money = await _balance_changes(ctx, deltas)
    return Preview(
        summary=f"Change the {', '.join(fields)} of transaction {tx.id}",
        changes=changes + money,
        warnings=[MONEY] if money else [],
        context={"description": tx.description},
    )


register(route_tool(
    name="transactions_update_money", risk="sensitive",
    router=ROUTER, method="PUT", path="/api/v1/transactions/{transaction_id}",
    fields=("amount", "account_id", "type", "status"),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Change the amount, account, type (income or expense) or status (settled or pending) "
        "of one transaction. This moves account balances. Transactions linked to another one "
        f"(a transfer leg, a matched import) are refused. {CONFIRM}"
    ),
    entity=Transaction, check=_money_check, present=_money_present,
    no_change="the transaction already has these values",
))


# ── transactions_create ───────────────────────────────────────────────────

async def _create_check(ctx: ToolContext, _row, args) -> None:
    await transaction_service.validate_account(ctx.db, args.account_id, ctx.org_id)
    await transaction_service.validate_category_for_type(
        ctx.db, args.category_id, ctx.org_id, TransactionType(args.type)
    )


async def _create_present(ctx: ToolContext, _row, args, changes: list[Change]) -> Preview:
    named = {c.field: c.after for c in changes}
    cur = await _currencies(ctx, {args.account_id})
    changes = _priced(changes, {"new:1": cur.get(args.account_id)})
    if named["status"] == SETTLED.value and "settled_date" not in named:
        # The service stamps a settled row's settlement date with its date.
        changes.append(Change("transactions", "new:1", "settled_date", None, named["date"]))
    deltas: dict[int, Decimal] = {}
    if named["status"] == SETTLED.value:
        _add(deltas, args.account_id, _signed(args.amount, args.type))
    money = await _balance_changes(ctx, deltas)
    rule = await _rule_change(ctx, args.description, args.category_id)
    return Preview(
        summary=f"Create an {args.type} transaction",
        changes=changes + money + rule,
        warnings=([MONEY] if money else []) + ([RULE] if rule else []),
        context={"description": args.description},
    )


register(route_tool(
    name="transactions_create", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/transactions",
    fields=("account_id", "category_id", "description", "amount", "type", "status", "date",
            "settled_date"),
    required=("account_id", "category_id", "description", "amount", "type", "date"),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Create one income or expense transaction. A settled one moves its account's balance; "
        "the category also teaches the categorization rule for its description. Use "
        f"transactions_transfer to move money between two accounts. {CONFIRM}"
    ),
    entity=Transaction, check=_create_check, present=_create_present,
))


# ── transactions_delete ───────────────────────────────────────────────────
#
# ``plan_transaction_delete`` (TBD-664 A2) is the decision ``delete_transaction``
# applies, so this disclosure is the delete itself, not a second copy of its
# rules. The two foreign keys onto ``transactions``:
#
# * ``transactions.linked_transaction_id`` (SET NULL): every surviving row that
#   pointed at a deleted one loses its link, and a matched import row whose
#   amount is outside the balance is also demoted to REJECTED (TBD-294);
# * ``transaction_tags.transaction_id`` (CASCADE): the deleted rows' tags.

DESCRIBE = ("description", "amount", "type", "status", "date", "account_id")


def _deleted(row: Transaction) -> list[Change]:
    return [
        Change("transactions", row.id, c, v, None)
        for c, v in (
            ("description", {"untrusted": row.description}), ("amount", f"{row.amount:.2f}"),
            ("type", row.type.value), ("status", row.status.value), ("date", _iso(row.date)),
            ("account_id", row.account_id),
        )
    ]


async def _batch_changes(ctx: ToolContext, plan) -> list[Change]:
    """The counters ``apply_batch_settlement`` moves, and an auto-close, per batch,
    predicted the way ``close_batch_if_complete`` decides it."""
    s = plan.settlement
    out: list[Change] = []
    for bid in sorted(set(s.row_delta) | set(s.accepted_delta) | set(s.pending_delta)):
        batch = await ctx.db.scalar(
            select(ImportBatch).where(ImportBatch.id == bid, ImportBatch.org_id == ctx.org_id)
            .execution_options(populate_existing=True)
        )
        if batch is None:
            continue
        before = {"row_count": batch.row_count, "accepted_count": batch.accepted_count,
                  "pending_count": batch.pending_count}
        after = {
            "row_count": max(batch.row_count - s.row_delta.get(bid, 0), 0),
            "accepted_count": max(batch.accepted_count - s.accepted_delta.get(bid, 0), 0),
            "pending_count": max(batch.pending_count - s.pending_delta.get(bid, 0), 0),
        }
        status = batch.status
        if bid in s.pending_delta and status != ImportBatchStatus.CLOSED and after["pending_count"] == 0:
            gone = set(s.deleted_ids) | set(s.demoted_ids)
            recount = await ctx.db.scalar(
                select(func.count(Transaction.id)).where(
                    Transaction.import_batch_id == bid,
                    Transaction.reconciliation_state.in_(list(PENDING_STATES)),
                    Transaction.id.notin_(gone),
                )
            )
            if recount:
                after["pending_count"] = int(recount)  # its drift repair
            else:
                status = ImportBatchStatus.CLOSED
        out += [
            Change("import_batches", bid, f"{col}_delta", None, str(after[col] - before[col]))
            for col in ("row_count", "accepted_count", "pending_count") if after[col] != before[col]
        ]
        if status != batch.status:
            out.append(Change("import_batches", bid, "status", batch.status.value, status.value))
    return out


async def _delete_cascade(ctx: ToolContext, tx: Transaction, args) -> list[Change]:
    plan = await transaction_service.plan_transaction_delete(ctx.db, ctx.org_id, tx.id)
    deleted = set(plan.deleted)
    out: list[Change] = []
    for rid in sorted(deleted - {tx.id}):  # the other leg of a transfer
        out += _deleted(plan.deleted[rid])
    tags = (await ctx.db.execute(
        select(TransactionTag.transaction_id, TransactionTag.tag_id)
        .where(TransactionTag.transaction_id.in_(sorted(deleted)))
        .order_by(TransactionTag.transaction_id, TransactionTag.tag_id)
    )).all()
    out += [Change("transaction_tags", [t, g], "tag_id", g, None) for t, g in tags]
    prior = plan.settlement.prior_states
    for r in sorted(plan.rows.values(), key=lambda r: r.id):
        if r.id in deleted or r.linked_transaction_id not in deleted:
            continue
        if r.id in prior:
            out.append(Change("transactions", r.id, "reconciliation_state", prior[r.id], "rejected"))
        out.append(Change("transactions", r.id, "linked_transaction_id", r.linked_transaction_id, None))
    deltas: dict[int, Decimal] = {}
    for r in plan.reverts:
        _add(deltas, r.account_id, -_signed(r.amount, r.type))
    out += await _balance_changes(ctx, deltas)
    out += await _batch_changes(ctx, plan)
    return out


async def _delete_present(ctx: ToolContext, tx: Transaction, args, changes: list[Change]) -> Preview:
    gone = {c.id for c in changes if c.entity == "transactions" and c.field == "type"}
    accounts = {c.id: c.before for c in changes if c.entity == "transactions" and c.field == "account_id"}
    cur = await _currencies(ctx, set(accounts.values()))
    changes = _priced(changes, {rid: cur.get(aid) for rid, aid in accounts.items()})
    demoted = sum(1 for c in changes if c.field == "reconciliation_state")
    warnings = ["A delete cannot be undone."]
    if len(gone) > 1:
        warnings.append("This is one leg of a transfer: the other leg is deleted too.")
    if demoted:
        warnings.append(
            f"{demoted} matched import row(s) pointing at it will be marked rejected, "
            "which cannot be undone."
        )
    if any(c.field == "balance_delta" for c in changes):
        warnings.append(MONEY)
    deleted = [{"entity": "transactions", "id": rid} for rid in sorted(gone)] + [
        {"entity": "transaction_tags", "id": c.id} for c in changes if c.entity == "transaction_tags"
    ]
    return Preview(
        summary=f"Delete transaction {tx.id}" + (" and the other leg of its transfer" if len(gone) > 1 else ""),
        changes=changes, warnings=warnings,
        context={"description": tx.description, "deleted": deleted},
    )


register(route_tool(
    name="transactions_delete", risk="sensitive",
    router=ROUTER, method="DELETE", path="/api/v1/transactions/{transaction_id}",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description=(
        "Delete one transaction. A settled one gives its amount back to its account; deleting "
        "one leg of a transfer deletes the other; matched import rows pointing at it are marked "
        "rejected and reported as demoted_ids. Cannot be undone. "
        f"{CONFIRM}"
    ),
    entity=Transaction, describe=DESCRIBE, present=_delete_present,
    cascade=CascadeDisclosure(
        covers=frozenset({"transactions.linked_transaction_id", "transaction_tags.transaction_id"}),
        disclose=_delete_cascade, locks=True,
    ),
))


# ── transactions_transfer ─────────────────────────────────────────────────
#
# Two rows, linked to each other, so a hand-written preview: the legs are
# ``new:1`` (expense, source account) and ``new:2`` (income, destination),
# and each names the other as its ``linked_transaction_id``.

async def _transfer_preview(ctx: ToolContext, args) -> Preview:
    body = TransferCreate.model_validate(
        {k: getattr(args, k) for k in TransferCreate.model_fields if k in args.model_fields_set}
    )
    if body.from_account_id == body.to_account_id:
        raise ValidationError("Source and destination accounts must be different")
    accounts = {
        r.id: r for r in (await ctx.db.execute(
            select(Account.id, Account.name, Account.currency).where(
                Account.id.in_([body.from_account_id, body.to_account_id]), Account.org_id == ctx.org_id,
            )
        )).all()
    }
    if len(accounts) != 2:
        raise ValidationError("Invalid account")
    src, dst = accounts[body.from_account_id], accounts[body.to_account_id]
    if src.currency != dst.currency:
        raise ValidationError("Transfer legs must have the same currency")
    description = body.description.strip() or f"Transfer from {src.name} to {dst.name}"
    if body.category_id is not None:
        await transaction_service.validate_transfer_category(ctx.db, body.category_id, ctx.org_id)
        category_id = body.category_id
    else:
        category_id = await ctx.db.scalar(
            select(Category.id).where(Category.slug == "transfer", Category.org_id == ctx.org_id)
        )
        if category_id is None:
            # The service would create the category: name one instead.
            raise ToolError(
                "unsupported_in_v1", "the organization has no Transfer category; pass a category_id",
                data={"reason": "no_transfer_category"},
            )
    settled = body.date if body.status == SETTLED.value else None
    changes: list[Change] = []
    for leg, account, tx_type, other in (
        ("new:1", src, "expense", "new:2"), ("new:2", dst, "income", "new:1"),
    ):
        changes += [
            Change("transactions", leg, f, None, v, src.currency if f == "amount" else None)
            for f, v in (
                ("account_id", account.id), ("category_id", category_id),
                ("description", {"untrusted": description}), ("amount", f"{body.amount:.2f}"),
                ("type", tx_type), ("status", body.status), ("date", _iso(body.date)),
                ("settled_date", _iso(settled)), ("linked_transaction_id", other),
            )
            if v is not None
        ]
    deltas: dict[int, Decimal] = {}
    if settled is not None:
        _add(deltas, src.id, -body.amount)
        _add(deltas, dst.id, body.amount)
    money = await _balance_changes(ctx, deltas)
    return Preview(
        summary="Move money between two accounts",
        changes=changes + money,
        warnings=[MONEY] if money else [],
        context={
            "description": description,
            "from": {"account_name": src.name}, "to": {"account_name": dst.name},
        },
    )


register(route_tool(
    name="transactions_transfer", risk="sensitive",
    router=ROUTER, method="POST", path="/api/v1/transactions/transfer",
    fields=("from_account_id", "to_account_id", "category_id", "description", "amount", "status",
            "date"),
    required=("from_account_id", "to_account_id", "amount", "date"),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Move money between two of the organization's accounts in the same currency: an expense "
        "on the source and an income on the destination, linked as one transfer. Without a "
        f"category_id it uses the Transfer category. {CONFIRM}"
    ),
    preview=_transfer_preview,
))
