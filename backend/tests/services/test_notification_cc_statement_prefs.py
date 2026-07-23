"""Preference-gating tests for the ``cc_statement`` notification category.

CC Statement Alerts V1 (Task 1) adds a fifth notification category —
``cc_statement`` — that two later scheduler jobs (reminder + close) will
dispatch through. This module pins the substrate-level contract:

- The enum member exists with the exact wire value ``"cc_statement"``.
- The category is wired into BOTH ``_IN_APP_PREF_FIELD`` and
  ``_EMAIL_PREF_FIELD``. Missing from either map silently force-sends
  (both maps default-allow a category absent from the dict — see
  ``_in_app_preference_allows``/``_email_preference_allows``), which
  defeats the user's opt-out. This is the F6 security concern from the
  design spec.
- The preference row defaults both channels ON (opt-out), mirroring
  ``email_account``/``in_app_account`` — NOT the opt-in
  ``org_activity`` shape.

Follows the established local fixture pattern in
``test_notification_service.py`` (``session_factory`` + ``_seed_user``)
rather than a shared ``db_session``/``seed_user`` conftest fixture,
since no such shared fixtures exist in this test tree.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.notification import NotificationCategory
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import notification_service as ns


def test_cc_statement_category_exists():
    assert NotificationCategory.CC_STATEMENT.value == "cc_statement"


def test_cc_statement_wired_in_both_pref_maps():
    # Missing from either map silently force-sends (security F6). Both
    # must be present.
    assert ns._IN_APP_PREF_FIELD[NotificationCategory.CC_STATEMENT] == "in_app_cc_statement"
    assert ns._EMAIL_PREF_FIELD[NotificationCategory.CC_STATEMENT] == "email_cc_statement"


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


async def _seed_user(factory, *, username: str = "alice", email: str = "alice@ex.io") -> int:
    async with factory() as db:
        org = Organization(name=f"Org-{username}", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username=username,
            email=email,
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return user.id


@pytest.mark.asyncio
async def test_cc_statement_prefs_default_on(session_factory):
    user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        prefs = await ns.get_preferences(db, user_id=user_id)
        assert prefs.email_cc_statement is True
        assert prefs.in_app_cc_statement is True


@pytest.mark.asyncio
async def test_dispatch_respects_cc_statement_in_app_optout(session_factory):
    """Opting out of in-app cc_statement skips the row write."""
    user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        prefs = await ns.get_preferences(db, user_id=user_id)
        prefs.in_app_cc_statement = False
        await db.commit()

    async with session_factory() as db:
        row = await ns.dispatch_notification(
            db,
            user_id=user_id,
            category=NotificationCategory.CC_STATEMENT,
            event_type="cc_statement.reminder",
            title="t",
            body="b",
        )
        assert row is None
