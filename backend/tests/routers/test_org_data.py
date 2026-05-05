"""Router tests for L3.1 — POST /api/v1/orgs/data/reset.

Service-layer behavior is pinned in
``tests/services/test_org_data_service.py``. This file pins the auth
gate (owner-only via ``require_org_owner``), the typed-confirm
contract, response shape, and the structured audit-log emissions.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Base
from app.models.subscription import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import Organization, Role, User
from app.routers import org_data as org_data_module
from app.routers.org_data import router as org_data_router
from app.security import hash_password


# ── Fixture: in-memory aiosqlite + FK enforcement ──────────────────────────


@pytest_asyncio.fixture
async def session_factory():
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


# ── Test app builder ───────────────────────────────────────────────────────


def make_app(session_factory, current_user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await current_user_resolver(session_factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(org_data_router)
    return app


# ── Seed helpers ───────────────────────────────────────────────────────────


ORG_NAME = "Acme Household"


async def _seed(factory) -> dict:
    """One org with owner + admin + member, all in the same org."""
    async with factory() as db:
        plan = Plan(slug="free", name="Free")
        db.add(plan)
        org = Organization(name=ORG_NAME, billing_cycle_day=1)
        db.add(org)
        await db.commit()
        owner = User(
            org_id=org.id, username="owner", email="o@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        admin = User(
            org_id=org.id, username="admin", email="a@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        member = User(
            org_id=org.id, username="member", email="m@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([owner, admin, member])
        await db.commit()
        sub = Subscription(
            org_id=org.id, plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            billing_interval=BillingInterval.MONTHLY,
        )
        db.add(sub)
        await db.commit()
        return {
            "org_id": org.id,
            "owner_id": owner.id,
            "admin_id": admin.id,
            "member_id": member.id,
        }


def _resolver_for(role: Role):
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.role == role))
            ).scalar_one()
    return resolve


# ── Audit-event capture ────────────────────────────────────────────────────


class _CapturingLogger:
    """Substitute for the structlog logger that records (event, kwargs)
    tuples for every ainfo / aerror call."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def ainfo(self, event: str, **kwargs):
        self.events.append((event, dict(kwargs)))

    async def aerror(self, event: str, **kwargs):
        self.events.append((event, dict(kwargs)))


@pytest.fixture
def capture_logger(monkeypatch):
    cap = _CapturingLogger()
    monkeypatch.setattr(org_data_module, "logger", cap)
    return cap


# ── Auth gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_owner_succeeds(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.OWNER))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/data/reset",
            json={"confirm_phrase": f"RESET {ORG_NAME}"},
        )
    assert res.status_code == 200
    body = res.json()
    assert "deleted_rows_by_table" in body
    assert isinstance(body["deleted_rows_by_table"], dict)


@pytest.mark.asyncio
async def test_reset_admin_forbidden(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/data/reset",
            json={"confirm_phrase": f"RESET {ORG_NAME}"},
        )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_reset_member_forbidden(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.MEMBER))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/data/reset",
            json={"confirm_phrase": f"RESET {ORG_NAME}"},
        )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_reset_unauthenticated_rejected(session_factory):
    # Build the app WITHOUT the get_current_user override so the real
    # auth dep runs. FastAPI's HTTPBearer dep returns 403 on missing
    # Authorization header (not 401) — same as every other authed
    # endpoint in this app. The point is "no auth → no access".
    await _seed(session_factory)
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(org_data_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/data/reset",
            json={"confirm_phrase": f"RESET {ORG_NAME}"},
        )
    assert res.status_code in (401, 403)


# ── Phrase validation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_phrase", [
    f"RESET {ORG_NAME.lower()}",   # wrong case
    "RESET Wrong",                 # wrong name
    "RESET",                       # too short
    ORG_NAME,                      # missing the verb
    "",                            # empty
])
async def test_reset_wrong_phrase_400(session_factory, bad_phrase):
    await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.OWNER))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/data/reset",
            json={"confirm_phrase": bad_phrase},
        )
    assert res.status_code == 400, f"phrase {bad_phrase!r} unexpectedly accepted"


@pytest.mark.asyncio
async def test_reset_phrase_trimmed(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.OWNER))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/data/reset",
            json={"confirm_phrase": f"   RESET {ORG_NAME}  "},
        )
    assert res.status_code == 200


# ── Audit log emissions ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_logs_audit_event(session_factory, capture_logger):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.OWNER))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/data/reset",
            json={"confirm_phrase": f"RESET {ORG_NAME}"},
        )
    assert res.status_code == 200

    success_events = [e for e in capture_logger.events if e[0] == "org.data.reset"]
    assert len(success_events) == 1
    payload = success_events[0][1]
    assert payload["actor_user_id"] == seed["owner_id"]
    assert payload["actor_email"] == "o@acme.io"
    assert payload["actor_role"] == "owner"
    assert payload["org_id"] == seed["org_id"]
    assert payload["org_name"] == ORG_NAME
    assert isinstance(payload["deleted_rows_by_table"], dict)


@pytest.mark.asyncio
async def test_reset_failure_logs_failed_event(monkeypatch, session_factory, capture_logger):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _resolver_for(Role.OWNER))

    from app.services import org_data_service

    async def boom(*a, **kw):
        raise RuntimeError("simulated DB failure")
    monkeypatch.setattr(org_data_service, "reset_org_data", boom)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/data/reset",
            json={"confirm_phrase": f"RESET {ORG_NAME}"},
        )
    assert res.status_code == 500

    failed = [e for e in capture_logger.events if e[0] == "org.data.reset.failed"]
    assert len(failed) == 1
    payload = failed[0][1]
    assert payload["actor_user_id"] == seed["owner_id"]
    assert payload["org_id"] == seed["org_id"]
    assert payload["error_type"] == "RuntimeError"
    assert "simulated DB failure" in payload["error"]
