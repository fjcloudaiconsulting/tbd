"""transaction_service.create_transaction + update_transaction learn rules.

Covers Task 8 of L3.10.
"""
import datetime
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.category_rule import CategoryRule, RuleSource
from app.models.user import Organization
from app.schemas.transaction import TransactionCreate, TransactionUpdate
from app.services import transaction_service


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _r):
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
    """Mirror the seed pattern from tests/services/test_import_execute_with_rules.py."""
    org = Organization(name="X", billing_cycle_day=1)
    db.add(org)
    await db.flush()

    groc = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        is_system=True, type=CategoryType.EXPENSE,
    )
    rest = Category(
        org_id=org.id, name="Restaurants", slug="restaurants",
        is_system=True, type=CategoryType.EXPENSE,
    )
    db.add_all([groc, rest])
    await db.flush()

    atype = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(atype)
    await db.flush()

    acct = Account(
        org_id=org.id, account_type_id=atype.id, name="Checking",
        balance=Decimal("0"),
    )
    db.add(acct)
    await db.commit()
    return {
        "org_id": org.id, "account_id": acct.id,
        "groceries_id": groc.id, "restaurants_id": rest.id,
    }


async def test_create_transaction_learns_user_edit(db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    body = TransactionCreate(
        account_id=seed["account_id"], category_id=seed["groceries_id"],
        description="POS LIDL *9999", amount=Decimal("12.50"),
        type="expense", status="settled", date=datetime.date(2026, 5, 1),
    )
    await transaction_service.create_transaction(db_session, seed["org_id"], body)

    rule = (await db_session.execute(select(CategoryRule))).scalar_one()
    assert rule.source == RuleSource.USER_EDIT
    assert rule.category_id == seed["groceries_id"]
    assert rule.match_count == 1


async def test_update_changing_category_learns_and_bumps(db_session: AsyncSession) -> None:
    """Create writes a rule once; update to a new category bumps match_count to 2."""
    seed = await _seed(db_session)
    body = TransactionCreate(
        account_id=seed["account_id"], category_id=seed["restaurants_id"],
        description="POS LIDL *9999", amount=Decimal("12.50"),
        type="expense", status="settled", date=datetime.date(2026, 5, 1),
    )
    tx = await transaction_service.create_transaction(db_session, seed["org_id"], body)

    update = TransactionUpdate(category_id=seed["groceries_id"])
    await transaction_service.update_transaction(
        db_session, seed["org_id"], tx.id, update,
    )

    rule = (await db_session.execute(select(CategoryRule))).scalar_one()
    assert rule.source == RuleSource.USER_EDIT
    assert rule.category_id == seed["groceries_id"]
    assert rule.match_count == 2


async def test_update_without_category_change_does_not_learn(
    db_session: AsyncSession,
) -> None:
    """Update that touches only amount → no rule write, no match_count bump."""
    seed = await _seed(db_session)
    body = TransactionCreate(
        account_id=seed["account_id"], category_id=seed["groceries_id"],
        description="POS LIDL *9999", amount=Decimal("12.50"),
        type="expense", status="settled", date=datetime.date(2026, 5, 1),
    )
    tx = await transaction_service.create_transaction(db_session, seed["org_id"], body)

    update = TransactionUpdate(amount=Decimal("13.00"))
    await transaction_service.update_transaction(
        db_session, seed["org_id"], tx.id, update,
    )

    rule = (await db_session.execute(select(CategoryRule))).scalar_one()
    assert rule.match_count == 1


async def test_create_transaction_with_is_imported_does_not_learn(
    db_session: AsyncSession,
) -> None:
    """Imports learn via execute_import (Task 7), not via create_transaction."""
    seed = await _seed(db_session)
    body = TransactionCreate(
        account_id=seed["account_id"], category_id=seed["groceries_id"],
        description="POS LIDL *9999", amount=Decimal("12.50"),
        type="expense", status="settled", date=datetime.date(2026, 5, 1),
    )
    await transaction_service.create_transaction(
        db_session, seed["org_id"], body, is_imported=True,
    )
    rules = (await db_session.execute(select(CategoryRule))).scalars().all()
    assert rules == []
