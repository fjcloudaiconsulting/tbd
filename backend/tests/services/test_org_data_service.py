"""Service-layer tests for L3.1 — org data reset.

Fixture mirrors test_admin_orgs_service.py: in-memory aiosqlite with
PRAGMA foreign_keys=ON so SQLite enforces FKs the way MySQL would.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.category_rule import CategoryRule, RuleSource
from app.models.feature_override import OrgFeatureOverride
from app.models.merchant_dictionary import MerchantDictionaryEntry
from app.models.forecast_plan import (
    ForecastItemType, ForecastPlan, ForecastPlanItem, ItemSource, PlanStatus,
)
from app.models.invitation import Invitation
from app.models.recurring import Frequency, RecurringTransaction
from app.models.settings import OrgSetting
from app.models.subscription import (
    BillingInterval, Plan, Subscription, SubscriptionStatus,
)
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import org_data_service


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_full_org(factory, *, name: str = "Acme") -> dict:
    """Seed an org plus one row in every wipe-list AND preserve-list table.

    Returns ``{"org_id": int, "owner_id": int}``.
    """
    async with factory() as db:
        plan = (
            await db.execute(select(Plan).where(Plan.slug == "free"))
        ).scalar_one_or_none()
        if plan is None:
            plan = Plan(slug="free", name="Free")
            db.add(plan)
            await db.commit()

        org = Organization(name=name, billing_cycle_day=1)
        db.add(org)
        await db.commit()

        owner = User(
            org_id=org.id, username=f"{name}_owner",
            email=f"{name}_owner@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        member = User(
            org_id=org.id, username=f"{name}_member",
            email=f"{name}_member@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([owner, member])
        await db.commit()

        sub = Subscription(
            org_id=org.id, plan_id=plan.id,
            status=SubscriptionStatus.TRIALING,
            billing_interval=BillingInterval.MONTHLY,
            trial_start=datetime.date.today(),
            trial_end=datetime.date.today() + datetime.timedelta(days=14),
        )
        db.add(sub)

        atype = AccountType(org_id=org.id, name="Checking", slug=f"checking-{name}")
        db.add(atype)
        await db.commit()

        account = Account(
            org_id=org.id, account_type_id=atype.id,
            name="Main", balance=Decimal("100.00"),
        )
        master = Category(
            org_id=org.id, name="Food", slug=f"food-{name}",
            type=CategoryType.EXPENSE,
        )
        db.add_all([account, master])
        await db.commit()

        sub_cat = Category(
            org_id=org.id, parent_id=master.id, name="Groceries",
            slug=f"groceries-{name}", type=CategoryType.EXPENSE,
        )
        db.add(sub_cat)
        bp = BillingPeriod(
            org_id=org.id,
            start_date=datetime.date.today().replace(day=1),
            end_date=None,
        )
        db.add(bp)
        await db.commit()

        recurring = RecurringTransaction(
            org_id=org.id, account_id=account.id, category_id=master.id,
            description="Rent", amount=Decimal("1500.00"),
            type="expense", frequency=Frequency.MONTHLY,
            next_due_date=datetime.date.today(),
        )
        db.add(recurring)
        await db.commit()

        tx = Transaction(
            org_id=org.id, account_id=account.id, category_id=master.id,
            recurring_id=recurring.id,
            description="Lunch", amount=Decimal("12.34"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=datetime.date.today(),
            settled_date=datetime.date.today(),
        )
        budget = Budget(
            org_id=org.id, category_id=master.id,
            amount=Decimal("400.00"),
            period_start=datetime.date.today().replace(day=1),
        )
        plan_row = ForecastPlan(
            org_id=org.id, billing_period_id=bp.id, status=PlanStatus.ACTIVE,
        )
        setting = OrgSetting(
            org_id=org.id, key=f"{name}_setting", value="x",
        )
        db.add_all([tx, budget, plan_row, setting])
        await db.commit()

        plan_item = ForecastPlanItem(
            plan_id=plan_row.id, org_id=org.id, category_id=master.id,
            type=ForecastItemType.EXPENSE, source=ItemSource.MANUAL,
            planned_amount=Decimal("400.00"),
        )
        invite = Invitation(
            org_id=org.id, email=f"invitee_{name}@acme.io",
            role=Role.MEMBER, open_email=f"invitee_{name}@acme.io",
            created_by=owner.id,
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=7),
        )
        rule = CategoryRule(
            org_id=org.id,
            normalized_token=f"TEST{name.upper()}",
            raw_description_seen=f"POS {name} *0001",
            category_id=master.id,
            match_count=1,
            source=RuleSource.USER_PICK,
        )
        override = OrgFeatureOverride(
            org_id=org.id,
            feature_key="ai.budget",
            value=False,
            set_by=owner.id,
        )
        db.add_all([plan_item, invite, rule, override])
        await db.commit()

        return {"org_id": org.id, "owner_id": owner.id}


async def _count(db: AsyncSession, model, **filt) -> int:
    stmt = select(func.count()).select_from(model)
    for k, v in filt.items():
        stmt = stmt.where(getattr(model, k) == v)
    return (await db.scalar(stmt)) or 0


# ── wipe_org_data ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wipe_clears_all_org_scoped_data(session_factory):
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        counts = await org_data_service.wipe_org_data(db, org_id=seeded["org_id"])
        await db.commit()

    expected_keys = {
        "transactions", "forecast_plan_items", "budgets",
        "recurring_transactions", "forecast_plans", "billing_periods",
        "accounts", "account_types", "category_rules", "categories",
    }
    assert set(counts.keys()) == expected_keys
    for key, n in counts.items():
        assert n >= 1, f"expected >=1 row deleted from {key}, got {n}"

    async with session_factory() as db:
        for model in (Transaction, ForecastPlanItem, Budget, RecurringTransaction,
                      ForecastPlan, BillingPeriod, Account, AccountType,
                      CategoryRule, Category):
            assert await _count(db, model, org_id=seeded["org_id"]) == 0, (
                f"{model.__name__} not wiped"
            )


@pytest.mark.asyncio
async def test_wipe_preserves_org_shell(session_factory):
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        await org_data_service.wipe_org_data(db, org_id=seeded["org_id"])
        await db.commit()

    async with session_factory() as db:
        assert await _count(db, Organization, id=seeded["org_id"]) == 1
        assert await _count(db, User, org_id=seeded["org_id"]) == 2
        assert await _count(db, Subscription, org_id=seeded["org_id"]) == 1
        assert await _count(db, OrgSetting, org_id=seeded["org_id"]) == 1
        assert await _count(db, OrgFeatureOverride, org_id=seeded["org_id"]) == 1
        assert await _count(db, Invitation, org_id=seeded["org_id"]) == 1


@pytest.mark.asyncio
async def test_wipe_does_not_touch_merchant_dictionary(session_factory):
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        db.add(MerchantDictionaryEntry(
            normalized_token="LIDL", category_slug="groceries",
            is_seed=True, vote_count=0,
        ))
        await db.commit()

    async with session_factory() as db:
        await org_data_service.wipe_org_data(db, org_id=seeded["org_id"])
        await db.commit()

    async with session_factory() as db:
        rows = (await db.execute(
            select(MerchantDictionaryEntry)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].normalized_token == "LIDL"


@pytest.mark.asyncio
async def test_wipe_does_not_touch_other_orgs(session_factory):
    target = await _seed_full_org(session_factory, name="Target")
    keep = await _seed_full_org(session_factory, name="Keep")

    async with session_factory() as db:
        await org_data_service.wipe_org_data(db, org_id=target["org_id"])
        await db.commit()

    async with session_factory() as db:
        for model in (Transaction, Budget, Account, AccountType, Category,
                      CategoryRule, BillingPeriod, RecurringTransaction,
                      ForecastPlan, ForecastPlanItem):
            assert await _count(db, model, org_id=keep["org_id"]) >= 1, (
                f"{model.__name__} for keep org unexpectedly wiped"
            )


@pytest.mark.asyncio
async def test_wipe_handles_categories_with_parent_id(session_factory):
    """Self-FK on categories.parent_id requires a parent_id-null trick
    before bulk DELETE. If broken, MySQL strict FK refuses; SQLite with
    PRAGMA foreign_keys=ON does the same."""
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        counts = await org_data_service.wipe_org_data(db, org_id=seeded["org_id"])
        await db.commit()
    assert counts["categories"] == 2  # master + sub


@pytest.mark.asyncio
async def test_wipe_idempotent(session_factory):
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        await org_data_service.wipe_org_data(db, org_id=seeded["org_id"])
        await db.commit()
    async with session_factory() as db:
        second = await org_data_service.wipe_org_data(db, org_id=seeded["org_id"])
        await db.commit()
    assert all(n == 0 for n in second.values())


# ── reset_org_data ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_returns_counts_and_wipes_data(session_factory):
    seeded = await _seed_full_org(session_factory)

    async with session_factory() as db:
        counts = await org_data_service.reset_org_data(db, org_id=seeded["org_id"])
        await db.commit()

    expected_keys = {
        "transactions", "forecast_plan_items", "budgets",
        "recurring_transactions", "forecast_plans", "billing_periods",
        "accounts", "account_types", "category_rules", "categories",
    }
    assert set(counts.keys()) == expected_keys

    async with session_factory() as db:
        # Org shell still alive (wrapper didn't accidentally call cascade).
        assert await _count(db, Organization, id=seeded["org_id"]) == 1
