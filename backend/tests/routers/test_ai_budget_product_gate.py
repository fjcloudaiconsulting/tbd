"""TBD-197 PR 1 — F6 / F6c: the Budgets product gate outranks the AI gate.

``POST /api/v1/ai/budget/rebalance`` carries two independent gates:

  System 1 (``feature_gate.require_feature(Feature.BUDGETS)``)  → 404
  System 2 (``feature_deps.require_feature("ai.budget")``)      → 403 + upsell

An org that switched Budgets off must get the **404** — a 403 advertising an
AI budget upsell for a product area the org just turned off is incoherent, and
it also leaks that the endpoint exists. FastAPI solves ``APIRouter(...)``
constructor dependencies before route-decorator dependencies, which is the
whole reason the System-1 dep goes on the constructor and not next to the
existing ``dependencies=[Depends(require_feature("ai.budget"))]``.

⚠ NON-VACUITY: ``ai.budget`` is pinned **False** in F6. Its natural home,
``test_ai_budget_router.py``, sets ``"ai.budget": True`` everywhere — written
there, the "dep ordering" mutant passes, because the 403 branch never fires.
F6c is the control that proves the 403 is still reachable at all.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

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
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.settings import OrgSetting
from app.models.user import Organization, Role, User
from app.routers.ai_budget import router as ai_budget_router
from app.security import hash_password
from app.services.feature_gate import Feature, org_preference_key


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


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="AI Gate Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="ai-gate-admin",
            email="ai-gate@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id}


def _make_app(factory, user_id: int, ai_budget_entitled: bool) -> FastAPI:
    app = FastAPI()

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    async def override_user() -> User:
        async with factory() as db:
            return (
                await db.execute(select(User).where(User.id == user_id))
            ).scalar_one()

    async def override_features() -> dict[str, bool]:
        return {"ai.budget": ai_budget_entitled}

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_current_org_features] = override_features
    app.include_router(ai_budget_router)
    return app


@pytest.mark.asyncio
async def test_f6_orgpref_off_returns_404_not_403(session_factory):
    """F6. Writes ``OrgSetting(org, "orgpref.budgets", "off")`` and pins the
    ``ai.budget`` entitlement **False**.

    Expects **404** — the product gate answers first.
    Mutant killed: the System-1 dep placed after the ``ai.budget`` dep (route
    decorator instead of router constructor), which yields 403.
    """
    ids = await _seed(session_factory)
    async with session_factory() as db:
        db.add(
            OrgSetting(
                org_id=ids["org_id"],
                key=org_preference_key(Feature.BUDGETS),
                value="off",
            )
        )
        await db.commit()

    app = _make_app(session_factory, ids["user_id"], ai_budget_entitled=False)
    with TestClient(app) as client:
        res = client.post("/api/v1/ai/budget/rebalance")
    assert res.status_code == 404, res.text


@pytest.mark.asyncio
async def test_f6c_control_budgets_on_still_returns_403(session_factory):
    """F6c control. **No** ``orgpref.budgets`` row; ``ai.budget`` still False.

    Expects **403** with the AI feature payload. Proves F6's 404 came from the
    product gate rather than from a blanket 404 or a broken route.
    """
    ids = await _seed(session_factory)
    app = _make_app(session_factory, ids["user_id"], ai_budget_entitled=False)
    with TestClient(app) as client:
        res = client.post("/api/v1/ai/budget/rebalance")
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["feature_key"] == "ai.budget"
