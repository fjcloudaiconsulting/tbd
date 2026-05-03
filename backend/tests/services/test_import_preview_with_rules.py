"""build_preview attaches suggestions; transfers don't get suggested.

Covers Task 6 of L3.10: the preview integration of infer_category.
"""
import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.merchant_dictionary import MerchantDictionaryEntry
from app.models.user import Organization
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
    org = Organization(name="X", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    groceries = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        is_system=True, type=CategoryType.EXPENSE,
    )
    db.add(groceries)
    atype = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(atype)
    await db.flush()
    acct = Account(
        org_id=org.id, account_type_id=atype.id, name="Checking",
        balance=Decimal("0"),
    )
    db.add(acct)
    db.add(MerchantDictionaryEntry(
        normalized_token="LIDL", category_slug="groceries",
        is_seed=True, vote_count=0,
    ))
    await db.commit()
    return {"org_id": org.id, "account_id": acct.id, "groceries_id": groceries.id}


async def test_preview_attaches_shared_dictionary_suggestion(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session)
    rows = [
        ParsedRow(
            row_number=1, date=datetime.date(2026, 5, 1),
            description="POS LIDL *9999", amount=Decimal("12.50"),
            type="expense", counterparty=None, transaction_type=None,
        ),
    ]
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"], account_id=seed["account_id"],
        file_name="t.csv", parsed_rows=rows,
    )
    assert result.rows[0].suggested_category_id == seed["groceries_id"]
    assert result.rows[0].suggestion_source == "shared_dictionary"


async def test_preview_legacy_online_banking_string_no_longer_triggers_transfer(
    db_session: AsyncSession,
) -> None:
    """PR-C C1 removed the transaction_type heuristic; the row is now plain."""
    seed = await _seed(db_session)
    rows = [
        ParsedRow(
            row_number=1, date=datetime.date(2026, 5, 1),
            description="POS LIDL *9999", amount=Decimal("12.50"),
            type="expense", counterparty=None, transaction_type="online banking",
        ),
    ]
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"], account_id=seed["account_id"],
        file_name="t.csv", parsed_rows=rows,
    )
    # PR-C C1 removed the `transaction_type=='online banking'` heuristic.
    # C2 wires real detectors instead; with no DB matches, action is "none".
    assert result.rows[0].transfer_match_action == "none"
    # The smart-rules suggestion no longer skips on the legacy heuristic.
    # POS LIDL still resolves via the shared dictionary seed.
    assert result.rows[0].suggested_category_id == seed["groceries_id"]
    assert result.rows[0].suggestion_source == "shared_dictionary"


async def test_preview_default_source_when_no_match(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session)
    rows = [
        ParsedRow(
            row_number=1, date=datetime.date(2026, 5, 1),
            description="POS UNKNOWN MERCHANT", amount=Decimal("4.00"),
            type="expense", counterparty=None, transaction_type=None,
        ),
    ]
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"], account_id=seed["account_id"],
        file_name="t.csv", parsed_rows=rows,
    )
    assert result.rows[0].suggested_category_id is None
    assert result.rows[0].suggestion_source == "default"


async def test_preview_emits_aggregate_metric(
    db_session: AsyncSession,
) -> None:
    """One smart_rules.preview_built event per preview, with the right shape.

    The architect mandated aggregate (not per-row) telemetry. Structlog routes
    through its own renderer, so caplog doesn't see records — patching the
    bound logger's ainfo is the reliable way to capture the call.
    """
    seed = await _seed(db_session)
    rows = [
        ParsedRow(
            row_number=i, date=datetime.date(2026, 5, 1),
            description=desc, amount=Decimal("1"),
            type="expense", counterparty=None, transaction_type=None,
        )
        for i, desc in enumerate(["POS LIDL *0001", "POS LIDL *0002", "POS UNKNOWN"], 1)
    ]
    with patch.object(import_service.logger, "ainfo", new_callable=AsyncMock) as spy:
        await import_service.build_preview(
            db_session, org_id=seed["org_id"], account_id=seed["account_id"],
            file_name="t.csv", parsed_rows=rows,
        )

    aggregate_calls = [
        c for c in spy.call_args_list
        if c.args and c.args[0] == "smart_rules.preview_built"
    ]
    assert len(aggregate_calls) == 1, "exactly one aggregate metric per preview"
    kwargs = aggregate_calls[0].kwargs
    assert kwargs["org_id"] == seed["org_id"]
    assert kwargs["rows_total"] == 3
    assert kwargs["suggested_count"] == 2
    assert kwargs["source_split"] == {"shared_dictionary": 2, "default": 1}
