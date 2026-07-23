"""Tests for Task 2 of CC Statement Alerts V1: per-send email
suppression and a divergent email body on
``dispatch_notification_to_org_members``.

- ``send_email=False`` skips the email channel entirely (in-app fanout
  still runs). Lets a job send in-app-only (e.g. the later CC
  statement reminder job).
- ``email_body`` overrides the body used for the email channel while
  the in-app row keeps the original ``body``. Lets the close job omit
  the dollar amount from the email while the in-app body keeps it.

Both kwargs default to the prior behavior (``send_email=True``,
``email_body=None`` -> falls back to ``body``) so every existing
caller (billing_close, billing_reminder, recurring_generation) is
unaffected.

Fixture idiom mirrors ``test_scheduler_notifications.py`` /
``test_notification_service.py`` (in-memory SQLite + FK pragma +
``async_sessionmaker``) rather than the brief's placeholder
``db_session``/``seed_org_with_member`` names, which don't exist in
this repo's test suite.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.notification import NotificationCategory
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import notification_service as ns


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


@pytest_asyncio.fixture
async def org_with_member(session_factory):
    """One org + one active member. Returns the org id."""
    async with session_factory() as db:
        org = Organization(name="ChannelCtrlOrg", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        user = User(
            org_id=org.id,
            username="member-a",
            email="member-a@test.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return org.id


@pytest.mark.asyncio
async def test_send_email_false_skips_email(session_factory, org_with_member):
    org_id = org_with_member
    with patch.object(ns, "_send_notification_email_best_effort", new=AsyncMock()) as m:
        async with session_factory() as db:
            written = await ns.dispatch_notification_to_org_members(
                db,
                org_id=org_id,
                category=NotificationCategory.CC_STATEMENT,
                event_type="t",
                title="x",
                body="y",
                send_email=False,
            )
            await db.commit()
    assert written == 1
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_body_overrides_body_for_email_only(session_factory, org_with_member):
    org_id = org_with_member
    with patch.object(ns, "_send_notification_email_best_effort", new=AsyncMock()) as m:
        async with session_factory() as db:
            await ns.dispatch_notification_to_org_members(
                db,
                org_id=org_id,
                category=NotificationCategory.CC_STATEMENT,
                event_type="t",
                title="x",
                body="in-app 100.00 EUR due",
                email_body="open the app",
            )
            await db.commit()
    m.assert_awaited_once()
    assert m.await_args.kwargs["body"] == "open the app"


@pytest.mark.asyncio
async def test_email_body_none_falls_back_to_body(session_factory, org_with_member):
    """Default (no ``email_body`` passed) keeps prior behavior: the
    email channel uses the same ``body`` as the in-app row.
    """
    org_id = org_with_member
    with patch.object(ns, "_send_notification_email_best_effort", new=AsyncMock()) as m:
        async with session_factory() as db:
            await ns.dispatch_notification_to_org_members(
                db,
                org_id=org_id,
                category=NotificationCategory.CC_STATEMENT,
                event_type="t",
                title="x",
                body="same body everywhere",
            )
            await db.commit()
    m.assert_awaited_once()
    assert m.await_args.kwargs["body"] == "same body everywhere"
