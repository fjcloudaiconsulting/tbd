"""Forecast plan service — editable plans for income/expense per billing period.

Users can create a plan for a billing period, auto-populate from recurring
templates and historical averages, then manually adjust. The plan tracks
actual vs planned for each line item.
"""

import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import BillingPeriod
from app.models.category import Category
from app.models.forecast_plan import (
    ForecastItemType,
    ForecastPlan,
    ForecastPlanItem,
    ItemSource,
    PlanStatus,
)
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.forecast_plan import (
    BulkUpsertRequest,
    ForecastPlanItemCreate,
    ForecastPlanItemResponse,
    ForecastPlanItemUpdate,
    ForecastPlanResponse,
)
from app.services import currency_service
from app.services.billing_service import period_spend_window_end, resolve_period
from app.services.date_utils import occurrences_in_window
from app.services.exceptions import NotFoundError, ValidationError
from app.services.recurring_filters import active_series_filter, remaining_occurrences
from app.services.settings_service import (
    FORECAST_GRANULARITY_SUBCATEGORY,
    get_forecast_input_granularity,
)
from app.services.transaction_filters import (
    effective_period_date_expr,
    org_currency_filter,
    reportable_transaction_filter,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get_or_create_plan_row(
    db: AsyncSession, org_id: int, period_id: int,
) -> ForecastPlan:
    """Get or create a plan row, handling concurrent insert races."""
    result = await db.execute(
        select(ForecastPlan).where(
            ForecastPlan.org_id == org_id,
            ForecastPlan.billing_period_id == period_id,
        )
    )
    plan = result.scalar_one_or_none()
    if plan is not None:
        return plan

    plan = ForecastPlan(
        org_id=org_id,
        billing_period_id=period_id,
        status=PlanStatus.DRAFT,
    )
    db.add(plan)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        result = await db.execute(
            select(ForecastPlan).where(
                ForecastPlan.org_id == org_id,
                ForecastPlan.billing_period_id == period_id,
            )
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            raise ValidationError("Failed to create forecast plan")
    return plan


def _require_draft(plan: ForecastPlan) -> None:
    """Raise if the plan is active (read-only)."""
    if plan.status == PlanStatus.ACTIVE:
        raise ValidationError("Cannot modify an active plan. Revert to draft first.")


async def _validate_category_granularity(
    db: AsyncSession, org_id: int, category_id: int,
    item_type: ForecastItemType | None = None,
    *, subcategory_mode: bool = False,
) -> None:
    """Validate that the category exists, belongs to the org, has the right
    granularity for the org's build mode, and (when ``item_type`` is
    supplied) has a compatible CategoryType.

    Granularity rule (spec R1, mode-aware):
      - master mode (default): item category must be a MASTER (parent_id is
        None). Subcategories are rejected — legacy behavior.
      - subcategory mode: item category may be a SUBCATEGORY or a MASTER.
        A master's own item and its sub items coexist and SUM (TBD-466).

    Compatibility rule (PR #144 review fix):
      - INCOME item  → category.type ∈ {INCOME, BOTH}
      - EXPENSE item → category.type ∈ {EXPENSE, BOTH}
    Mismatch raises ValidationError, mapped to HTTP 400 by app.main.
    """
    result = await db.execute(
        select(Category).where(Category.id == category_id, Category.org_id == org_id)
    )
    cat = result.scalar_one_or_none()
    if cat is None:
        raise ValidationError("Invalid category")
    if not subcategory_mode and cat.parent_id is not None:
        raise ValidationError(
            "Forecast plan items must use master categories, not subcategories"
        )
    if item_type is not None and not _category_type_matches(cat.type, item_type):
        raise ValidationError(
            f"Category type ({cat.type.value}) does not match "
            f"forecast item type ({item_type.value})"
        )


def _category_type_matches(
    cat_type, item_type: ForecastItemType,
) -> bool:
    """Compatibility check used by upsert_item / bulk_upsert. Mirrors the
    transaction-side helper but speaks ForecastItemType (which is income/
    expense only — there is no TRANSFER on plan items)."""
    from app.models.category import CategoryType
    if cat_type == CategoryType.BOTH:
        return True
    if item_type == ForecastItemType.INCOME:
        return cat_type == CategoryType.INCOME
    if item_type == ForecastItemType.EXPENSE:
        return cat_type == CategoryType.EXPENSE
    return False


async def _compute_actuals_batch(
    db: AsyncSession, org_id: int,
    items: list[ForecastPlanItem],
    period_start: datetime.date, period_end: datetime.date | None,
    currency_scope: dict | None = None,
) -> dict[tuple[int, str], Decimal]:
    """Compute actual amounts for all plan items in two queries (income + expense).

    Returns a dict keyed by (category_id, type_value) → actual amount.

    Each transaction lands in exactly ONE bucket (TBD-466). A row whose own
    (category, type) has an item stays there; otherwise it rolls up to its
    parent's (category, type). So a master's own item takes spend on the
    master plus spend on subs that have no item OF THAT TYPE. Keying by
    type matters: a both-typed sub with only an INCOME item must still roll
    its EXPENSE spend up to the master.

    ``currency_scope`` (TBD-325 PR 2) is a PARAMETER, resolved once by the
    caller, for the same reason ``period_end`` is: this is the batched form and
    must not re-derive per-request facts. ``None`` means unscoped.
    """
    if not items:
        return {}

    item_cat_ids = {item.category_id for item in items}
    item_keys = {(item.category_id, item.type.value) for item in items}

    # Children of every item category, so a sub's spend can roll up.
    sub_result = await db.execute(
        select(Category.id, Category.parent_id).where(
            Category.parent_id.in_(item_cat_ids), Category.org_id == org_id
        )
    )
    parent_of: dict[int, int] = dict(sub_result.all())

    # ⚠ BOTH halves: ``parent_of`` alone drops the item categories' own spend.
    all_cat_ids = list(item_cat_ids | set(parent_of))

    # Single query: sum by (category_id, type) for all relevant categories.
    # Match budget_service rules so plan actuals and budget spent agree:
    #   - settled_date for period assignment (transactions count when they
    #     settle, not when purchased — a card swipe on Apr 28 that settles
    #     May 2 belongs to May, same as the budget treats it).
    #   - linked_transaction_id IS NULL excludes transfer halves so a
    #     budgeted category isn't inflated by an account-to-account move.
    q = select(
        Transaction.category_id,
        Transaction.type,
        func.coalesce(func.sum(Transaction.amount), 0),
    ).where(
        Transaction.org_id == org_id,
        Transaction.category_id.in_(all_cat_ids),
        Transaction.status == TransactionStatus.SETTLED,
        Transaction.settled_date >= period_start,
        Transaction.type.in_(["income", "expense"]),
        reportable_transaction_filter(),
        # TBD-325 PR 2. Currency lives only on ``accounts.currency`` --
        # ``transactions`` has no currency column -- so an unscoped actual is
        # EUR and USD added together and the variance against a
        # single-currency plan is meaningless. There is no FX in the system,
        # so scope rather than convert.
        org_currency_filter(org_id, currency_scope),
    )
    if period_end is not None:
        q = q.where(Transaction.settled_date <= period_end)
    q = q.group_by(Transaction.category_id, Transaction.type)

    result = await db.execute(q)

    actuals: dict[tuple[int, str], Decimal] = {}
    for cat_id, tx_type_raw, amount in result.all():
        tx_type = tx_type_raw.value if hasattr(tx_type_raw, "value") else str(tx_type_raw)
        key = (cat_id, tx_type)
        if key not in item_keys:
            key = (parent_of.get(cat_id, cat_id), tx_type)
        actuals[key] = actuals.get(key, Decimal("0")) + Decimal(str(amount))

    return actuals


async def _build_response(
    db: AsyncSession, org_id: int, plan: ForecastPlan,
    *, today: datetime.date | None = None,
    currency_scope: dict | None = None,
) -> ForecastPlanResponse:
    """Render a plan, resolving the currency scope for the whole response.

    TBD-325 PR 2: the scope is resolved HERE, not in each of the eleven entry
    points, because this function is the single funnel every one of them ends
    in and (per the ``today`` note below) fires exactly once per request. The
    ``currency_scope`` argument exists for the one caller that already needed
    the scope for its own aggregates -- ``populate_from_sources`` -- so that
    path pays for one resolution rather than two.
    """
    granularity = await get_forecast_input_granularity(db, org_id)
    period = plan.billing_period
    p_start = period.start_date
    p_end = period.end_date

    # ── TBD-240 §4 ────────────────────────────────────────────────────────
    #
    # TWO different ends live in this function, and they are not
    # interchangeable:
    #
    #   * `p_end` — the RAW stored `end_date`, `None` while the period is
    #     open. It is what the response emits, unchanged (D5): the derived end
    #     is for queries only, and `ForecastPlanResponse.period_end` keeps
    #     meaning "what the period row says".
    #   * `window_end` — the spend window. `_compute_actuals_batch` sums
    #     SETTLED rows, so leaving it on `p_end` let an open period's actuals
    #     run unbounded forward and swallow every future stub's window.
    #
    # ⚠ `populate_from_sources` in this same file uses a THIRD expression
    # (`p_start + 1 month - 1 day`) and still does — but NOT for the reason
    # this comment used to give, and NOT pending the follow-up it used to
    # point at. TBD-240 §7 named "separate the settled-sum bound from the
    # projection horizon" as that follow-up;
    # `specs/2026-07-30-forecast-period-window-design.md` (TBD-243)
    # **overrules it**. Splitting the two bounds opens a gap between them that
    # `generate_due_transactions`' roster-independent materialisation window
    # reaches into, so an obligation lands in neither bucket before
    # materialisation and one bucket after, and `forecast_net` moves with no
    # user action (§2 of that spec reproduces the break). `forecast_service`
    # and `account_balance_forecast_service` were moved to ONE window instead.
    #
    # `populate_from_sources` is deliberately NOT part of that change, for a
    # reason of its own: it is a WRITE path whose window feeds
    # `master_monthly["__current__"]`, which is divided by `len(months)` as ONE
    # month slot. Widening it to today on a lapsed org would count months of
    # spend as a single month and inflate every `planned_amount`. Weighting the
    # current slot by elapsed months is a separate design question — see that
    # spec §3.2, which also records why `:512`'s loop-termination guard must
    # not be rewired.
    #
    # TBD-764 note: the current period is no longer an averaging slot (it is
    # B' in `R + max(H', B')`), but the reason stands: widened, B' and R would
    # each hold months of spend and still be planned as one period.
    #
    # `today` is injectable (D6) because the window floors at the wall clock.
    # All eleven `_build_response` call sites pass it through from their own
    # entry point; none of them is a loop, so this fires once per request.
    window_end = await period_spend_window_end(db, org_id, period, today=today)

    if currency_scope is None:
        currency_scope = await currency_service.resolve_currency_scope(
            db, org_id=org_id
        )

    # Batch compute actuals for all items (2 queries instead of 2*N)
    actuals = await _compute_actuals_batch(
        db, org_id, plan.items, p_start, window_end, currency_scope
    )

    # Batch fetch category names (avoids lazy-load MissingGreenlet in async)
    cat_ids = {item.category_id for item in plan.items}
    cat_info: dict[int, tuple[str, int | None]] = {}
    if cat_ids:
        cat_result = await db.execute(
            select(Category.id, Category.name, Category.parent_id).where(
                Category.id.in_(cat_ids), Category.org_id == org_id
            )
        )
        for cid, cname, pid in cat_result.all():
            cat_info[cid] = (cname, pid)

    item_responses = []
    total_planned_income = Decimal("0")
    total_planned_expense = Decimal("0")
    total_actual_income = Decimal("0")
    total_actual_expense = Decimal("0")

    for item in plan.items:
        actual = actuals.get((item.category_id, item.type.value), Decimal("0"))
        cname, pid = cat_info.get(item.category_id, ("Unknown", None))
        resp = ForecastPlanItemResponse(
            id=item.id,
            plan_id=item.plan_id,
            category_id=item.category_id,
            category_name=cname,
            parent_id=pid,
            type=item.type.value,
            planned_amount=item.planned_amount,
            source=item.source.value,
            actual_amount=actual,
            variance=actual - item.planned_amount,
        )
        item_responses.append(resp)

        if item.type == ForecastItemType.INCOME:
            total_planned_income += item.planned_amount
            total_actual_income += actual
        else:
            total_planned_expense += item.planned_amount
            total_actual_expense += actual

    return ForecastPlanResponse(
        id=plan.id,
        billing_period_id=plan.billing_period_id,
        period_start=p_start,
        period_end=p_end,
        status=plan.status.value,
        total_planned_income=total_planned_income,
        total_planned_expense=total_planned_expense,
        total_actual_income=total_actual_income,
        total_actual_expense=total_actual_expense,
        forecast_input_granularity=granularity,
        items=item_responses,
    )


# ── CRUD ─────────────────────────────────────────────────────────────────────

async def get_or_create_plan(
    db: AsyncSession, org_id: int, period_start: datetime.date | None = None,
    *, today: datetime.date | None = None,
) -> ForecastPlanResponse:
    """Get existing plan for a period, or create a new draft.

    ``today`` is threaded into ``resolve_period`` (TBD-299): its fallback arm
    reaches ``get_current_period``, which auto-creates. Holding a resolved
    clock and dropping it there anchors the org's very first ``BillingPeriod``
    — a money-row anchor — to a second, independent clock read. Every
    ``resolve_period`` call in this module threads it for that reason.
    """
    period = await resolve_period(db, org_id, period_start, today=today)
    plan = await _get_or_create_plan_row(db, org_id, period.id)
    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan, today=today)


async def get_plan_for_period(
    db: AsyncSession, org_id: int, period_start: datetime.date | None = None,
    *, today: datetime.date | None = None,
) -> ForecastPlanResponse | None:
    """Read-only: return the plan for a period, or None if none exists.

    Unlike get_or_create_plan, this does NOT auto-create a draft when no
    plan is present. The Dashboard reads through this so that visiting it
    doesn't pollute the database with empty drafts. The Forecasts page
    keeps using get_or_create_plan because users land there to set one up.

    ⚠ "Side-effect-free" covers the *plan* row only. ``resolve_period``'s
    fallback still auto-creates the *period*, which is exactly why ``today``
    has to reach it (TBD-299).
    """
    period = await resolve_period(db, org_id, period_start, today=today)
    result = await db.execute(
        select(ForecastPlan).where(
            ForecastPlan.org_id == org_id,
            ForecastPlan.billing_period_id == period.id,
        )
    )
    plan = result.scalar_one_or_none()
    if plan is None:
        return None
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan, today=today)


# ── populate: history already covered by a template (TBD-764) ──────────────

_TEMPLATE_MATCH_TOLERANCE = Decimal("0.10")


def _template_occurrences_in_month(
    r: RecurringTransaction, year: int, month: int,
) -> int:
    """How many occurrences ``r``'s grid puts in calendar month ``year-month``.

    A CAP for the history match, not an occurrence walk: it is aligned on the
    template's own ``next_due_date`` (weekday, 14-day phase, quarter month,
    anniversary month) and never projects anything. A past month has no
    frontier to walk from, which is why ``occurrences_in_window`` is not used.
    """
    anchor = r.next_due_date
    first = datetime.date(year, month, 1)
    last = first + relativedelta(months=1) - datetime.timedelta(days=1)
    freq = r.frequency
    if freq == Frequency.WEEKLY:
        offset = (anchor.weekday() - first.weekday()) % 7
        return len(range(1 + offset, last.day + 1, 7))
    if freq == Frequency.BIWEEKLY:
        offset = (anchor - first).days % 14
        return len(range(1 + offset, last.day + 1, 14))
    months_apart = (year * 12 + month) - (anchor.year * 12 + anchor.month)
    if freq == Frequency.QUARTERLY:
        return 1 if months_apart % 3 == 0 else 0
    if freq == Frequency.YEARLY:
        return 1 if months_apart % 12 == 0 else 0
    return 1


def _drop_template_matches(
    rows: list[tuple],
    templates: list[RecurringTransaction],
    year: int, month: int,
    generated_counts: dict[tuple[int, tuple[int, int]], int],
) -> list[tuple]:
    """One DUE month of one group's history, minus the rows an active
    template already plans.

    ``rows`` start ``(category_id, account_id, amount, ...)``: SETTLED history
    with ``recurring_id`` NULL, bucketed by their own ``date`` (the template's
    grid is due dates; a month-end bill settling on the 2nd would otherwise
    put two rows in one month). The group key already fixed the type.

    A row matches a template on the same category and account with an amount
    within +/-10% of the template's (inclusive). Each template takes at most
    the occurrences its grid has that month MINUS the settled rows it
    generated itself that month (``generated_counts``, by
    ``(recurring_id, (year, month))``),
    closest amount first, so a second bill of a similar size stays in H'.

    Known limits, both erring toward planning MORE:
      - exact ``category_id`` and ``account_id``: a template on the master
        (Housing) misses rent imported on its sub (Housing > Rent), and a bill
        that moved accounts misses its older rows;
      - greedy, not optimal: templates go in id order and a matched row is
        gone for the next one, so A=100 can take a 105 row that B=110 needed,
        leaving 92 for nobody (an optimal assignment would match both).
    """
    matched: set[int] = set()
    for t in sorted(templates, key=lambda t: t.id):
        cap = _template_occurrences_in_month(t, year, month) - generated_counts.get(
            (t.id, (year, month)), 0
        )
        if cap <= 0:
            continue
        band = t.amount * _TEMPLATE_MATCH_TOLERANCE
        candidates = sorted(
            (abs(row[2] - t.amount), i)
            for i, row in enumerate(rows)
            if i not in matched
            and row[0] == t.category_id
            and row[1] == t.account_id
            and abs(row[2] - t.amount) <= band
        )
        matched.update(i for _, i in candidates[:cap])
    return [row for i, row in enumerate(rows) if i not in matched]


async def populate_from_sources(
    db: AsyncSession, org_id: int, period_start: datetime.date | None = None,
    *, today: datetime.date | None = None,
    pre_existing_sub_pairs: frozenset[tuple[int, str]] | None = None,
) -> ForecastPlanResponse:
    """Auto-populate plan items from recurring obligations, the period's own
    rows and 3-month history averages: per group, R + max(H', B') (TBD-764).

    Only adds items for categories not already in the plan.

    ``pre_existing_sub_pairs``: the (master_id, type) pairs that had a sub
    item BEFORE this run. ``refresh_from_sources`` passes the set it read
    before its own delete; by default it is read from the plan's current
    items. See the double-planning guard below.
    """
    period = await resolve_period(db, org_id, period_start, today=today)
    plan = await _get_or_create_plan_row(db, org_id, period.id)
    _require_draft(plan)
    await db.refresh(plan, ["billing_period", "items"])

    # Existing (category_id, type_str) combos — always use strings for consistency
    existing_keys: set[tuple[int, str]] = {(i.category_id, i.type.value) for i in plan.items}

    p_start = period.start_date
    p_end = period.end_date or (p_start + relativedelta(months=1) - datetime.timedelta(days=1))

    # TBD-325 PR 2: resolved ONCE for this entry point and threaded into ALL
    # FOUR sources below (history, recurring templates, current period, and
    # the period's generated rows -- TBD-764, which share the current-period
    # select and its clause) AND into ``_build_response`` at the bottom, so
    # the suggestion and the actuals it is rendered against are in the same
    # money.
    #
    # ⚠ "four", not "two". An earlier cut said two and scoped two; the
    # recurring source was the one it missed, and a persisted cross-currency
    # ``planned_amount`` is the result. If you add a fifth source, scope it.
    currency_scope = await currency_service.resolve_currency_scope(db, org_id=org_id)
    currency_clause = org_currency_filter(org_id, currency_scope)

    # ── Pre-fetch category → master mapping ──
    cat_result = await db.execute(
        select(Category.id, Category.parent_id).where(Category.org_id == org_id)
    )
    cat_to_master: dict[int, int] = {}
    for cid, pid in cat_result.all():
        cat_to_master[cid] = pid if pid else cid

    # ── Granularity-aware grouping (spec R2) ──
    # master mode (default): roll every transaction/recurring up to its
    #   master, producing master-level items (legacy behavior).
    # subcategory mode: group by the transaction's OWN subcategory, so a
    #   master's forecast is built from per-sub items. Transactions tagged
    #   directly to a master stay at the master id (they have no sub to fall
    #   into) and become the master's own item, which sums with its subs.
    granularity = await get_forecast_input_granularity(db, org_id)
    sub_mode = granularity == FORECAST_GRANULARITY_SUBCATEGORY

    def group_key(cat_id: int) -> int:
        if sub_mode:
            return cat_id
        return cat_to_master.get(cat_id, cat_id)

    # Double-planning guard for populate.
    #
    # Master mode: a master item rolls up ALL its subs' history, so next to a
    # sub item it would plan that sub's spend twice. Seeded from the
    # persisted plan and updated as candidates are added.
    #
    # Subcategory mode (TBD-466 review): a master item may be a master-mode
    # rollup carried across a mode switch or a copy, so it can already hold
    # its subs' spend. It claims its subtree for a type, and its subs are not
    # suggested, only if BOTH hold:
    #   (i)  the master item exists when this run starts (for refresh: it
    #        SURVIVED refresh's delete), and
    #   (ii) NO sub item under that (master, type) existed before the run
    #        (for refresh: before its delete). A master next to subs is the
    #        summing kind, and its subs must come back on refresh.
    # ``source`` cannot tell a rollup apart: edit, upsert, bulk and copy all
    # write MANUAL and refresh keeps MANUAL, so the rule ignores source.
    # Master candidates are never skipped here, and items created DURING this
    # run block nothing (they cover only their own leaf).
    #
    # Accepted costs, both erring toward the visible:
    #   - a hand-added master item with no subs stops populate from suggesting
    #     that master's subs. That plans less, and the user can still add the
    #     subs by hand.
    #   - Escape 1: a rolled-up M carried into subcategory mode stops claiming
    #     once the user hand-adds a sub under it; M renders as "(other)", so
    #     the user can see it.
    claimed_master: set[tuple[int, str]] = set()
    claimed_sub: set[tuple[int, str]] = set()
    for i in plan.items:
        m = cat_to_master.get(i.category_id, i.category_id)
        if i.category_id == m:
            claimed_master.add((m, i.type.value))
        else:
            claimed_sub.add((m, i.type.value))
    # Snapshots, before this run adds anything.
    seeded_master = frozenset(claimed_master)
    if pre_existing_sub_pairs is None:
        pre_existing_sub_pairs = frozenset(claimed_sub)

    def would_conflict(cat_id: int, type_value: str) -> bool:
        """True if adding (cat_id, type) would plan the same spend twice."""
        m = cat_to_master.get(cat_id, cat_id)
        key = (m, type_value)
        if sub_mode:
            return (
                cat_id != m
                and key in seeded_master
                and key not in pre_existing_sub_pairs
            )
        if cat_id == m:
            return key in claimed_sub
        return key in claimed_master

    def claim(cat_id: int, type_value: str) -> None:
        m = cat_to_master.get(cat_id, cat_id)
        key = (m, type_value)
        if cat_id == m:
            claimed_master.add(key)
        else:
            claimed_sub.add(key)

    # ── TBD-764: ONE computation per group ──
    #
    #     planned = R + max(H', B')
    #
    #   R  = the group's recurring obligations in the period: its materialised
    #        occurrences (rows with ``recurring_id`` set, any status, by
    #        effective date) plus each active template's occurrences that have
    #        no row yet.
    #   B' = settled + pending rows WITHOUT ``recurring_id`` in the period.
    #   H' = monthly average of SETTLED history rows without ``recurring_id``
    #        over the three months before the period.
    #
    # Every row is split on ``recurring_id``, so a template's money lives in R
    # and nowhere else, and a group holding rent AND variable spend plans both.
    # This replaced a template branch that claimed the whole group and made the
    # history pass skip it: a mixed group lost its variable spend and its
    # manual pending rows, and once generation pushed the template's frontier
    # past ``p_end`` the group fell through to the history pass, which averaged
    # its own rent occurrence in as one month slot.
    #
    # History paid BEFORE its template existed is the other half of the split.
    # Onboarding imports first and sets up templates second, and promoting a
    # row links that row only, so a bill's past is usually plain rows with
    # ``recurring_id`` NULL. Counted in H' next to the template's R they plan
    # the bill twice (rent 1500 -> 3000). So a NULL history row that looks like
    # one of the group's templates is matched out of H': see
    # ``_drop_template_matches``. What is left (variable spend) stays.
    #
    # Only a template that HAS an occurrence in this period matches: one it
    # projects (an occurrence with no row yet) or one it already generated (a
    # row dated or settling in the period, in ANY state -- a skipped
    # occurrence still means "this bill is handled by the template, and the
    # user skipped it"). A template created now to start NEXT period ("I pay
    # this month by hand") contributes nothing here and must not erase the
    # history that is this period's only signal.
    #
    # Known and NOT solved here, exactly as in ``forecast_service``:
    #   - a hand-keyed pending row duplicating a template bill counts in B'
    #     AND in R;
    #   - in the period being planned, a plain settled bank row next to the
    #     generated pending occurrence it was never reconciled with counts in
    #     B' AND in R (rent 1500 -> 3000). In LATER periods that bank row is
    #     history, and the template match keeps it out of H'.
    #   - a PAUSED template plans 0: it projects nothing, its generated
    #     history keeps ``recurring_id`` so it stays out of H', and a paused
    #     template matches nothing. Paused means "do not plan this". Stopping
    #     or deleting the template NULLs ``recurring_id`` and that history
    #     counts in H' again.

    def type_str(raw) -> str:
        return raw.value if hasattr(raw, "value") else str(raw)

    # ── R, first half: occurrences of active templates with no row yet ──
    # ALL active templates are loaded, not only those due by ``p_end``: the
    # history match below needs a template whose frontier generation already
    # pushed past the period, and that is the common case once the period's
    # occurrence exists. Only the projection narrows to ``next_due <= p_end``.
    rec_result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.org_id == org_id,
            active_series_filter(),
            # TBD-325 PR 2. ⚠ THE THIRD SOURCE, and the one that made this a
            # HALF-FIX until it was caught in review.
            #
            # The history and current-period aggregates (below) were scoped
            # while this one was not, so ``planned_amount`` was written as a
            # CROSS-CURRENCY total and PERSISTED to ``forecast_plan_items``.
            # It then became ``total_planned_expense`` -- the DENOMINATOR of
            # OnTrackTile's verdict and of the budget bars -- against a
            # numerator that IS scoped. Measured: an EUR org with a USD 3000
            # recurring planned 3025.00 against 100.00 of scoped actuals, a
            # 2925 phantom underspend that survives the scope because it is
            # stored.
            #
            # Scoping a numerator and not its denominator is worse than
            # scoping neither.
            #
            # ⚠ Like ``forecast_service``'s recurring select, this is scoped
            # ON THE SELECT, not in the Python accumulation below:
            # ``RecurringTransaction.account`` is a bare ``relationship()`` and
            # touching ``r.account.currency`` in that loop raises
            # ``MissingGreenlet`` on an AsyncSession.
            org_currency_filter(
                org_id, currency_scope, RecurringTransaction.account_id
            ),
        )
    )
    active_templates = list(rec_result.scalars().all())
    recurring_items = [r for r in active_templates if r.next_due_date <= p_end]

    # Anti-double-count probe, copied VERBATIM from
    # ``forecast_service.compute_forecast`` (there it is inline, not a helper,
    # and this change does not reach into that module). Keep the two
    # identical: the predicate is generation's own create-condition negated
    # -- (org_id, recurring_id, date), NO status, NO reportability, NO
    # effective-date term -- so it asks only "does a row for this occurrence
    # exist?". A skipped occurrence is such a row: the probe drops its date
    # from the projection, and the reportable filter on the rows below drops
    # the row, so it counts 0 in both halves. A row dated in this period but
    # expected to settle in the next one is also found here, and counts in R
    # next period by its effective date. The date bounds are a narrowing only.
    materialised: set[tuple[int, datetime.date]] = set()
    if recurring_items:
        rows = await db.execute(
            select(Transaction.recurring_id, Transaction.date).where(
                Transaction.org_id == org_id,
                Transaction.recurring_id.in_([r.id for r in recurring_items]),
                Transaction.date >= p_start,
                Transaction.date <= p_end,
            )
        )
        materialised = {(rid, d) for rid, d in rows.all()}

    r_totals: dict[tuple[int, str], Decimal] = {}
    # Templates with an occurrence in this period; see the history match.
    in_period_templates: set[int] = set()
    for r in recurring_items:
        key = (group_key(r.category_id), type_str(r.type))
        if key in existing_keys:
            continue

        # TBD-275: this WAS a hand-rolled duplicate of ``occurrences_in_window``
        # -- same fast-forward, same collect, same origin, its own subtly
        # different loop conditions and no iteration cap on the collect half. A
        # second copy of the occurrence grid is a second thing to remember to
        # budget, and it would have been missed. It is now the shared walk, so
        # this plan source spends instalments exactly as the forecast and
        # generation do.
        for d in occurrences_in_window(
            r.next_due_date, r.frequency, p_start, p_end,
            budget=remaining_occurrences(r),
        ):
            if (r.id, d) in materialised:
                continue
            r_totals[key] = r_totals.get(key, Decimal("0")) + r.amount
            in_period_templates.add(r.id)

    # Cash-basis: select + filter by the effective settled date so a row
    # dated in month X but settled in month Y buckets (and windows) by Y,
    # consistent with the list/reports/forecast.
    eff_date = effective_period_date_expr()

    # ── R, second half, and B': the period's rows, split on recurring_id ──
    # SETTLED OR PENDING: the user's already-imported bills for the period
    # being planned are real expectations and feed the suggestion right away,
    # not after they settle. Generated rows go to R whatever the template's
    # state now (paused, ended or deleted-and-kept), plain rows to B'.
    generated = Transaction.recurring_id.is_not(None)
    current_q = (
        select(
            Transaction.category_id,
            Transaction.type,
            generated.label("generated"),
            func.coalesce(func.sum(Transaction.amount), 0).label("total"),
        )
        .where(
            Transaction.org_id == org_id,
            Transaction.status.in_(
                [TransactionStatus.SETTLED, TransactionStatus.PENDING]
            ),
            eff_date >= p_start,
            eff_date <= p_end,
            Transaction.type.in_(["income", "expense"]),
            reportable_transaction_filter(),
            # One clause for BOTH the current-period source and the
            # generated-rows source (TBD-764): they are this one select.
            currency_clause,
        )
        .group_by(Transaction.category_id, Transaction.type, generated)
    )
    current_result = await db.execute(current_q)
    b_totals: dict[tuple[int, str], Decimal] = {}
    for cat_id, tx_type_raw, is_generated, total in current_result.all():
        key = (group_key(cat_id), type_str(tx_type_raw))
        amt = Decimal(str(total))
        if amt <= 0:
            continue
        bucket = r_totals if is_generated else b_totals
        bucket[key] = bucket.get(key, Decimal("0")) + amt

    # ── H': 3-month history average ──
    # SETTLED only. Pending transactions from months ago shouldn't drive
    # averages — if they never settled, treating them as real history would
    # inflate the suggestion. Generated rows belong to R, never to H': a
    # template's past occurrences averaged in here would plan it twice.
    #
    # Month bucketing happens in Python (not SQL date_format) so the
    # query stays portable between MySQL and SQLite (test fixtures run
    # in-memory SQLite).
    three_months_ago = p_start - relativedelta(months=3)
    hist_q = (
        select(
            Transaction.category_id,
            Transaction.account_id,
            Transaction.type,
            eff_date.label("eff_date"),
            Transaction.amount,
            Transaction.date,
        )
        .where(
            Transaction.org_id == org_id,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.recurring_id.is_(None),
            eff_date >= three_months_ago,
            eff_date < p_start,
            Transaction.type.in_(["income", "expense"]),
            reportable_transaction_filter(),
            # TBD-325 PR 2. Currency is on ``accounts.currency`` only, so an
            # unscoped history mixes currencies into one monthly average and
            # every ``planned_amount`` derived from it. No FX, so scope.
            currency_clause,
        )
    )
    hist_result = await db.execute(hist_q)

    # Group by the granularity key (master in master mode, sub in sub mode).
    # Each row keeps BOTH months: its effective month buckets H' (cash basis,
    # unchanged), its due month (``date``) is where the template match looks.
    # Structure: {(group_id, type): [(cat, account, amount, eff_month, due_month)]}
    hist_rows: dict[tuple[int, str], list[tuple]] = {}
    for cat_id, account_id, tx_type_raw, eff_dt, amount, due in hist_result.all():
        key = (group_key(cat_id), type_str(tx_type_raw))
        hist_rows.setdefault(key, []).append((
            cat_id, account_id, Decimal(str(amount)),
            (eff_dt.year, eff_dt.month), (due.year, due.month),
        ))

    # The history match (see the comment at the top of this computation).
    # Two small selects, both keyed on the org's active templates only:
    #   - which templates already have a row in THIS period, in any state
    #     (projected ones were collected above);
    #   - how many SETTLED, reportable rows each template generated per due
    #     month over the history rows' date span: those occurrences were paid
    #     through the template, so its cap is spent. A generated row left
    #     PENDING (never reconciled with the bank row that paid it) or skipped
    #     spent nothing, and the plain row next to it IS that payment.
    templates_by_group: dict[tuple[int, str], list[RecurringTransaction]] = {}
    generated_counts: dict[tuple[int, tuple[int, int]], int] = {}
    template_ids = [r.id for r in active_templates]
    if template_ids and hist_rows:
        in_period = await db.execute(
            select(Transaction.recurring_id).distinct().where(
                Transaction.org_id == org_id,
                Transaction.recurring_id.in_(template_ids),
                or_(
                    and_(Transaction.date >= p_start, Transaction.date <= p_end),
                    and_(eff_date >= p_start, eff_date <= p_end),
                ),
            )
        )
        in_period_templates.update(rid for (rid,) in in_period.all())
        for r in active_templates:
            if r.id in in_period_templates:
                key = (group_key(r.category_id), type_str(r.type))
                templates_by_group.setdefault(key, []).append(r)

    if templates_by_group:
        due_dates = [
            datetime.date(*row[4], 1)
            for key in templates_by_group
            for row in hist_rows.get(key, [])
        ]
        if due_dates:
            gen = await db.execute(
                select(Transaction.recurring_id, Transaction.date).where(
                    Transaction.org_id == org_id,
                    Transaction.recurring_id.in_(
                        [r.id for ts in templates_by_group.values() for r in ts]
                    ),
                    Transaction.status == TransactionStatus.SETTLED,
                    reportable_transaction_filter(),
                    Transaction.date >= min(due_dates),
                    Transaction.date < max(due_dates) + relativedelta(months=1),
                )
            )
            for rid, d in gen.all():
                k = (rid, (d.year, d.month))
                generated_counts[k] = generated_counts.get(k, 0) + 1

    # Structure: {(group_id, type): {eff_month: total}}. EVERY month that had
    # a history row counts in the divisor and the 2-month gate, even when the
    # match took all of it (it holds 0): electricity 80 with 80+200, 80,
    # 80+100 averages 300 over 3 months, not 2.
    hist_monthly: dict[tuple[int, str], dict[tuple[int, int], Decimal]] = {}
    for key, rows in hist_rows.items():
        months = hist_monthly.setdefault(key, {})
        for row in rows:
            months.setdefault(row[3], Decimal("0"))
        kept = rows
        templates = templates_by_group.get(key)
        if templates:
            by_due: dict[tuple[int, int], list[tuple]] = {}
            for row in rows:
                by_due.setdefault(row[4], []).append(row)
            kept = [
                row
                for (year, month), due_rows in by_due.items()
                for row in _drop_template_matches(
                    due_rows, templates, year, month, generated_counts
                )
            ]
        for row in kept:
            months[row[3]] += row[2]

    # ── One item per group ──
    # Insertion order only keeps the run deterministic; the guards below do
    # not depend on it (``would_conflict`` reads the seeded snapshots, and in
    # master mode every candidate is a master).
    candidates = dict.fromkeys([*r_totals, *b_totals, *hist_monthly])
    for key in candidates:
        if key in existing_keys:
            continue
        grp_id, tx_type = key
        # Skip groups that would double-plan a leftover sub (or master).
        if would_conflict(grp_id, tx_type):
            continue
        r_total = r_totals.get(key, Decimal("0"))
        # Pure-history signals need 2+ months to avoid one-off noise; the
        # current period's own rows (B') need no such gate.
        months = hist_monthly.get(key, {})
        h_avg = Decimal("0")
        if len(months) >= 2:
            h_avg = Decimal(str(round(float(sum(months.values()) / len(months)), 2)))
        planned = r_total + max(h_avg, b_totals.get(key, Decimal("0")))
        if planned <= 0:
            continue

        item = ForecastPlanItem(
            plan_id=plan.id,
            org_id=org_id,
            category_id=grp_id,
            type=ForecastItemType(tx_type),
            planned_amount=planned,
            source=ItemSource.RECURRING if r_total > 0 else ItemSource.HISTORY,
        )
        db.add(item)
        existing_keys.add(key)
        claim(grp_id, tx_type)

    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(
        db, org_id, plan, today=today, currency_scope=currency_scope
    )


async def refresh_from_sources(
    db: AsyncSession, org_id: int, period_start: datetime.date | None = None,
    *, today: datetime.date | None = None,
) -> ForecastPlanResponse:
    """Replace auto-generated plan items, preserving manual edits.

    Drops every item whose source is RECURRING or HISTORY (the populate
    output) and re-runs ``populate_from_sources`` against fresh data.
    Items with source=MANUAL stay untouched — that flag is set whenever
    the user adds an item by hand or edits an auto-generated one, so the
    rule is "user-touched stays, never-touched gets refreshed".

    ⚠ The inner ``populate_from_sources`` call already threaded ``today``,
    which is what made THIS drop invisible: on the auto-create path the period
    row is already committed by the time populate runs, so the inner thread
    could never repair the outer anchor (TBD-299).
    """
    period = await resolve_period(db, org_id, period_start, today=today)
    plan = await _get_or_create_plan_row(db, org_id, period.id)
    _require_draft(plan)
    await db.refresh(plan, ["billing_period", "items"])

    # TBD-466 review: the (master, type) pairs that had a sub item BEFORE the
    # delete. Taken after it, deleting a HISTORY sub would let a surviving
    # master claim the subtree and silently drop that sub for good.
    item_cat_ids = {i.category_id for i in plan.items}
    parent_of = dict((await db.execute(
        select(Category.id, Category.parent_id).where(
            Category.id.in_(item_cat_ids),
            Category.org_id == org_id,
            Category.parent_id.is_not(None),
        )
    )).all()) if item_cat_ids else {}
    pre_existing_sub_pairs = frozenset(
        (parent_of[i.category_id], i.type.value)
        for i in plan.items if i.category_id in parent_of
    )

    for item in list(plan.items):
        if item.source != ItemSource.MANUAL:
            await db.delete(item)
    await db.flush()
    await db.refresh(plan, ["billing_period", "items"])

    # Re-run populate against the cleared slate. populate handles its own
    # commit; we ride on its commit so the deletes and inserts land
    # together.
    return await populate_from_sources(
        db, org_id, period_start=period_start, today=today,
        pre_existing_sub_pairs=pre_existing_sub_pairs,
    )


async def upsert_item(
    db: AsyncSession, org_id: int, plan_id: int, body: ForecastPlanItemCreate,
    *, today: datetime.date | None = None,
) -> ForecastPlanResponse:
    """Add or update a single plan item."""
    plan = await _get_plan(db, org_id, plan_id)
    _require_draft(plan)
    await db.refresh(plan, ["items"])

    granularity = await get_forecast_input_granularity(db, org_id)
    sub_mode = granularity == FORECAST_GRANULARITY_SUBCATEGORY
    await _validate_category_granularity(
        db, org_id, body.category_id, ForecastItemType(body.type),
        subcategory_mode=sub_mode,
    )

    # Find existing item
    existing = None
    for item in plan.items:
        if item.category_id == body.category_id and item.type.value == body.type:
            existing = item
            break

    # Public writes are always MANUAL (PR #144 #2). The schema no longer
    # carries a source field; recurring/history are reserved for internal
    # populate/refresh/copy paths.
    if existing:
        existing.planned_amount = body.planned_amount
        existing.source = ItemSource.MANUAL
    else:
        new_item = ForecastPlanItem(
            plan_id=plan.id,
            org_id=org_id,
            category_id=body.category_id,
            type=ForecastItemType(body.type),
            planned_amount=body.planned_amount,
            source=ItemSource.MANUAL,
        )
        db.add(new_item)

    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan, today=today)


async def bulk_upsert(
    db: AsyncSession, org_id: int, plan_id: int, body: BulkUpsertRequest,
    *, today: datetime.date | None = None,
) -> ForecastPlanResponse:
    """Bulk add/update multiple plan items at once."""
    plan = await _get_plan(db, org_id, plan_id)
    _require_draft(plan)
    await db.refresh(plan, ["items"])

    granularity = await get_forecast_input_granularity(db, org_id)
    sub_mode = granularity == FORECAST_GRANULARITY_SUBCATEGORY

    # Validate all category IDs belong to the org and match the org's build
    # granularity. Atomic: any invalid or wrong-granularity row rejects the
    # whole batch before any insert, matching the existing convention where
    # an invalid ID raises ValidationError before any commit.
    requested_ids = {item.category_id for item in body.items}
    cat_by_id: dict[int, Category] = {}
    if requested_ids:
        valid_result = await db.execute(
            select(Category).where(
                Category.id.in_(requested_ids),
                Category.org_id == org_id,
            )
        )
        for cat in valid_result.scalars().all():
            cat_by_id[cat.id] = cat
        # master mode rejects subcategories; subcategory mode takes both.
        wrong_granularity = set() if sub_mode else {
            cid for cid, cat in cat_by_id.items() if cat.parent_id is not None
        }
        invalid = (requested_ids - cat_by_id.keys()) | wrong_granularity
        if invalid:
            raise ValidationError(
                f"Invalid or wrong-granularity category IDs: {sorted(invalid)}"
            )

    # Per-row type compatibility (PR #144 review fix). Reject the whole
    # batch on first mismatch so partial inserts can't leave the plan in a
    # mixed state.
    for item_data in body.items:
        cat = cat_by_id[item_data.category_id]
        if not _category_type_matches(cat.type, ForecastItemType(item_data.type)):
            raise ValidationError(
                f"Category type ({cat.type.value}) does not match "
                f"forecast item type ({item_data.type}) for category_id="
                f"{item_data.category_id}"
            )

    existing_map = {
        (i.category_id, i.type.value): i for i in plan.items
    }

    # Public bulk writes are always MANUAL (PR #144 #2). See upsert_item.
    for item_data in body.items:
        key = (item_data.category_id, item_data.type)
        if key in existing_map:
            existing_map[key].planned_amount = item_data.planned_amount
            existing_map[key].source = ItemSource.MANUAL
        else:
            new_item = ForecastPlanItem(
                plan_id=plan.id,
                org_id=org_id,
                category_id=item_data.category_id,
                type=ForecastItemType(item_data.type),
                planned_amount=item_data.planned_amount,
                source=ItemSource.MANUAL,
            )
            db.add(new_item)
            existing_map[key] = new_item  # track to handle duplicates in same request

    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan, today=today)


async def update_item(
    db: AsyncSession, org_id: int, plan_id: int, item_id: int, body: ForecastPlanItemUpdate,
    *, today: datetime.date | None = None,
) -> ForecastPlanResponse:
    """Update a single plan item amount."""
    plan = await _get_plan(db, org_id, plan_id)
    _require_draft(plan)

    item = None
    for i in plan.items:
        if i.id == item_id:
            item = i
            break

    if item is None:
        raise NotFoundError("Forecast plan item")

    item.planned_amount = body.planned_amount
    item.source = ItemSource.MANUAL

    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan, today=today)


async def delete_item(
    db: AsyncSession, org_id: int, plan_id: int, item_id: int,
    *, today: datetime.date | None = None,
) -> ForecastPlanResponse:
    """Remove a single plan item."""
    plan = await _get_plan(db, org_id, plan_id)
    _require_draft(plan)

    item = None
    for i in plan.items:
        if i.id == item_id:
            item = i
            break

    if item is None:
        raise NotFoundError("Forecast plan item")

    await db.delete(item)
    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan, today=today)


async def activate_plan(
    db: AsyncSession, org_id: int, plan_id: int,
    *, today: datetime.date | None = None,
) -> ForecastPlanResponse:
    """Mark plan as active (finalized). Active plans are read-only."""
    plan = await _get_plan(db, org_id, plan_id)
    _require_draft(plan)

    if not plan.items:
        raise ValidationError("Cannot activate an empty plan")

    plan.status = PlanStatus.ACTIVE
    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan, today=today)


async def revert_to_draft(
    db: AsyncSession, org_id: int, plan_id: int,
    *, today: datetime.date | None = None,
) -> ForecastPlanResponse:
    """Revert an active plan back to draft for editing."""
    plan = await _get_plan(db, org_id, plan_id)

    if plan.status != PlanStatus.ACTIVE:
        raise ValidationError("Plan is already a draft")

    plan.status = PlanStatus.DRAFT
    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan, today=today)


async def discard_plan(
    db: AsyncSession, org_id: int, plan_id: int,
    *, today: datetime.date | None = None,
) -> ForecastPlanResponse:
    """Remove all items from a draft plan."""
    plan = await _get_plan(db, org_id, plan_id)
    _require_draft(plan)

    for item in list(plan.items):
        await db.delete(item)

    plan.status = PlanStatus.DRAFT
    await db.commit()
    await db.refresh(plan, ["billing_period", "items"])
    return await _build_response(db, org_id, plan, today=today)


async def copy_from_period(
    db: AsyncSession, org_id: int,
    target_period_start: datetime.date | None,
    source_period_start: datetime.date,
    *, today: datetime.date | None = None,
) -> ForecastPlanResponse:
    """Copy plan items from a previous period to the target period.

    Only the TARGET resolve can auto-create: ``target_period_start`` is
    optional, so it can take ``resolve_period``'s fallback arm. The SOURCE
    resolve takes a required, non-optional date and therefore always goes down
    the lookup-or-``ValidationError`` path — ``today=`` there is defensive
    consistency, not observable behaviour, and is deliberately unfenced
    (TBD-299).
    """
    target_period = await resolve_period(
        db, org_id, target_period_start, today=today
    )
    source_period = await resolve_period(
        db, org_id, source_period_start, today=today
    )

    # Get source plan
    source_result = await db.execute(
        select(ForecastPlan).where(
            ForecastPlan.org_id == org_id,
            ForecastPlan.billing_period_id == source_period.id,
        )
    )
    source_plan = source_result.scalar_one_or_none()
    if source_plan is None or not source_plan.items:
        raise ValidationError("Source period has no plan to copy")

    # Get or create target plan (race-safe)
    target_plan = await _get_or_create_plan_row(db, org_id, target_period.id)
    await db.refresh(target_plan, ["billing_period", "items"])
    _require_draft(target_plan)

    existing_keys: set[tuple[int, str]] = set()
    if target_plan.items:
        existing_keys = {(i.category_id, i.type.value) for i in target_plan.items}

    # Double-planning guard (TBD-466 review), seeded ONLY from the target as
    # it was before this copy, in both directions: a target master item may
    # be a rollup that already carries its subs, and a source master item may
    # be one. Deliberately no claims inside the loop, so a source holding a
    # master item AND its sub items still copies all of them into an empty
    # target (there they are the summing kind, built side by side).
    ids = {i.category_id for i in source_plan.items} | {
        i.category_id for i in target_plan.items
    }
    master_of = {
        cid: pid if pid is not None else cid
        for cid, pid in (await db.execute(
            select(Category.id, Category.parent_id).where(
                Category.id.in_(ids), Category.org_id == org_id
            )
        )).all()
    }
    target_master: set[tuple[int, str]] = set()
    target_sub: set[tuple[int, str]] = set()
    for i in target_plan.items:
        m = master_of.get(i.category_id, i.category_id)
        (target_master if i.category_id == m else target_sub).add((m, i.type.value))

    for src_item in source_plan.items:
        key = (src_item.category_id, src_item.type.value)
        if key in existing_keys:
            continue
        m = master_of.get(src_item.category_id, src_item.category_id)
        gkey = (m, src_item.type.value)
        if src_item.category_id == m:
            if gkey in target_sub:
                continue
        # Same claim rule as populate: a target master next to target subs is
        # the summing kind and claims nothing, so a source sub still copies.
        elif gkey in target_master and gkey not in target_sub:
            continue
        # L3.11 residual cleanup (PR #294, 2026-05-16): copied items
        # are always MANUAL on the target plan, regardless of the
        # source-period item's ``source``. ``copy_from_period`` is a
        # user-initiated action — the user is explicitly choosing to
        # carry these forecasts forward and will edit them — so the
        # invariant "MANUAL = user-curated" demands MANUAL here.
        # Without this pin, copies of items left over from
        # ``populate_from_sources`` would land as RECURRING/HISTORY
        # and silently vanish on the next ``refresh_from_sources``
        # on the target period.
        new_item = ForecastPlanItem(
            plan_id=target_plan.id,
            org_id=org_id,
            category_id=src_item.category_id,
            type=src_item.type,
            planned_amount=src_item.planned_amount,
            source=ItemSource.MANUAL,
        )
        db.add(new_item)
        existing_keys.add(key)

    await db.commit()
    await db.refresh(target_plan, ["billing_period", "items"])
    return await _build_response(db, org_id, target_plan, today=today)


# ── Internal ─────────────────────────────────────────────────────────────────

async def _get_plan(db: AsyncSession, org_id: int, plan_id: int) -> ForecastPlan:
    result = await db.execute(
        select(ForecastPlan).where(
            ForecastPlan.id == plan_id,
            ForecastPlan.org_id == org_id,
        )
    )
    plan = result.scalar_one_or_none()
    if plan is None:
        raise NotFoundError("Forecast plan")
    return plan
