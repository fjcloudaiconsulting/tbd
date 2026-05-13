"""Feedback router tests — HTTP surface + audit wiring.

Covers:
- Auth-required (no current_user override -> 401-ish from the dep).
- Message validation (empty rejects, oversize rejects).
- Category enum validation.
- Identity opt-in default-OFF behavior end-to-end.
- Audit event `feedback.submitted` emitted with the right fields.

Pattern mirrors `test_tags.py`: in-memory SQLite + FastAPI app with
dependency overrides.
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
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.feedback import FeedbackCategory, FeedbackEntry
from app.models.user import Organization, Role, User
from app.routers.feedback import router as feedback_router
from app.security import hash_password


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


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Feedback Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="reporter",
            email="r@x.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id}


def make_app(factory, user_id: int):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def override_current_user() -> User:
        async with factory() as db:
            return (
                await db.execute(select(User).where(User.id == user_id))
            ).scalar_one()

    def override_session_factory():
        return factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(feedback_router)
    return app


# ---------------------------------------------------------------------------
# Happy path + privacy contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_anonymous_by_default(session_factory):
    """include_identity defaults False; the row must be anonymous."""
    seeds = await _seed(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/feedback",
            json={
                "message": "Something is off on the dashboard",
                "category": "bug",
                "context": {
                    "url": "http://localhost/dashboard",
                    "user_agent": "TestClient",
                    "theme": "dark",
                },
            },
        )
    assert res.status_code == 201, res.text
    async with session_factory() as db:
        rows = (await db.execute(select(FeedbackEntry))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id is None, "user_id stored despite include_identity not set"
    assert row.org_id is None
    assert row.category == FeedbackCategory.BUG


@pytest.mark.asyncio
async def test_submit_feedback_identified_when_optin(session_factory):
    seeds = await _seed(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/feedback",
            json={
                "message": "Please add export to CSV",
                "category": "feature",
                "include_identity": True,
                "context": {},
            },
        )
    assert res.status_code == 201, res.text
    async with session_factory() as db:
        row = (await db.execute(select(FeedbackEntry))).scalar_one()
    assert row.user_id == seeds["user_id"]
    assert row.org_id == seeds["org_id"]


@pytest.mark.asyncio
async def test_submit_feedback_strips_query_string_from_url(session_factory):
    """Privacy invariant: even if frontend sends ?token=..., we don't store it."""
    seeds = await _seed(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/feedback",
            json={
                "message": "x",
                "category": "other",
                "context": {"url": "http://localhost/login?token=SECRET"},
            },
        )
    assert res.status_code == 201, res.text
    async with session_factory() as db:
        row = (await db.execute(select(FeedbackEntry))).scalar_one()
    assert "SECRET" not in str(row.context)
    assert row.context["url"] == "http://localhost/login"


@pytest.mark.asyncio
async def test_submit_feedback_emits_audit_event(session_factory):
    seeds = await _seed(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/feedback",
            json={
                "message": "Crash on import preview",
                "category": "bug",
                "include_identity": False,
                "context": {},
            },
        )
    assert res.status_code == 201
    async with session_factory() as db:
        events = (
            await db.execute(
                select(AuditEvent).where(AuditEvent.event_type == "feedback.submitted")
            )
        ).scalars().all()
    assert len(events) == 1
    audit = events[0]
    assert audit.actor_user_id == seeds["user_id"]
    assert audit.outcome.value == "success"
    assert audit.detail["category"] == "bug"
    assert audit.detail["identity_attached"] is False
    # The audit log must NOT mirror the message body.
    assert "Crash" not in str(audit.detail)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_empty_message_rejected(session_factory):
    seeds = await _seed(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/feedback",
            json={"message": "", "category": "bug"},
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_submit_feedback_oversize_message_rejected(session_factory):
    seeds = await _seed(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/feedback",
            json={"message": "x" * 5001, "category": "bug"},
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_submit_feedback_invalid_category_rejected(session_factory):
    seeds = await _seed(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/feedback",
            json={"message": "x", "category": "spam"},
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_submit_feedback_unknown_field_rejected(session_factory):
    """`extra="forbid"` on the schema — typos won't silently succeed."""
    seeds = await _seed(session_factory)
    app = make_app(session_factory, seeds["user_id"])
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/feedback",
            json={"message": "x", "category": "bug", "wat": "no"},
        )
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_requires_auth(session_factory):
    """No get_current_user override -> the real HTTPBearer dep runs and
    rejects the unauth call. We mount the router into an app WITHOUT the
    auth override so this verifies the dep is wired.
    """
    app = FastAPI()
    app.include_router(feedback_router)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/feedback",
            json={"message": "x", "category": "bug"},
        )
    assert res.status_code in (401, 403)
