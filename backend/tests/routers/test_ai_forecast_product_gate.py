"""TBD-197 PR 2 — the Forecast product gate outranks the AI gate.

``POST /api/v1/ai/forecast/refine`` carries two independent gates:

  System 1 (``feature_gate.require_feature(Feature.FORECAST)``)  → 404
  System 2 (``feature_deps.require_feature("ai.forecast")``)     → 403 + upsell

An org that switched Forecast off must get the **404**. FastAPI solves
``APIRouter(...)`` constructor dependencies before both route-decorator
dependencies and handler *signature* dependencies — and on this router the
existing ``ai.forecast`` gate is a signature parameter
(``_gate: dict = Depends(require_feature("ai.forecast"))``), which is why the
System-1 dep goes on the constructor.

⚠ NON-VACUITY — and this is where the PR brief was wrong. It asked for
``ai.forecast`` pinned **True**. Under that pin the ordering mutant SURVIVES:
System 2 passes for an entitled org, so a System-1 dep placed *after* it still
answers 404 and the test cannot tell the two placements apart. The ordering
mutant is only observable when ``ai.forecast`` is **False**, where the wrong
placement yields 403 and the right one yields 404. That is the same shape PR 1
used for ``ai.budget`` in F6/F6c, and it is what this file pins.

The ``ai.forecast: True`` case is still worth a row — it kills a DIFFERENT
mutant (spec §11: "delete the System-1 gate on the AI routers, the ``ai.*``
dep already denies"), because a Pro org has ``ai.forecast: True`` and would
otherwise reach the handler with Forecast switched off. Both rows are below.
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
from app.routers.ai_forecast import router as ai_forecast_router
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
        org = Organization(name="AI Forecast Gate Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="ai-forecast-admin",
            email="ai-forecast@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id}


def _make_app(factory, user_id: int, ai_forecast_entitled: bool) -> FastAPI:
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
        return {"ai.forecast": ai_forecast_entitled}

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_current_org_features] = override_features
    app.include_router(ai_forecast_router)
    return app


async def _opt_out_of_forecast(factory, org_id: int) -> None:
    async with factory() as db:
        db.add(
            OrgSetting(
                org_id=org_id,
                key=org_preference_key(Feature.FORECAST),
                value="off",
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_orgpref_off_returns_404_not_403(session_factory):
    """Writes ``OrgSetting(org, "orgpref.forecast", "off")`` and pins the
    ``ai.forecast`` entitlement **False**.

    Expects **404** — the product gate answers first.
    Mutant killed: the System-1 dep placed after the ``ai.forecast`` dep (as a
    second handler signature parameter instead of a router-constructor
    dependency), which yields a 403 advertising an AI upsell for a product area
    the org just switched off.
    """
    ids = await _seed(session_factory)
    await _opt_out_of_forecast(session_factory, ids["org_id"])

    app = _make_app(session_factory, ids["user_id"], ai_forecast_entitled=False)
    with TestClient(app) as client:
        res = client.post("/api/v1/ai/forecast/refine", json={})
    assert res.status_code == 404, res.text


@pytest.mark.asyncio
async def test_entitled_org_with_forecast_off_still_404s(session_factory):
    """Writes the same ``orgpref.forecast="off"`` row but pins ``ai.forecast``
    **True** — a Pro org.

    Expects **404**. Mutant killed: dropping the System-1 gate from this router
    entirely on the theory that the ``ai.*`` dep already denies (spec §11,
    rejected with evidence). It does not: an entitled org sails straight past
    it into ``POST /api/v1/ai/forecast/refine``.
    """
    ids = await _seed(session_factory)
    await _opt_out_of_forecast(session_factory, ids["org_id"])

    app = _make_app(session_factory, ids["user_id"], ai_forecast_entitled=True)
    with TestClient(app) as client:
        res = client.post("/api/v1/ai/forecast/refine", json={})
    assert res.status_code == 404, res.text


@pytest.mark.asyncio
async def test_control_forecast_on_still_returns_403(session_factory):
    """Control. **No** ``orgpref.forecast`` row; ``ai.forecast`` still False.

    Expects **403** with the AI feature payload. Proves the 404s above came
    from the product gate rather than from a blanket 404 or a broken route.
    """
    ids = await _seed(session_factory)
    app = _make_app(session_factory, ids["user_id"], ai_forecast_entitled=False)
    with TestClient(app) as client:
        res = client.post("/api/v1/ai/forecast/refine", json={})
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["feature_key"] == "ai.forecast"
