"""Historical spending actuals, per category — the SOURCE the forecast reads.

⚠⚠ **DEPENDENCY DIRECTION: ``forecast_service`` imports THIS module, never the
reverse.** ⚠⚠

Spending-by-Category is a **historical actuals** rollup: SETTLED, reportable
``EXPENSE`` rows inside a billing period's spend window. A forecast is that
same figure plus projections — but the actuals half does not become a Forecast
feature by being one of a forecast's addends, and TBD-197 gives every org a
Forecast opt-out that 404s ``GET /api/v1/forecast``. While these queries lived
only inside ``compute_forecast``, switching Forecast off took the dashboard
Spending-by-Category donut with it: a tile showing what already happened went
blank over a period holding real settled expense. TBD-221 extracts them here so
the actuals surface can be mounted UNGATED, on the transactions router.

Three rules this module exists to keep, each of which was a live hazard:

1. **EXPENSE only, ``executed`` only.** Both queries below filter
   ``TransactionType.EXPENSE``, and nothing here emits ``pending``,
   ``recurring`` or ``forecast``. Those are synthesized from templates that
   have not materialised — that IS the Forecast product, and re-exporting them
   from here would re-gate this surface by the back door. The cut line is
   happened-vs-projected. A later "it's only income, let's add it" turns one
   donut into a sum of two opposite signs.

2. **The window is an ARGUMENT, never re-derived.** ``compute_forecast``'s
   docstring records the invariant: ``today`` is resolved ONCE per computation
   because two independent clock reads inside one computation is the straddle
   trap TBD-240 D6 exists to prevent. So :func:`executed_expense_by_category`
   takes ``p_start`` and ``window_end`` as plain arguments and reads no clock at
   all, and :func:`resolve_spend_window` takes ``today`` as a REQUIRED
   keyword — there is deliberately no ``None`` default that would let a caller
   silently open a second clock read.

3. **One derivation, one query, two callers.** ``compute_forecast`` calls the
   very functions the endpoint calls. Copying either into a second place is how
   the two surfaces drift while both look right — and they would drift
   silently, because each one's own tests would keep passing.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import BillingPeriod
from app.models.category import Category
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services.billing_service import get_current_period, period_spend_window_end
from app.services.transaction_filters import reportable_transaction_filter


async def resolve_spend_window(
    db: AsyncSession,
    org_id: int,
    period_start: datetime.date | None,
    *,
    today: datetime.date,
) -> tuple[datetime.date, datetime.date]:
    """Resolve ``(p_start, window_end)`` — the period's ONE window (TBD-243).

    ``window_end`` bounds EVERY backward sum and EVERY forward projection
    horizon of whatever computes over this period. There is deliberately no
    second value and no second name: the backward sum and the forward
    projection are two halves of one total joined by a materialisation event,
    and ``generate_due_transactions`` materialises on ``current_cycle_window``,
    which is ROSTER-INDEPENDENT. Two windows would open a gap the
    materialisation window reaches into — an obligation in neither bucket
    before and one bucket after — and ``forecast_net`` would move with no user
    action. ``specs/2026-07-30-forecast-period-window-design.md`` §2 reproduces
    that break; §4 F4 fences it.

    Closed rows take ``end_date`` verbatim and are NEVER floored: flooring one
    would re-open reported history for every org (§4 F6). The ``None`` arm is
    the roster tail, where ``period_effective_end`` is genuinely unbounded; the
    calendar expression there is FORCED, not chosen — the ``while d <=
    window_end`` loops in ``forecast_service`` cannot terminate on ``None``, and
    it is what keeps ``period_end`` non-null in both response contracts (§4 F5).

    ``today`` is a REQUIRED keyword, not an injectable with a ``None`` default.
    Every caller has already resolved its own clock, and ``get_current_period``
    may AUTO-CREATE the period being computed over (TBD-297): a default here
    would let that anchor come from a second, independently-read clock, which
    is the straddle TBD-240 D6 exists to prevent. Both consumed sites are
    below, and there are exactly two.
    """
    if period_start:
        result = await db.execute(
            select(BillingPeriod).where(
                BillingPeriod.org_id == org_id,
                BillingPeriod.start_date == period_start,
            )
        )
        period = result.scalar_one_or_none()
        if period is None:
            period = await get_current_period(db, org_id, today=today)
    else:
        period = await get_current_period(db, org_id, today=today)

    p_start = period.start_date

    if period.end_date is not None:
        window_end = period.end_date
    else:
        derived = await period_spend_window_end(db, org_id, period, today=today)
        window_end = derived if derived is not None else (
            p_start + relativedelta(months=1) - datetime.timedelta(days=1)
        )
    return p_start, window_end


async def executed_expense_by_category(
    db: AsyncSession,
    org_id: int,
    p_start: datetime.date,
    window_end: datetime.date,
) -> dict[int, Decimal]:
    """SETTLED reportable EXPENSE in ``[p_start, window_end]``, grouped by the
    row's **own** ``category_id``.

    Buckets by ``settled_date``, not purchase date: a transaction counts against
    the period in which it settled (cash basis — important for credit-card late
    settlements).

    ``reportable_transaction_filter()`` — not a hand-rolled
    ``linked_transaction_id IS NULL`` — is load-bearing and is the whole of D1
    in ``specs/2026-08-05-spending-donut-server-rollup.md``. It drops transfer
    legs, manual balance adjustments, and SKIPPED / REJECTED reconciliation rows
    whose amount was already reverted from ``accounts.balance``.
    ``is_manual_adjustment`` is the only one of those on the wire, which makes
    "filter just that one" the half-fix that looks correct.

    Grouped by ID and never by name: ``categories`` carries no
    ``UNIQUE(org_id, name)``, so a master and its own subcategory can share a
    label. Name-grouping merges them into one slice a drilldown cannot open.
    The window is an argument; this function reads no clock — see the module
    docstring, rule 2.
    """
    result = await db.execute(
        select(
            Transaction.category_id,
            func.sum(Transaction.amount),
        ).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.settled_date >= p_start,
            Transaction.settled_date <= window_end,
            reportable_transaction_filter(),
        ).group_by(Transaction.category_id)
    )
    return {row[0]: Decimal(str(row[1])) for row in result.all()}


async def load_category_meta(
    db: AsyncSession, org_id: int, category_ids: set[int]
) -> dict[int, dict]:
    """Map ``category_id -> {"name", "parent_id"}`` for *category_ids*.

    Org-scoped, so a stale id from another tenant resolves to nothing rather
    than leaking a name. Callers supply the fallback for a missing id;
    ``compute_forecast`` uses ``"Unknown"``.
    """
    if not category_ids:
        return {}
    result = await db.execute(
        select(Category.id, Category.name, Category.parent_id).where(
            Category.id.in_(category_ids), Category.org_id == org_id
        )
    )
    return {row[0]: {"name": row[1], "parent_id": row[2]} for row in result.all()}


async def compute_spending_by_category(
    db: AsyncSession,
    org_id: int,
    period_start: datetime.date | None = None,
    *,
    today: datetime.date | None = None,
) -> dict:
    """The payload behind ``GET /api/v1/transactions/spending-by-category``.

    ``today`` is resolved ONCE, here, and passed down; nothing below reads the
    clock again (module docstring, rule 2).

    ``executed_expense`` is the SUM OF THE ROWS, not a second scalar query.
    That is deliberate: it makes the donut's centre figure equal the sum of its
    slices by construction rather than by two queries agreeing. The cross-check
    against ``/api/v1/forecast``'s independently-computed scalar is a test
    (F-B), which is where that comparison belongs.

    Money is string-serialised, matching ``compute_forecast``'s wire contract
    for the same numbers.
    """
    today = today if today is not None else datetime.date.today()
    p_start, window_end = await resolve_spend_window(
        db, org_id, period_start, today=today
    )

    by_category = await executed_expense_by_category(db, org_id, p_start, window_end)
    meta = await load_category_meta(db, org_id, set(by_category))

    categories = []
    for cid in sorted(by_category):
        info = meta.get(cid, {"name": "Unknown", "parent_id": None})
        categories.append({
            "category_id": cid,
            "category_name": info["name"],
            "parent_id": info["parent_id"],
            "executed": str(by_category[cid]),
        })

    executed_expense = sum(by_category.values(), Decimal("0"))

    return {
        "period_start": p_start.isoformat(),
        "period_end": window_end.isoformat(),
        "executed_expense": str(executed_expense),
        "categories": categories,
    }
