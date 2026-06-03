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
from app.services import transaction_service

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
