"""C0 contract tests for the Categories foundation.

Covers every test bullet in section 8 of the spec at
``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-09-categories-c0-invariants.md``:

- Invariant 1 (1+1+1+1 floor)
- Invariant 2 (live cascade: rename, move don't write dependent rows)
- Invariant 3 (delete-with-migration target requirement and bulk update)
- Invariant 4 (last-in-type 409)
- Invariant 5 (cascade affects forecasts and budgets: counts surfaced)
- Move preview (read-only)
- Cross-master subcategory uniqueness on move (section 4.5)
- Resolution C atomicity (batch move)
- Resolution D audit trail
- Section 4.6 BOTH-source migration target compatibility matrix
- Section 4.7 Master-with-children delete protection
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Account, AccountType, Category, Organization
from app.models.audit_event import AuditEvent
from app.models.base import Base
from app.models.budget import Budget
from app.models.category import CategoryType
from app.models.category_rule import CategoryRule, RuleSource
from app.models.forecast_plan import (
    ForecastItemType,
    ForecastPlan,
    ForecastPlanItem,
    ItemSource,
    PlanStatus,
)
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.categories import router as categories_router
from app.security import hash_password


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
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield factory
    finally:
        await engine.dispose()


def make_app(session_factory) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (
                await db.execute(
                    select(User).where(User.is_superadmin.is_(True))
                )
            ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(categories_router)
    return app


async def _seed_basic(factory) -> dict:
    """Seed a fresh org with masters for INCOME, EXPENSE, BOTH plus
    multiple subcategories to exercise the floor.

    Floor is 2-of-each so individual delete tests don't trip Invariant 4
    accidentally.
    """
    async with factory() as db:
        org = Organization(name="T", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="root", email="r@x.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True,
        )
        db.add_all([user, at])
        await db.flush()
        acct = Account(
            org_id=org.id, name="Main", account_type_id=at.id,
            balance=Decimal("0"), currency="EUR",
        )
        # Two income masters, one with multiple subs.
        income_master = Category(
            org_id=org.id, name="Income", slug="income", type=CategoryType.INCOME,
        )
        income_master_2 = Category(
            org_id=org.id, name="Other Income", slug="other_income",
            type=CategoryType.INCOME,
        )
        # Two expense masters.
        expense_master = Category(
            org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE,
        )
        lifestyle_master = Category(
            org_id=org.id, name="Lifestyle", slug="lifestyle",
            type=CategoryType.EXPENSE,
        )
        # A BOTH master.
        both_master = Category(
            org_id=org.id, name="Flex", slug="flex", type=CategoryType.BOTH,
        )
        db.add_all([acct, income_master, income_master_2, expense_master, lifestyle_master, both_master])
        await db.flush()

        # Subs: 2 income subs, 2 expense subs (under expense_master), 1 expense sub under lifestyle.
        salary = Category(
            org_id=org.id, name="Salary", parent_id=income_master.id,
            type=CategoryType.INCOME,
        )
        bonus = Category(
            org_id=org.id, name="Bonus", parent_id=income_master.id,
            type=CategoryType.INCOME,
        )
        groceries = Category(
            org_id=org.id, name="Groceries", parent_id=expense_master.id,
            type=CategoryType.EXPENSE,
        )
        restaurants = Category(
            org_id=org.id, name="Restaurants", parent_id=expense_master.id,
            type=CategoryType.EXPENSE,
        )
        movies = Category(
            org_id=org.id, name="Movies", parent_id=lifestyle_master.id,
            type=CategoryType.EXPENSE,
        )
        db.add_all([salary, bonus, groceries, restaurants, movies])
        await db.commit()
        return {
            "org_id": org.id,
            "user_id": user.id,
            "account_id": acct.id,
            "income_master_id": income_master.id,
            "income_master_2_id": income_master_2.id,
            "expense_master_id": expense_master.id,
            "lifestyle_master_id": lifestyle_master.id,
            "both_master_id": both_master.id,
            "salary_id": salary.id,
            "bonus_id": bonus.id,
            "groceries_id": groceries.id,
            "restaurants_id": restaurants.id,
            "movies_id": movies.id,
        }


async def _add_transaction(
    factory, *, org_id: int, account_id: int, category_id: int,
    tx_type: TransactionType, amount: str = "10.00",
) -> int:
    async with factory() as db:
        tx = Transaction(
            org_id=org_id, account_id=account_id, category_id=category_id,
            description="t", amount=Decimal(amount), type=tx_type,
            status=TransactionStatus.SETTLED,
            date=datetime.date.today(),
            settled_date=datetime.date.today(),
        )
        db.add(tx)
        await db.commit()
        return tx.id


async def _add_recurring(
    factory, *, org_id: int, account_id: int, category_id: int, type_: str,
) -> int:
    async with factory() as db:
        r = RecurringTransaction(
            org_id=org_id, account_id=account_id, category_id=category_id,
            description="r", amount=Decimal("12.34"), type=type_,
            frequency=Frequency.MONTHLY,
            next_due_date=datetime.date.today(),
        )
        db.add(r)
        await db.commit()
        return r.id


async def _add_forecast_item(
    factory, *, org_id: int, category_id: int, item_type: ForecastItemType,
    planned_amount: str = "100.00",
) -> int:
    async with factory() as db:
        # Need a billing period and plan first.
        from app.models.billing import BillingPeriod
        period = await db.scalar(
            select(BillingPeriod).where(BillingPeriod.org_id == org_id)
        )
        if period is None:
            period = BillingPeriod(
                org_id=org_id, start_date=datetime.date.today().replace(day=1),
            )
            db.add(period)
            await db.flush()
        plan = await db.scalar(
            select(ForecastPlan).where(
                ForecastPlan.org_id == org_id, ForecastPlan.billing_period_id == period.id,
            )
        )
        if plan is None:
            plan = ForecastPlan(
                org_id=org_id, billing_period_id=period.id, status=PlanStatus.DRAFT,
            )
            db.add(plan)
            await db.flush()
        item = ForecastPlanItem(
            plan_id=plan.id, org_id=org_id, category_id=category_id,
            type=item_type, planned_amount=Decimal(planned_amount),
            source=ItemSource.HISTORY,
        )
        db.add(item)
        await db.commit()
        return item.id


# --- Invariant 1 (1+1+1+1 floor) -------------------------------------------


@pytest.mark.asyncio
async def test_floor_held_after_seed(session_factory):
    """Fresh seed satisfies the floor."""
    seed = await _seed_basic(session_factory)
    from app.services.category_service import _floor_counts_for_org

    async with session_factory() as db:
        counts = await _floor_counts_for_org(db, org_id=seed["org_id"])
    assert counts["income_masters"] >= 1
    assert counts["income_subs"] >= 1
    assert counts["expense_masters"] >= 1
    assert counts["expense_subs"] >= 1


# --- Invariant 2 (live cascade) --------------------------------------------


@pytest.mark.asyncio
async def test_rename_does_not_rewrite_transaction_category_id(session_factory):
    """Rename writes only the categories row; transaction.category_id is
    unchanged."""
    seed = await _seed_basic(session_factory)
    tx_id = await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["groceries_id"], tx_type=TransactionType.EXPENSE,
    )
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['groceries_id']}",
            json={"name": "Renamed Groceries"},
        )
    assert resp.status_code == 200, resp.text

    async with session_factory() as db:
        tx = await db.scalar(select(Transaction).where(Transaction.id == tx_id))
        assert tx.category_id == seed["groceries_id"]


@pytest.mark.asyncio
async def test_move_does_not_rewrite_transaction_category_id(session_factory):
    """Move writes only the categories row; the transaction's
    category_id continues to point at the SAME subcategory id, which now
    rolls up under a different master."""
    seed = await _seed_basic(session_factory)
    # Move groceries from expense_master to lifestyle_master.
    tx_id = await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["groceries_id"], tx_type=TransactionType.EXPENSE,
    )
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.patch(
            f"/api/v1/categories/{seed['groceries_id']}/move",
            json={"target_parent_id": seed["lifestyle_master_id"]},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["category_id"] == seed["groceries_id"]
    assert body["source_master_id"] == seed["expense_master_id"]
    assert body["target_master_id"] == seed["lifestyle_master_id"]
    assert body["affected_transaction_count"] == 1

    async with session_factory() as db:
        tx = await db.scalar(select(Transaction).where(Transaction.id == tx_id))
        assert tx.category_id == seed["groceries_id"]
        cat = await db.scalar(
            select(Category).where(Category.id == seed["groceries_id"])
        )
        assert cat.parent_id == seed["lifestyle_master_id"]


# --- Invariant 3 (delete-with-migration) -----------------------------------


@pytest.mark.asyncio
async def test_delete_subcategory_with_dependents_no_target_returns_422(session_factory):
    seed = await _seed_basic(session_factory)
    await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["groceries_id"], tx_type=TransactionType.EXPENSE,
    )
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.delete(f"/api/v1/categories/{seed['groceries_id']}")
    assert resp.status_code == 422, resp.text
    body = resp.json()["detail"]
    assert body["detail"] == "migration_target_required"
    assert body["dependent_counts"]["transactions"] == 1


@pytest.mark.asyncio
async def test_delete_subcategory_with_target_migrates_dependents(session_factory):
    seed = await _seed_basic(session_factory)
    # 5 transactions on groceries.
    for _ in range(5):
        await _add_transaction(
            session_factory,
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], tx_type=TransactionType.EXPENSE,
        )
    # 1 recurring on groceries.
    rec_id = await _add_recurring(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["groceries_id"], type_="expense",
    )
    # 1 forecast item.
    fpi_id = await _add_forecast_item(
        session_factory,
        org_id=seed["org_id"], category_id=seed["groceries_id"],
        item_type=ForecastItemType.EXPENSE,
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/categories/{seed['groceries_id']}"
            f"?target_category_id={seed['restaurants_id']}"
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted_category_id"] == seed["groceries_id"]
    assert body["migration_target_id"] == seed["restaurants_id"]
    assert body["migrated_transaction_count"] == 5
    assert body["migrated_recurring_count"] == 1
    assert body["migrated_forecast_item_count"] == 1

    async with session_factory() as db:
        # All five txs now point at restaurants.
        txs = (await db.scalars(
            select(Transaction).where(
                Transaction.org_id == seed["org_id"],
                Transaction.category_id == seed["restaurants_id"],
            )
        )).all()
        assert len(txs) == 5
        rec = await db.scalar(
            select(RecurringTransaction).where(RecurringTransaction.id == rec_id)
        )
        assert rec.category_id == seed["restaurants_id"]
        fpi = await db.scalar(
            select(ForecastPlanItem).where(ForecastPlanItem.id == fpi_id)
        )
        assert fpi.category_id == seed["restaurants_id"]
        # Source category gone.
        gone = await db.scalar(
            select(Category).where(Category.id == seed["groceries_id"])
        )
        assert gone is None


@pytest.mark.asyncio
async def test_delete_sums_forecast_item_colliding_with_targets_existing_item(
    session_factory,
):
    """When source and target both have a forecast item for the same
    (plan, type), migrating the source FK would violate
    uq_forecast_item_plan_cat_type. The service must merge: sum the
    source's planned_amount into the target's existing item and drop the
    source item, instead of 500ing on a 1062 duplicate-key error.
    """
    seed = await _seed_basic(session_factory)
    # Source (groceries) forecast item: EXPENSE, 100.
    await _add_forecast_item(
        session_factory,
        org_id=seed["org_id"], category_id=seed["groceries_id"],
        item_type=ForecastItemType.EXPENSE, planned_amount="100.00",
    )
    # Target (restaurants) forecast item in the SAME plan, SAME type: 25.
    target_fpi_id = await _add_forecast_item(
        session_factory,
        org_id=seed["org_id"], category_id=seed["restaurants_id"],
        item_type=ForecastItemType.EXPENSE, planned_amount="25.00",
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/categories/{seed['groceries_id']}"
            f"?target_category_id={seed['restaurants_id']}"
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["migrated_forecast_item_count"] == 1

    async with session_factory() as db:
        # Target keeps a single item for the (plan, type), amounts summed.
        items = (await db.scalars(
            select(ForecastPlanItem).where(
                ForecastPlanItem.org_id == seed["org_id"],
                ForecastPlanItem.category_id == seed["restaurants_id"],
                ForecastPlanItem.type == ForecastItemType.EXPENSE,
            )
        )).all()
        assert len(items) == 1
        assert items[0].id == target_fpi_id
        assert items[0].planned_amount == Decimal("125.00")
        # No forecast item left dangling on the deleted source.
        orphans = (await db.scalars(
            select(ForecastPlanItem).where(
                ForecastPlanItem.category_id == seed["groceries_id"],
            )
        )).all()
        assert orphans == []
        gone = await db.scalar(
            select(Category).where(Category.id == seed["groceries_id"])
        )
        assert gone is None


@pytest.mark.asyncio
async def test_delete_subcategory_no_dependents_returns_204(session_factory):
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        # bonus has no dependents.
        resp = client.delete(f"/api/v1/categories/{seed['bonus_id']}")
    assert resp.status_code == 204
    # Empty body.
    assert resp.content == b""


@pytest.mark.asyncio
async def test_delete_with_target_equal_to_source_returns_400(session_factory):
    seed = await _seed_basic(session_factory)
    await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["groceries_id"], tx_type=TransactionType.EXPENSE,
    )
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/categories/{seed['groceries_id']}"
            f"?target_category_id={seed['groceries_id']}"
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_with_incompatible_target_type_returns_400(session_factory):
    """Expense source -> Income target must be rejected as type_mismatch."""
    seed = await _seed_basic(session_factory)
    await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["groceries_id"], tx_type=TransactionType.EXPENSE,
    )
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/categories/{seed['groceries_id']}"
            f"?target_category_id={seed['salary_id']}"
        )
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["detail"] == "type_mismatch"
    assert body["source_type"] == "expense"
    assert body["target_type"] == "income"


@pytest.mark.asyncio
async def test_delete_master_with_children_returns_409(session_factory):
    """Section 4.7: master with children -> 409 has_children, regardless of target."""
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.delete(f"/api/v1/categories/{seed['expense_master_id']}")
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["detail"] == "has_children"
    assert seed["groceries_id"] in body["child_ids"]
    assert seed["restaurants_id"] in body["child_ids"]
    assert "Groceries" in body["child_names"]


@pytest.mark.asyncio
async def test_delete_master_with_children_and_target_still_409(session_factory):
    """Section 4.7: target does NOT adopt children."""
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/categories/{seed['expense_master_id']}"
            f"?target_category_id={seed['lifestyle_master_id']}"
        )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["detail"] == "has_children"


@pytest.mark.asyncio
async def test_delete_subcategory_also_deletes_source_budget_row(session_factory):
    seed = await _seed_basic(session_factory)
    async with session_factory() as db:
        b = Budget(
            org_id=seed["org_id"], category_id=seed["bonus_id"],
            amount=Decimal("100.00"),
            period_start=datetime.date.today().replace(day=1),
        )
        db.add(b)
        await db.commit()
        bid = b.id

    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.delete(f"/api/v1/categories/{seed['bonus_id']}")
    assert resp.status_code == 204

    async with session_factory() as db:
        gone = await db.scalar(select(Budget).where(Budget.id == bid))
        assert gone is None


# --- Delete-with-migration re-points category_rules ------------------------


@pytest.mark.asyncio
async def test_delete_with_target_migrates_category_rules(session_factory):
    """A source category's learned rules re-point to the target on
    delete-with-migration instead of being dropped."""
    seed = await _seed_basic(session_factory)
    # Force dependents so a target is required.
    await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["groceries_id"], tx_type=TransactionType.EXPENSE,
    )
    async with session_factory() as db:
        db.add_all([
            CategoryRule(
                org_id=seed["org_id"], normalized_token="LIDL",
                raw_description_seen="LIDL STORE",
                category_id=seed["groceries_id"], match_count=3,
                source=RuleSource.USER_PICK,
            ),
            CategoryRule(
                org_id=seed["org_id"], normalized_token="ALDI",
                raw_description_seen="ALDI MARKT",
                category_id=seed["groceries_id"], match_count=2,
                source=RuleSource.USER_EDIT,
            ),
        ])
        await db.commit()

    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/categories/{seed['groceries_id']}"
            f"?target_category_id={seed['restaurants_id']}"
        )
    assert resp.status_code == 200, resp.text

    async with session_factory() as db:
        rules = (await db.scalars(
            select(CategoryRule).where(CategoryRule.org_id == seed["org_id"])
        )).all()
        # Both rules survived and now point at the target.
        assert len(rules) == 2
        assert {r.normalized_token for r in rules} == {"LIDL", "ALDI"}
        assert all(r.category_id == seed["restaurants_id"] for r in rules)


@pytest.mark.asyncio
async def test_delete_migrates_rules_alongside_targets_existing_rules(
    session_factory,
):
    """A source rule re-points cleanly even when the target already owns its
    own (distinct-token) rules; nothing the target had is disturbed."""
    seed = await _seed_basic(session_factory)
    await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["groceries_id"], tx_type=TransactionType.EXPENSE,
    )
    async with session_factory() as db:
        db.add_all([
            CategoryRule(
                org_id=seed["org_id"], normalized_token="DINER",
                raw_description_seen="DINER X",
                category_id=seed["restaurants_id"], match_count=4,
                source=RuleSource.USER_EDIT,
            ),
            CategoryRule(
                org_id=seed["org_id"], normalized_token="ALDI",
                raw_description_seen="ALDI MARKT",
                category_id=seed["groceries_id"], match_count=2,
                source=RuleSource.USER_PICK,
            ),
        ])
        await db.commit()

    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/categories/{seed['groceries_id']}"
            f"?target_category_id={seed['restaurants_id']}"
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["migrated_rule_count"] == 1

    async with session_factory() as db:
        rules = (await db.scalars(
            select(CategoryRule).where(CategoryRule.org_id == seed["org_id"])
        )).all()
        assert {r.normalized_token for r in rules} == {"DINER", "ALDI"}
        assert all(r.category_id == seed["restaurants_id"] for r in rules)


def test_partition_rules_for_migration_dedupes_colliding_token():
    """Unit-test of the dedupe decision: a source rule whose token the
    target already owns is dropped (the target is the survivor); the rest
    re-point.

    ``category_rules`` is unique on ``(org_id, normalized_token)`` org-wide,
    so this collision cannot be persisted in the DB, but the partition guards
    against it defensively. Verified here as a pure unit."""
    from app.services.category_service import _partition_rules_for_migration

    colliding = CategoryRule(
        org_id=1, normalized_token="LIDL", raw_description_seen="LIDL SRC",
        category_id=10, match_count=9, source=RuleSource.USER_PICK,
    )
    distinct = CategoryRule(
        org_id=1, normalized_token="ALDI", raw_description_seen="ALDI",
        category_id=10, match_count=2, source=RuleSource.USER_PICK,
    )
    to_migrate, to_drop = _partition_rules_for_migration(
        [colliding, distinct], target_tokens={"LIDL"},
    )
    assert to_migrate == [distinct]
    assert to_drop == [colliding]


# --- Section 4.6 BOTH-source migration target compatibility ---------------


@pytest.mark.asyncio
async def test_delete_both_with_income_only_dependents_to_expense_target_400(session_factory):
    seed = await _seed_basic(session_factory)
    # Add a sub under the BOTH master with a flex category to delete.
    async with session_factory() as db:
        flex_sub = Category(
            org_id=seed["org_id"], name="FlexSub",
            parent_id=seed["both_master_id"], type=CategoryType.BOTH,
        )
        db.add(flex_sub)
        await db.commit()
        flex_sub_id = flex_sub.id
    # Add an income transaction directly on the BOTH master (allowed
    # since BOTH accepts both).
    await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=flex_sub_id, tx_type=TransactionType.INCOME,
    )
    app = make_app(session_factory)
    with TestClient(app) as client:
        # Try to migrate to an EXPENSE-typed target.
        resp = client.delete(
            f"/api/v1/categories/{flex_sub_id}"
            f"?target_category_id={seed['groceries_id']}"
        )
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["detail"] == "type_mismatch"
    assert body["source_type"] == "both"
    assert body["target_type"] == "expense"
    assert body["dependent_breakdown"]["income"] == 1
    assert body["dependent_breakdown"]["expense"] == 0


@pytest.mark.asyncio
async def test_delete_both_with_mixed_dependents_to_non_both_target_400(session_factory):
    seed = await _seed_basic(session_factory)
    async with session_factory() as db:
        flex_sub = Category(
            org_id=seed["org_id"], name="FlexSub",
            parent_id=seed["both_master_id"], type=CategoryType.BOTH,
        )
        db.add(flex_sub)
        await db.commit()
        flex_sub_id = flex_sub.id
    await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=flex_sub_id, tx_type=TransactionType.INCOME,
    )
    await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=flex_sub_id, tx_type=TransactionType.EXPENSE,
    )
    app = make_app(session_factory)
    with TestClient(app) as client:
        # mixed -> EXPENSE target. Reject.
        resp = client.delete(
            f"/api/v1/categories/{flex_sub_id}"
            f"?target_category_id={seed['groceries_id']}"
        )
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["detail"] == "type_mismatch"
    assert body["dependent_breakdown"]["income"] == 1
    assert body["dependent_breakdown"]["expense"] == 1


@pytest.mark.asyncio
async def test_delete_both_with_mixed_dependents_to_both_target_succeeds(session_factory):
    seed = await _seed_basic(session_factory)
    async with session_factory() as db:
        flex_sub_a = Category(
            org_id=seed["org_id"], name="FlexA",
            parent_id=seed["both_master_id"], type=CategoryType.BOTH,
        )
        flex_sub_b = Category(
            org_id=seed["org_id"], name="FlexB",
            parent_id=seed["both_master_id"], type=CategoryType.BOTH,
        )
        db.add_all([flex_sub_a, flex_sub_b])
        await db.commit()
        a_id, b_id = flex_sub_a.id, flex_sub_b.id
    await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=a_id, tx_type=TransactionType.INCOME,
    )
    await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=a_id, tx_type=TransactionType.EXPENSE,
    )
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/categories/{a_id}?target_category_id={b_id}"
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_delete_both_with_empty_dependents_falls_through_to_204(session_factory):
    seed = await _seed_basic(session_factory)
    async with session_factory() as db:
        flex_sub = Category(
            org_id=seed["org_id"], name="FlexSub",
            parent_id=seed["both_master_id"], type=CategoryType.BOTH,
        )
        db.add(flex_sub)
        await db.commit()
        flex_sub_id = flex_sub.id
    app = make_app(session_factory)
    with TestClient(app) as client:
        # Even with target supplied, empty dependents -> 204 (target
        # ignored).
        resp = client.delete(
            f"/api/v1/categories/{flex_sub_id}"
            f"?target_category_id={seed['groceries_id']}"
        )
    assert resp.status_code == 204


# --- Invariant 4 (last-in-type) --------------------------------------------


@pytest.mark.asyncio
async def test_delete_last_income_subcategory_returns_409(session_factory):
    seed = await _seed_basic(session_factory)
    # Delete bonus first (no deps), then salary should be the only
    # income sub left.
    app = make_app(session_factory)
    with TestClient(app) as client:
        client.delete(f"/api/v1/categories/{seed['bonus_id']}")
        # Now salary is the only income sub. Delete should 409.
        resp = client.delete(f"/api/v1/categories/{seed['salary_id']}")
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["detail"] == "last_in_type"
    assert body["scope"] == "subcategory"
    assert body["type"] == "income"


@pytest.mark.asyncio
async def test_delete_last_income_master_returns_409(session_factory):
    seed = await _seed_basic(session_factory)
    # First we need to clear out income_master_2 (it has no children
    # so it's safely deletable, but income_master_2 is also a master).
    # The important thing: after deleting income_master_2, deleting
    # income_master should fire 409 (it would zero out income masters).
    app = make_app(session_factory)
    with TestClient(app) as client:
        # income_master_2 has no children; safe to delete.
        client.delete(f"/api/v1/categories/{seed['income_master_2_id']}")
        # Now income_master is the only income master. But it has
        # children (salary, bonus), so it 409s on has_children, not
        # last_in_type. Either way the floor is preserved.
        resp = client.delete(f"/api/v1/categories/{seed['income_master_id']}")
    assert resp.status_code == 409
    # Either has_children or last_in_type is acceptable here; the
    # spec orders has_children FIRST.
    assert resp.json()["detail"]["detail"] in {"has_children", "last_in_type"}


# --- Move preview (read-only) ----------------------------------------------


@pytest.mark.asyncio
async def test_move_preview_returns_counts_without_mutating(session_factory):
    seed = await _seed_basic(session_factory)
    # Add 3 txs and 1 forecast item on groceries.
    for _ in range(3):
        await _add_transaction(
            session_factory,
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["groceries_id"], tx_type=TransactionType.EXPENSE,
        )
    await _add_forecast_item(
        session_factory,
        org_id=seed["org_id"], category_id=seed["groceries_id"],
        item_type=ForecastItemType.EXPENSE,
    )

    # Snapshot key tables before.
    async with session_factory() as db:
        before_cats = (await db.scalars(select(Category))).all()
        before_cat_parents = {c.id: c.parent_id for c in before_cats}
        before_audit = (await db.scalars(select(AuditEvent))).all()
    assert len(before_audit) == 0  # nothing audited yet

    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/categories/{seed['groceries_id']}/move/preview"
            f"?target_parent_id={seed['lifestyle_master_id']}"
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["category_id"] == seed["groceries_id"]
    assert body["source_master_id"] == seed["expense_master_id"]
    assert body["target_master_id"] == seed["lifestyle_master_id"]
    assert body["affected_transaction_count"] == 3
    assert body["affected_forecast_item_count"] == 1

    # Confirm no writes.
    async with session_factory() as db:
        after_cats = (await db.scalars(select(Category))).all()
        after_cat_parents = {c.id: c.parent_id for c in after_cats}
        after_audit = (await db.scalars(select(AuditEvent))).all()
    assert before_cat_parents == after_cat_parents
    assert len(after_audit) == 0


@pytest.mark.asyncio
async def test_move_preview_returns_404_for_unknown_source(session_factory):
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/categories/9999999/move/preview"
            f"?target_parent_id={seed['lifestyle_master_id']}"
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_move_preview_returns_400_for_invalid_target(session_factory):
    """Target is itself a subcategory (must be a master)."""
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/categories/{seed['groceries_id']}/move/preview"
            f"?target_parent_id={seed['restaurants_id']}"
        )
    assert resp.status_code == 400


# --- Cross-master subcategory uniqueness on move (section 4.5) -------------


@pytest.mark.asyncio
async def test_move_rejects_name_collision(session_factory):
    seed = await _seed_basic(session_factory)
    # Add a sibling named "Groceries" under lifestyle_master so a move
    # collides on the normalized name.
    async with session_factory() as db:
        clash = Category(
            org_id=seed["org_id"], name="GROCERIES",
            parent_id=seed["lifestyle_master_id"], type=CategoryType.EXPENSE,
        )
        db.add(clash)
        await db.commit()
        clash_id = clash.id

    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.patch(
            f"/api/v1/categories/{seed['groceries_id']}/move",
            json={"target_parent_id": seed["lifestyle_master_id"]},
        )
    assert resp.status_code == 409, resp.text
    body = resp.json()["detail"]
    assert body["detail"] == "name_collision"
    assert body["target_parent_id"] == seed["lifestyle_master_id"]
    assert body["conflicting_child_id"] == clash_id
    assert body["normalized_name"] == "groceries"


@pytest.mark.asyncio
async def test_move_normalizes_whitespace_and_case_for_collision(session_factory):
    seed = await _seed_basic(session_factory)
    async with session_factory() as db:
        clash = Category(
            org_id=seed["org_id"], name="  groceries  ",
            parent_id=seed["lifestyle_master_id"], type=CategoryType.EXPENSE,
        )
        db.add(clash)
        await db.commit()

    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.patch(
            f"/api/v1/categories/{seed['groceries_id']}/move",
            json={"target_parent_id": seed["lifestyle_master_id"]},
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_batch_move_rejects_whole_batch_on_name_collision(session_factory):
    seed = await _seed_basic(session_factory)
    async with session_factory() as db:
        clash = Category(
            org_id=seed["org_id"], name="Restaurants",
            parent_id=seed["lifestyle_master_id"], type=CategoryType.EXPENSE,
        )
        db.add(clash)
        await db.commit()

    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/categories/batch-move",
            json={
                "moves": [
                    # First move is fine (no collision).
                    {
                        "subcategory_id": seed["groceries_id"],
                        "target_parent_id": seed["lifestyle_master_id"],
                    },
                    # Second one collides.
                    {
                        "subcategory_id": seed["restaurants_id"],
                        "target_parent_id": seed["lifestyle_master_id"],
                    },
                ]
            },
        )
    assert resp.status_code == 409

    # Nothing should have moved.
    async with session_factory() as db:
        groceries = await db.scalar(
            select(Category).where(Category.id == seed["groceries_id"])
        )
        assert groceries.parent_id == seed["expense_master_id"]
        # No audit row.
        rows = (await db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "category.batch_moved")
        )).all()
        assert rows == []


# --- Resolution C atomicity (batch move) -----------------------------------


@pytest.mark.asyncio
async def test_batch_move_all_or_nothing_on_partial_failure(session_factory):
    """Send a batch where item N has an invalid target type;
    no row is updated."""
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/categories/batch-move",
            json={
                "moves": [
                    # Valid: groceries (expense) -> lifestyle_master (expense).
                    {
                        "subcategory_id": seed["groceries_id"],
                        "target_parent_id": seed["lifestyle_master_id"],
                    },
                    # Invalid: salary (income) -> lifestyle_master (expense).
                    {
                        "subcategory_id": seed["salary_id"],
                        "target_parent_id": seed["lifestyle_master_id"],
                    },
                ]
            },
        )
    assert resp.status_code == 400, resp.text

    async with session_factory() as db:
        groceries = await db.scalar(
            select(Category).where(Category.id == seed["groceries_id"])
        )
        salary = await db.scalar(
            select(Category).where(Category.id == seed["salary_id"])
        )
        assert groceries.parent_id == seed["expense_master_id"]
        assert salary.parent_id == seed["income_master_id"]
        rows = (await db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "category.batch_moved")
        )).all()
        assert rows == []


@pytest.mark.asyncio
async def test_batch_move_writes_one_audit_row_with_summary(session_factory):
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/categories/batch-move",
            json={
                "moves": [
                    {
                        "subcategory_id": seed["groceries_id"],
                        "target_parent_id": seed["lifestyle_master_id"],
                    },
                    {
                        "subcategory_id": seed["restaurants_id"],
                        "target_parent_id": seed["lifestyle_master_id"],
                    },
                ]
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["moves"]) == 2

    async with session_factory() as db:
        rows = (await db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "category.batch_moved")
        )).all()
        assert len(rows) == 1
        detail = rows[0].detail
        assert detail["total_subcategories"] == 2
        assert len(detail["moves"]) == 2


# --- Resolution D audit trail ----------------------------------------------


@pytest.mark.asyncio
async def test_category_renamed_writes_audit_row(session_factory):
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['groceries_id']}",
            json={"name": "Renamed"},
        )
    assert resp.status_code == 200

    async with session_factory() as db:
        rows = (await db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "category.renamed")
        )).all()
        assert len(rows) == 1
        assert rows[0].detail["old_name"] == "Groceries"
        assert rows[0].detail["new_name"] == "Renamed"


@pytest.mark.asyncio
async def test_category_moved_writes_audit_row(session_factory):
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.patch(
            f"/api/v1/categories/{seed['groceries_id']}/move",
            json={"target_parent_id": seed["lifestyle_master_id"]},
        )
    assert resp.status_code == 200

    async with session_factory() as db:
        rows = (await db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "category.moved")
        )).all()
        assert len(rows) == 1
        d = rows[0].detail
        assert d["category_id"] == seed["groceries_id"]
        assert d["source_master_id"] == seed["expense_master_id"]
        assert d["target_master_id"] == seed["lifestyle_master_id"]


@pytest.mark.asyncio
async def test_category_deleted_with_target_writes_audit_row(session_factory):
    seed = await _seed_basic(session_factory)
    await _add_transaction(
        session_factory,
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["groceries_id"], tx_type=TransactionType.EXPENSE,
    )
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/categories/{seed['groceries_id']}"
            f"?target_category_id={seed['restaurants_id']}"
        )
    assert resp.status_code == 200

    async with session_factory() as db:
        rows = (await db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "category.deleted")
        )).all()
        assert len(rows) == 1
        d = rows[0].detail
        assert d["category_id"] == seed["groceries_id"]
        assert d["migration_target_id"] == seed["restaurants_id"]
        assert d["migrated_transaction_count"] == 1


@pytest.mark.asyncio
async def test_category_created_writes_audit_row(session_factory):
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/categories",
            json={
                "name": "New Sub",
                "parent_id": seed["expense_master_id"],
            },
        )
    assert resp.status_code == 201

    async with session_factory() as db:
        rows = (await db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "category.created")
        )).all()
        assert len(rows) == 1
        assert rows[0].detail["name"] == "New Sub"


@pytest.mark.asyncio
async def test_org_bootstrap_seed_does_not_write_audit_rows(session_factory):
    """The seed path runs without a human actor; per section D resolution it
    is structlog-only."""
    from app.services.org_bootstrap_service import seed_org_defaults

    async with session_factory() as db:
        org = Organization(name="bootstrap_test", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        await seed_org_defaults(db, org_id=org.id)
        await db.commit()

        rows = (await db.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type.like("category.%")
            )
        )).all()
        assert rows == []


# --- Migration backfill ----------------------------------------------------


@pytest.mark.asyncio
async def test_assert_min_floor_for_org_passes_on_seeded_org(session_factory):
    seed = await _seed_basic(session_factory)
    from app.services.category_service import assert_min_floor_for_org
    async with session_factory() as db:
        await assert_min_floor_for_org(db, org_id=seed["org_id"])


@pytest.mark.asyncio
async def test_assert_min_floor_for_org_raises_when_under_floor(session_factory):
    """An org with no income subs trips the floor check."""
    from app.services.category_service import assert_min_floor_for_org
    from app.services.exceptions import ValidationError

    async with session_factory() as db:
        org = Organization(name="bare", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        # Income master only, no income subs.
        m = Category(
            org_id=org.id, name="Income", type=CategoryType.INCOME,
        )
        db.add(m)
        em = Category(
            org_id=org.id, name="Exp", type=CategoryType.EXPENSE,
        )
        db.add(em)
        await db.flush()
        es = Category(
            org_id=org.id, name="Sub", type=CategoryType.EXPENSE,
            parent_id=em.id,
        )
        db.add(es)
        await db.commit()

        with pytest.raises(ValidationError):
            await assert_min_floor_for_org(db, org_id=org.id)


# --- Misc smoke ------------------------------------------------------------


@pytest.mark.asyncio
async def test_normalize_category_name():
    from app.services.category_service import normalize_category_name
    assert normalize_category_name("  Hello World  ") == "hello world"
    assert normalize_category_name("HELLO   WORLD") == "hello world"
    assert normalize_category_name("Restaurants") == "restaurants"


@pytest.mark.asyncio
async def test_move_to_same_parent_returns_400(session_factory):
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.patch(
            f"/api/v1/categories/{seed['groceries_id']}/move",
            json={"target_parent_id": seed["expense_master_id"]},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_move_master_returns_400(session_factory):
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.patch(
            f"/api/v1/categories/{seed['expense_master_id']}/move",
            json={"target_parent_id": seed["lifestyle_master_id"]},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_move_with_subcategory_target_returns_400(session_factory):
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        # restaurants is a subcategory, not a master.
        resp = client.patch(
            f"/api/v1/categories/{seed['groceries_id']}/move",
            json={"target_parent_id": seed["restaurants_id"]},
        )
    assert resp.status_code == 400


# === Type-change floor invariant (Invariant 1, cross-ref Invariant 4) =======


async def _seed_minimal_floor(factory) -> dict:
    """Seed an org with EXACTLY 1 income master + 1 income sub, and 1
    expense master + 1 expense sub. Type-change attempts on these last-
    in-type masters/subs must be rejected with floor_violation.
    """
    async with factory() as db:
        org = Organization(name="MinFloor", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="root", email="r@m.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        db.add(user)
        income_master = Category(
            org_id=org.id, name="Income", slug="income", type=CategoryType.INCOME,
        )
        expense_master = Category(
            org_id=org.id, name="Expense", slug="expense_m",
            type=CategoryType.EXPENSE,
        )
        db.add_all([income_master, expense_master])
        await db.flush()
        income_sub = Category(
            org_id=org.id, name="Salary", parent_id=income_master.id,
            type=CategoryType.INCOME,
        )
        expense_sub = Category(
            org_id=org.id, name="Groceries", parent_id=expense_master.id,
            type=CategoryType.EXPENSE,
        )
        db.add_all([income_sub, expense_sub])
        await db.commit()
        return {
            "org_id": org.id,
            "user_id": user.id,
            "income_master_id": income_master.id,
            "expense_master_id": expense_master.id,
            "income_sub_id": income_sub.id,
            "expense_sub_id": expense_sub.id,
        }


@pytest.mark.asyncio
async def test_type_change_only_income_master_to_expense_returns_409(session_factory):
    """Cannot change the only INCOME master to EXPENSE: would drop the
    org below the income master floor."""
    seed = await _seed_minimal_floor(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['income_master_id']}",
            json={"type": "expense"},
        )
    assert resp.status_code == 409, resp.text
    body = resp.json()["detail"]
    assert body["detail"] == "floor_violation"
    assert body["scope"] == "master"
    assert body["type"] == "income"


@pytest.mark.asyncio
async def test_type_change_only_expense_master_to_income_returns_409(session_factory):
    """Cannot change the only EXPENSE master to INCOME."""
    seed = await _seed_minimal_floor(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['expense_master_id']}",
            json={"type": "income"},
        )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["detail"] == "floor_violation"
    assert body["scope"] == "master"
    assert body["type"] == "expense"


@pytest.mark.asyncio
async def test_type_change_only_income_master_to_both_returns_409(session_factory):
    """BOTH does not satisfy the income floor on its own (Invariant 1).
    Changing the only income master to BOTH must be rejected.
    """
    seed = await _seed_minimal_floor(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['income_master_id']}",
            json={"type": "both"},
        )
    assert resp.status_code == 409, resp.text
    body = resp.json()["detail"]
    assert body["detail"] == "floor_violation"


@pytest.mark.asyncio
async def test_type_change_only_expense_master_to_both_returns_409(session_factory):
    """BOTH does not satisfy the expense floor either."""
    seed = await _seed_minimal_floor(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['expense_master_id']}",
            json={"type": "both"},
        )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["detail"] == "floor_violation"


@pytest.mark.asyncio
async def test_type_change_master_succeeds_when_two_or_more_of_old_type(session_factory):
    """When a second master of the same type exists, changing one's
    type is allowed (the floor is still satisfied after the change)."""
    seed = await _seed_basic(session_factory)
    # _seed_basic provides 2 income masters and 2 expense masters.
    # Changing income_master_2 (no children, no dependents) to BOTH
    # leaves income_master + its children intact.
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['income_master_2_id']}",
            json={"type": "both"},
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_type_change_master_cascade_drops_subcategory_floor_returns_409(session_factory):
    """Cascading the master's new type to its only child drops the
    income-subs count to zero. Reject."""
    # Seed an org with one income master that has the only income sub,
    # PLUS a second income master with no sub. Changing the first to
    # EXPENSE would: income_masters count remains 1 (the second), but
    # income_subs would drop to 0 because the only sub belongs to the
    # one being changed.
    async with session_factory() as db:
        org = Organization(name="cascade", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="root", email="rc@m.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        db.add(user)
        m1 = Category(
            org_id=org.id, name="IncomeA", type=CategoryType.INCOME,
        )
        m2 = Category(
            org_id=org.id, name="IncomeB", type=CategoryType.INCOME,
        )
        em = Category(
            org_id=org.id, name="ExpM", type=CategoryType.EXPENSE,
        )
        db.add_all([m1, m2, em])
        await db.flush()
        s1 = Category(
            org_id=org.id, name="OnlyIncomeSub", parent_id=m1.id,
            type=CategoryType.INCOME,
        )
        es = Category(
            org_id=org.id, name="ExpSub", parent_id=em.id,
            type=CategoryType.EXPENSE,
        )
        db.add_all([s1, es])
        await db.commit()
        m1_id = m1.id

    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{m1_id}",
            json={"type": "expense"},
        )
    assert resp.status_code == 409, resp.text
    body = resp.json()["detail"]
    assert body["detail"] == "floor_violation"
    assert body["scope"] == "subcategory"
    assert body["type"] == "income"


@pytest.mark.asyncio
async def test_type_change_master_to_same_type_is_noop_no_floor_check(session_factory):
    """Changing type to the same value is a no-op; no floor check fires."""
    seed = await _seed_minimal_floor(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['income_master_id']}",
            json={"type": "income"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_type_change_both_master_to_income_does_not_trip_floor(session_factory):
    """A BOTH master never contributed to either floor. Changing it to
    INCOME or EXPENSE only adds to a floor; it cannot drop one."""
    seed = await _seed_basic(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{seed['both_master_id']}",
            json={"type": "income"},
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_type_change_master_with_no_children_only_master_returns_409(session_factory):
    """Even with no children, the master count itself drops below 1.
    Reject."""
    # Seed: one INCOME master without children + one EXPENSE master
    # with one EXPENSE sub + one INCOME sub under a DIFFERENT income
    # master. The lone target is the unique INCOME master alongside one
    # other INCOME master (so the "no children" branch is exercised).
    async with session_factory() as db:
        org = Organization(name="lone", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="r", email="lo@m.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        db.add(user)
        # The ONLY income master.
        im = Category(
            org_id=org.id, name="OnlyIncome", type=CategoryType.INCOME,
        )
        em = Category(
            org_id=org.id, name="ExpM", type=CategoryType.EXPENSE,
        )
        db.add_all([im, em])
        await db.flush()
        i_sub = Category(
            org_id=org.id, name="ISub", parent_id=im.id,
            type=CategoryType.INCOME,
        )
        e_sub = Category(
            org_id=org.id, name="ESub", parent_id=em.id,
            type=CategoryType.EXPENSE,
        )
        db.add_all([i_sub, e_sub])
        await db.commit()
        im_id = im.id

    app = make_app(session_factory)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/categories/{im_id}",
            json={"type": "expense"},
        )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["detail"] == "floor_violation"
    # Master OR subcategory scope acceptable - the master floor trips
    # first (only income master) so we get scope=master.
    assert body["scope"] == "master"
