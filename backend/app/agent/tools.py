"""The agent tools: six reads (TBD-559), ``budgets_update_amount`` (TBD-577)
and ``transactions_set_category`` (TBD-580); ``transactions_get`` and
``transactions_update`` (TBD-664). ``budgets_update_amount`` and the two
TBD-664 tools are route-bound (``app.agent.route_tool``): they run the route
handler itself.

Each tool is a thin adapter over the code its mirrored REST route runs, scoped
by ``ctx``. ``accounts_list`` and ``categories_list`` call the route handlers
themselves: those routes have no service layer, and calling the handler keeps
the result byte-for-byte what REST returns instead of a second copy of the query.

⚠ The three period-based tools resolve the period READ-ONLY before calling the
service and always pass it explicitly. ``list_budgets``, ``compute_forecast``
and ``compute_spending_by_category`` all reach ``get_current_period`` when
handed no period (or an unknown one), and that AUTO-CREATES and COMMITS a
``BillingPeriod``: a read tool must never write.

No module here may cause network egress (fenced by
``tests/agent/test_registry_fences.py``).
"""
from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.agent.registry import Change, Preview, ToolContext, ToolError, ToolSpec, register
from app.agent.route_tool import route_tool
from app.models import Budget, Category, Organization, Transaction
from app.models.transaction import TransactionStatus
from app.models.category_rule import CategoryRule
from app.models.user import Role
from app.routers.accounts import list_accounts
from app.routers import budgets as budgets_router
from app.routers import transactions as transactions_router
from app.routers.categories import list_categories
from app.schemas.forecast import ForecastResponse
from app.schemas.transaction import SpendingByCategoryResponse, TransactionUpdate
from app.services import (
    billing_service,
    budget_service,
    currency_service,
    forecast_service,
    spending_service,
    transaction_service,
)
from app.services.category_rules_service import normalize_description
from app.services.exceptions import NotFoundError, ValidationError
from app.services.feature_gate import Feature
from app.services.transaction_filters import REVERTED_RECONCILIATION_STATES, is_reportable_transaction

MAX_SEARCH_DAYS = 366
MAX_PAGE = 50


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class PeriodArgs(_Args):
    period_start: datetime.date | None = Field(
        default=None,
        description="Start date of a billing period; omit for the current open period.",
    )


class SetCategoryArgs(_Args):
    # Mirrors ``PUT /transactions/{transaction_id}`` with only ``category_id``.
    transaction_id: int
    category_id: int


class TransactionSearchArgs(_Args):
    date_from: datetime.date
    date_to: datetime.date
    category_match: Literal["exact", "subtree"] = Field(
        description=(
            "'subtree': a master category id also matches its subcategories' rows. "
            "'exact': only rows whose own category is the given id."
        ),
    )
    account_id: list[int] | None = Field(default=None, max_length=MAX_PAGE)
    category_id: list[int] | None = Field(default=None, max_length=MAX_PAGE)
    type: Literal["income", "expense"] | None = None
    status: Literal["settled", "pending"] | None = None
    search: str | None = Field(default=None, max_length=200)
    reportable: bool = False
    limit: int = Field(default=MAX_PAGE, ge=1, le=MAX_PAGE)
    offset: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def _bounded_range(self) -> TransactionSearchArgs:
        if self.date_to < self.date_from:
            raise ValueError("date_to is before date_from")
        if (self.date_to - self.date_from).days >= MAX_SEARCH_DAYS:
            raise ValueError(f"date range is limited to {MAX_SEARCH_DAYS} days")
        return self


async def _resolve_period_start(
    ctx: ToolContext, requested: datetime.date | None
) -> datetime.date:
    """The start of an EXISTING period, found without writing anything.

    The services look the period up again by this start. That second read is
    in the same transaction, so on MySQL's REPEATABLE READ it sees the same
    snapshot and cannot miss the row this one found; a miss there is what
    would reach the auto-create fallback.
    """
    if requested is not None:
        if await billing_service._find_period_by_start(ctx.db, ctx.org_id, requested) is None:
            raise ToolError("period_not_found", str(requested))
        return requested
    period = await billing_service.find_open_period(ctx.db, ctx.org_id)
    if period is None:
        raise ToolError("no_open_period", "the organization has no open billing period")
    return period.start_date


async def _accounts_list(ctx: ToolContext, args: NoArgs) -> list[dict]:
    rows = await list_accounts(current_user=ctx.user, db=ctx.db)
    return [r.model_dump(mode="json") for r in rows]


async def _categories_list(ctx: ToolContext, args: NoArgs) -> list[dict]:
    rows = await list_categories(current_user=ctx.user, db=ctx.db)
    return [r.model_dump(mode="json") for r in rows]


async def _budgets_list(ctx: ToolContext, args: PeriodArgs) -> dict:
    start = await _resolve_period_start(ctx, args.period_start)
    budgets = await budget_service.list_budgets(ctx.db, ctx.org_id, period_start=start)
    scope = await currency_service.resolve_currency_scope(ctx.db, org_id=ctx.org_id)
    return {
        "period_start": start.isoformat(),
        "currency_scope": scope,
        "budgets": [b.model_dump(mode="json") for b in budgets],
    }


async def _spending_by_category(ctx: ToolContext, args: PeriodArgs) -> dict:
    start = await _resolve_period_start(ctx, args.period_start)
    out = await spending_service.compute_spending_by_category(ctx.db, ctx.org_id, period_start=start)
    return SpendingByCategoryResponse.model_validate(out).model_dump(mode="json")


async def _forecast_get(ctx: ToolContext, args: PeriodArgs) -> dict:
    start = await _resolve_period_start(ctx, args.period_start)
    out = await forecast_service.compute_forecast(ctx.db, ctx.org_id, period_start=start)
    return ForecastResponse.model_validate(out).model_dump(mode="json")


async def _transactions_search(ctx: ToolContext, args: TransactionSearchArgs) -> dict:
    items, total = await transaction_service.list_transactions(
        ctx.db, ctx.org_id,
        account_id=args.account_id,
        category_id=args.category_id,
        tx_type=args.type,
        status=args.status,
        date_from=args.date_from,
        date_to=args.date_to,
        search=args.search,
        category_match=args.category_match,
        reportable=args.reportable,
        limit=args.limit,
        offset=args.offset,
    )
    rows = []
    for tx in items:
        row = transaction_service.to_response(tx).model_dump(mode="json")
        row["currency"] = tx.account.currency if tx.account else None
        rows.append(row)
    return {"items": rows, "total": total, "limit": args.limit, "offset": args.offset}


register(ToolSpec(
    name="accounts_list", risk="read", args=NoArgs, product_area=None,
    min_role=Role.MEMBER, mirrors_route=("GET", "/api/v1/accounts"),
    description="List the organization's accounts with balances and currencies.",
    run=_accounts_list,
))
register(ToolSpec(
    name="categories_list", risk="read", args=NoArgs, product_area=None,
    min_role=Role.MEMBER, mirrors_route=("GET", "/api/v1/categories"),
    description="List the organization's categories (masters and subcategories).",
    run=_categories_list,
))
register(ToolSpec(
    name="budgets_list", risk="read", args=PeriodArgs, product_area=Feature.BUDGETS,
    min_role=Role.MEMBER, mirrors_route=("GET", "/api/v1/budgets"),
    description="List budgets with spend for a billing period (default: the open period).",
    run=_budgets_list,
))
register(ToolSpec(
    name="transactions_search", risk="read", args=TransactionSearchArgs, product_area=None,
    min_role=Role.MEMBER, mirrors_route=("GET", "/api/v1/transactions"),
    description=(
        f"Search transactions in a date range of at most {MAX_SEARCH_DAYS} days, "
        f"{MAX_PAGE} rows per page."
    ),
    run=_transactions_search,
))
register(ToolSpec(
    name="spending_by_category", risk="read", args=PeriodArgs, product_area=None,
    min_role=Role.MEMBER,
    mirrors_route=("GET", "/api/v1/transactions/spending-by-category"),
    description="Settled expense per category for a billing period (default: the open period).",
    run=_spending_by_category,
))
register(ToolSpec(
    name="forecast_get", risk="read", args=PeriodArgs, product_area=Feature.FORECAST,
    min_role=Role.MEMBER, mirrors_route=("GET", "/api/v1/forecast"),
    description="Forecast income, expense and net for a billing period (default: the open period).",
    run=_forecast_get,
))


# ── budgets_update_amount (route-bound since TBD-664, same spec as v1) ────

async def _budget_present(ctx: ToolContext, budget: Budget, args, changes: list[Change]) -> Preview:
    category_name = await ctx.db.scalar(select(Category.name).where(Category.id == budget.category_id))
    org = await ctx.db.get(Organization, ctx.org_id)
    currency = org.primary_currency or (
        await currency_service.resolve_currency_scope(ctx.db, org_id=ctx.org_id)
    ).get("currency")
    [amount] = changes
    return Preview(
        summary=f"Change the amount of budget {budget.id} from {amount.before} to {amount.after}"
        + (f" {currency}" if currency else ""),
        changes=[Change("budgets", budget.id, "amount", amount.before, amount.after, currency)],
        context={"category_name": category_name, "period_start": budget.period_start.isoformat()},
    )


register(route_tool(
    name="budgets_update_amount", risk="write", args_name="BudgetAmountArgs",
    router=budgets_router.router, method="PUT", path="/api/v1/budgets/{budget_id}",
    fields=("amount",), required=("amount",),
    product_area=Feature.BUDGETS, min_role=Role.MEMBER,
    description=(
        "Change the amount of one existing budget. Returns a preview to confirm; "
        "nothing changes until it is confirmed."
    ),
    entity=Budget, present=_budget_present, no_change="the budget already has this amount",
))


# ── transactions_set_category ─────────────────────────────────────────────
#
# ``update_transaction`` also writes rows this preview could not list: the
# category is mirrored onto a transfer partner, and onto the recurring template
# plus every pending sibling. v1 refuses any row with a link or a series, at
# preview, at confirm's re-preview, and again in execute.
#
# The preview reads the row FOR UPDATE: in ``confirm`` nothing commits between
# the re-preview and the execute, so the row it fingerprinted (description,
# category, links) is the row ``update_transaction`` writes. Ceiling: the rule
# row is read unlocked, so a concurrent import re-learning the same token in
# that window makes the disclosed ``before`` stale (most-recent-wins anyway).
#
# The one derived write it may do is the org's category rule for the row's
# normalized description, listed with the token as its id: editing the
# description changes the token, so the confirm re-preview goes stale. The
# token is bank or user text, so the id is wrapped as untrusted. An
# ``agent:auto`` principal learns no rule and lists none (``ctx.auto``).


async def _load_editable(ctx: ToolContext, transaction_id: int) -> Transaction:
    tx = await ctx.db.scalar(
        select(Transaction)
        .where(Transaction.id == transaction_id, Transaction.org_id == ctx.org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if tx is None:
        raise NotFoundError("Transaction")
    if tx.recurring_id is not None:
        raise ToolError(
            "unsupported_in_v1", "a transaction in a recurring series cannot be recategorized here",
            data={"reason": "recurring_series"},
        )
    if tx.linked_transaction_id is not None:
        raise ToolError(
            "unsupported_in_v1", "a linked transaction cannot be recategorized here",
            data={"reason": "linked_transaction"},
        )
    if tx.is_manual_adjustment:
        raise ValidationError("Manual balance adjustments cannot be edited")
    return tx


def _rule_token(ctx: ToolContext, tx: Transaction) -> str:
    """The rule ``update_transaction`` would learn, or "" for none (the same
    gate it applies: reportable rows only, and never for an auto principal)."""
    if ctx.auto or not is_reportable_transaction(tx):
        return ""
    return normalize_description(tx.description)


async def _rule_category(ctx: ToolContext, token: str) -> int | None:
    return await ctx.db.scalar(
        select(CategoryRule.category_id)
        .where(CategoryRule.org_id == ctx.org_id, CategoryRule.normalized_token == token)
        .execution_options(populate_existing=True)
    )


async def _set_category_preview(ctx: ToolContext, args: SetCategoryArgs) -> Preview:
    tx = await _load_editable(ctx, args.transaction_id)
    if tx.category_id == args.category_id:
        raise ToolError("no_change", "the transaction already has this category")
    await transaction_service.validate_category_for_type(
        ctx.db, args.category_id, ctx.org_id, tx.type
    )
    names = dict((await ctx.db.execute(
        select(Category.id, Category.name).where(
            Category.org_id == ctx.org_id, Category.id.in_([tx.category_id, args.category_id])
        )
    )).all())
    changes = [Change("transactions", tx.id, "category_id", tx.category_id, args.category_id)]
    warnings = []
    token = _rule_token(ctx, tx)
    if token:
        changes.append(Change(
            "category_rules", {"untrusted": token}, "category_id",
            await _rule_category(ctx, token), args.category_id,
        ))
        warnings.append(
            "Also updates the organization's categorization rule for this description, "
            "which categorizes future imports."
        )
    return Preview(
        summary=(
            f"Change the category of transaction {tx.id} "
            f"from category {tx.category_id} to category {args.category_id}"
        ),
        changes=changes,
        warnings=warnings,
        context={
            "description": tx.description,
            "from": {"category_name": names.get(tx.category_id)},
            "to": {"category_name": names.get(args.category_id)},
        },
    )


async def _set_category_execute(ctx: ToolContext, args: SetCategoryArgs) -> dict:
    tx = await _load_editable(ctx, args.transaction_id)
    token = _rule_token(ctx, tx)
    out = await transaction_service.update_transaction(
        ctx.db, ctx.org_id, args.transaction_id,
        TransactionUpdate(category_id=args.category_id), learn=not ctx.auto,
    )
    result = transaction_service.to_response(out).model_dump(mode="json")
    # ``update_transaction`` swallows a failed rule write: report whether the
    # rule now maps to the target (True too if it already did and the write failed).
    result["rule_learned"] = bool(token) and await _rule_category(ctx, token) == args.category_id
    return result


register(ToolSpec(
    name="transactions_set_category", risk="write", args=SetCategoryArgs,
    product_area=None, min_role=Role.MEMBER,
    mirrors_route=("PUT", "/api/v1/transactions/{transaction_id}"),
    description=(
        "Change the category of one transaction. Transactions in a recurring series "
        "or linked to another transaction are refused. Returns a preview to confirm; "
        "nothing changes until it is confirmed."
    ),
    preview=_set_category_preview, execute=_set_category_execute,
))


# ── transactions_get / transactions_update (route-bound, TBD-664) ─────────

register(route_tool(
    name="transactions_get", risk="read",
    router=transactions_router.router, method="GET", path="/api/v1/transactions/{transaction_id}",
    fields=(), product_area=None, min_role=Role.MEMBER,
    description="Read one transaction by id, with its tags.",
))


# ``transactions_update`` edits the fields that move no money: description,
# date and the settlement date. Amount, account, type and status move
# ``accounts.balance`` and are a ``sensitive`` tool (PR B); the category has its
# own tool (it learns a categorization rule). Its preview refuses what
# ``update_transaction`` would write beyond the row or silently ignore:
# a description edit on a recurring row (it renames the template and every
# pending sibling), clearing a SETTLED row's settlement date (the service keeps
# it), and a date pair out of order or a skipped occurrence's date (which the
# service refuses only at execute). A transfer leg is fine: none of these fields is mirrored to the
# partner, and the balance revert/apply on its own account nets to zero.

async def _tx_update_check(ctx: ToolContext, tx: Transaction, args) -> None:
    sent = args.model_fields_set
    if tx.is_manual_adjustment:
        raise ValidationError("Manual balance adjustments cannot be edited")
    if "description" in sent and tx.recurring_id is not None and args.description != tx.description:
        raise ToolError(
            "unsupported_in_v1", "the description of a transaction in a recurring series cannot be edited here",
            data={"reason": "recurring_series"},
        )
    date = args.date if "date" in sent else tx.date
    if (
        "date" in sent and date != tx.date and tx.recurring_id is not None
        and tx.reconciliation_state in REVERTED_RECONCILIATION_STATES
    ):
        raise ValidationError(f"The date of a {tx.reconciliation_state} transaction cannot be changed")
    settled = args.settled_date if "settled_date" in sent else tx.settled_date
    if "settled_date" in sent and settled is None and tx.status == TransactionStatus.SETTLED:
        raise ValidationError("A settled transaction keeps its settlement date")
    if settled is not None and settled < date:
        raise ValidationError("settled_date must be on or after date")


register(route_tool(
    name="transactions_update", risk="write",
    router=transactions_router.router, method="PUT", path="/api/v1/transactions/{transaction_id}",
    fields=("description", "date", "settled_date"), nullable=("settled_date",),
    product_area=None, min_role=Role.MEMBER,
    description=(
        "Edit the description, date or settlement date of one transaction (for a pending "
        "transaction the settlement date is the expected one; null clears it). Use "
        "transactions_set_category for the category. Returns a preview to confirm; nothing "
        "changes until it is confirmed."
    ),
    entity=Transaction, check=_tx_update_check,
    no_change="the transaction already has these values",
))
