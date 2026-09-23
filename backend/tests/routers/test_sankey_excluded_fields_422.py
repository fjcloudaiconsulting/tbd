"""TBD-552 — through the ROUTER, prove the Sankey endpoint refuses the
DENIED filter fields with a body that names the whitelist, not just a status
code.

⚠ A bare 422 status assertion is satisfiable WITHOUT the whitelist existing
at all: an unknown field 422s on the closed ``FilterField`` enum before it
ever reaches ``_apply_user_filters``, and a malformed value 422s in
``_coerce_filter_scalar``, both upstream of the code under test. So every
filter here is a REAL ``FilterField`` member with a VALID value
(``transfer=true``, ``currency="EUR"``, ``account_type=1``... except
``account_type`` has no FILTER published, so the two DENIED members actually
reachable through the AST layer are ``transfer`` and ``currency`` — see
``_SANKEY_DENIED_FILTER_FIELDS``), and the assertion reads the response BODY
for the sentence the ValueError carries.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.routers.reports import router as reports_router
from app.security import hash_password


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch):
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)


def _make_app(session_factory, user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_user() -> User:
        return await user_resolver(session_factory)

    def override_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_session_factory] = override_factory
    app.include_router(reports_router)
    return app


def _resolver(username: str):
    async def resolve(sf):
        async with sf() as db:
            from sqlalchemy import select as _s
            return (await db.execute(_s(User).where(User.username == username))).scalar_one()
    return resolve


@pytest_asyncio.fixture
async def seeded_org(session_factory) -> dict:
    async with session_factory() as db:
        org = Organization(name="Org", billing_cycle_day=1, primary_currency="EUR")
        db.add(org)
        await db.commit()

        user = User(
            org_id=org.id, username="user_a", email="a@example.com",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER,
            email_verified=True,
        )
        db.add(user)
        await db.commit()

        at = AccountType(org_id=org.id, name="Checking")
        db.add(at)
        await db.commit()

        acct = Account(
            org_id=org.id, account_type_id=at.id, name="Bank",
            currency="EUR", balance=Decimal("0"),
        )
        db.add(acct)
        await db.commit()

        cat = Category(org_id=org.id, name="Salary")
        db.add(cat)
        await db.commit()

        today = date(2026, 6, 1)
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description="Salary", amount=Decimal("5000"),
                type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
                date=today, settled_date=today,
            )
        )
        await db.commit()
        return {"org_id": org.id}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters",
    [
        [{"field": "transfer", "op": "eq", "value": True}],
        [{"field": "currency", "op": "eq", "value": "EUR"}],
    ],
    ids=["transfer", "currency"],
)
async def test_denied_filter_field_422s_with_whitelist_in_body(
    session_factory, seeded_org, filters
):
    """A REAL member, a VALID value — the only way to reach the whitelist
    check rather than an upstream enum/scalar-coercion 422."""
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/reports/query/sankey", json={"filters": filters}
        )

    assert res.status_code == 422, res.text
    assert "not supported on the Sankey endpoint" in res.json()["detail"]
