"""Plans lifecycle (TBD-602): Draft -> Start -> Finish | Delete, edit after
start, the budget rollover, broken links and progress.

Every transition is one transaction that the router commits: the plan row is
locked ``FOR UPDATE`` (a concurrent second call waits, then finds the new
status), the status is written first, and each step creates its row unless the
plan or goal already links one. So a re-run, or the call that lost the lock
race, is a no-op: Start on an active plan and Finish on a finished one return
it unchanged.

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
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app._time import utcnow_naive
from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.recurring import RecurringTransaction
from app.models.scenario import PlanGoal, PlanGoalKind, Scenario, ScenarioStatus
from app.models.tag import Tag
from app.models.transaction import Transaction, TransactionStatus
from app.schemas.plan_projection import PlanProjectionResponse
from app.schemas.recurring import RECURRING_DESCRIPTION_MAX_LENGTH, RecurringCreate
from app.services import (
    billing_service,
    plan_draft_service,
    plan_projection_service,
    recurring_service,
    tag_service,
)
from app.services.account_balance_forecast_service import LIABILITY_SLUGS
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


async def lock(db: AsyncSession, plan_id: int) -> Scenario:
    """The plan row, locked and re-read (``populate_existing``: the identity
    map must carry the locked state, the codebase's FOR UPDATE invariant)."""
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
    period = await billing_service.get_current_period(db, plan.org_id, today=today)
    plan = await lock(db, plan.id)
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

    await _master_category(db, plan)
    await _tag(db, plan)
    for goal in goals:
        await _start_goal_steps(db, plan, goal, today)
    await _ensure_budgets(db, plan, period.start_date)
    return plan


async def start_goal(db: AsyncSession, *, plan: Scenario, goal: PlanGoal,
                     today: datetime.date | None = None) -> None:
    """Edit after start: a goal added or enabled on an active plan (locked by
    the caller) gets Start's steps, for that goal only."""
    await db.flush()
    await _check_topology(db, plan)
    await _master_category(db, plan)
    await _start_goal_steps(db, plan, goal, today or datetime.date.today())


async def _check_topology(db: AsyncSession, plan: Scenario) -> None:
    """Money flows one way: from a funding account into goal accounts. So a
    funding account is no active plan's goal account (its own plan's
    included), a goal account is no active plan's funding account, neither is
    a liability, and an account backs one enabled goal. ``plan`` is active
    (flushed), so the queries below include it."""
    active = (await db.scalars(select(Scenario).where(
        Scenario.org_id == plan.org_id, Scenario.status == _ACTIVE))).all()
    goals = (await db.scalars(select(PlanGoal).where(
        PlanGoal.scenario_id.in_([p.id for p in active]), PlanGoal.enabled.is_(True),
        PlanGoal.account_id.is_not(None)))).all()
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
            raise ValidationError(
                f"account {plan.funding_account_id} is the goal account of an active plan, "
                "so it cannot fund this one")
    for account_id in mine:
        if slugs.get(account_id) in LIABILITY_SLUGS:
            raise ValidationError("a goal cannot save into a credit card or loan")
        if account_id in funding:
            raise ValidationError(f"account {account_id} funds an active plan, so it cannot be a goal account")
        if goal_accounts[account_id] > 1:
            raise ValidationError(f"account {account_id} already backs another enabled goal")


async def _master_category(db: AsyncSession, plan: Scenario) -> Category:
    if plan.category_id is not None:
        cat = await db.get(Category, plan.category_id)
        if cat is None or cat.parent_id is not None:
            # Budgets are master-only, and the plan's Budget is on this category.
            raise ValidationError("a plan's category must be a master category")
        return cat
    cat = Category(org_id=plan.org_id, name=plan.name[:100], type=CategoryType.BOTH)
    db.add(cat)
    await db.flush()
    plan.category_id = cat.id
    return cat


def _tag_name(plan: Scenario) -> str:
    """The plan name in tag form (``normalize_tag_name`` refuses anything else)."""
    cleaned = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 \-]", " ", plan.name.lower())).strip()
    return cleaned[:tag_service.TAG_NAME_MAX_LENGTH].strip() or f"plan {plan.id}"


async def _tag(db: AsyncSession, plan: Scenario) -> None:
    if plan.tag_id is None:
        name = _tag_name(plan)
        tag = await tag_service._get_tag_by_normalized(db, plan.org_id, name) or await tag_service.create_tag(
            db, org_id=plan.org_id, name=name, created_by_user_id=plan.user_id)
        plan.tag_id = tag.id
    held = await db.scalar(select(Scenario.id).where(
        Scenario.tag_id == plan.tag_id, Scenario.id != plan.id, Scenario.status != ScenarioStatus.DRAFT))
    if held is not None:
        raise ValidationError(
            f"the tag of plan {held} would be shared: rename this plan or link another tag")


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
        # Occurrences today, today + 1 month, ... up to and including the target date.
        months = (goal.target_date.year - today.year) * 12 + goal.target_date.month - today.month
        count = months + (today + relativedelta(months=months) <= goal.target_date)
        if count <= 0:
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
        return
    spec = _series_spec(plan, goal, today)
    if spec is None:
        return
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
    moves the balance without being progress."""
    account = await db.get(Account, account_id)
    pending = await db.scalar(select(func.coalesce(func.sum(signed_amount_expr()), 0)).where(
        Transaction.account_id == account_id, Transaction.status == TransactionStatus.PENDING,
        Transaction.is_manual_adjustment.is_(False), non_reverted_transaction_filter()))
    return (account.balance + Decimal(str(pending))).quantize(_CENTS)


# ── Budgets and the rollover ─────────────────────────────────────────────


async def _ensure_budgets(db: AsyncSession, plan: Scenario, current_start: datetime.date) -> None:
    """The plan's Budget on its master category in the current period and
    every later one that exists (stubs), unless one is there already
    (``uq_budget_org_cat_period``)."""
    if plan.monthly_budget is None or plan.category_id is None:
        return
    have = set((await db.scalars(select(Budget.period_start).where(
        Budget.org_id == plan.org_id, Budget.category_id == plan.category_id,
        Budget.period_start >= current_start))).all())
    for period in (await db.scalars(select(BillingPeriod).where(
            BillingPeriod.org_id == plan.org_id, BillingPeriod.start_date >= current_start))).all():
        if period.start_date not in have:
            # ``period_end`` is the raw row value, as ``budget_service.create_budget`` writes it.
            db.add(Budget(org_id=plan.org_id, category_id=plan.category_id, amount=plan.monthly_budget,
                          period_start=period.start_date, period_end=period.end_date))


async def budget_changed(db: AsyncSession, *, plan: Scenario) -> None:
    """Edit after start: a new ``monthly_budget`` reaches the current Budget
    and the later ones the plan already has. Past Budgets are history."""
    period = await billing_service.find_open_period(db, plan.org_id)
    if period is None or plan.category_id is None:
        return
    for budget in (await db.scalars(select(Budget).where(
            Budget.org_id == plan.org_id, Budget.category_id == plan.category_id,
            Budget.period_start >= period.start_date))).all():
        budget.amount = plan.monthly_budget
    await _ensure_budgets(db, plan, period.start_date)


async def rollover(db: AsyncSession, org_id: int) -> None:
    """Every healthy active plan's Budget in the open period and the stubs
    after it; a plan that needs attention is skipped. Run by
    ``billing_service.close_period`` (the scheduler and the manual close both
    call it) and ``ensure_future_periods`` (periods made outside a close).

    Commits, and never raises: it runs after the close has committed, and a
    close must not be reported failed over a Budget. The work sits in a
    SAVEPOINT, so a failure discards only it and expires only the rows it
    touched, never the caller's. A miss is filled by the next
    ``ensure_future_periods`` (every Budgets page visit)."""
    try:
        async with db.begin_nested():
            period = await billing_service.find_open_period(db, org_id)
            # Locked: a Finish racing this waits, or is waited for and seen.
            plans = [] if period is None else (await db.scalars(select(Scenario).where(
                Scenario.org_id == org_id, Scenario.status == _ACTIVE).order_by(Scenario.id)
                .with_for_update().execution_options(populate_existing=True))).all()
            for plan in plans:
                if not await attention(db, plan):
                    await _ensure_budgets(db, plan, period.start_date)
    except Exception:  # see the docstring: the close has committed
        logger.warning("plans.rollover_failed", extra={"org_id": org_id}, exc_info=True)
    await db.commit()


# ── Finish and Delete ────────────────────────────────────────────────────


async def finish(db: AsyncSession, *, plan: Scenario) -> Scenario:
    plan = await lock(db, plan.id)
    if plan.status == ScenarioStatus.FINISHED:
        return plan
    if plan.status != _ACTIVE:
        raise ConflictError("only an active plan can be finished")
    plan.status, plan.finished_at = ScenarioStatus.FINISHED, utcnow_naive()
    await _wind_down(db, plan)
    return plan


async def delete_plan(db: AsyncSession, *, plan: Scenario) -> None:
    """Any state. A started plan gets Finish's side effects first; its
    category, tag, accounts, current and past Budgets and transactions stay
    as ordinary rows."""
    plan = await lock(db, plan.id)
    if plan.status != ScenarioStatus.DRAFT:
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
