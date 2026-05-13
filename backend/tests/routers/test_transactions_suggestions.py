"""Router-level tests for description-suggestion autocomplete (L3.2 Wave 2A).

Focus: privacy + caching + end-to-end shape. Ranking is covered by the
service-layer tests; here we verify the HTTP surface:

- Auth required (401 without a session).
- ``Cache-Control: private, max-age=60`` (per §5.4).
- The handler logs only ``org_id``, ``type``, ``query_length``,
  ``result_count`` — NEVER the raw ``q`` or returned descriptions.
  This is the most important guard (§5.4 privacy rule).
"""
from __future__ import annotations

import datetime
import json
import logging
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.routers.transactions import router as transactions_router
from app.security import hash_password
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


# ── shared fixtures ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_world(factory) -> tuple[int, int]:
    """Seed an org with an account, a category, and a few transactions.

    Returns ``(org_id, user_id)``.
    """
    async with factory() as db:
        org = Organization(name="LogTest", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        db.add(at)
        await db.flush()
        acct = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="Main",
            balance=Decimal("0.00"),
            currency="EUR",
        )
        cat = Category(org_id=org.id, name="Groceries", type=CategoryType.EXPENSE)
        db.add_all([acct, cat])
        await db.flush()
        # A few transactions so the ranking has data to chew on.
        for d, desc in [
            (datetime.date(2026, 5, 11), "Albert Heijn"),
            (datetime.date(2026, 5, 10), "Albert Heijn"),
            (datetime.date(2026, 5, 9), "Albert Cuyp"),
        ]:
            db.add(
                Transaction(
                    org_id=org.id,
                    account_id=acct.id,
                    category_id=cat.id,
                    description=desc,
                    amount=Decimal("12.34"),
                    type=TransactionType.EXPENSE,
                    status=TransactionStatus.SETTLED,
                    date=d,
                    settled_date=d,
                    is_imported=False,
                )
            )
        user = User(
            org_id=org.id,
            username="logtest",
            email="log@test.example",
            password_hash=hash_password("pw-test-12345"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return org.id, user.id


def _make_app(session_factory, *, authenticated: bool = True) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    if authenticated:
        async def override_current_user() -> User:
            from sqlalchemy import select
            async with session_factory() as db:
                return (
                    await db.execute(
                        select(User).where(User.is_superadmin.is_(True))
                    )
                ).scalar_one()
        app.dependency_overrides[get_current_user] = override_current_user
    else:
        async def reject_user():
            raise HTTPException(status_code=401, detail="not authenticated")
        app.dependency_overrides[get_current_user] = reject_user

    app.dependency_overrides[get_db] = override_get_db

    @app.exception_handler(NotFoundError)
    async def _nfe(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _vle(request, exc):
        return JSONResponse(status_code=400, content={"detail": exc.detail})

    @app.exception_handler(ConflictError)
    async def _cfe(request, exc):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    app.include_router(transactions_router)
    return app


# ── auth ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_requires_auth(session_factory):
    await _seed_world(session_factory)
    app = _make_app(session_factory, authenticated=False)
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/transactions/suggestions/descriptions",
            params={"type": "expense", "q": "Alb"},
        )
    assert resp.status_code == 401


# ── shape + ranking sanity at HTTP layer ────────────────────────────────


@pytest.mark.asyncio
async def test_returns_ranked_suggestions(session_factory):
    await _seed_world(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/transactions/suggestions/descriptions",
            params={"type": "expense", "q": "Alb", "limit": 10},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "suggestions" in body
    descs = [s["description"] for s in body["suggestions"]]
    assert descs == ["Albert Heijn", "Albert Cuyp"]
    # Verify the contract response shape — every suggestion carries the
    # required keys, including the category hint.
    for s in body["suggestions"]:
        assert {
            "description",
            "category_id",
            "category_name",
            "use_count",
            "last_used",
        } == set(s.keys())


@pytest.mark.asyncio
async def test_cache_control_private(session_factory):
    """Privacy: response is cacheable per-user only (§5.4)."""
    await _seed_world(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/transactions/suggestions/descriptions",
            params={"type": "expense"},
        )
    assert resp.status_code == 200
    cc = resp.headers.get("cache-control", "")
    assert "private" in cc
    assert "max-age=60" in cc


# ── no-PII logging guard ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_pii_in_logs(session_factory, caplog):
    """Strictest privacy guard (§5.4).

    During a request with known query strings ('AlbertSecret123', plus
    seeded descriptions 'Albert Heijn' / 'Albert Cuyp'), NONE of those
    strings may appear in any APPLICATION log record at INFO level or
    above. INFO is what production runs at; DB-driver DEBUG logs do
    include bound parameter values, but those are off in production
    and never reach the JSON log stream.

    The metric event MUST still carry query_length + result_count so
    operators can monitor usage without seeing user input.
    """
    await _seed_world(session_factory)
    app = _make_app(session_factory)

    # Force structlog routing through stdlib so caplog sees our event.
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    with caplog.at_level(logging.INFO):
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/transactions/suggestions/descriptions",
                params={"type": "expense", "q": "AlbertSecret123"},
            )
    assert resp.status_code == 200

    # Only inspect APPLICATION logs (app.*). Excludes httpx access-log
    # records (which include the URL, where ``q`` lives in the query
    # string by design) and DB-driver DEBUG which is off in production.
    app_records = [
        r for r in caplog.records
        if r.name.startswith("app.") and r.levelno >= logging.INFO
    ]
    rendered = "\n".join(
        r.getMessage() + " " + json.dumps(r.__dict__, default=str)
        for r in app_records
    )
    # The raw query string must NEVER appear in any application log.
    assert "AlbertSecret123" not in rendered, (
        f"Raw query leaked into application log output:\n{rendered}"
    )
    # Seeded descriptions must likewise stay out of any application log.
    for desc_str in ("Albert Heijn", "Albert Cuyp"):
        assert desc_str not in rendered, (
            f"Description '{desc_str}' leaked into application logs:\n{rendered}"
        )

    # The metric/event must still carry query_length + result_count.
    metric_lines = [
        r for r in app_records
        if r.name.startswith("app.routers.transactions.suggestions")
    ]
    assert metric_lines, "Expected at least one suggestions logger event"
    blob = "\n".join(
        json.dumps(r.__dict__, default=str) + "\n" + r.getMessage()
        for r in metric_lines
    )
    assert "query_length" in blob
    assert "result_count" in blob


# ── q-omitted path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_q_omitted_returns_top_n(session_factory):
    """When q is omitted, the most-used descriptions surface."""
    await _seed_world(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/transactions/suggestions/descriptions",
            params={"type": "expense"},
        )
    assert resp.status_code == 200
    descs = [s["description"] for s in resp.json()["suggestions"]]
    # "Albert Heijn" has 2 uses; "Albert Cuyp" has 1. Most-used first.
    assert descs == ["Albert Heijn", "Albert Cuyp"]


# ── q < 2 chars is rejected at the router ───────────────────────────────


@pytest.mark.asyncio
async def test_short_q_is_rejected(session_factory):
    await _seed_world(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/transactions/suggestions/descriptions",
            params={"type": "expense", "q": "a"},
        )
    assert resp.status_code == 422
