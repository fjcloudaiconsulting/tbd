"""The v1 read tools (TBD-559).

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

from app.agent.registry import ToolContext, ToolError, ToolSpec, register
from app.models.user import Role
from app.routers.accounts import list_accounts
from app.routers.categories import list_categories
from app.schemas.forecast import ForecastResponse
from app.schemas.transaction import SpendingByCategoryResponse
from app.services import (
    billing_service,
    budget_service,
    currency_service,
    forecast_service,
    spending_service,
    transaction_service,
)
from app.services.feature_gate import Feature

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
