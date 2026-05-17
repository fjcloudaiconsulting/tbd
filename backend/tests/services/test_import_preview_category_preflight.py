"""Layer B preflight: per-row-type category requirement at preview time.

Architect-refined Category Fallback design (post-L3.10). When a CSV /
OFX parse yields ``type="expense"`` rows the org must have at least one
EXPENSE-or-BOTH category. Same shape for ``income``. ``BOTH`` covers
either side. The exception carries a structured ``missing_types`` list
the router maps to a 400 with code ``missing_category_type``.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.user import Organization
from app.services import import_service
from app.services.exceptions import MissingCategoryTypeError
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


async def _seed_org(
    db: AsyncSession, *, category_types: list[CategoryType] | None = None,
) -> dict:
    """Seed an org + checking account, plus a category of each requested type.

    ``category_types=None`` means seed NO categories (the empty-org case).
    """
    org = Organization(name="Test", billing_cycle_day=1)
    db.add(org)
    await db.flush()

    atype = AccountType(
        org_id=org.id, name="Checking", slug="checking", is_system=True,
    )
    db.add(atype)
    await db.flush()
    acct = Account(
        org_id=org.id, account_type_id=atype.id, name="Checking",
        balance=Decimal("0"),
    )
    db.add(acct)

    for i, ct in enumerate(category_types or []):
        slug = f"cat_{ct.value}_{i}"
        db.add(Category(
            org_id=org.id, name=slug.title(), slug=slug,
            type=ct, is_system=False,
        ))
    await db.commit()
    return {"org_id": org.id, "account_id": acct.id}


def _row(row_num: int, *, type: str) -> ParsedRow:
    return ParsedRow(
        row_number=row_num,
        date=datetime.date(2026, 5, 10),
        description=f"Row {row_num}",
        amount=Decimal("10.00"),
        type=type,
        counterparty=None,
        transaction_type=None,
    )


# ── Happy paths: required type present ────────────────────────────────────


async def test_expense_rows_with_expense_category_passes(
    db_session: AsyncSession,
) -> None:
    seed = await _seed_org(db_session, category_types=[CategoryType.EXPENSE])
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"], account_id=seed["account_id"],
        file_name="t.csv", parsed_rows=[_row(1, type="expense")],
    )
    assert result.total_rows == 1


async def test_expense_rows_with_both_category_passes(
    db_session: AsyncSession,
) -> None:
    seed = await _seed_org(db_session, category_types=[CategoryType.BOTH])
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"], account_id=seed["account_id"],
        file_name="t.csv", parsed_rows=[_row(1, type="expense")],
    )
    assert result.total_rows == 1


async def test_income_rows_with_income_category_passes(
    db_session: AsyncSession,
) -> None:
    seed = await _seed_org(db_session, category_types=[CategoryType.INCOME])
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"], account_id=seed["account_id"],
        file_name="t.csv", parsed_rows=[_row(1, type="income")],
    )
    assert result.total_rows == 1


async def test_mixed_rows_with_both_specific_types_passes(
    db_session: AsyncSession,
) -> None:
    seed = await _seed_org(
        db_session,
        category_types=[CategoryType.EXPENSE, CategoryType.INCOME],
    )
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"], account_id=seed["account_id"],
        file_name="t.csv",
        parsed_rows=[_row(1, type="expense"), _row(2, type="income")],
    )
    assert result.total_rows == 2


async def test_mixed_rows_with_only_both_category_passes(
    db_session: AsyncSession,
) -> None:
    """A single BOTH-typed category satisfies both row types."""
    seed = await _seed_org(db_session, category_types=[CategoryType.BOTH])
    result = await import_service.build_preview(
        db_session, org_id=seed["org_id"], account_id=seed["account_id"],
        file_name="t.csv",
        parsed_rows=[_row(1, type="expense"), _row(2, type="income")],
    )
    assert result.total_rows == 2


# ── Sad paths: required type missing ──────────────────────────────────────


async def test_expense_rows_with_only_income_category_raises(
    db_session: AsyncSession,
) -> None:
    seed = await _seed_org(db_session, category_types=[CategoryType.INCOME])
    with pytest.raises(MissingCategoryTypeError) as exc:
        await import_service.build_preview(
            db_session, org_id=seed["org_id"], account_id=seed["account_id"],
            file_name="t.csv", parsed_rows=[_row(1, type="expense")],
        )
    assert exc.value.missing_types == ["expense"]


async def test_income_rows_with_only_expense_category_raises(
    db_session: AsyncSession,
) -> None:
    seed = await _seed_org(db_session, category_types=[CategoryType.EXPENSE])
    with pytest.raises(MissingCategoryTypeError) as exc:
        await import_service.build_preview(
            db_session, org_id=seed["org_id"], account_id=seed["account_id"],
            file_name="t.csv", parsed_rows=[_row(1, type="income")],
        )
    assert exc.value.missing_types == ["income"]


async def test_mixed_rows_missing_income_raises_for_income_only(
    db_session: AsyncSession,
) -> None:
    """Mixed rows where the org has expense but no income flag only income."""
    seed = await _seed_org(db_session, category_types=[CategoryType.EXPENSE])
    with pytest.raises(MissingCategoryTypeError) as exc:
        await import_service.build_preview(
            db_session, org_id=seed["org_id"], account_id=seed["account_id"],
            file_name="t.csv",
            parsed_rows=[_row(1, type="expense"), _row(2, type="income")],
        )
    assert exc.value.missing_types == ["income"]


async def test_mixed_rows_with_no_categories_raises_for_both(
    db_session: AsyncSession,
) -> None:
    seed = await _seed_org(db_session, category_types=[])
    with pytest.raises(MissingCategoryTypeError) as exc:
        await import_service.build_preview(
            db_session, org_id=seed["org_id"], account_id=seed["account_id"],
            file_name="t.csv",
            parsed_rows=[_row(1, type="expense"), _row(2, type="income")],
        )
    assert exc.value.missing_types == ["expense", "income"]
    # Message is non-empty and contains a hint about what's missing.
    # (Frontend reads ``code``/``missing_types``; ``message`` is fallback.)
    assert "income" in exc.value.message.lower()
    assert "expense" in exc.value.message.lower()


async def test_expense_rows_with_other_org_category_raises(
    db_session: AsyncSession,
) -> None:
    """Org-scoping: a category on a sibling org doesn't satisfy our check."""
    # Org A: has the row, no categories.
    seed_a = await _seed_org(db_session, category_types=[])
    # Org B: has the expense category. Should NOT count for Org A.
    other_org = Organization(name="Other", billing_cycle_day=1)
    db_session.add(other_org)
    await db_session.flush()
    db_session.add(Category(
        org_id=other_org.id, name="Other Expense", slug="other_exp",
        type=CategoryType.EXPENSE, is_system=False,
    ))
    await db_session.commit()

    with pytest.raises(MissingCategoryTypeError) as exc:
        await import_service.build_preview(
            db_session, org_id=seed_a["org_id"], account_id=seed_a["account_id"],
            file_name="t.csv", parsed_rows=[_row(1, type="expense")],
        )
    assert exc.value.missing_types == ["expense"]
