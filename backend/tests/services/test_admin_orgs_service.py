"""Service-layer tests for L4.3 — admin org management.

The fixture enables `PRAGMA foreign_keys=ON` so SQLite enforces
referential integrity the way MySQL would in prod. Without that,
deleting a parent table (categories, users, accounts) before its
children would silently succeed under SQLite while exploding under
MySQL.
"""
from __future__ import annotations

import datetime
from app._time import utcnow_naive
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
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
    ForecastItemType,
    ForecastPlan,
    ForecastPlanItem,
    ItemSource,
    PlanStatus,
)
from app.models.invitation import Invitation
from app.models.recurring import Frequency, RecurringTransaction
from app.models.settings import OrgSetting
from app.models.subscription import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import admin_orgs_service
from app.services.exceptions import NotFoundError, ValidationError


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Enable FK enforcement so SQLite catches violations the way MySQL
    # would. Without this, parent rows can be deleted while children
    # still reference them — the test would pass but prod would explode.
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
    """Build a fully-loaded org so the cascade test exercises every
    child table that references org_id (or transitively does)."""
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

        atype = AccountType(org_id=org.id, name="Checking", slug="checking")
        db.add(atype)
        await db.commit()

        account = Account(
            org_id=org.id, account_type_id=atype.id,
            name="Main", balance=Decimal("100.00"),
        )
        master = Category(
            org_id=org.id, name="Food", slug="food",
            type=CategoryType.EXPENSE,
        )
        db.add_all([account, master])
        await db.commit()

        # Self-FK row — child category referencing the master. The
        # cascade must null parent_id before deleting categories or
        # the FK explodes.
        sub_cat = Category(
            org_id=org.id, parent_id=master.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE,
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
        # Smart-rules row — category_rules.category_id FKs to categories.id,
        # so the cascade must wipe these before the bulk DELETE on categories.
        rule = CategoryRule(
            org_id=org.id,
            normalized_token=f"TEST{name.upper()}",
            raw_description_seen=f"POS {name} *0001",
            category_id=master.id,
            match_count=1,
            source=RuleSource.USER_PICK,
        )
        # L4.11: per-org feature override. The cascade must wipe these
        # before deleting users (set_by FKs to users with ON DELETE SET
        # NULL, but the override row itself is org-scoped).
        override = OrgFeatureOverride(
            org_id=org.id,
            feature_key="ai.budget",
            value=False,
            set_by=owner.id,
        )
        db.add_all([plan_item, invite, rule, override])
        await db.commit()

        return {"org_id": org.id, "owner_id": owner.id}


async def _count(db, model, **filt) -> int:
    from sqlalchemy import func
    stmt = select(func.count()).select_from(model)
    for k, v in filt.items():
        stmt = stmt.where(getattr(model, k) == v)
    return (await db.scalar(stmt)) or 0


@pytest.mark.asyncio
async def test_delete_org_cascade_removes_all_children_and_keeps_other_org(
    session_factory,
):
    target = await _seed_full_org(session_factory, name="Target")
    keep = await _seed_full_org(session_factory, name="Keep")

    # Shared dictionary is org-agnostic — must survive org deletion.
    async with session_factory() as db:
        db.add(MerchantDictionaryEntry(
            normalized_token="LIDL", category_slug="groceries",
            is_seed=True, vote_count=0,
        ))
        await db.commit()

    async with session_factory() as db:
        result = await admin_orgs_service.delete_org_cascade(
            db, org_id=target["org_id"],
        )
        await db.commit()

    # Returned row counts surface what was removed (used by the audit log).
    assert result["organizations"] == 1
    assert result["users"] == 2
    assert result["transactions"] == 1
    assert result["categories"] == 2  # master + sub
    assert result["category_rules"] == 1
    assert result["org_feature_overrides"] == 1
    # Order isn't asserted — just that every table reports.
    expected_tables = {
        "transactions", "forecast_plan_items", "budgets", "invitations",
        "recurring_transactions", "forecast_plans", "billing_periods",
        "accounts", "account_types", "category_rules", "categories",
        "settings", "org_feature_overrides", "users", "subscriptions",
        "organizations",
    }
    assert expected_tables <= set(result.keys())

    async with session_factory() as db:
        # Target gone everywhere.
        assert await _count(db, Organization, id=target["org_id"]) == 0
        assert await _count(db, User, org_id=target["org_id"]) == 0
        assert await _count(db, Transaction, org_id=target["org_id"]) == 0
        assert await _count(db, Category, org_id=target["org_id"]) == 0
        assert await _count(db, Subscription, org_id=target["org_id"]) == 0
        assert await _count(db, Invitation, org_id=target["org_id"]) == 0
        assert await _count(db, CategoryRule, org_id=target["org_id"]) == 0
        assert await _count(db, OrgFeatureOverride, org_id=target["org_id"]) == 0
        # Sibling org survives intact.
        assert await _count(db, Organization, id=keep["org_id"]) == 1
        assert await _count(db, User, org_id=keep["org_id"]) == 2
        assert await _count(db, Transaction, org_id=keep["org_id"]) == 1
        assert await _count(db, Category, org_id=keep["org_id"]) == 2
        assert await _count(db, CategoryRule, org_id=keep["org_id"]) == 1
        assert await _count(db, OrgFeatureOverride, org_id=keep["org_id"]) == 1
        # merchant_dictionary is shared (no org_id) — must NOT be touched.
        from sqlalchemy import func
        md_count = await db.scalar(
            select(func.count()).select_from(MerchantDictionaryEntry)
        )
        assert md_count == 1


@pytest.mark.asyncio
async def test_delete_org_404_when_missing(session_factory):
    async with session_factory() as db:
        with pytest.raises(NotFoundError):
            await admin_orgs_service.delete_org_cascade(db, org_id=99999)


@pytest.mark.asyncio
async def test_list_orgs_paginates_and_returns_metadata(session_factory):
    a = await _seed_full_org(session_factory, name="Alpha")
    b = await _seed_full_org(session_factory, name="Beta")
    async with session_factory() as db:
        page = await admin_orgs_service.list_orgs(db, limit=10, offset=0)
        # Paginated envelope
        assert page["total"] >= 2
        assert page["limit"] == 10
        assert page["offset"] == 0
        names = sorted(item["name"] for item in page["items"])
        assert "Alpha" in names and "Beta" in names
        # Metadata for one row — pick Alpha
        alpha = next(item for item in page["items"] if item["name"] == "Alpha")
        assert alpha["id"] == a["org_id"]
        assert alpha["user_count"] == 2
        assert alpha["active_user_count"] == 2
        # Subscription metadata flows through
        assert alpha["subscription_status"] == "trialing"


@pytest.mark.asyncio
async def test_list_orgs_is_not_n_plus_one(session_factory):
    """Pin the design intent: list_orgs must serve a paged result in a
    bounded number of SQL round-trips, not one fetch per row. Five
    orgs at limit=10 should not balloon the cursor count."""
    for i in range(5):
        await _seed_full_org(session_factory, name=f"Org{i}")

    queries: list[str] = []

    async with session_factory() as db:
        bind = db.get_bind()
        sync_engine = bind.sync_engine if hasattr(bind, "sync_engine") else bind

        @event.listens_for(sync_engine, "before_cursor_execute")
        def _capture(conn, cursor, statement, params, context, executemany):  # noqa: ARG001
            stripped = statement.strip().split()[0].upper()
            if stripped in {"SELECT", "WITH"}:
                queries.append(statement)

        try:
            page = await admin_orgs_service.list_orgs(db, limit=10, offset=0)
        finally:
            event.remove(sync_engine, "before_cursor_execute", _capture)

        assert len(page["items"]) == 5
        # 5 orgs at the previous N+1 implementation = 1 (count) + 1
        # (orgs) + 5 * 5 (sub, plan, user, active_user, newest) = 27.
        # Tighten the bound to something a single grouped query can
        # comfortably stay under.
        assert len(queries) < 10, f"Too many SELECTs: {len(queries)} → {queries}"


@pytest.mark.asyncio
async def test_list_orgs_search_filters_by_name(session_factory):
    await _seed_full_org(session_factory, name="Acme")
    await _seed_full_org(session_factory, name="Beta")
    async with session_factory() as db:
        page = await admin_orgs_service.list_orgs(db, q="bet", limit=10, offset=0)
        names = [item["name"] for item in page["items"]]
        assert names == ["Beta"]


@pytest.mark.asyncio
async def test_get_org_detail_returns_subscription_members_and_counts(session_factory):
    target = await _seed_full_org(session_factory, name="Detail")
    async with session_factory() as db:
        d = await admin_orgs_service.get_org_detail(db, org_id=target["org_id"])
        assert d["name"] == "Detail"
        assert d["subscription"]["status"] == "trialing"
        assert d["subscription"]["plan_slug"] == "free"
        # Members include both seeded users.
        usernames = sorted(m["username"] for m in d["members"])
        assert usernames == ["Detail_member", "Detail_owner"]
        # Counts roll up from the seed.
        assert d["counts"]["transactions"] == 1
        assert d["counts"]["accounts"] == 1
        assert d["counts"]["budgets"] == 1


@pytest.mark.asyncio
async def test_get_org_detail_404_when_missing(session_factory):
    async with session_factory() as db:
        with pytest.raises(NotFoundError):
            await admin_orgs_service.get_org_detail(db, org_id=99999)


@pytest.mark.asyncio
async def test_update_subscription_changes_only_provided_fields(session_factory):
    target = await _seed_full_org(session_factory, name="Sub")
    async with session_factory() as db:
        before, after = await admin_orgs_service.update_subscription(
            db,
            org_id=target["org_id"],
            status=SubscriptionStatus.ACTIVE,
            trial_end=datetime.date.today() + datetime.timedelta(days=30),
        )
        await db.commit()
        assert before["status"] == "trialing"
        assert after["status"] == "active"
        # Diff also captures the trial_end change.
        assert before["trial_end"] != after["trial_end"]
        # Unchanged fields are NOT in either dict.
        assert "plan_id" not in before
        assert "plan_id" not in after


@pytest.mark.asyncio
async def test_update_subscription_404_when_missing(session_factory):
    async with session_factory() as db:
        with pytest.raises(NotFoundError):
            await admin_orgs_service.update_subscription(
                db, org_id=99999, status=SubscriptionStatus.ACTIVE,
            )


@pytest.mark.asyncio
async def test_update_subscription_rejects_unknown_plan(session_factory):
    target = await _seed_full_org(session_factory, name="BadPlan")
    async with session_factory() as db:
        with pytest.raises(ValidationError):
            await admin_orgs_service.update_subscription(
                db, org_id=target["org_id"], plan_id=99999,
            )
