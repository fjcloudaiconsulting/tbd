"""Recurring transfers (TBD-600): a series that moves money between two accounts.

A ``transfer`` template generates ONE linked pair per occurrence through
``transaction_service._link_pair`` (an EXPENSE leg on ``account_id``, an INCOME
leg on ``to_account_id``), so saving never reads as spending. The fences:

* every generation entry point (scheduler job, manual generate, catch-up,
  materialise-next) writes exactly one linked pair per occurrence;
* net worth is unchanged by an occurrence; budgets, Sankey and the org
  forecast see no income or expense; the per-account forecast moves both sides;
* stop and delete remove both pending legs; auto_settle settles both legs;
* the API boundary refuses a transfer without a valid destination.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.scenario import Scenario, ScenarioType
from app.schemas.recurring import RecurringCreate, RecurringUpdate
from app.schemas.reports_query import SankeyQuery
from app.schemas.transaction import TransactionUpdate
from app.services import (
    account_balance_forecast_service,
    budget_service,
    category_service,
    forecast_plan_service,
    forecast_service,
    recurring_service,
    scenario_engine,
    transaction_service,
)
from app.services.exceptions import ValidationError
from app.services.sankey_service import build_sankey
from app.services.scheduler.jobs.recurring_generation import RecurringGenerationJob

TODAY = datetime.date(2026, 7, 10)
P_START = datetime.date(2026, 7, 1)
AMOUNT = Decimal("250.00")


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def seed(db) -> dict:
    org = Organization(name="T", billing_cycle_day=1)
    other = Organization(name="Other", billing_cycle_day=1)
    db.add_all([org, other])
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    at_other = AccountType(org_id=other.id, name="Checking", slug="checking", is_system=True)
    db.add_all([at, at_other])
    await db.flush()
    main = Account(org_id=org.id, name="Main", account_type_id=at.id,
                   balance=Decimal("1000.00"), currency="EUR", is_default=True)
    savings = Account(org_id=org.id, name="Savings", account_type_id=at.id,
                      balance=Decimal("0.00"), currency="EUR")
    usd = Account(org_id=org.id, name="USD", account_type_id=at.id,
                  balance=Decimal("0.00"), currency="USD")
    foreign = Account(org_id=other.id, name="Theirs", account_type_id=at_other.id,
                      balance=Decimal("0.00"), currency="EUR")
    db.add_all([main, savings, usd, foreign])
    await db.flush()
    transfer = Category(org_id=org.id, name="Transfer", slug="transfer",
                        type=CategoryType.BOTH, is_system=True)
    food = Category(org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE)
    salary = Category(org_id=org.id, name="Salary", slug="salary", type=CategoryType.INCOME)
    db.add_all([transfer, food, salary])
    await db.flush()
    db.add(BillingPeriod(org_id=org.id, start_date=P_START))
    await db.commit()
    return {"org_id": org.id, "main": main.id, "savings": savings.id, "usd": usd.id,
            "foreign": foreign.id, "transfer": transfer.id, "food": food.id,
            "salary": salary.id}


async def _create(db, seed, **overrides) -> RecurringTransaction:
    body = dict(
        account_id=seed["main"], to_account_id=seed["savings"],
        category_id=seed["transfer"], description="To savings", amount=AMOUNT,
        type="transfer", frequency="monthly",
        next_due_date=datetime.date(2026, 7, 5), auto_settle=False,
    )
    body.update(overrides)
    return await recurring_service.create_recurring(
        db, seed["org_id"], RecurringCreate(**body), today=TODAY
    )


async def _rows(db, org_id) -> list[Transaction]:
    return list((await db.execute(
        select(Transaction).where(Transaction.org_id == org_id).order_by(Transaction.id)
        .execution_options(populate_existing=True)
    )).scalars().all())


async def _balances(db, seed) -> dict[int, Decimal]:
    rows = (await db.execute(
        select(Account.id, Account.balance).where(Account.org_id == seed["org_id"])
    )).all()
    return {aid: bal for aid, bal in rows}


def _assert_pairs(rows: list[Transaction], seed, r_id: int, n: int) -> None:
    """Exactly ``n`` linked pairs of the series, and nothing else."""
    assert len(rows) == 2 * n
    by_id = {t.id: t for t in rows}
    for t in rows:
        partner = by_id[t.linked_transaction_id]
        assert partner.linked_transaction_id == t.id
        assert t.recurring_id == r_id and partner.recurring_id == r_id
        assert t.date == partner.date and t.amount == partner.amount == AMOUNT
        assert t.category_id == seed["transfer"]
    expense = [t for t in rows if t.type == TransactionType.EXPENSE]
    assert {t.account_id for t in expense} == {seed["main"]}
    assert {t.account_id for t in rows if t.type == TransactionType.INCOME} == {seed["savings"]}
    assert len(expense) == n


# ── generation entry points ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_generate_writes_one_linked_pair(db, seed):
    r = await _create(db, seed)
    out = recurring_service.to_response(r)
    assert (out.type, out.to_account_id, out.to_account_name) == (
        "transfer", seed["savings"], "Savings"
    )
    res = await recurring_service.generate_due_transactions(db, seed["org_id"], today=TODAY)
    assert res["generated"] == 1
    _assert_pairs(await _rows(db, seed["org_id"]), seed, r.id, 1)
    # Idempotent: a second run finds the occurrence and writes nothing.
    await recurring_service.generate_due_transactions(db, seed["org_id"], today=TODAY)
    _assert_pairs(await _rows(db, seed["org_id"]), seed, r.id, 1)


@pytest.mark.asyncio
async def test_catch_up_writes_one_pair_per_occurrence(db, seed):
    r = await _create(db, seed, next_due_date=datetime.date(2026, 7, 2), frequency="weekly")
    # Generation runs a cycle later: the frontier is behind, catch-up walks it.
    await recurring_service.generate_due_transactions(
        db, seed["org_id"], today=datetime.date(2026, 7, 31)
    )
    _assert_pairs(await _rows(db, seed["org_id"]), seed, r.id, 5)


@pytest.mark.asyncio
async def test_scheduler_job_writes_one_linked_pair(db, seed, monkeypatch):
    r = await _create(db, seed)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.record_run", _noop
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.dispatch_notification_to_org_members",
        _noop,
    )
    org = await db.get(Organization, seed["org_id"])
    await RecurringGenerationJob().run(db, org, TODAY)
    _assert_pairs(await _rows(db, seed["org_id"]), seed, r.id, 1)


@pytest.mark.asyncio
async def test_materialise_and_skip_next_write_one_pair(db, seed):
    r = await _create(db, seed)
    await recurring_service.materialise_next(
        db, seed["org_id"], r.id, occurrence_date=datetime.date(2026, 7, 5),
        skipped=False, today=TODAY,
    )
    await recurring_service.materialise_next(
        db, seed["org_id"], r.id, occurrence_date=datetime.date(2026, 8, 5),
        skipped=True, today=TODAY,
    )
    rows = await _rows(db, seed["org_id"])
    _assert_pairs(rows, seed, r.id, 2)
    assert all(t.status == TransactionStatus.PENDING for t in rows)
    assert [t.reconciliation_state for t in rows if t.date.month == 8] == ["skipped"] * 2


# ── money: balances, net worth, budgets, Sankey, forecasts ─────────────────


@pytest.mark.asyncio
async def test_settled_occurrence_moves_both_accounts_and_keeps_net_worth(db, seed):
    income = Transaction(
        org_id=seed["org_id"], account_id=seed["main"], category_id=seed["salary"],
        description="Pay", amount=Decimal("3000"), type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 7, 2),
        settled_date=datetime.date(2026, 7, 2),
    )
    spend = Transaction(
        org_id=seed["org_id"], account_id=seed["main"], category_id=seed["food"],
        description="Food", amount=Decimal("400"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=datetime.date(2026, 7, 3),
        settled_date=datetime.date(2026, 7, 3),
    )
    db.add_all([income, spend])
    await db.commit()
    before = await _balances(db, seed)
    sankey_before = await build_sankey(db, org_id=seed["org_id"], query=SankeyQuery(filters=[]))
    forecast_before = await forecast_service.compute_forecast(db, seed["org_id"], today=TODAY)

    await _create(db, seed, auto_settle=True)
    forecast_projected = await forecast_service.compute_forecast(db, seed["org_id"], today=TODAY)
    await recurring_service.generate_due_transactions(db, seed["org_id"], today=TODAY)

    after = await _balances(db, seed)
    assert after[seed["main"]] == before[seed["main"]] - AMOUNT
    assert after[seed["savings"]] == before[seed["savings"]] + AMOUNT
    assert sum(after.values()) == sum(before.values())
    assert all(t.status == TransactionStatus.SETTLED for t in await _rows(db, seed["org_id"]))

    sankey_after = await build_sankey(db, org_id=seed["org_id"], query=SankeyQuery(filters=[]))
    assert sankey_after.links == sankey_before.links
    assert await budget_service._compute_spent(
        db, seed["org_id"], seed["transfer"], P_START, datetime.date(2026, 7, 31)
    ) == Decimal("0")
    forecast_after = await forecast_service.compute_forecast(db, seed["org_id"], today=TODAY)
    for fc in (forecast_projected, forecast_after):
        for key in ("forecast_income", "forecast_expense", "recurring_income",
                    "recurring_expense", "executed_income", "executed_expense"):
            assert fc[key] == forecast_before[key], key


@pytest.mark.asyncio
async def test_plan_populate_plans_no_transfer(db, seed):
    await _create(db, seed, next_due_date=datetime.date(2026, 7, 20))
    plan = await forecast_plan_service.populate_from_sources(
        db, seed["org_id"], P_START, today=TODAY
    )
    assert plan.items == []
    assert plan.total_planned_income == plan.total_planned_expense == Decimal("0")


@pytest.mark.asyncio
async def test_account_forecast_projects_both_legs(db, seed):
    await _create(db, seed, next_due_date=datetime.date(2026, 7, 20))
    fc = await account_balance_forecast_service.compute_account_balance_forecast(
        db, seed["org_id"], today=TODAY
    )
    by_id = {a["account_id"]: a for a in fc["accounts"]}
    assert [line["amount"] for line in by_id[seed["main"]]["recurring_lines"]] == ["-250.00"]
    assert [line["amount"] for line in by_id[seed["savings"]]["recurring_lines"]] == ["250.00"]
    assert Decimal(by_id[seed["main"]]["expected_month_end_balance"]) == Decimal("750.00")
    assert Decimal(by_id[seed["savings"]]["expected_month_end_balance"]) == Decimal("250.00")


@pytest.mark.asyncio
async def test_scenario_projection_moves_both_accounts(db, seed):
    r = await _create(db, seed)
    state = await scenario_engine.build_world_state(db, org_id=seed["org_id"], user_id=0)
    snap = next(s for s in state.recurring if s.id == r.id)
    assert (snap.type, snap.account_id, snap.to_account_id) == (
        "transfer", seed["main"], seed["savings"]
    )
    scenario = Scenario(
        org_id=seed["org_id"], user_id=0, name="baseline", scenario_type=ScenarioType.CUSTOM,
        params_json={"scenario_type": "custom", "label": "t", "events": []}, horizon_months=6,
    )
    result = scenario_engine.AnalyticEngine().simulate(
        scenario_engine.SimulationRequest(
            scenario=scenario, state=state, horizon_months=6, options={}
        )
    )
    final = {
        s["account_id"]: Decimal(s["points"][-1]["projected_balance"])
        for s in result["per_account_series"]
    }
    moved = Decimal("1000.00") - final[seed["main"]]
    assert moved >= AMOUNT * 5
    assert final[seed["savings"]] == moved
    assert final[seed["usd"]] == Decimal("0.00")


# ── stop / delete / auto_settle ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "delete"])
async def test_stop_and_delete_remove_both_pending_legs(db, seed, action):
    # The removal is keyed on the wall clock (pending rows dated today or
    # later), so the occurrence is written ahead of it with materialise-next.
    today = datetime.date.today()
    due = today + datetime.timedelta(days=10)
    r = await _create(db, seed, next_due_date=due)
    await recurring_service.materialise_next(
        db, seed["org_id"], r.id, occurrence_date=due, skipped=False, today=today
    )
    assert len(await _rows(db, seed["org_id"])) == 2
    if action == "stop":
        await recurring_service.stop_recurring(db, seed["org_id"], r.id)
    else:
        r.is_active = False
        await db.commit()
        await recurring_service.delete_recurring(db, seed["org_id"], r.id)
    assert await _rows(db, seed["org_id"]) == []


@pytest.mark.asyncio
async def test_auto_settle_settles_both_legs(db, seed):
    await _create(db, seed, next_due_date=datetime.date(2026, 7, 20), auto_settle=True)
    await recurring_service.generate_due_transactions(db, seed["org_id"], today=TODAY)
    assert all(t.status == TransactionStatus.PENDING for t in await _rows(db, seed["org_id"]))
    before = await _balances(db, seed)

    await recurring_service.generate_due_transactions(
        db, seed["org_id"], today=datetime.date(2026, 7, 20)
    )
    rows = [t for t in await _rows(db, seed["org_id"]) if t.date == datetime.date(2026, 7, 20)]
    assert [t.status for t in rows] == [TransactionStatus.SETTLED] * 2
    after = await _balances(db, seed)
    assert after[seed["main"]] == before[seed["main"]] - AMOUNT
    assert after[seed["savings"]] == before[seed["savings"]] + AMOUNT


# ── API boundary validation ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides, message", [
    ({"to_account_id": None}, "needs a destination"),
    ({"to_account_id": "main"}, "must be different"),
    ({"to_account_id": "foreign"}, "Invalid account"),
    ({"to_account_id": "usd"}, "same currency"),
    ({"category_id": "food"}, "Category type"),
    ({"type": "expense", "category_id": "food"}, "Only a transfer"),
])
async def test_create_refuses_invalid_transfer(db, seed, overrides, message):
    resolved = {k: seed[v] if isinstance(v, str) and v in seed else v for k, v in overrides.items()}
    with pytest.raises(ValidationError, match=message):
        await _create(db, seed, **resolved)
    assert (await db.scalar(select(func.count()).select_from(RecurringTransaction))) == 0


@pytest.mark.asyncio
async def test_update_validates_accounts_and_refuses_type_change(db, seed):
    r_id = (await _create(db, seed)).id
    for body, message in (
        (RecurringUpdate(to_account_id=seed["main"]), "must be different"),
        (RecurringUpdate(account_id=seed["savings"]), "must be different"),
        (RecurringUpdate(to_account_id=seed["usd"]), "same currency"),
        (RecurringUpdate(type="expense"), "cannot change type"),
    ):
        with pytest.raises(ValidationError, match=message):
            await recurring_service.update_recurring(db, seed["org_id"], r_id, body, today=TODAY)
        await db.rollback()
    plain = await _create(db, seed, type="expense", category_id=seed["food"], to_account_id=None)
    with pytest.raises(ValidationError, match="Only a transfer"):
        await recurring_service.update_recurring(
            db, seed["org_id"], plain.id, RecurringUpdate(to_account_id=seed["savings"]), today=TODAY
        )


@pytest.mark.asyncio
async def test_moving_a_leg_does_not_rewrite_the_template(db, seed):
    """A leg's account edit propagates to an income/expense template, never to
    a transfer's: it would rewrite the source with the edited leg's account."""
    r = await _create(db, seed)
    third = Account(org_id=seed["org_id"], name="Third", account_type_id=(
        await db.get(Account, seed["main"])).account_type_id,
        balance=Decimal("0"), currency="EUR")
    db.add(third)
    await db.commit()
    await recurring_service.generate_due_transactions(db, seed["org_id"], today=TODAY)
    income_leg = next(t for t in await _rows(db, seed["org_id"]) if t.type == TransactionType.INCOME)
    await transaction_service.update_transaction(
        db, seed["org_id"], income_leg.id, TransactionUpdate(account_id=third.id)
    )
    r = await db.get(RecurringTransaction, r.id, populate_existing=True)
    assert (r.account_id, r.to_account_id) == (seed["main"], seed["savings"])


@pytest.mark.asyncio
async def test_transfer_template_needs_a_both_migration_target(db, seed):
    """A category whose only dependent is a transfer template is not empty:
    deleting it must migrate the template to a BOTH category."""
    await _create(db, seed)
    breakdown = await category_service._dependent_breakdown(
        db, org_id=seed["org_id"], category_id=seed["transfer"]
    )
    assert (breakdown.income_count, breakdown.expense_count, breakdown.recurring_count) == (1, 1, 1)
    assert await category_service._count_incompatible_for_category(
        db, seed["org_id"], seed["transfer"], CategoryType.EXPENSE
    ) == (0, 1, 0)
