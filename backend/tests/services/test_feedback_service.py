"""Feedback service tests — privacy contract is the focus.

Three invariants under test:

1. `normalize_context` strips query strings off URLs.
2. `create_feedback_entry` respects `include_identity=False` even when
   the caller provides user_id / org_id.
3. `create_feedback_entry` records user_id / org_id when
   `include_identity=True`.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.feedback import FeedbackCategory, FeedbackEntry
from app.models.user import Organization, Role, User
from app.schemas.feedback import FeedbackContext
from app.security import hash_password
from app.services import feedback_service


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


async def _seed_user(factory):
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


# ---------------------------------------------------------------------------
# normalize_context — privacy stripping
# ---------------------------------------------------------------------------


def test_normalize_context_strips_query_string():
    ctx = FeedbackContext(url="http://localhost/login?token=xyz&foo=bar")
    out = feedback_service.normalize_context(ctx)
    assert out["url"] == "http://localhost/login"
    assert "token" not in out["url"]
    assert "xyz" not in out["url"]


def test_normalize_context_strips_fragment():
    ctx = FeedbackContext(url="http://localhost/transactions#tx-12345")
    out = feedback_service.normalize_context(ctx)
    assert out["url"] == "http://localhost/transactions"
    assert "12345" not in out["url"]


def test_normalize_context_preserves_clean_path():
    ctx = FeedbackContext(url="http://localhost/import/123/reconcile")
    out = feedback_service.normalize_context(ctx)
    assert out["url"] == "http://localhost/import/123/reconcile"


def test_normalize_context_packs_viewport():
    ctx = FeedbackContext(viewport_w=1440, viewport_h=900)
    out = feedback_service.normalize_context(ctx)
    assert out["viewport"] == {"w": 1440, "h": 900}


def test_normalize_context_empty_url_returns_no_url_key():
    ctx = FeedbackContext()
    out = feedback_service.normalize_context(ctx)
    assert "url" not in out


# ---------------------------------------------------------------------------
# create_feedback_entry — identity opt-in gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_feedback_anonymous_when_include_identity_false(session_factory):
    """The PRIVACY DEFAULT. user_id / org_id MUST be NULL on the row."""
    seeds = await _seed_user(session_factory)
    async with session_factory() as db:
        await feedback_service.create_feedback_entry(
            db,
            user_id=seeds["user_id"],
            org_id=seeds["org_id"],
            message="Something broke",
            category=FeedbackCategory.BUG,
            context=FeedbackContext(theme="light"),
            include_identity=False,
        )
        await db.commit()

    async with session_factory() as db:
        row = (await db.execute(select(FeedbackEntry))).scalar_one()
        assert row.user_id is None, "user_id leaked despite include_identity=False"
        assert row.org_id is None, "org_id leaked despite include_identity=False"
        assert row.message == "Something broke"
        assert row.category == FeedbackCategory.BUG
        assert row.context == {"theme": "light"}


@pytest.mark.asyncio
async def test_create_feedback_identified_when_include_identity_true(session_factory):
    seeds = await _seed_user(session_factory)
    async with session_factory() as db:
        await feedback_service.create_feedback_entry(
            db,
            user_id=seeds["user_id"],
            org_id=seeds["org_id"],
            message="Please follow up",
            category=FeedbackCategory.FEATURE,
            context=FeedbackContext(),
            include_identity=True,
        )
        await db.commit()

    async with session_factory() as db:
        row = (await db.execute(select(FeedbackEntry))).scalar_one()
        assert row.user_id == seeds["user_id"]
        assert row.org_id == seeds["org_id"]


@pytest.mark.asyncio
async def test_create_feedback_url_stripped_in_db(session_factory):
    """Defense-in-depth: even via the service path, the persisted
    `context` JSON must NOT contain the query string. This is the
    integration check that the normalize_context contract reaches DB.
    """
    seeds = await _seed_user(session_factory)
    async with session_factory() as db:
        await feedback_service.create_feedback_entry(
            db,
            user_id=seeds["user_id"],
            org_id=seeds["org_id"],
            message="x",
            category=FeedbackCategory.OTHER,
            context=FeedbackContext(
                url="http://localhost/login?token=secret123",
                user_agent="Mozilla/5.0",
            ),
            include_identity=False,
        )
        await db.commit()

    async with session_factory() as db:
        row = (await db.execute(select(FeedbackEntry))).scalar_one()
        assert "secret123" not in str(row.context)
        assert "token" not in str(row.context)
        assert row.context.get("url") == "http://localhost/login"
        assert row.context.get("user_agent") == "Mozilla/5.0"
