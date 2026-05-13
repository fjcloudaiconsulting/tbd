"""FITID cross-batch dedup scope (PR #247 P2).

OFX FITIDs are unique within an account (OFX spec §11.4.4). The
preview-level cross-batch dedup must scope by ``(org_id, account_id,
fitid)`` so two different bank exports for two different accounts that
happen to share a FITID string are NOT flagged as duplicates of each
other. The original implementation scoped only by ``(org_id, fitid)``;
this test pins the account-scoped behavior.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import (
    Account,
    AccountType,
    Category,
    Organization,
)
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import (
    Transaction,
    TransactionStatus,
    TransactionType,
)
from app.services import import_service
from app.services.import_parser import ParsedRow


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
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db: AsyncSession) -> dict:
    org = Organization(name="Primary", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    atype = AccountType(
        org_id=org.id, name="Checking", slug="checking", is_system=True
    )
    db.add(atype)
    await db.flush()
    acct_a = Account(
        org_id=org.id,
        name="Account A",
        account_type_id=atype.id,
        balance=Decimal("0"),
        currency="EUR",
    )
    acct_b = Account(
        org_id=org.id,
        name="Account B",
        account_type_id=atype.id,
        balance=Decimal("0"),
        currency="EUR",
    )
    db.add_all([acct_a, acct_b])
    await db.flush()
    cat = Category(
        org_id=org.id,
        name="Groceries",
        slug="groceries",
        type=CategoryType.EXPENSE,
    )
    db.add(cat)
    await db.commit()
    return {
        "org_id": org.id,
        "account_a_id": acct_a.id,
        "account_b_id": acct_b.id,
        "category_id": cat.id,
    }


def _parsed(fitid: str | None) -> ParsedRow:
    return ParsedRow(
        row_number=1,
        date=datetime.date(2026, 5, 10),
        description="Test merchant",
        amount=Decimal("12.50"),
        type="expense",
        counterparty=None,
        transaction_type="DEBIT",
        fitid=fitid,
        bank_id="BANK",
        account_type_ofx="CHECKING",
    )


@pytest.mark.asyncio
async def test_fitid_dedup_does_not_cross_accounts(db_session):
    """Two different accounts in the same org that happen to share a
    FITID string must NOT collide at preview time."""
    seed = await _seed(db_session)

    # Pre-existing transaction on Account A with FITID "SHARED".
    existing = Transaction(
        org_id=seed["org_id"],
        account_id=seed["account_a_id"],
        category_id=seed["category_id"],
        description="Bank A",
        amount=Decimal("12.50"),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=datetime.date(2026, 5, 10),
        settled_date=datetime.date(2026, 5, 10),
        is_imported=True,
        fitid="SHARED",
        reconciliation_state="accepted",
    )
    db_session.add(existing)
    await db_session.commit()

    # Importing the same FITID into Account B must NOT trigger a
    # duplicate flag -- different account, different bank semantically.
    preview = await import_service.build_preview(
        db_session,
        org_id=seed["org_id"],
        account_id=seed["account_b_id"],
        file_name="bank-b.ofx",
        parsed_rows=[_parsed("SHARED")],
        source_format="ofx",
    )
    assert preview.duplicate_count == 0
    assert preview.rows[0].is_duplicate is False


@pytest.mark.asyncio
async def test_fitid_dedup_catches_cross_batch_same_account(db_session):
    """Re-importing the SAME FITID into the SAME account is still
    flagged (the legitimate cross-batch dedup case)."""
    seed = await _seed(db_session)

    existing = Transaction(
        org_id=seed["org_id"],
        account_id=seed["account_a_id"],
        category_id=seed["category_id"],
        description="Bank A",
        amount=Decimal("12.50"),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=datetime.date(2026, 5, 10),
        settled_date=datetime.date(2026, 5, 10),
        is_imported=True,
        fitid="SAME",
        reconciliation_state="accepted",
    )
    db_session.add(existing)
    await db_session.commit()

    preview = await import_service.build_preview(
        db_session,
        org_id=seed["org_id"],
        account_id=seed["account_a_id"],
        file_name="bank-a.ofx",
        # Use a different date / description so only FITID matches.
        parsed_rows=[
            ParsedRow(
                row_number=1,
                date=datetime.date(2026, 5, 11),
                description="DIFFERENT MERCHANT",
                amount=Decimal("99.99"),
                type="expense",
                counterparty=None,
                transaction_type="DEBIT",
                fitid="SAME",
                bank_id="BANK",
                account_type_ofx="CHECKING",
            )
        ],
        source_format="ofx",
    )
    # Same account + same FITID -> duplicate flag fires.
    assert preview.duplicate_count == 1
    assert preview.rows[0].is_duplicate is True
