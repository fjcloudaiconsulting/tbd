"""Editing a recurring-linked transaction syncs name/category forward.

- name/category edit on any linked instance updates the template AND all
  PENDING sibling instances
- SETTLED instances are never touched
- amount-only edits do not propagate
- editing a non-origin (settled) instance still propagates to the series
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.transaction import TransactionUpdate
from app.services import recurring_service, transaction_service

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db_session():
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
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db: AsyncSession) -> dict:
    org = Organization(name="T", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db.add(acct)
    await db.flush()
    exp = Category(org_id=org.id, name="Gym", slug="gym", type=CategoryType.EXPENSE)
    exp2 = Category(org_id=org.id, name="Health", slug="health", type=CategoryType.EXPENSE)
    db.add_all([exp, exp2])
    await db.commit()
    return {
        "org_id": org.id, "account_id": acct.id,
        "exp_cat": exp.id, "exp_cat2": exp2.id,
    }


async def _add_template(db: AsyncSession, seed: dict) -> int:
    r = RecurringTransaction(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["exp_cat"], description="Gym", amount=Decimal("30.00"),
        type="expense", frequency=Frequency.MONTHLY, next_due_date=date.today(),
        auto_settle=False, is_active=True,
    )
    db.add(r)
    await db.commit()
    return r.id


async def _add_instance(
    db: AsyncSession, seed: dict, recurring_id: int, *, status: TransactionStatus,
    description: str = "Gym", category_id: int | None = None, dt: date | None = None,
) -> int:
    when = dt or date.today()
    tx = Transaction(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=category_id or seed["exp_cat"], description=description,
        amount=Decimal("30.00"), type=TransactionType.EXPENSE, status=status,
        date=when,
        settled_date=when if status == TransactionStatus.SETTLED else None,
        recurring_id=recurring_id,
    )
    db.add(tx)
    await db.commit()
    return tx.id


async def test_edit_name_propagates_to_template_and_pending(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    p1 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    p2 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    settled = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.SETTLED,
        dt=date.today() - timedelta(days=10),
    )

    await transaction_service.update_transaction(
        db_session, seed["org_id"], p1, TransactionUpdate(description="Gym Membership"),
    )

    db_session.expire_all()
    assert (await db_session.get(RecurringTransaction, rid)).description == "Gym Membership"
    assert (await db_session.get(Transaction, p1)).description == "Gym Membership"
    assert (await db_session.get(Transaction, p2)).description == "Gym Membership"
    assert (await db_session.get(Transaction, settled)).description == "Gym"


async def test_edit_category_propagates_to_template_and_pending(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    p1 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    settled = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.SETTLED,
        dt=date.today() - timedelta(days=10),
    )

    await transaction_service.update_transaction(
        db_session, seed["org_id"], p1, TransactionUpdate(category_id=seed["exp_cat2"]),
    )

    db_session.expire_all()
    assert (await db_session.get(RecurringTransaction, rid)).category_id == seed["exp_cat2"]
    assert (await db_session.get(Transaction, p1)).category_id == seed["exp_cat2"]
    assert (await db_session.get(Transaction, settled)).category_id == seed["exp_cat"]


async def test_amount_only_edit_does_not_propagate(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    p1 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)

    await transaction_service.update_transaction(
        db_session, seed["org_id"], p1, TransactionUpdate(amount=Decimal("99.00")),
    )

    db_session.expire_all()
    tmpl = await db_session.get(RecurringTransaction, rid)
    assert tmpl.description == "Gym"
    assert tmpl.amount == Decimal("30.00")


async def test_edit_from_settled_instance_still_propagates(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    pending = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    settled = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.SETTLED,
        dt=date.today() - timedelta(days=10),
    )

    await transaction_service.update_transaction(
        db_session, seed["org_id"], settled, TransactionUpdate(description="Renamed"),
    )

    db_session.expire_all()
    assert (await db_session.get(RecurringTransaction, rid)).description == "Renamed"
    assert (await db_session.get(Transaction, pending)).description == "Renamed"
    assert (await db_session.get(Transaction, settled)).description == "Renamed"


async def test_edit_name_and_category_together_propagate(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    p1 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)

    await transaction_service.update_transaction(
        db_session,
        seed["org_id"],
        p1,
        TransactionUpdate(description="Gym Plus", category_id=seed["exp_cat2"]),
    )

    db_session.expire_all()
    tmpl = await db_session.get(RecurringTransaction, rid)
    assert tmpl.description == "Gym Plus"
    assert tmpl.category_id == seed["exp_cat2"]
    inst = await db_session.get(Transaction, p1)
    assert inst.description == "Gym Plus"
    assert inst.category_id == seed["exp_cat2"]


async def test_category_not_propagated_when_type_also_changed(db_session):
    from app.models.category import Category, CategoryType
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    p1 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)

    inc = Category(org_id=seed["org_id"], name="Bonus", slug="bonus", type=CategoryType.INCOME)
    db_session.add(inc)
    await db_session.commit()
    inc_id = inc.id

    await transaction_service.update_transaction(
        db_session, seed["org_id"], p1,
        TransactionUpdate(type="income", category_id=inc_id),
    )

    db_session.expire_all()
    tmpl = await db_session.get(RecurringTransaction, rid)
    # Template keeps its original expense category and type; no corrupting cross-type write.
    assert tmpl.category_id == seed["exp_cat"]
    assert tmpl.type == "expense"
    # The edited row itself did change (its own type/category), but that's local.
    edited = await db_session.get(Transaction, p1)
    assert edited.category_id == inc_id


async def test_category_not_propagated_from_type_diverged_instance(db_session):
    from app.models.category import Category, CategoryType
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)  # expense template, exp_cat
    diverged = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    sibling = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    inc1 = Category(org_id=seed["org_id"], name="Salary", slug="salary", type=CategoryType.INCOME)
    inc2 = Category(org_id=seed["org_id"], name="Bonus", slug="bonus", type=CategoryType.INCOME)
    db_session.add_all([inc1, inc2])
    await db_session.commit()
    inc1_id, inc2_id = inc1.id, inc2.id

    # Diverge `diverged` to income (type + compatible income category together).
    await transaction_service.update_transaction(
        db_session, seed["org_id"], diverged,
        TransactionUpdate(type="income", category_id=inc1_id),
    )
    # Now edit ONLY its category to another income category (no type change).
    await transaction_service.update_transaction(
        db_session, seed["org_id"], diverged, TransactionUpdate(category_id=inc2_id),
    )

    db_session.expire_all()
    # Expense template + expense sibling must NOT receive the income category.
    assert (await db_session.get(RecurringTransaction, rid)).category_id == seed["exp_cat"]
    assert (await db_session.get(Transaction, sibling)).category_id == seed["exp_cat"]
    # The edited (income) row itself changes.
    assert (await db_session.get(Transaction, diverged)).category_id == inc2_id


async def test_category_propagation_skips_type_diverged_sibling(db_session):
    from app.models.category import Category, CategoryType
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)  # expense template, exp_cat
    normal = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    diverged_sib = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    inc = Category(org_id=seed["org_id"], name="Salary", slug="salary", type=CategoryType.INCOME)
    db_session.add(inc)
    await db_session.commit()
    inc_id = inc.id

    # Diverge the sibling to income.
    await transaction_service.update_transaction(
        db_session, seed["org_id"], diverged_sib,
        TransactionUpdate(type="income", category_id=inc_id),
    )
    # Edit the NORMAL expense instance's category.
    await transaction_service.update_transaction(
        db_session, seed["org_id"], normal, TransactionUpdate(category_id=seed["exp_cat2"]),
    )

    db_session.expire_all()
    # Template (expense) + the normal expense row get the new expense category.
    assert (await db_session.get(RecurringTransaction, rid)).category_id == seed["exp_cat2"]
    assert (await db_session.get(Transaction, normal)).category_id == seed["exp_cat2"]
    # The income-diverged sibling is NOT overwritten with an expense category.
    assert (await db_session.get(Transaction, diverged_sib)).category_id == inc_id


async def test_unchanged_resave_does_not_propagate(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)  # description "Gym"
    p1 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING, description="Gym")
    # Drift the template so that, IF propagation wrongly fired, it would overwrite this.
    tmpl = await db_session.get(RecurringTransaction, rid)
    tmpl.description = "Drifted"
    await db_session.commit()
    # Re-save with the SAME description (no real change) plus an amount edit.
    await transaction_service.update_transaction(
        db_session, seed["org_id"], p1,
        TransactionUpdate(description="Gym", amount=Decimal("12.00")),
    )
    db_session.expire_all()
    # description didn't change → propagation must not have fired → drift preserved.
    assert (await db_session.get(RecurringTransaction, rid)).description == "Drifted"


async def test_stop_clears_recurring_link_on_survivors(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    settled = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.SETTLED,
        dt=date.today() - timedelta(days=10),
    )
    future_pending = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.PENDING,
        dt=date.today() + timedelta(days=10),
    )

    await recurring_service.stop_recurring(db_session, seed["org_id"], rid)

    db_session.expire_all()
    survivor = await db_session.get(Transaction, settled)
    assert survivor is not None
    assert survivor.recurring_id is None
    assert (await db_session.get(Transaction, future_pending)) is None


async def test_delete_clears_recurring_link_on_survivors(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    settled = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.SETTLED,
        dt=date.today() - timedelta(days=10),
    )

    await recurring_service.delete_recurring(db_session, seed["org_id"], rid)

    db_session.expire_all()
    survivor = await db_session.get(Transaction, settled)
    assert survivor is not None
    assert survivor.recurring_id is None
