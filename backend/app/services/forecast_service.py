"""Forecast service — compute projected month-end from executed + pending + recurring.

Forecast = Settled (what happened) + Pending (committed but not settled) + Upcoming Recurring (will be generated)

This gives the user a complete picture of where the month is heading.

⚠ **The "executed" half of this file lives in ``spending_service``** (TBD-221),
and this module is a CALLER of it — the period window, the per-category
executed rollup and the category-name lookup all come from there. The direction
is forced: spending-by-category is a historical-actuals surface that must stay
reachable when an org switches Forecast off (TBD-197), so it cannot depend on
this module. Do not copy any of those three back in; two copies drift silently
because each surface's own tests keep passing.
"""

import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services import spending_service
from app.services.date_utils import occurrences_in_window
from app.services.recurring_filters import active_series_filter, remaining_occurrences
from app.services.transaction_filters import (
    effective_period_date_expr,
    reportable_transaction_filter,
)


async def compute_forecast(
    db: AsyncSession,
    org_id: int,
    period_start: datetime.date | None = None,
    *,
    today: datetime.date | None = None,
) -> dict:
    """Compute the full forecast for a billing period.

    ``today`` is keyword-only and injectable because the period window floors
    at the wall clock. It is resolved ONCE here and consumed at exactly ONE
    site: the ``spending_service.resolve_spend_window`` call below, which takes
    it as a REQUIRED keyword and itself spends it at exactly three places —
    the two ``get_current_period`` calls that may auto-create the period this
    function then computes over (TBD-297), and the ``period_spend_window_end``
    call that derives ``window_end``.

    ⚠ That count was "exactly ONE site" until TBD-297, then "THREE" until
    TBD-221 moved the derivation into ``spending_service``; the number has
    always been the wrong thing to memorise. What is invariant is the RULE:
    the clock is read once per computation and travels as an argument
    thereafter. Deleting a ``today=`` anywhere on that path is not removing a
    redundancy — without them an org with no open row gets its first period
    anchored by a second, independent clock. Everything downstream, including
    the whole recurring projection, is bound to ``window_end`` and never
    re-reads the clock: two independent clock reads inside one computation is
    the straddle trap TBD-240 D6 exists to prevent, and
    ``test_forecast_parity_after_generate`` asserts a conservation invariant
    ACROSS two calls that a mid-computation tick would break.

    Signature note: ``period_start`` stays positional-with-default and ``today``
    stays keyword-only. ``test_ai_forecast_refine_service``'s fakes implement
    ``(db, org_id, period_start=None)`` and are never called with ``today=``.

    Returns:
        executed_income: sum of settled income
        executed_expense: sum of settled expenses
        pending_income: sum of pending income
        pending_expense: sum of pending expenses
        recurring_income: projected income from recurring templates
        recurring_expense: projected expenses from recurring templates
        forecast_income: executed + pending + recurring income
        forecast_expense: executed + pending + recurring expense
        forecast_net: forecast_income - forecast_expense
        executed_net: executed_income - executed_expense
        categories: per-category breakdown with executed + forecast
    """
    # Clock first (TBD-297): `get_current_period` auto-creates when the org has
    # no open row, and resolving `today` AFTER that call left the injected clock
    # governing every part of this computation except the anchor of the period
    # it computes over.
    today = today if today is not None else datetime.date.today()

    # ── The period's ONE window (TBD-243), derived ONCE by the shared helper ─
    # `window_end` bounds EVERY backward sum and EVERY forward projection
    # horizon in this function, and it is the SAME derivation
    # `GET /api/v1/transactions/spending-by-category` uses — shared, not
    # copied, so the two surfaces cannot report the same period with two
    # different ends. `spending_service.resolve_spend_window` carries the full
    # rationale (closed rows verbatim, the roster-tail calendar fallback, why
    # there is exactly one window and not two).
    p_start, window_end = await spending_service.resolve_spend_window(
        db, org_id, period_start, today=today
    )

    # ── Executed (settled) — uses settled_date for period assignment ─────
    # Transactions count against the period in which they settled,
    # not when the purchase happened (important for CC late settlements).
    #
    # Transfer halves are persisted as type=income/expense with a non-null
    # linked_transaction_id; excluding them keeps executed/pending totals
    # aligned with the dashboard client-side aggregates and with how users
    # think about real income vs. real spending.
    executed_income = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.INCOME,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.settled_date >= p_start,
            Transaction.settled_date <= window_end,
            reportable_transaction_filter(),
        )
    ) or Decimal("0")

    executed_expense = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.settled_date >= p_start,
            Transaction.settled_date <= window_end,
            reportable_transaction_filter(),
        )
    ) or Decimal("0")

    # ── Pending — buckets by effective settled date (the settled-date
    # estimate when set, else purchase date) so pending rows fall in the
    # period they're expected to clear, consistent with the list/reports.
    pending_income = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.INCOME,
            Transaction.status == TransactionStatus.PENDING,
            effective_period_date_expr() >= p_start,
            effective_period_date_expr() <= window_end,
            reportable_transaction_filter(),
        )
    ) or Decimal("0")

    pending_expense = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.PENDING,
            effective_period_date_expr() >= p_start,
            effective_period_date_expr() <= window_end,
            reportable_transaction_filter(),
        )
    ) or Decimal("0")

    # ── Upcoming recurring (not yet generated for this period) ────────────
    # The invariant (TBD-260): `recurring_*` projects exactly the occurrences
    # of each active template that fall in `[p_start, window_end]` and have NOT
    # already been materialised; `pending_*`/`executed_*` count exactly the
    # materialised ones. The two sets partition the same occurrence grid, so
    # `generate_due_transactions` moves an amount between buckets and never
    # into or out of the total.
    #
    # There is deliberately NO clock predicate here. The bound is on the
    # OCCURRENCE, not on `next_due_date`: `next_due_date` is a FRONTIER (the
    # next un-materialised occurrence), not an occurrence date, so a template
    # whose frontier sits before `p_start` still has occurrences inside the
    # window — and `generate_due_transactions`, whose catch-up loop has no
    # lower bound at all, materialises every one of them. Gating the template
    # out by its stale frontier (`next_due_date > today`, which is what shipped,
    # or `next_due_date >= p_start`) drops real in-window obligations and
    # `forecast_expense` then moves the moment the scheduler ticks.
    # `specs/2026-07-30-forecast-overdue-recurring-design.md` traces all four
    # candidate bounds.
    #
    # TBD-275: ``active_series_filter()`` replaces the bare ``is_active`` test.
    # An exhausted instalment series keeps ``is_active = True`` (exhaustion is
    # derived, never written -- see ``recurring_filters``), so ``is_active``
    # alone would project a finished 12-month plan forever while generation
    # creates nothing. That is the TBD-260 defect with the sign flipped: the
    # projection stays high and never reconciles.
    result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.org_id == org_id,
            active_series_filter(),
            RecurringTransaction.next_due_date <= window_end,
        )
    )
    recurring_items = list(result.scalars().all())

    # Anti-double-count probe. The predicate is generation's OWN create-
    # condition, negated: `generate_due_transactions` matches on
    # (org_id, recurring_id, date) with NO status, NO reportability and NO
    # effective-date term, and takes the `exists` branch — skip, advance.
    # This probe must therefore ask exactly one question: "does a row for
    # this occurrence already exist?" Narrowing it (a reportable filter, or
    # bounding on effective_period_date_expr) projects occurrences generation
    # will never materialise, and the value then moves on its own at the next
    # scheduler tick — the defect this ticket removes.
    # The date bounds below are a NARROWING ONLY: every projected occurrence
    # is inside [p_start, window_end] by construction, so the key equality
    # already carries the semantics. Do not read meaning into them.
    materialised: set[tuple[int, datetime.date]] = set()
    if recurring_items:
        rows = await db.execute(
            select(Transaction.recurring_id, Transaction.date).where(
                Transaction.org_id == org_id,
                Transaction.recurring_id.in_([r.id for r in recurring_items]),
                Transaction.date >= p_start,
                Transaction.date <= window_end,
            )
        )
        materialised = {(rid, d) for rid, d in rows.all()}

    recurring_income = Decimal("0")
    recurring_expense = Decimal("0")
    # Populated by the SAME walk as the totals below. Splitting them into two
    # loops is how the breakdown and the totals came to disagree on suppressed
    # occurrences; `ai_forecast_refine_service` reads this breakdown as the
    # baseline it hands the model, and F3 asserts it sums to the totals.
    cat_recurring: dict[int, Decimal] = {}

    for r in recurring_items:
        # ``budget`` is spent by the fast-forward as well as the collect loop.
        # A series whose frontier sits before ``p_start`` burns instalments
        # just getting to the window, because generation's catch-up loop --
        # which has no lower bound -- materialises every one of those
        # occurrences and increments the same counter. Budgeting only what is
        # collected would project obligations generation will never create.
        for d in occurrences_in_window(
            r.next_due_date, r.frequency, p_start, window_end,
            budget=remaining_occurrences(r),
        ):
            if (r.id, d) in materialised:
                continue
            if r.type == "income":
                recurring_income += r.amount
            else:
                recurring_expense += r.amount
                cat_recurring[r.category_id] = (
                    cat_recurring.get(r.category_id, Decimal("0")) + r.amount
                )

    # ── Per-category breakdown ────────────────────────────────────────────
    # Executed by category. THE SAME CALL the ungated spending-by-category
    # endpoint makes — that is the point of TBD-221, and the reason Forecast
    # by Category matches the dashboard donut is now that they are one query
    # rather than two that agree. Do not inline it back: a copy here would
    # drift from the donut with both surfaces' tests still green.
    cat_executed = await spending_service.executed_expense_by_category(
        db, org_id, p_start, window_end
    )

    # Pending by category
    cat_pend_result = await db.execute(
        select(
            Transaction.category_id,
            func.sum(Transaction.amount),
        ).where(
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.PENDING,
            effective_period_date_expr() >= p_start,
            effective_period_date_expr() <= window_end,
            reportable_transaction_filter(),
        ).group_by(Transaction.category_id)
    )
    cat_pending = {row[0]: Decimal(str(row[1])) for row in cat_pend_result.all()}

    # `cat_recurring` was built above, inside the one occurrence walk that
    # produced `recurring_expense`. Do not re-walk here.

    # Merge all category IDs
    all_cat_ids = set(cat_executed.keys()) | set(cat_pending.keys()) | set(cat_recurring.keys())

    # Get category names. Note the id set is the UNION — a category reached
    # only through `cat_recurring` or `cat_pending` has no executed row, so
    # this lookup must not be folded into the executed rollup above.
    cat_names = await spending_service.load_category_meta(db, org_id, all_cat_ids)

    categories = []
    for cid in sorted(all_cat_ids):
        ex = cat_executed.get(cid, Decimal("0"))
        pe = cat_pending.get(cid, Decimal("0"))
        rc = cat_recurring.get(cid, Decimal("0"))
        info = cat_names.get(cid, {"name": "Unknown", "parent_id": None})
        categories.append({
            "category_id": cid,
            "category_name": info["name"],
            "parent_id": info["parent_id"],
            "executed": str(ex),
            "pending": str(pe),
            "recurring": str(rc),
            "forecast": str(ex + pe + rc),
        })

    # ── Totals ────────────────────────────────────────────────────────────
    forecast_income = executed_income + pending_income + recurring_income
    forecast_expense = executed_expense + pending_expense + recurring_expense

    return {
        "period_start": p_start.isoformat(),
        "period_end": window_end.isoformat(),
        "executed_income": str(executed_income),
        "executed_expense": str(executed_expense),
        "executed_net": str(executed_income - executed_expense),
        "pending_income": str(pending_income),
        "pending_expense": str(pending_expense),
        "recurring_income": str(recurring_income),
        "recurring_expense": str(recurring_expense),
        "forecast_income": str(forecast_income),
        "forecast_expense": str(forecast_expense),
        "forecast_net": str(forecast_income - forecast_expense),
        "categories": categories,
    }
