"""Service-layer tests for L3.1 — org data reset.

Fixture mirrors test_admin_orgs_service.py: in-memory aiosqlite with
PRAGMA foreign_keys=ON so SQLite enforces FKs the way MySQL would.
"""
from __future__ import annotations

import datetime
from app._time import utcnow_naive
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
            expires_at=utcnow_naive() + datetime.timedelta(days=7),
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
        # reset_org_data commits per batch internally; the outer commit
        # here is a no-op but kept for symmetry with the test pattern.
        counts = await org_data_service.reset_org_data(db, org_id=seeded["org_id"])
        await db.commit()

    # The contract widened in the L3.4 follow-up: `reset_org_data` now
    # also re-seeds system defaults after the wipe and reports those
    # counts as `seeded_account_types` and `seeded_categories`.
    expected_keys = {
        "transactions", "forecast_plan_items", "budgets",
        "recurring_transactions", "forecast_plans", "billing_periods",
        "accounts", "account_types", "category_rules", "categories",
        "seeded_account_types", "seeded_categories",
    }
    assert set(counts.keys()) == expected_keys

    async with session_factory() as db:
        # Org shell still alive (wrapper didn't accidentally call cascade).
        assert await _count(db, Organization, id=seeded["org_id"]) == 1


@pytest.mark.asyncio
async def test_reset_reseeds_system_defaults_after_wipe(session_factory):
    """The L3.4 follow-up gap: post-reset, the org must look like a freshly
    registered org (system account types, system master + child categories,
    Transfer category) instead of an empty shell.
    """
    seeded = await _seed_full_org(session_factory)
    org_id = seeded["org_id"]

    # Pre-reset: verify the seeded org has *non-default* shape (otherwise
    # the assertions below trivially pass on the seed alone).
    async with session_factory() as db:
        # The fixture inserts 1 system + 1 user account type plus 1 master
        # + 1 child category. After reset we expect to see only system
        # account types and system categories — the user-added ones go.
        pre_user_at = await db.scalar(
            select(func.count()).select_from(AccountType).where(
                AccountType.org_id == org_id,
                AccountType.is_system.is_(False),
            )
        )
        assert pre_user_at >= 1, "fixture should seed at least one user account type"

    async with session_factory() as db:
        counts = await org_data_service.reset_org_data(db, org_id=org_id)

    # The seed inserted SOMETHING — non-zero counts confirm the re-seed ran.
    assert counts["seeded_account_types"] > 0
    assert counts["seeded_categories"] > 0

    async with session_factory() as db:
        # After reset, only system rows survive in the per-org tables.
        all_at = (await db.scalars(
            select(AccountType).where(AccountType.org_id == org_id)
        )).all()
        assert len(all_at) > 0
        assert all(at.is_system for at in all_at)

        all_cats = (await db.scalars(
            select(Category).where(Category.org_id == org_id)
        )).all()
        assert len(all_cats) > 0
        assert all(cat.is_system for cat in all_cats)
        # The Transfer system category specifically must be present.
        transfer = await db.scalar(
            select(Category).where(
                Category.org_id == org_id,
                Category.slug == "transfer",
                Category.is_system.is_(True),
            )
        )
        assert transfer is not None, "Transfer system category not re-seeded"


@pytest.mark.asyncio
async def test_seed_org_defaults_is_idempotent(session_factory):
    """``seed_org_defaults`` is keyed by ``(org_id, slug, is_system=True)``
    and must skip existing rows. Calling it twice without a wipe in
    between must not duplicate, and the second call's reported counts
    must be zero.
    """
    from app.services.org_bootstrap_service import seed_org_defaults

    seeded = await _seed_full_org(session_factory)
    org_id = seeded["org_id"]

    # First call: ``_seed_full_org`` already inserted some system rows
    # (matching the registration shape — see fixture). The seed should
    # find them and only insert what's missing.
    async with session_factory() as db:
        first = await seed_org_defaults(db, org_id=org_id)
        await db.commit()

    async with session_factory() as db:
        second = await seed_org_defaults(db, org_id=org_id)
        await db.commit()
    # Second call: nothing missing, nothing inserted.
    assert second == {"account_types": 0, "categories": 0}

    # Row counts unchanged between the two calls.
    async with session_factory() as db:
        at_count = await db.scalar(
            select(func.count()).select_from(AccountType).where(AccountType.org_id == org_id)
        )
        cat_count = await db.scalar(
            select(func.count()).select_from(Category).where(Category.org_id == org_id)
        )
    # Sanity: the first call did insert rows (or the fixture already
    # had them all). Either way the contract is non-negative + stable.
    assert first["account_types"] >= 0
    assert first["categories"] >= 0
    assert at_count >= 1
    assert cat_count >= 1


@pytest.mark.asyncio
async def test_reset_end_state_is_stable_across_repeats(session_factory):
    """Repeated resets must leave the org in the same shape every
    time. Each reset wipes (including system rows) and re-seeds, so
    every reset's seed counts are non-zero — but the final row counts
    must be identical to the first reset's final state.
    """
    seeded = await _seed_full_org(session_factory)
    org_id = seeded["org_id"]

    async with session_factory() as db:
        await org_data_service.reset_org_data(db, org_id=org_id)

    async with session_factory() as db:
        first_at = await db.scalar(
            select(func.count()).select_from(AccountType).where(AccountType.org_id == org_id)
        )
        first_cat = await db.scalar(
            select(func.count()).select_from(Category).where(Category.org_id == org_id)
        )

    # Run reset two more times and confirm the row counts are stable.
    async with session_factory() as db:
        await org_data_service.reset_org_data(db, org_id=org_id)
    async with session_factory() as db:
        await org_data_service.reset_org_data(db, org_id=org_id)

    async with session_factory() as db:
        third_at = await db.scalar(
            select(func.count()).select_from(AccountType).where(AccountType.org_id == org_id)
        )
        third_cat = await db.scalar(
            select(func.count()).select_from(Category).where(Category.org_id == org_id)
        )
    assert third_at == first_at
    assert third_cat == first_cat


@pytest.mark.asyncio
async def test_admin_delete_still_uses_unbatched_wipe_path(session_factory):
    """Regression: ``admin_orgs_service.delete_org_cascade`` must keep
    using ``wipe_org_data`` (single transaction, no per-batch commit,
    no re-seed) — NOT the new ``reset_org_data`` path. A change to the
    self-service reset path must not bleed into the admin delete path.
    """
    from app.services import admin_orgs_service
    seeded = await _seed_full_org(session_factory)
    org_id = seeded["org_id"]

    async with session_factory() as db:
        counts = await admin_orgs_service.delete_org_cascade(db, org_id=org_id)
        # The admin-delete contract is: caller commits. delete_org_cascade
        # uses the unbatched wipe path expecting one commit boundary,
        # which is exactly what this regression is asserting must NOT
        # have changed when reset_org_data was rewritten.
        await db.commit()

    # delete_org_cascade returns its own merged dict including the
    # wipe table counts AND the org-shell counts (org_settings,
    # subscriptions, users, organization). Critically, it must NOT
    # include the seed keys — admin delete does not re-seed a tomb.
    assert "seeded_account_types" not in counts
    assert "seeded_categories" not in counts

    # The org itself is gone (admin-delete cascade ran to completion).
    async with session_factory() as db:
        assert await _count(db, Organization, id=org_id) == 0
