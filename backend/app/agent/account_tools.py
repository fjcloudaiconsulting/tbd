"""The account tools (TBD-665): accounts, account types and credit-card cycle
payments.

Each is route-bound (``app.agent.route_tool``) except
``accounts_adjust_balance``: its route parses the body from the raw
``Request`` (so its role and flag gates answer before a 422), so its tool is
hand-written over the same service with the same gates.

Tiers, as in TBD-664: an edit of existing columns is ``write``; a create, a
delete, a cycle payment and the balance adjustment are ``sensitive`` (never
auto, confirmed only by the person in the app). Each preview names every row
its confirm writes (fenced by ``tests/agent/test_agent_honesty.py``).

``PUT /accounts/{id}`` is mirrored for its plain columns only, so it stays in
the gaps ledger. The opening balance moves the balance, and the type, credit
card and loan columns take the handler's atomic path, which locks the account
on a second session while the confirm's re-preview holds that row lock on
``ctx.db`` (it would wait on itself).
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from pydantic import ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import logs
from app.agent.registry import Change, Preview, ToolContext, ToolError, ToolSpec, register
from app.agent.route_tool import CascadeDisclosure, refusals, route_tool
from app.models import Account, Category, Transaction
from app.models.account import AccountType
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.category import CategoryType
from app.models.import_batch import ImportBatch
from app.models.recurring import RecurringTransaction
from app.models.user import Organization, Role
from app.routers import account_types as account_types_router
from app.routers import accounts as accounts_router
from app.routers import cc_cycle_payments as cycle_router
from app.schemas.account import BalanceAdjustmentRequest, BalanceAdjustmentResponse
from app.services.cc_cycle_payment_service import validate_cycle_payment
from app.services.exceptions import NotFoundError
from app.services.payment_source_service import validate_payment_source_account
from app.services.transaction_service import (
    _DESCRIPTION_MAX_LENGTH,
    BALANCE_ADJUSTMENT_CATEGORY_SLUG,
    adjust_account_balance,
    assert_no_dependents,
)

CONFIRM = "Returns a preview; nothing changes until the user confirms it in the app."
CONFIRM_WRITE = "Returns a preview to confirm; nothing changes until it is confirmed."
ACCOUNTS = accounts_router.router
TYPES = account_types_router.router
CYCLES = cycle_router.router


# ── reads ─────────────────────────────────────────────────────────────────

register(route_tool(
    name="accounts_get", risk="read",
    router=ACCOUNTS, method="GET", path="/api/v1/accounts/{account_id}",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description="Read one account by id, with its balance and its credit card or loan settings.",
))
register(route_tool(
    name="account_types_list", risk="read",
    router=TYPES, method="GET", path="/api/v1/account-types",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description="List the organization's account types, with how many accounts use each.",
))
register(route_tool(
    name="cycle_payments_list", risk="read",
    router=CYCLES, method="GET", path="/api/v1/accounts/{account_id}/cycle-payments",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description=(
        "The next three billing cycles of a credit card account (close and due dates), each "
        "with the payment amount set for it, if any."
    ),
))


# ── accounts_create ───────────────────────────────────────────────────────
#
# The template names every column the insert writes from the body; the live
# balance is seeded from the opening balance. The opening balance date is
# required here: the route's fallback is the database's CURRENT_DATE, which a
# preview cannot state. The handler validates everything before it inserts,
# so a refused create fails at confirm and writes nothing.

async def _create_present(ctx: ToolContext, _row, args, changes: list[Change]) -> Preview:
    named = {c.field: c.after for c in changes}
    opening, cur = named["opening_balance"], named["currency"]
    # Built directly, not by ``dataclasses.replace`` (fenced app-wide).
    changes = [
        Change(c.entity, c.id, c.field, c.before, c.after, cur) if c.field == "opening_balance" else c
        for c in changes
    ] + [Change("accounts", "new:1", "balance", None, opening, cur)]
    return Preview(
        summary="Create an account",
        changes=changes,
        warnings=["The account's balance starts at its opening balance."] if Decimal(opening) else [],
        context={"name": args.name},
    )


register(route_tool(
    name="accounts_create", risk="sensitive",
    router=ACCOUNTS, method="POST", path="/api/v1/accounts",
    fields=("name", "account_type_id", "currency", "opening_balance", "opening_balance_date",
            "close_day", "payment_day", "payment_day_relative_month", "payment_source_account_id",
            "credit_limit", "apr", "payment_strategy", "fixed_payment_amount", "principal_amount",
            "interest_rate_apr", "term_months", "origination_date", "first_payment_date"),
    required=("name", "account_type_id", "opening_balance_date"),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Create an account of one of the organization's account types (see account_types_list). "
        "Its balance starts at the opening balance. close_day and payment_day are for credit "
        "cards; the loan fields are required for a loan and refused otherwise. "
        f"{CONFIRM}"
    ),
    entity=Account, present=_create_present,
))


# ── accounts_update ───────────────────────────────────────────────────────
#
# The fast path of ``update_account``: no balance moves and neither of its
# admin-only branches (an opening balance over settled activity, a type change
# dropping cycle payments) is reachable from these fields. Making an account
# the default clears every other default account of the org: listed, and
# refused for an ``agent:auto`` principal (no derived row without a confirm).

async def _update_check(ctx: ToolContext, acct: Account, args) -> None:
    sent = args.model_fields_set
    if "is_active" in sent and args.is_active is False and acct.balance != 0:
        raise ToolError(
            "conflict", f"Cannot deactivate account with balance {acct.balance}. Transfer the balance first.",
        )
    if "payment_source_account_id" in sent:
        with refusals():
            await validate_payment_source_account(
                ctx.db, org_id=ctx.org_id, source_account_id=args.payment_source_account_id,
                target_account_id=acct.id,
            )


async def _update_present(ctx: ToolContext, acct: Account, args, changes: list[Change]) -> Preview:
    fields = [c.field for c in changes]
    if args.is_default:
        others = await ctx.db.scalars(
            select(Account.id).where(
                Account.org_id == ctx.org_id, Account.id != acct.id, Account.is_default.is_(True),
            ).order_by(Account.id)
        )
        cleared = [Change("accounts", i, "is_default", True, False) for i in others]
        if cleared and ctx.auto:  # an unconfirmed write writes no derived row
            raise ToolError(
                "unsupported_in_v1", "making an account the default clears the current default; "
                "stage it with a confirming token", data={"reason": "derived_rows"},
            )
        changes = changes + cleared
    return Preview(
        summary=f"Change the {', '.join(fields)} of account {acct.id}",
        changes=changes,
        context={"name": acct.name},
    )


register(route_tool(
    name="accounts_update", risk="write",
    router=ACCOUNTS, method="PUT", path="/api/v1/accounts/{account_id}",
    fields=("name", "is_active", "is_default", "payment_source_account_id", "opening_balance_date"),
    nullable=("payment_source_account_id",),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Rename an account, activate or deactivate it (only at a zero balance), make it the "
        "default account, set or clear (null) the account its bill is paid from, or change its "
        f"opening balance date. {CONFIRM_WRITE}"
    ),
    entity=Account, check=_update_check, present=_update_present,
    no_change="the account already has these values",
))


# ── accounts_delete ───────────────────────────────────────────────────────
#
# The route refuses a nonzero balance and any transaction. The other foreign
# keys onto ``accounts``: an import batch or a recurring template blocks the
# delete (no ON DELETE, so the database would refuse it); a liability paid
# from this account loses that link (SET NULL); its cycle payments go with it
# (CASCADE).

async def _delete_check(ctx: ToolContext, acct: Account, args) -> None:
    if acct.balance != 0:
        raise ToolError(
            "conflict", f"Cannot delete account with balance {acct.balance}. Transfer the balance first.",
        )
    with refusals():
        for model, noun in (
            (Transaction, "transaction"), (ImportBatch, "import batch"),
            (RecurringTransaction, "recurring transaction"),
        ):
            await assert_no_dependents(
                ctx.db, model, [model.account_id == acct.id, model.org_id == ctx.org_id], noun, "account",
            )


async def _delete_cascade(ctx: ToolContext, acct: Account, args) -> list[Change]:
    paid_from = await ctx.db.scalars(
        select(Account.id).where(Account.payment_source_account_id == acct.id).order_by(Account.id)
    )
    out = [Change("accounts", i, "payment_source_account_id", acct.id, None) for i in paid_from]
    payments = await ctx.db.scalars(
        select(CcCyclePayment).where(CcCyclePayment.account_id == acct.id).order_by(CcCyclePayment.id)
    )
    for p in payments:
        out += [
            Change("cc_cycle_payments", p.id, f, v, None, acct.currency if f == "amount" else None)
            for f, v in (("account_id", p.account_id), ("period_anchor_year", p.period_anchor_year),
                         ("period_anchor_month", p.period_anchor_month), ("amount", f"{p.amount:.2f}"))
        ]
    return out


async def _delete_present(ctx: ToolContext, acct: Account, args, changes: list[Change]) -> Preview:
    payments = sorted({c.id for c in changes if c.entity == "cc_cycle_payments"})
    return Preview(
        summary=f"Delete account {acct.id}",
        changes=changes,
        warnings=["A delete cannot be undone."]
        + ([f"Its {len(payments)} cycle payment(s) are deleted too."] if payments else []),
        context={
            "name": acct.name,
            "deleted": [{"entity": "accounts", "id": acct.id}]
            + [{"entity": "cc_cycle_payments", "id": i} for i in payments],
        },
    )


register(route_tool(
    name="accounts_delete", risk="sensitive",
    router=ACCOUNTS, method="DELETE", path="/api/v1/accounts/{account_id}",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description=(
        "Delete an account with a zero balance and no transactions, import batches or recurring "
        "transactions. Its cycle payments are deleted with it. Cannot be undone. "
        f"{CONFIRM}"
    ),
    entity=Account, describe=("name", "account_type_id", "currency", "is_active"),
    check=_delete_check, present=_delete_present,
    cascade=CascadeDisclosure(
        covers=frozenset({
            "accounts.payment_source_account_id", "cc_cycle_payments.account_id",
            "import_batches.account_id", "recurring_transactions.account_id", "transactions.account_id",
        }),
        disclose=_delete_cascade,
    ),
))


# ── accounts_adjust_balance (hand-written) ────────────────────────────────
#
# The route's gates, in its order: owner or admin (``min_role``), the org's
# manual-adjustment switch, the account in the org, a nonzero delta. The
# service books the delta as one settled manual-adjustment transaction in the
# org's Balance Adjustment category, creating that category the first time.
# The route's per-IP rate limit has no client here: the agent's own preview
# and confirm limits, and the person's confirm in the app, bound it.

class AdjustBalanceArgs(BalanceAdjustmentRequest):
    model_config = ConfigDict(extra="forbid")
    account_id: int


async def _adjust_preview(ctx: ToolContext, args: AdjustBalanceArgs) -> Preview:
    allowed = await ctx.db.scalar(
        select(Organization.allow_manual_balance_adjustment).where(Organization.id == ctx.org_id)
    )
    if not allowed:
        raise ToolError("forbidden", "Manual balance adjustment is disabled for this organization")
    acct = await ctx.db.scalar(
        select(Account).where(Account.id == args.account_id, Account.org_id == ctx.org_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if acct is None:
        raise NotFoundError("Account")
    delta = args.target_balance - acct.balance
    if delta == 0:
        raise ToolError("no_change", "the account already has this balance")
    # The service's description, built from the same locked balance.
    description = f"Balance adjustment: {acct.balance} -> {args.target_balance}"
    if args.reason:
        description = f"{description} ({args.reason})"
    description = description[:_DESCRIPTION_MAX_LENGTH]
    today = datetime.date.today().isoformat()
    category_id = await ctx.db.scalar(
        select(Category.id).where(
            Category.slug == BALANCE_ADJUSTMENT_CATEGORY_SLUG, Category.org_id == ctx.org_id,
            Category.is_system.is_(True),
        )
    )
    new_category = []
    if category_id is None:
        category_id = "new:2"
        new_category = [
            Change("categories", "new:2", f, None, v)
            for f, v in (("name", "Balance Adjustment"), ("slug", BALANCE_ADJUSTMENT_CATEGORY_SLUG),
                         ("description", "Generated by manual balance adjustments"),
                         ("type", CategoryType.BOTH.value), ("is_system", True))
        ]
    changes = [
        Change("transactions", "new:1", f, None, v, acct.currency if f == "amount" else None)
        for f, v in (
            ("account_id", acct.id), ("category_id", category_id),
            ("description", {"untrusted": description}), ("amount", f"{abs(delta):.2f}"),
            ("type", "income" if delta > 0 else "expense"), ("status", "settled"),
            ("date", today), ("settled_date", today), ("is_manual_adjustment", True),
        )
    ]
    return Preview(
        summary=f"Set the balance of account {acct.id} to {args.target_balance:.2f} {acct.currency}",
        changes=changes + new_category
        + [Change("accounts", acct.id, "balance_delta", None, f"{delta:.2f}", acct.currency)],
        warnings=["Moves money: the account balance listed here changes, booked as an adjustment."]
        + (["Also creates the organization's Balance Adjustment category."] if new_category else []),
        context={"account_name": acct.name, "description": description},
    )


async def _adjust_execute(ctx: ToolContext, args: AdjustBalanceArgs) -> dict:
    org_name = await ctx.db.scalar(select(Organization.name).where(Organization.id == ctx.org_id))
    with refusals():
        tx, old, new, delta = await adjust_account_balance(
            ctx.db, ctx.org_id, args.account_id,
            target_balance=args.target_balance, reason=args.reason,
            actor_user_id=ctx.user.id, actor_email=ctx.user.email, actor_org_name=org_name,
            request_id=logs.CONTEXT.get({}).get("request_id"), ip_address=None,
        )
    return BalanceAdjustmentResponse(
        account_id=args.account_id, old_balance=old, new_balance=new, delta=delta, transaction_id=tx.id,
    ).model_dump(mode="json")


register(ToolSpec(
    name="accounts_adjust_balance", risk="sensitive", args=AdjustBalanceArgs,
    product_area=None, min_role=Role.ADMIN,
    mirrors_route=("POST", "/api/v1/accounts/{account_id}/adjust-balance"),
    description=(
        "Set an account's balance to a target amount (owner or admin, and only when the "
        "organization allows manual balance adjustments). The difference is booked as one "
        f"settled adjustment transaction. {CONFIRM}"
    ),
    preview=_adjust_preview, execute=_adjust_execute,
))


# ── account types ─────────────────────────────────────────────────────────

async def _type_check(ctx: ToolContext, at: AccountType, args) -> None:
    if at.is_system:
        raise ToolError("conflict", "A system account type cannot be renamed or deleted")


async def _type_delete_check(ctx: ToolContext, at: AccountType, args) -> None:
    await _type_check(ctx, at, args)
    with refusals():
        await assert_no_dependents(
            ctx.db, Account, [Account.account_type_id == at.id, Account.org_id == ctx.org_id],
            "account", "type",
        )


async def _no_rows(ctx: ToolContext, at: AccountType, args) -> list[Change]:
    return []  # its only reference, ``accounts.account_type_id``, is refused above


register(route_tool(
    name="account_types_create", risk="sensitive",
    router=TYPES, method="POST", path="/api/v1/account-types",
    fields=("name",), required=("name",), product_area=None, min_role=Role.MEMBER,
    description=f"Create an account type. {CONFIRM}",
    entity=AccountType,
))
register(route_tool(
    name="account_types_update", risk="write",
    router=TYPES, method="PUT", path="/api/v1/account-types/{type_id}",
    fields=("name",), required=("name",), product_area=None, min_role=Role.MEMBER,
    description=f"Rename an account type (system types cannot be renamed). {CONFIRM_WRITE}",
    entity=AccountType, check=_type_check, no_change="the account type already has this name",
))
register(route_tool(
    name="account_types_delete", risk="sensitive",
    router=TYPES, method="DELETE", path="/api/v1/account-types/{type_id}",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description=(
        "Delete an account type no account uses (system types cannot be deleted). Cannot be "
        f"undone. {CONFIRM}"
    ),
    entity=AccountType, describe=("name",), check=_type_delete_check,
    cascade=CascadeDisclosure(covers=frozenset({"accounts.account_type_id"}), disclose=_no_rows),
))


# ── credit card cycle payments ────────────────────────────────────────────
#
# The planned payment for one billing cycle of a credit card, keyed by the
# cycle's close month. Owner or admin (the routes check inline). Three ids in
# the path, so the previews are hand-written; each runs the routes' own cycle
# validation (a credit card with a close day, a current or future cycle).

def _cycle_label(args) -> str:
    return f"{args.year}-{args.month:02d}"


async def _cycle_rows(ctx: ToolContext, args, amount: Decimal | None):
    acct = await ctx.db.scalar(
        select(Account).options(selectinload(Account.account_type))
        .where(Account.id == args.account_id, Account.org_id == ctx.org_id)
        .execution_options(populate_existing=True)
    )
    if acct is None:
        raise NotFoundError("Account")
    with refusals():
        validate_cycle_payment(
            account=acct, account_slug=acct.account_type.slug if acct.account_type else None,
            year=args.year, month=args.month, today=datetime.date.today(), amount=amount,
        )
    row = await ctx.db.scalar(
        select(CcCyclePayment).where(
            CcCyclePayment.account_id == acct.id, CcCyclePayment.period_anchor_year == args.year,
            CcCyclePayment.period_anchor_month == args.month,
        ).with_for_update().execution_options(populate_existing=True)
    )
    return acct, row


def _cycle_insert(acct: Account, args) -> Preview:
    return Preview(
        summary=f"Set the payment for the {_cycle_label(args)} cycle of account {acct.id}",
        changes=[
            Change("cc_cycle_payments", "new:1", f, None, v, acct.currency if f == "amount" else None)
            for f, v in (("account_id", acct.id), ("period_anchor_year", args.year),
                         ("period_anchor_month", args.month), ("amount", f"{args.amount:.2f}"))
        ],
        context={"account_name": acct.name},
    )


async def _cycle_create_preview(ctx: ToolContext, args) -> Preview:
    acct, row = await _cycle_rows(ctx, args, args.amount)
    if row is not None:
        raise ToolError("conflict", "cycle payment already exists")
    return _cycle_insert(acct, args)


async def _cycle_upsert_preview(ctx: ToolContext, args) -> Preview:
    acct, row = await _cycle_rows(ctx, args, args.amount)
    if row is None:
        return _cycle_insert(acct, args)
    before, after = f"{row.amount:.2f}", f"{args.amount:.2f}"
    if before == after:
        raise ToolError("no_change", "the cycle already has this payment")
    return Preview(
        summary=f"Change the payment for the {_cycle_label(args)} cycle of account {acct.id}",
        changes=[Change("cc_cycle_payments", row.id, "amount", before, after, acct.currency)],
        context={"account_name": acct.name},
    )


async def _cycle_delete_preview(ctx: ToolContext, args) -> Preview:
    acct, row = await _cycle_rows(ctx, args, None)
    if row is None:
        raise NotFoundError("Cycle payment")
    return Preview(
        summary=f"Remove the payment for the {_cycle_label(args)} cycle of account {acct.id}",
        changes=[
            Change("cc_cycle_payments", row.id, f, v, None, acct.currency if f == "amount" else None)
            for f, v in (("account_id", row.account_id), ("period_anchor_year", row.period_anchor_year),
                         ("period_anchor_month", row.period_anchor_month), ("amount", f"{row.amount:.2f}"))
        ],
        context={"account_name": acct.name, "deleted": [{"entity": "cc_cycle_payments", "id": row.id}]},
    )


CYCLE_PATH = "/api/v1/accounts/{account_id}/cycle-payments/{year}/{month}"
CYCLE_ARGS = "Owner or admin; a credit card account, a current or future cycle (year and month of its close)."

register(route_tool(
    name="cycle_payments_create", risk="sensitive",
    router=CYCLES, method="POST", path=CYCLE_PATH,
    fields=("amount",), required=("amount",), product_area=None, min_role=Role.ADMIN,
    description=f"Set the payment amount for a credit card cycle that has none. {CYCLE_ARGS} {CONFIRM}",
    preview=_cycle_create_preview,
))
register(route_tool(
    name="cycle_payments_upsert", risk="sensitive",
    router=CYCLES, method="PUT", path=CYCLE_PATH,
    fields=("amount",), required=("amount",), product_area=None, min_role=Role.ADMIN,
    description=f"Set or change the payment amount for a credit card cycle. {CYCLE_ARGS} {CONFIRM}",
    preview=_cycle_upsert_preview,
))
register(route_tool(
    name="cycle_payments_delete", risk="sensitive",
    router=CYCLES, method="DELETE", path=CYCLE_PATH,
    fields=(), product_area=None, min_role=Role.ADMIN,
    description=f"Remove the payment amount set for a credit card cycle. {CYCLE_ARGS} {CONFIRM}",
    preview=_cycle_delete_preview,
))
