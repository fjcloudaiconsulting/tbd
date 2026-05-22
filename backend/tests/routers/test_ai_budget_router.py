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
    assert rows[0].outcome.value == "success"
