"""TBD-602: a plan's Start on REAL MySQL.

Run with PLAN_LIFECYCLE_MYSQL_URL=mysql+aiomysql://... pointing at a
disposable database already at ``alembic upgrade head``. Two concurrent Starts
of one plan must create one set of rows: the plan row's ``FOR UPDATE`` makes
the second wait, then find the plan active. SQLite drops ``FOR UPDATE``, so
this fence needs InnoDB. The fixture deletes the rows these tests write.
"""
from __future__ import annotations

import asyncio
import datetime
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Account, AccountType, Organization
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category
from app.models.recurring import RecurringTransaction
from app.models.scenario import PlanGoal, Scenario, ScenarioStatus, ScenarioType
from app.models.tag import Tag
from app.models.user import Role, User
from app.services import (
    billing_service,
    plan_lifecycle_service,
    plan_projection_service,
)

URL = os.environ.get("PLAN_LIFECYCLE_MYSQL_URL")
if not URL:
    # CI sets REQUIRED=1, so a missing URL there is red, never a skip.
    if os.environ.get("PLAN_LIFECYCLE_MYSQL_REQUIRED") == "1":
        raise RuntimeError("PLAN_LIFECYCLE_MYSQL_URL is required here")
    pytestmark = pytest.mark.skipif(True, reason="PLAN_LIFECYCLE_MYSQL_URL not set")

_CREATED_ORGS: list[int] = []
# Children first: categories reference their parent, scenarios their links.
_TABLES = ("budgets", "plan_goals", "scenarios", "recurring_transactions", "tags", "billing_periods",
           "accounts", "account_types", "users")


@pytest_asyncio.fixture
async def eng():
    e = create_async_engine(URL)

    @event.listens_for(e.sync_engine, "connect")
    def _short_wait(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("SET SESSION innodb_lock_wait_timeout = 10")
        cur.close()

    _CREATED_ORGS.clear()
    try:
        yield e
    finally:
        async with e.begin() as conn:
            for org_id in _CREATED_ORGS:
                params = {"o": org_id}
                for table in _TABLES[:4]:
                    await conn.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), params)
                await conn.execute(text("DELETE FROM categories WHERE org_id = :o AND parent_id IS NOT NULL"), params)
                await conn.execute(text("DELETE FROM categories WHERE org_id = :o"), params)
                for table in _TABLES[4:]:
                    await conn.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), params)
                await conn.execute(text("DELETE FROM organizations WHERE id = :o"), params)
        await e.dispose()


@pytest.fixture
def factory(eng):
    return async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)


async def _seed(factory) -> dict:
    """A draft plan funded from checking, with a monthly spend and a savings goal."""
    today = datetime.date.today()
    async with factory() as db:
        org = Organization(name=f"Plans {uuid.uuid4()}", billing_cycle_day=1, primary_currency="EUR")
        db.add(org)
        await db.flush()
        _CREATED_ORGS.append(org.id)
        user = User(org_id=org.id, username=f"u{uuid.uuid4().hex[:12]}",
                    email=f"{uuid.uuid4().hex[:12]}@example.com", password_hash="x",
                    role=Role.OWNER, is_active=True, email_verified=True)
        checking = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=False)
        db.add_all([user, checking])
        await db.flush()
        main = Account(org_id=org.id, name="Main", account_type_id=checking.id,
                       balance=Decimal("1000.00"), currency="EUR")
        pot = Account(org_id=org.id, name="Pot", account_type_id=checking.id,
                      balance=Decimal("0.00"), currency="EUR")
        db.add_all([main, pot, BillingPeriod(
            org_id=org.id, start_date=billing_service.current_cycle_window(1, today)[0])])
        await db.flush()
        plan = Scenario(org_id=org.id, user_id=user.id, name=f"Trip {uuid.uuid4().hex[:6]}",
                        scenario_type=ScenarioType.CUSTOM,
                        params_json={"scenario_type": "custom", "label": "x", "events": []},
                        currency="EUR", monthly_budget=Decimal("300.00"), funding_account_id=main.id)
        db.add(plan)
        await db.flush()
        db.add_all([
            PlanGoal(org_id=org.id, scenario_id=plan.id, name="Food", tier="want",
                     kind="monthly_spend", monthly_amount=Decimal("100.00")),
            PlanGoal(org_id=org.id, scenario_id=plan.id, name="Fund", tier="want",
                     kind="savings_target", monthly_amount=Decimal("50.00"), account_id=pot.id),
        ])
        await db.commit()
        return {"org": org.id, "plan": plan.id}


async def _start(factory, plan_id: int) -> None:
    async with factory() as db:
        await plan_lifecycle_service.start(db, plan=await db.get(Scenario, plan_id))
        await db.commit()


async def test_concurrent_starts_create_one_set_of_rows(factory, monkeypatch):
    """FENCE (TBD-602). Wrong implementation: Start without the row lock (or
    reading the status before it), so the second call also sees a draft and
    both create a category, a tag, a Budget and the series."""
    s = await _seed(factory)
    in_first, release = asyncio.Event(), asyncio.Event()
    real = plan_projection_service.project

    async def held(*a, **k):
        # The first Start holds the plan's lock here until released.
        if not in_first.is_set():
            in_first.set()
            await release.wait()
        return await real(*a, **k)

    monkeypatch.setattr(plan_projection_service, "project", held)
    first = asyncio.create_task(_start(factory, s["plan"]))
    await asyncio.wait_for(in_first.wait(), timeout=10)
    second = asyncio.create_task(_start(factory, s["plan"]))
    await asyncio.sleep(1)
    assert not second.done(), "the second Start did not wait on the plan's row lock"
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=20)

    async with factory() as db:
        assert (await db.get(Scenario, s["plan"])).status == ScenarioStatus.ACTIVE

        async def count(model):
            return await db.scalar(select(func.count()).select_from(model).where(model.org_id == s["org"]))

        # The master and one child per goal; one tag, one Budget, one series per goal.
        assert [await count(m) for m in (Category, Tag, Budget, RecurringTransaction)] == [3, 1, 1, 2]
