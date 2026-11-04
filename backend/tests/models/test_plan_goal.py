"""Plans schema, additive only (TBD-598): the new ``scenarios`` columns and
``plan_goals``.

SQLite with ``PRAGMA foreign_keys=ON``, so the CASCADE and SET NULL rules in
the DDL run. The MySQL up/down check of migration 098 is a merge gate.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.scenario import (
    PlanGoal,
    PlanGoalKind,
    PlanGoalTier,
    Scenario,
    ScenarioType,
)
from app.models.tag import Tag
from app.models.transaction import TransactionType
from app.models.user import Organization, Role, User
from app.services import admin_orgs_service, org_data_service


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(db) -> dict:
    """An org with a plan linked to every new FK target, and one goal."""
    org = Organization(name="Acme", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    user = User(
        org_id=org.id, username="u", email="u@example.test", password_hash="x",
        role=Role.OWNER, is_active=True, email_verified=True,
    )
    atype = AccountType(org_id=org.id, name="Savings", slug="savings")
    master = Category(org_id=org.id, name="Trip", type=CategoryType.EXPENSE)
    tag = Tag(org_id=org.id, name="trip", name_normalized="trip")
    db.add_all([user, atype, master, tag])
    await db.flush()
    account = Account(org_id=org.id, account_type_id=atype.id, name="Pot")
    child = Category(
        org_id=org.id, name="Flights", type=CategoryType.EXPENSE, parent_id=master.id
    )
    db.add_all([account, child])
    await db.flush()
    recurring = RecurringTransaction(
        org_id=org.id, account_id=account.id, category_id=child.id,
        amount=Decimal("50.00"), description="Save", type=TransactionType.EXPENSE,
        frequency=Frequency.MONTHLY, next_due_date=date(2026, 12, 1),
    )
    db.add(recurring)
    await db.flush()
    plan = Scenario(
        org_id=org.id, user_id=user.id, name="Lisbon", scenario_type=ScenarioType.TRIP,
        params_json={}, funding_account_id=account.id, category_id=master.id,
        tag_id=tag.id,
    )
    db.add(plan)
    await db.flush()
    goal = PlanGoal(
        org_id=org.id, scenario_id=plan.id, name="Flights",
        tier=PlanGoalTier.ESSENTIAL, kind=PlanGoalKind.SAVINGS_TARGET,
        target_amount=Decimal("800.00"), target_date=date(2027, 6, 1),
        category_id=child.id, account_id=account.id,
        start_balance=Decimal("120.50"), funding_recurring_id=recurring.id,
        params_json={"note": "economy"},
    )
    db.add(goal)
    await db.commit()
    return {"org_id": org.id, "plan_id": plan.id, "goal_id": goal.id}


@pytest.mark.asyncio
async def test_legacy_scenario_row_gets_valid_defaults(session_factory):
    """A row that names only the pre-098 columns is a draft plan with zero
    rates and no links. Raw SQL, so the server defaults are what fills it,
    not the ORM's Python defaults."""
    async with session_factory() as db:
        ids = await _seed(db)
        user_id = await db.scalar(select(User.id))
        await db.execute(
            text(
                "INSERT INTO scenarios (org_id, user_id, name, scenario_type, params_json) "
                "VALUES (:o, :u, 'Old', 'custom', '{}')"
            ),
            {"o": ids["org_id"], "u": user_id},
        )
        await db.commit()
        row = (await db.execute(text(
            "SELECT status, return_pct, inflation_pct, currency, monthly_budget, "
            "funding_account_id, category_id, tag_id, started_at, finished_at, "
            "baseline_projection_json FROM scenarios WHERE name = 'Old'"
        ))).one()
        assert row.status == "draft"
        assert Decimal(str(row.return_pct)) == Decimal(str(row.inflation_pct)) == Decimal("0")
        assert set(row[3:]) == {None}


@pytest.mark.asyncio
async def test_goal_round_trips_with_defaults(session_factory):
    async with session_factory() as db:
        ids = await _seed(db)
        goal = await db.get(PlanGoal, ids["goal_id"])
        assert goal.enabled is True and goal.sort_order == 0
        # VARCHAR since migration 100: the stored string, not the enum member.
        assert goal.tier == PlanGoalTier.ESSENTIAL.value
        assert goal.kind == PlanGoalKind.SAVINGS_TARGET.value
        assert goal.start_balance == Decimal("120.50")
        assert goal.params_json == {"note": "economy"}


@pytest.mark.asyncio
async def test_deleting_a_plan_deletes_its_goals(session_factory):
    async with session_factory() as db:
        ids = await _seed(db)
        await db.delete(await db.get(Scenario, ids["plan_id"]))
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(PlanGoal)) == 0


@pytest.mark.asyncio
async def test_org_reset_detaches_plans_instead_of_failing(session_factory):
    """Reset wipes accounts, categories, tags and recurring rows. The plan
    and its goal survive with their links cleared. A RESTRICT FK would make
    the reset raise."""
    async with session_factory() as db:
        ids = await _seed(db)
        await org_data_service.reset_org_data(db, org_id=ids["org_id"])
        db.expire_all()
        plan = await db.get(Scenario, ids["plan_id"])
        goal = await db.get(PlanGoal, ids["goal_id"])
        assert plan.funding_account_id is plan.category_id is plan.tag_id is None
        assert goal.category_id is goal.account_id is goal.funding_recurring_id is None


@pytest.mark.asyncio
async def test_org_delete_removes_plans_and_goals(session_factory):
    async with session_factory() as db:
        ids = await _seed(db)
        await admin_orgs_service.delete_org_cascade(db, org_id=ids["org_id"])
        await db.commit()
        for model in (Scenario, PlanGoal):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


def test_goal_refuses_an_unknown_tier_or_kind():
    """Migration 100 dropped the ENUMs, so the model is the only check."""
    goal = PlanGoal(tier="want", kind=PlanGoalKind.LUMP)
    assert (goal.tier, goal.kind) == ("want", "lump")
    with pytest.raises(ValueError):
        goal.tier = "nice_to_have"
    with pytest.raises(ValueError):
        goal.kind = "pension"
