"""The v1 agent tools: six reads (TBD-559), ``budgets_update_amount`` (TBD-577)
and ``transactions_set_category`` (TBD-580).

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
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.agent.registry import Change, Preview, ToolContext, ToolError, ToolSpec, register
from app.models import Budget, Category, Organization, Transaction
from app.models.category_rule import CategoryRule
from app.models.user import Role
from app.routers.accounts import list_accounts
from app.routers.categories import list_categories
from app.schemas.budget import BudgetUpdate
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
from app.services.transaction_filters import is_reportable_transaction

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


class BudgetAmountArgs(_Args):
    # Mirrors ``PUT /budgets/{budget_id}`` (``BudgetUpdate.amount``: gt=0) and
    # is stricter where the column is (Numeric(12, 2)): fenced by F-R4.
    budget_id: int
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


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


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


async def _budgets_update_amount_preview(ctx: ToolContext, args: BudgetAmountArgs) -> Preview:
    found = (await ctx.db.execute(
        select(Budget, Category.name)
        .join(Category, Category.id == Budget.category_id)
        .where(Budget.id == args.budget_id, Budget.org_id == ctx.org_id)
        .execution_options(populate_existing=True)
    )).first()
    if found is None:
        raise NotFoundError("Budget")
    budget, category_name = found
    if budget.amount == args.amount:
        raise ToolError("no_change", "the budget already has this amount")
    org = await ctx.db.get(Organization, ctx.org_id)
    currency = org.primary_currency or (
        await currency_service.resolve_currency_scope(ctx.db, org_id=ctx.org_id)
    ).get("currency")
    before, after = _money(budget.amount), _money(args.amount)
    return Preview(
        summary=f"Change the amount of budget {budget.id} from {before} to {after}"
        + (f" {currency}" if currency else ""),
        changes=[Change("budgets", budget.id, "amount", before, after, currency)],
        context={"category_name": category_name, "period_start": budget.period_start.isoformat()},
    )


async def _budgets_update_amount_execute(ctx: ToolContext, args: BudgetAmountArgs) -> dict:
    out = await budget_service.update_budget(
        ctx.db, ctx.org_id, args.budget_id, BudgetUpdate(amount=args.amount)
    )
    return out.model_dump(mode="json")


register(ToolSpec(
    name="budgets_update_amount", risk="write", args=BudgetAmountArgs,
    product_area=Feature.BUDGETS, min_role=Role.MEMBER,
    mirrors_route=("PUT", "/api/v1/budgets/{budget_id}"),
    description=(
        "Change the amount of one existing budget. Returns a preview to confirm; "
        "nothing changes until it is confirmed."
    ),
    preview=_budgets_update_amount_preview, execute=_budgets_update_amount_execute,
))


# ── transactions_set_category ─────────────────────────────────────────────
#
# ``update_transaction`` also writes rows this preview could not list: the
# category is mirrored onto a transfer partner, and onto the recurring template
# plus every pending sibling. v1 refuses any row with a link or a series, at
# preview, at confirm's re-preview, and once more under the row lock in
# execute (the re-preview and the execute are two transactions).
#
# The one derived write it may do is the org's category rule for the row's
# normalized description, listed with the token as its id: editing the
# description changes the token, so the confirm re-preview goes stale. An
# ``agent:auto`` principal learns no rule and lists none (``ctx.auto``).


async def _load_editable(ctx: ToolContext, transaction_id: int, *, lock: bool) -> Transaction:
    q = (
        select(Transaction)
        .where(Transaction.id == transaction_id, Transaction.org_id == ctx.org_id)
        .execution_options(populate_existing=True)
    )
    tx = await ctx.db.scalar(q.with_for_update() if lock else q)
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


async def _set_category_preview(ctx: ToolContext, args: SetCategoryArgs) -> Preview:
    tx = await _load_editable(ctx, args.transaction_id, lock=False)
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
        rule_category = await ctx.db.scalar(
            select(CategoryRule.category_id)
            .where(CategoryRule.org_id == ctx.org_id, CategoryRule.normalized_token == token)
            .execution_options(populate_existing=True)
        )
        changes.append(Change("category_rules", token, "category_id", rule_category, args.category_id))
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
    tx = await _load_editable(ctx, args.transaction_id, lock=True)
    token = _rule_token(ctx, tx)
    out = await transaction_service.update_transaction(
        ctx.db, ctx.org_id, args.transaction_id,
        TransactionUpdate(category_id=args.category_id), learn=not ctx.auto,
    )
    result = transaction_service.to_response(out).model_dump(mode="json")
    # ``update_transaction`` swallows a failed rule write; report what landed.
    result["rule_learned"] = bool(token) and await ctx.db.scalar(
        select(CategoryRule.category_id)
        .where(CategoryRule.org_id == ctx.org_id, CategoryRule.normalized_token == token)
        .execution_options(populate_existing=True)
    ) == args.category_id
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
