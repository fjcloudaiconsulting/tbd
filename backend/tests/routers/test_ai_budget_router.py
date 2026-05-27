"""LAI.3 — router tests for ``POST /api/v1/ai/budget/rebalance``.

Pins:
- Feature gate ``ai.budget=False`` → 403 ``feature_not_enabled``.
- Gate True + service returns ``ok`` → 200 with typed payload.
- Gate True + service returns ``llm_unavailable`` → 200 + empty-state
  status (UI shouldn't crash).
- Audit row is written with the structural outcome + count.
"""
from __future__ import annotations

import base64
import datetime
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.auth.feature_deps import get_current_org_features
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.routers.ai_budget import router as ai_budget_router
from app.schemas.budget_rebalance import BudgetRebalanceResponse


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
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _set_ai_key(monkeypatch):
    monkeypatch.setattr(
        app_settings,
        "ai_credential_encryption_key",
        base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
    )
    monkeypatch.setattr(
        app_settings, "ai_credential_encryption_key_prev", ""
    )


def _make_app(session_factory, *, features: dict[str, bool], user: User):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return user

    def override_get_session_factory():
        return session_factory

    async def override_features() -> dict[str, bool]:
        return features

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.dependency_overrides[get_current_org_features] = override_features
    app.include_router(ai_budget_router)
    return app


@pytest_asyncio.fixture
async def org_and_user(session_factory) -> tuple[Organization, User]:
    async with session_factory() as session:
        org = Organization(name="Acme", billing_cycle_day=1)
        session.add(org)
        await session.commit()
        user = User(
            id=1,
            org_id=org.id,
            username="owner",
            email="owner@example.com",
            password_hash="x" * 64,
            role=Role.OWNER,
        )
        session.add(user)
        await session.commit()
        return org, user


def test_feature_gate_closed_returns_403(session_factory, org_and_user):
    _, user = org_and_user
    app = _make_app(
        session_factory,
        features={
            "ai.budget": False,
            "ai.forecast": False,
            "ai.smart_plan": False,
            "ai.autocategorize": False,
        },
        user=user,
    )
    client = TestClient(app)
    res = client.post("/api/v1/ai/budget/rebalance")
    assert res.status_code == 403
    body = res.json()
    assert body["detail"]["code"] == "feature_not_enabled"


def test_feature_gate_open_returns_typed_response(
    session_factory, org_and_user
):
    org, user = org_and_user
    app = _make_app(
        session_factory,
        features={
            "ai.budget": True,
            "ai.forecast": False,
            "ai.smart_plan": False,
            "ai.autocategorize": False,
        },
        user=user,
    )
    fake = BudgetRebalanceResponse(
        status="ok",
        period_start=str(datetime.date.today().replace(day=1)),
        summary="trim a little",
        suggestions=[],
    )
    with patch(
        "app.services.budget_rebalance_service.suggest_rebalance",
        new=AsyncMock(return_value=fake),
    ):
        client = TestClient(app)
        res = client.post("/api/v1/ai/budget/rebalance")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ok"
    assert body["summary"] == "trim a little"
    assert body["suggestions"] == []


def test_llm_unavailable_response_still_returns_200(
    session_factory, org_and_user
):
    org, user = org_and_user
    app = _make_app(
        session_factory,
        features={
            "ai.budget": True,
            "ai.forecast": False,
            "ai.smart_plan": False,
            "ai.autocategorize": False,
        },
        user=user,
    )
    fake = BudgetRebalanceResponse(
        status="llm_unavailable",
        period_start=str(datetime.date.today().replace(day=1)),
        summary="AI rebalance is temporarily unavailable.",
    )
    with patch(
        "app.services.budget_rebalance_service.suggest_rebalance",
        new=AsyncMock(return_value=fake),
    ):
        client = TestClient(app)
        res = client.post("/api/v1/ai/budget/rebalance")
    assert res.status_code == 200
    assert res.json()["status"] == "llm_unavailable"


def test_audit_row_written_with_outcome_and_count(
    session_factory, org_and_user
):
    """Each rebalance request must produce an audit row.

    The audit detail carries the structural outcome (success/failure)
    AND the suggestion count — never prompt or completion content.
    """
    org, user = org_and_user
    app = _make_app(
        session_factory,
        features={
            "ai.budget": True,
            "ai.forecast": False,
            "ai.smart_plan": False,
            "ai.autocategorize": False,
        },
        user=user,
    )
    fake = BudgetRebalanceResponse(
        status="ok",
        period_start=str(datetime.date.today().replace(day=1)),
        summary="x",
        suggestions=[],  # empty but ok status is success outcome
    )
    with patch(
        "app.services.budget_rebalance_service.suggest_rebalance",
        new=AsyncMock(return_value=fake),
    ):
        client = TestClient(app)
        res = client.post("/api/v1/ai/budget/rebalance")
    assert res.status_code == 200

    import asyncio

    async def _read_audit():
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type
                        == "ai.budget.rebalance.requested"
                    )
                )
            ).scalars().all()
            return rows

    rows = asyncio.get_event_loop().run_until_complete(_read_audit())
    assert len(rows) == 1
    detail = rows[0].detail or {}
    assert detail.get("status") == "ok"
    assert detail.get("suggestion_count") == 0


def test_audit_row_on_llm_unavailable_is_failure(
    session_factory, org_and_user
):
    """When the service returns ``llm_unavailable``, the audit row
    must land with ``outcome='failure'`` so ops can count real LLM
    outages without noise from user-state preconditions."""
    org, user = org_and_user
    app = _make_app(
        session_factory,
        features={
            "ai.budget": True,
            "ai.forecast": False,
            "ai.smart_plan": False,
            "ai.autocategorize": False,
        },
        user=user,
    )
    fake = BudgetRebalanceResponse(
        status="llm_unavailable",
        period_start=str(datetime.date.today().replace(day=1)),
        summary="AI temporarily unavailable.",
    )
    with patch(
        "app.services.budget_rebalance_service.suggest_rebalance",
        new=AsyncMock(return_value=fake),
    ):
        client = TestClient(app)
        res = client.post("/api/v1/ai/budget/rebalance")
    assert res.status_code == 200

    import asyncio

    async def _read_audit():
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type
                        == "ai.budget.rebalance.requested"
                    )
                )
            ).scalars().all()
            return rows

    rows = asyncio.get_event_loop().run_until_complete(_read_audit())
    assert len(rows) == 1
    assert rows[0].outcome.value == "failure"
    detail = rows[0].detail or {}
    assert detail.get("status") == "llm_unavailable"


def test_audit_row_on_empty_no_budgets_is_success(
    session_factory, org_and_user
):
    """``empty_no_budgets`` is a user-state precondition, NOT a system
    failure — audit outcome must reflect that so ops dashboards don't
    treat \"user hasn't set up budgets\" as a real outage."""
    org, user = org_and_user
    app = _make_app(
        session_factory,
        features={
            "ai.budget": True,
            "ai.forecast": False,
            "ai.smart_plan": False,
            "ai.autocategorize": False,
        },
        user=user,
    )
    fake = BudgetRebalanceResponse(
        status="empty_no_budgets",
        period_start=str(datetime.date.today().replace(day=1)),
        summary="No budgets are set.",
    )
    with patch(
        "app.services.budget_rebalance_service.suggest_rebalance",
        new=AsyncMock(return_value=fake),
    ):
        client = TestClient(app)
        res = client.post("/api/v1/ai/budget/rebalance")
    assert res.status_code == 200

    import asyncio

    async def _read_audit():
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type
                        == "ai.budget.rebalance.requested"
                    )
                )
            ).scalars().all()
            return rows

    rows = asyncio.get_event_loop().run_until_complete(_read_audit())
    assert len(rows) == 1
    assert rows[0].outcome.value == "success"
    detail = rows[0].detail or {}
    assert detail.get("status") == "empty_no_budgets"


def test_endpoint_never_mutates_budgets_table(
    session_factory, org_and_user
):
    """Architectural invariant: POST /rebalance is suggestion-only.
    The budgets table must be byte-identical before and after the
    request, regardless of how the service responded. Pinning at the
    router level guards against a future regression that auto-applies
    even one row.
    """
    import asyncio
    from decimal import Decimal

    from app.models.budget import Budget
    from app.models.category import Category, CategoryType

    org, user = org_and_user

    async def _seed_budgets():
        async with session_factory() as session:
            cat = Category(
                org_id=org.id,
                name="Groceries",
                slug="groceries",
                type=CategoryType.EXPENSE,
                is_system=False,
            )
            session.add(cat)
            await session.commit()
            b = Budget(
                org_id=org.id,
                category_id=cat.id,
                amount=Decimal("400.00"),
                period_start=datetime.date.today().replace(day=1),
                period_end=None,
            )
            session.add(b)
            await session.commit()
            return [(b.id, b.category_id, b.amount)]

    before = asyncio.get_event_loop().run_until_complete(_seed_budgets())

    app = _make_app(
        session_factory,
        features={
            "ai.budget": True,
            "ai.forecast": False,
            "ai.smart_plan": False,
            "ai.autocategorize": False,
        },
        user=user,
    )

    # Use the actual ok-status response shape with a non-empty
    # suggestion list — the regression we want to catch is "service
    # returned a suggestion AND the router secretly applied it".
    from app.schemas.budget_rebalance import BudgetDeltaSuggestion

    fake = BudgetRebalanceResponse(
        status="ok",
        period_start=str(datetime.date.today().replace(day=1)),
        summary="trim a little",
        suggestions=[
            BudgetDeltaSuggestion(
                category_id=before[0][1],
                category_name="Groceries",
                current_amount=Decimal("400.00"),
                suggested_amount=Decimal("450.00"),
                delta_amount=Decimal("50.00"),
                reasoning="bump",
            )
        ],
    )
    with patch(
        "app.services.budget_rebalance_service.suggest_rebalance",
        new=AsyncMock(return_value=fake),
    ):
        client = TestClient(app)
        res = client.post("/api/v1/ai/budget/rebalance")
    assert res.status_code == 200, res.text

    async def _read_budgets():
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(Budget).where(Budget.org_id == org.id)
                )
            ).scalars().all()
            return [(b.id, b.category_id, b.amount) for b in rows]

    after = asyncio.get_event_loop().run_until_complete(_read_budgets())
    assert before == after, (
        "POST /rebalance must NEVER mutate the budgets table; "
        f"before={before}, after={after}"
    )
