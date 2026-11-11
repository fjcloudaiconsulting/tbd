"""Plans lifecycle (TBD-602): Draft -> Start -> Finish | Delete, edit after
start, the budget rollover, broken links and progress.

Every transition is one transaction that the router commits: the plan row is
locked ``FOR UPDATE`` (a concurrent second call waits, then finds the new
status), the status is written first, and each step creates its row unless the
plan or goal already links one. So a re-run, or the call that lost the lock
race, is a no-op: Start on an active plan and Finish on a finished one return
it unchanged.

Every plan write first locks ALL the org's plan rows, in id order
(``lock_plans``): the org's plan mutex. It serialises Start's checks across
plans (an account claimed once, a tag and a category held by one plan).

The snapshot must not predate the locks: under REPEATABLE READ a transaction
reads from the snapshot of its first plain read, and every route reads the
plan before it locks it. So ``lock`` ends that read-only transaction first,
and the first read after the locks starts a fresh snapshot that sees whatever
a lock holder committed while this write waited (a goal disabled, a Budget
added). One choke point, rather than a locking read at every read after it.

Not the ``organizations`` row, which ``currency_service`` locks: every insert
of an org-scoped row takes a shared lock on it (the foreign key check), so
holding it through a Start stalls the whole org, and closes a cycle with
``close_period`` (which holds Budget row locks, then inserts a period).
Measured on MySQL: a close waited out its timeout behind a held Start. The plan
rows are referenced by ``plan_goals`` only, which plan writes lock anyway.
The rollover never waits for the plan locks (``rollover``), so it cannot be
one side of a lock cycle.

What Start makes is ordinary data the user owns (the master category and one
child per enabled goal, the tag, the funding series, the Budgets). Finish and
Delete stop the series and drop the Budgets of later periods; the category, the
tag, the accounts, past and current Budgets and every transaction stay.

Progress is read from the goal's category and account, never from
``funding_recurring_id``: ``stop_recurring`` nulls ``recurring_id`` on every
surviving row, so a series-keyed sum would lose its history the moment the
user stopped the series.

Rights (the creator, or an org owner or admin) are ``plan_draft_service``'s;
the routers check them before calling in here.
"""
from __future__ import annotations

import datetime
import logging
import re
from collections import Counter
from decimal import Decimal
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app._time import utcnow_naive
from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.scenario import PlanGoal, PlanGoalKind, Scenario, ScenarioStatus
from app.models.tag import Tag
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import Organization
from app.schemas.plan_projection import PlanProjectionResponse
from app.schemas.recurring import RECURRING_DESCRIPTION_MAX_LENGTH, RecurringCreate
from app.services import (
    billing_service,
    feature_gate,
    plan_draft_service,
    plan_projection_service,
    recurring_service,
    tag_service,
)
from app.services.account_balance_forecast_service import LIABILITY_SLUGS
from app.services.date_utils import occurrences_in_window
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.transaction_filters import (
    non_reverted_transaction_filter,
    reportable_transaction_filter,
    signed_amount_expr,
)

logger = logging.getLogger(__name__)

_ACTIVE = ScenarioStatus.ACTIVE
_CENTS = Decimal("0.01")
_TRANSFER_KINDS = {PlanGoalKind.SAVINGS_TARGET.value, PlanGoalKind.RETIREMENT.value}
_SPEND_KINDS = {PlanGoalKind.LUMP.value, PlanGoalKind.MONTHLY_SPEND.value}


# Coded 409s: a claim another plan (or another goal) already holds.
ACCOUNT_TAKEN, TAG_TAKEN, CATEGORY_TAKEN = "plan_account_taken", "plan_tag_taken", "plan_category_taken"
SERIES_LINKED = "plan_series_linked"


async def lock_plans(db: AsyncSession, org_id: int, *, skip_locked: bool = False) -> list[Scenario]:
    """Every plan row of the org ``FOR UPDATE``, in id order: the first lock
    of every plan write (module docstring). Returns the rows it locked. With
    ``skip_locked``, it never waits: a plan another transaction holds is
    missing from the result."""
    return list((await db.scalars(
        select(Scenario).where(Scenario.org_id == org_id).order_by(Scenario.id)
        .with_for_update(skip_locked=skip_locked).execution_options(populate_existing=True))).all())


_LOCKED_IN = "plan_lifecycle.locked_in"
_WROTE_IN = "plan_lifecycle.wrote_in"


# A write already sent (flushed, or ORM DML, which never flushes) is not in
# db.new/dirty/deleted, and lock()'s rollback would silently drop it.
# shortcut: raw db.connection()/text() writes are not seen; route them through the ORM or tag them if one ever precedes lock().
@event.listens_for(Session, "after_flush")
def _flushed(session, _ctx) -> None:
    session.info[_WROTE_IN] = session.get_transaction()


@event.listens_for(Session, "do_orm_execute")
def _executed(state) -> None:
    if state.is_insert or state.is_update or state.is_delete:
        state.session.info[_WROTE_IN] = state.session.get_transaction()


async def reload(db: AsyncSession) -> None:
    """Re-read every object the session holds, after a rollback expired them:
    a caller's object (the route's user, the scheduler's org) must not hit a
    lazy load, which an async session refuses."""
    for obj in list(db.identity_map.values()):
        try:
            await db.refresh(obj)
        except InvalidRequestError:  # its row is gone
            db.expunge(obj)


async def lock(db: AsyncSession, plan: Scenario) -> Scenario:
    """The org's plans locked (``lock_plans``) on a fresh snapshot (module
    docstring), and this one re-read (``populate_existing``: the identity map
    must carry the locked state, the codebase's FOR UPDATE invariant).

    The first call in a transaction rolls back the read-only work before it,
    then reloads what the session holds; a later call in the same transaction
    (an agent's preview, then the route it confirms) keeps the locks it has."""
    org_id, plan_id = plan.org_id, plan.id
    held_in = db.sync_session.get_transaction()
    if held_in is None or db.info.get(_LOCKED_IN) is not held_in:
        if db.new or db.dirty or db.deleted or (held_in is not None and db.info.get(_WROTE_IN) is held_in):
            raise RuntimeError("plan_lifecycle_service.lock would roll back a pending write")
        await db.rollback()
        await lock_plans(db, org_id)
        db.info[_LOCKED_IN] = db.sync_session.get_transaction()
        await reload(db)
    row = (await db.execute(
        select(Scenario).where(Scenario.id == plan_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Scenario")
    return row


# ── Start ────────────────────────────────────────────────────────────────


async def start(db: AsyncSession, *, plan: Scenario, today: datetime.date | None = None) -> Scenario:
    today = today or datetime.date.today()
    if not plan_draft_service.is_plan(plan):
        raise ValidationError(f"scenario {plan.id} is not a plan")
    # Before the lock: ``get_current_period`` commits a period it auto-creates.
    period_start = (await billing_service.get_current_period(db, plan.org_id, today=today)).start_date
    plan = await lock(db, plan)
    if plan.status == _ACTIVE:
        return plan
    if plan.status != ScenarioStatus.DRAFT:
        raise ConflictError("a finished plan cannot be started again")
    plan.status, plan.started_at = _ACTIVE, utcnow_naive()

    all_goals = await plan_draft_service.list_goals(db, plan=plan)
    goals = [g for g in all_goals if g.enabled]
    currency = await plan_draft_service.plan_currency(db, org_id=plan.org_id, requested=plan.currency)
    # Links were checked when set; an account's currency or the org's scope may have moved since.
    for row, columns in [(plan, ("funding_account_id", "category_id", "tag_id"))] + [
            (g, ("account_id", "category_id", "funding_recurring_id")) for g in goals]:
        await plan_draft_service._check_links(
            db, org_id=plan.org_id, currency=currency, changes={c: getattr(row, c) for c in columns})
    await _check_topology(db, plan)
    # The plan as drafted, before its own series join the books.
    plan.baseline_projection_json = PlanProjectionResponse.model_validate(
        await plan_projection_service.project(
            db, plan.org_id, [plan], all_goals, today=today,
            horizon_end=plan_projection_service.default_horizon_end(today))
    ).model_dump(mode="json")

    await _master_category(db, plan, linked_at_start=True)
    await _tag(db, plan)
    for goal in goals:
        await _start_goal_steps(db, plan, goal, today)
    await _ensure_budgets(db, plan, period_start)
    return plan


async def start_goal(db: AsyncSession, *, plan: Scenario, goal: PlanGoal,
                     today: datetime.date | None = None) -> None:
    """Edit after start: a goal added or enabled on an active plan (locked by
    the caller) gets Start's steps, for that goal only."""
    await db.flush()
    await _check_topology(db, plan)
    await _master_category(db, plan)
    await _start_goal_steps(db, plan, goal, today or datetime.date.today())
    await budget_changed(db, plan=plan)  # its spend now counts against the Budget


async def _check_topology(db: AsyncSession, plan: Scenario) -> None:
    """Money flows one way: from a funding account into goal accounts. So a
    funding account is no active plan's goal account (its own plan's
    included), a goal account is no active plan's funding account, neither is
    a liability, and an account backs one enabled goal. ``plan`` is active
    (flushed), so the queries below include it."""
    # Locking reads (module docstring): another plan's commit is seen, not a snapshot.
    active = (await db.scalars(select(Scenario).where(
        Scenario.org_id == plan.org_id, Scenario.status == _ACTIVE).with_for_update(read=True))).all()
    goals = (await db.scalars(select(PlanGoal).where(
        PlanGoal.scenario_id.in_([p.id for p in active]), PlanGoal.enabled.is_(True),
        PlanGoal.account_id.is_not(None)).with_for_update(read=True))).all()
    goal_accounts = Counter(g.account_id for g in goals)
    funding = {p.funding_account_id for p in active if p.funding_account_id is not None}
    mine = [g.account_id for g in goals if g.scenario_id == plan.id]
    slugs = dict((await db.execute(
        select(Account.id, AccountType.slug).join(AccountType, AccountType.id == Account.account_type_id)
        .where(Account.id.in_([*mine, plan.funding_account_id or 0])))).all())
    if plan.funding_account_id is not None:
        if slugs.get(plan.funding_account_id) in LIABILITY_SLUGS:
            raise ValidationError("a plan cannot be funded from a credit card or loan")
        if plan.funding_account_id in goal_accounts:
            raise ConflictError(
                f"account {plan.funding_account_id} is the goal account of an active plan, "
                "so it cannot fund this one", code=ACCOUNT_TAKEN)
    for account_id in mine:
        if slugs.get(account_id) in LIABILITY_SLUGS:
            raise ValidationError("a goal cannot save into a credit card or loan")
        if account_id in funding:
            raise ConflictError(f"account {account_id} funds an active plan, so it cannot be a goal account",
                                code=ACCOUNT_TAKEN)
        if goal_accounts[account_id] > 1:
            raise ConflictError(f"account {account_id} already backs another enabled goal", code=ACCOUNT_TAKEN)


async def _master_category(db: AsyncSession, plan: Scenario, *, linked_at_start: bool = False) -> Category:
    if plan.category_id is not None:
        cat = await db.get(Category, plan.category_id)
        if cat is None or cat.parent_id is not None:
            # Budgets are master-only, and the plan's Budget is on this category.
            raise ValidationError("a plan's category must be a master category")
        if linked_at_start:
            await _refuse_a_held_category(db, plan)
        return cat
    cat = Category(org_id=plan.org_id, name=plan.name[:100], type=CategoryType.BOTH)
    db.add(cat)
    await db.flush()
    plan.category_id = cat.id
    return cat


async def _refuse_a_held_category(db: AsyncSession, plan: Scenario) -> None:
    """A category the user linked in the draft must be the plan's alone.
    Budgets key on (org, category, period), so on a category that already has
    one the plan would adopt it: rewrite its amount on a budget edit, delete
    the later ones on Finish."""
    held = await db.scalar(select(Scenario.id).where(
        Scenario.org_id == plan.org_id, Scenario.category_id == plan.category_id, Scenario.id != plan.id,
        Scenario.status != ScenarioStatus.DRAFT).with_for_update(read=True))
    budgeted = await db.scalar(select(Budget.id).where(
        Budget.org_id == plan.org_id, Budget.category_id == plan.category_id).limit(1))
    if held is not None or budgeted is not None:
        why = f"belongs to plan {held}" if held is not None else "already has budgets"
        raise ConflictError(
            f"category {plan.category_id} {why}: link a fresh category, or unlink it and "
            "Start creates one", code=CATEGORY_TAKEN)


def _tag_name(plan: Scenario) -> str:
    """The plan name in tag form (``normalize_tag_name`` refuses anything else)."""
    cleaned = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 \-]", " ", plan.name.lower())).strip()
    return cleaned[:tag_service.TAG_NAME_MAX_LENGTH].strip() or f"plan {plan.id}"


async def _find_tag(db: AsyncSession, org_id: int, name: str) -> Tag | None:
    # A locking read: a tag another Start just committed is seen and adopted.
    return await db.scalar(select(Tag).where(
        Tag.org_id == org_id, Tag.name_normalized == name).with_for_update(read=True))


async def _tag(db: AsyncSession, plan: Scenario) -> None:
    if plan.tag_id is None:
        name = _tag_name(plan)
        try:
            tag = await _find_tag(db, plan.org_id, name) or await tag_service.create_tag(
                db, org_id=plan.org_id, name=name, created_by_user_id=plan.user_id)
        except IntegrityError:
            # The plan locks serialise Starts, so this is a guard: the unique
            # name lost to a tag committed in between, which is another plan's.
            raise ConflictError(f"the tag '{name}' is taken: rename this plan or link another tag",
                                code=TAG_TAKEN) from None
        plan.tag_id = tag.id
    held = await db.scalar(select(Scenario.id).where(
        Scenario.org_id == plan.org_id, Scenario.tag_id == plan.tag_id, Scenario.id != plan.id,
        Scenario.status != ScenarioStatus.DRAFT).with_for_update(read=True))
    if held is not None:
        raise ConflictError(
            f"the tag of plan {held} would be shared: rename this plan or link another tag", code=TAG_TAKEN)


def _series_spec(plan: Scenario, goal: PlanGoal, today: datetime.date) -> dict[str, Any] | None:
    """The funding series a committed goal gets, or None. A lump is one
    expense on its date, from where it was saved (its account) or the funding
    account; a monthly spend is a monthly expense; a savings or retirement
    goal is a monthly transfer into its account. A monthly series stops at
    the goal's ``target_date``."""
    if goal.kind == PlanGoalKind.LUMP.value:
        if not goal.target_amount or goal.target_date is None:
            return None
        return {"type": "expense", "account_id": goal.account_id or plan.funding_account_id,
                "amount": goal.target_amount, "next_due_date": goal.target_date, "occurrence_count": 1}
    if not goal.monthly_amount:
        return None
    count = None
    if goal.target_date is not None:
        # Stepped as generation steps it (path-dependent at month ends: Jan 31,
        # Feb 28, Mar 28), from today up to and including the target date.
        count = len(occurrences_in_window(today, Frequency.MONTHLY, today, goal.target_date, budget=1201))
        if count == 0:
            return None
        # ``RecurringCreate``'s sanity bound (a century of instalments): beyond it, open-ended.
        count = count if count <= 1200 else None
    spec = {"type": "expense", "account_id": plan.funding_account_id, "amount": goal.monthly_amount,
            "next_due_date": today, "occurrence_count": count}
    if goal.kind in _TRANSFER_KINDS:
        spec.update(type="transfer", to_account_id=goal.account_id)
    return spec


async def _start_goal_steps(db: AsyncSession, plan: Scenario, goal: PlanGoal, today: datetime.date) -> None:
    if goal.category_id is not None:
        child = await db.get(Category, goal.category_id)
        if child is None or child.parent_id != plan.category_id:
            raise ValidationError(f"goal {goal.id}'s category must be a subcategory of the plan's category")
    else:
        master = await db.get(Category, plan.category_id)
        child = Category(org_id=plan.org_id, parent_id=master.id, name=goal.name[:100], type=master.type)
        db.add(child)
        await db.flush()
        goal.category_id = child.id
    if goal.account_id is not None and goal.start_balance is None:
        goal.start_balance = await _expected_balance(db, goal.account_id)
    if goal.funding_recurring_id is not None:
        # Only a draft link gets here: Start's own series come after the status flips.
        # Adopting it would flag the plan (its category is not the goal's) and Finish would stop it.
        raise ConflictError(f"goal {goal.id} is linked to an existing recurring item: unlink it and "
                            "Start creates the goal's own series", code=SERIES_LINKED)
    spec = _series_spec(plan, goal, today)
    if spec is None:
        return
    if goal.kind == PlanGoalKind.LUMP.value:
        earliest = await recurring_service.frontier_lower_bound(db, plan.org_id, today=today)
        if goal.target_date < earliest:
            raise ValidationError(
                f'The goal "{goal.name}" is due on {goal.target_date}, before the current billing cycle '
                f"started on {earliest}. Move its target date to {earliest} or later, or disable the goal.")
    if spec.get("to_account_id", 0) is None:
        raise ValidationError(f"goal {goal.id} saves monthly, so it needs an account to save into")
    if spec["account_id"] is None:
        raise ValidationError("set the plan's funding account: its goals draw on it")
    series = await recurring_service.create_recurring_no_commit(db, plan.org_id, RecurringCreate(
        **spec, category_id=goal.category_id, frequency="monthly",
        description=f"{plan.name}: {goal.name}"[:RECURRING_DESCRIPTION_MAX_LENGTH],
    ), today=today)
    goal.funding_recurring_id = series.id


async def _expected_balance(db: AsyncSession, account_id: int) -> Decimal:
    """Balance plus the pending delta: a pending row that settles after Start
    moves the balance without being progress.

    Every non-reverted pending row of the account counts, whatever its date,
    not the kernel's ``pending_delta`` (the billing window's rows only): a row
    dated after the window that is already booked at Start still lands in the
    balance later, and counting it as saved would overstate progress."""
    account = await db.get(Account, account_id)
    pending = await db.scalar(select(func.coalesce(func.sum(signed_amount_expr()), 0)).where(
        Transaction.account_id == account_id, Transaction.status == TransactionStatus.PENDING,
        Transaction.is_manual_adjustment.is_(False), non_reverted_transaction_filter()))
    return (account.balance + Decimal(str(pending))).quantize(_CENTS)


# ── Budgets and the rollover ─────────────────────────────────────────────


def _payments(series: RecurringTransaction, start: datetime.date, end: datetime.date) -> int:
    """How many of a monthly series' payments (a lump's single one included)
    fall in ``[start, end]``: the last one made (``last_spent_date``) and the
    ones still due, walked from the frontier as generation walks them and cut
    at ``occurrence_count``. A window past the goal's last payment counts none. Of the payments already
    made only the last is known, so a window holding two of them (materialised
    ahead, or a catch-up) counts one: rare for a monthly series in a monthly
    period, and the Budget errs low."""
    paid = int(series.last_spent_date is not None and start <= series.last_spent_date <= end)
    if end < series.next_due_date:
        return paid
    due = occurrences_in_window(series.next_due_date, Frequency.MONTHLY, series.next_due_date, end, budget=1201)
    if series.occurrence_count is not None:
        due = due[:max(series.occurrence_count - series.occurrences_elapsed, 0)]
    return paid + sum(1 for d in due if d >= start)


async def _spend_goals(db: AsyncSession, plan: Scenario) -> list[tuple[PlanGoal, RecurringTransaction]]:
    """The plan's started spend goals with their series. Savings and
    retirement goals are transfers, which no Budget counts."""
    return list((await db.execute(select(PlanGoal, RecurringTransaction).join(
        RecurringTransaction, RecurringTransaction.id == PlanGoal.funding_recurring_id).where(
        PlanGoal.scenario_id == plan.id, PlanGoal.enabled.is_(True),
        PlanGoal.kind.in_(_SPEND_KINDS)).order_by(PlanGoal.id))).all())


def _window_end(periods: list[BillingPeriod], i: int, cycle_day: int) -> datetime.date:
    """The last day ``periods[i]``'s Budget is for. Never unbounded
    (``period_effective_end`` can be), or a lump months out lands in the last
    period as well as its own; the roster tail ends the day before
    ``billing_service.plan_future_periods`` would start the next period (its
    formula, so an off-grid open period after a close is bounded right)."""
    period = periods[i]
    if period.end_date is not None:
        return period.end_date
    if i + 1 < len(periods):
        return periods[i + 1].start_date - datetime.timedelta(days=1)
    return billing_service._snap_to_cycle(
        period.start_date + relativedelta(months=1), cycle_day) - datetime.timedelta(days=1)


def _has_budget(plan: Scenario, goals: list) -> bool:
    """A plan has Budgets when it has a cap or a started spend goal (TBD-802).
    ``_owed`` and ``_ensure_budgets`` must agree on this, or the rollover
    takes the plan locks on every call."""
    return plan.category_id is not None and (plan.monthly_budget is not None or bool(goals))


def _amount(plan: Scenario, goals: list, start: datetime.date, end: datetime.date) -> Decimal:
    """A period's Budget: the cap plus each spend goal's payments in the
    window (a lump is a one-payment series), following the series if its date
    is moved. The master's Budget counts its children
    (``budget_service._compute_spent``), so a goal's spend lands against it.
    A lump paid by card settles, and so counts, when the card says."""
    total = plan.monthly_budget or Decimal(0)
    for goal, series in goals:
        each = goal.target_amount if goal.kind == PlanGoalKind.LUMP.value else goal.monthly_amount
        total += each * _payments(series, start, end)
    return total.quantize(_CENTS)


async def _ensure_budgets(db: AsyncSession, plan: Scenario, current_start: datetime.date,
                          *, recompute: bool = False) -> int:
    """The plan's Budget on its master category in the current period and
    every later one that exists (stubs), unless one is there already
    (``uq_budget_org_cat_period``); ``recompute`` also resets the amount of
    the ones there. Returns how many it added."""
    goals = await _spend_goals(db, plan)
    if not _has_budget(plan, goals):
        return 0
    added = 0
    # A locking read: the rollover's snapshot predates its locks (its probe).
    # Recompute writes the rows, so it locks them for writing up front.
    have = {b.period_start: b for b in (await db.scalars(select(Budget).where(
        Budget.org_id == plan.org_id, Budget.category_id == plan.category_id,
        Budget.period_start >= current_start).with_for_update(read=not recompute))).all()}
    periods = (await db.scalars(select(BillingPeriod).where(
        BillingPeriod.org_id == plan.org_id, BillingPeriod.start_date >= current_start)
        .order_by(BillingPeriod.start_date))).all()
    cycle_day = (await db.get(Organization, plan.org_id)).billing_cycle_day
    for i, period in enumerate(periods):
        amount = _amount(plan, goals, period.start_date, _window_end(periods, i, cycle_day))
        if period.start_date in have:
            if recompute:
                have[period.start_date].amount = amount
        else:
            # ``period_end`` is the raw row value, as ``budget_service.create_budget`` writes it.
            db.add(Budget(org_id=plan.org_id, category_id=plan.category_id, amount=amount,
                          period_start=period.start_date, period_end=period.end_date))
            added += 1
    return added


async def budget_changed(db: AsyncSession, *, plan: Scenario) -> None:
    """Edit after start: a new ``monthly_budget`` or a goal started late
    reaches the current Budget and the later ones, overwriting a hand edit or
    a rebalance of those rows. Past Budgets are history."""
    period = await billing_service.find_open_period(db, plan.org_id)
    if period is not None:
        await _ensure_budgets(db, plan, period.start_date, recompute=True)


async def _owed(db: AsyncSession, org_id: int, current_start: datetime.date) -> list[int]:
    """The ids (id order) of the active plans that lack a Budget in a period
    from ``current_start`` on; empty = nothing to do. A plain read: it decides
    whether the rollover locks anything at all."""
    plans = [p for p in (await db.scalars(select(Scenario).where(
        Scenario.org_id == org_id, Scenario.status == _ACTIVE, Scenario.category_id.is_not(None))
        .order_by(Scenario.id))).all()
        if _has_budget(p, await _spend_goals(db, p))
        and not await attention(db, p)]  # the rollover skips these anyway
    if not plans:
        return []
    starts = (await db.scalars(select(BillingPeriod.start_date).where(
        BillingPeriod.org_id == org_id, BillingPeriod.start_date >= current_start))).all()
    have = set((await db.execute(select(Budget.category_id, Budget.period_start).where(
        Budget.org_id == org_id, Budget.category_id.in_([p.category_id for p in plans]),
        Budget.period_start >= current_start))).all())
    return [p.id for p in plans if any((p.category_id, d) not in have for d in starts)]


async def rollover(db: AsyncSession, org_id: int) -> bool:
    """Every healthy active plan's Budget in the open period and the stubs
    after it; a plan that needs attention is skipped. Run by
    ``billing_service.close_period`` (the scheduler and the manual close both
    call it) and ``ensure_future_periods`` (periods made outside a close).

    Runs after the caller's work has committed, and never raises: a close must
    not be reported failed over a Budget. It locks and commits only when a
    plan is owed a Budget, and never waits for the plan locks: an org whose
    plans are being changed right now is skipped, so the rollover cannot be
    one side of a lock cycle with its caller's own locks. On any failure,
    database-level included (a deadlock rolls the whole transaction back), it
    rolls back, reloads what the session holds (so the caller's objects, the
    scheduler's org among them, stay readable) and logs
    ``plans.rollover.failed``; False then. No SAVEPOINT: it commits on success
    anyway, so one would only spare the caller's objects on a Python error,
    and the reload spares them on every error. A miss is filled by the next
    ``ensure_future_periods`` (every Budgets page visit)."""
    try:
        period = await billing_service.find_open_period(db, org_id)
        owed = await _owed(db, org_id, period.start_date) if period is not None else []
        if not owed:
            return True
        # An org that lost Plans cannot reach its plan to finish it (TBD-802).
        if not await feature_gate.resolve_feature(feature_gate.Feature.PLANS, org_id, db):
            return True
        # One fresh read: a plan committed since the probe is locked, one a
        # Finish holds is missing, and no second locking read can wait on it.
        locked = await lock_plans(db, org_id, skip_locked=True)
        if set(owed) - {p.id for p in locked}:
            await db.commit()  # release the ones it did lock
            logger.info("plans.rollover.skipped_busy", extra={"org_id": org_id, "plan_ids": owed})
            return True
        for plan in locked:
            if plan.status == _ACTIVE and not await attention(db, plan):
                await _ensure_budgets(db, plan, period.start_date)
        await db.commit()
        return True
    except Exception:  # see the docstring: the caller's work has committed
        logger.exception("plans.rollover.failed", extra={"org_id": org_id})
        try:
            await db.rollback()
            await reload(db)
        except Exception:  # the database is gone: the caller's next read says so
            logger.exception("plans.rollover.reload_failed", extra={"org_id": org_id})
        return False


# ── Finish and Delete ────────────────────────────────────────────────────


async def finish(db: AsyncSession, *, plan: Scenario) -> Scenario:
    plan = await lock(db, plan)
    if plan.status == ScenarioStatus.FINISHED:
        return plan
    if plan.status != _ACTIVE:
        raise ConflictError("only an active plan can be finished")
    plan.status, plan.finished_at = ScenarioStatus.FINISHED, utcnow_naive()
    await _wind_down(db, plan)
    return plan


async def delete_plan(db: AsyncSession, *, plan: Scenario) -> None:
    """Any state. An active plan gets Finish's side effects first; its
    category, tag, accounts, current and past Budgets and transactions stay
    as ordinary rows."""
    plan = await lock(db, plan)
    # A finished plan was wound down at Finish: what the user did since is theirs.
    if plan.status == _ACTIVE:
        await _wind_down(db, plan)
    # Explicit, though ``plan_goals`` cascades: SQLite runs without FK enforcement.
    await db.execute(delete(PlanGoal).where(PlanGoal.scenario_id == plan.id))
    await db.delete(plan)


async def _wind_down(db: AsyncSession, plan: Scenario) -> None:
    for goal in await plan_draft_service.list_goals(db, plan=plan):
        series = goal.funding_recurring_id and await db.get(RecurringTransaction, goal.funding_recurring_id)
        if series and series.org_id == plan.org_id and series.is_active:
            await recurring_service.stop_recurring_no_commit(db, plan.org_id, series.id)
    # Read-only: a Finish must not create a period. Every started plan's org has one.
    period = await billing_service.find_open_period(db, plan.org_id)
    if period is not None and plan.category_id is not None:
        await db.execute(delete(Budget).where(
            Budget.org_id == plan.org_id, Budget.category_id == plan.category_id,
            Budget.period_start > period.start_date))


# ── Broken links and progress ────────────────────────────────────────────


async def _live(db: AsyncSession, model, ref: int | None, org_id: int):
    row = ref is not None and await db.get(model, ref)
    return None if not row or row.org_id != org_id or getattr(row, "is_active", True) is False else row


async def attention(db: AsyncSession, plan: Scenario) -> list[str]:
    """Why an active plan needs attention: a link the user deleted, archived,
    stopped or moved. Empty when healthy; a draft or finished plan has none
    to break. The rollover skips a plan with any."""
    if plan.status != _ACTIVE:
        return []
    out = []
    master = await _live(db, Category, plan.category_id, plan.org_id)
    if master is None or master.parent_id is not None:
        out.append("the plan's category is gone or no longer a master category")
    if await _live(db, Tag, plan.tag_id, plan.org_id) is None:
        out.append("the plan's tag is gone")
    goals = [g for g in await plan_draft_service.list_goals(db, plan=plan) if g.enabled]
    today = datetime.date.today()
    specs = {g.id: _series_spec(plan, g, today) for g in goals}
    draws = any(s and s["account_id"] == plan.funding_account_id for s in specs.values())
    if (plan.funding_account_id is not None or draws) and await _live(
            db, Account, plan.funding_account_id, plan.org_id) is None:
        out.append("the plan's funding account is gone")
    for g in goals:
        child = await _live(db, Category, g.category_id, plan.org_id)
        if child is None:
            out.append(f"goal {g.id}: its category is gone")
        elif child.parent_id != plan.category_id:
            out.append(f"goal {g.id}: its category is no longer under the plan's category")
        if (g.account_id is not None or g.kind in _TRANSFER_KINDS and g.monthly_amount) and await _live(
                db, Account, g.account_id, plan.org_id) is None:
            out.append(f"goal {g.id}: its account is gone")
        spec = specs[g.id]
        if spec is None and g.funding_recurring_id is None:
            continue
        series = await _live(db, RecurringTransaction, g.funding_recurring_id, plan.org_id)
        if series is None:
            out.append(f"goal {g.id}: its funding series is gone or stopped")
        elif (series.category_id, series.account_id, series.to_account_id) != (
                g.category_id, (spec or {}).get("account_id", series.account_id),
                (spec or {}).get("to_account_id", series.to_account_id)):
            out.append(f"goal {g.id}: its funding series was moved to another category or account")
    return out


async def progress(db: AsyncSession, *, plan: Scenario) -> list[dict[str, Any]]:
    """Per goal, ``saved`` (its account's balance less its start balance) and,
    for a spend goal, ``spent`` (settled spending in its category since Start,
    up to Finish). Keyed on category and account only (module docstring)."""
    out = []
    for g in await plan_draft_service.list_goals(db, plan=plan):
        saved = spent = None
        account = await _live(db, Account, g.account_id, plan.org_id)
        if account is not None and g.start_balance is not None:
            saved = (account.balance - g.start_balance).quantize(_CENTS)
        if g.kind in _SPEND_KINDS and g.category_id is not None and plan.started_at is not None:
            spent = Decimal(str(await db.scalar(select(func.coalesce(func.sum(-signed_amount_expr()), 0)).where(
                Transaction.org_id == plan.org_id, Transaction.category_id == g.category_id,
                Transaction.status == TransactionStatus.SETTLED, reportable_transaction_filter(),
                Transaction.date >= plan.started_at.date(),
                *([Transaction.date <= plan.finished_at.date()] if plan.finished_at else []),
            )))).quantize(_CENTS)
        out.append({"goal_id": g.id, "saved": saved, "spent": spent})
    return out
