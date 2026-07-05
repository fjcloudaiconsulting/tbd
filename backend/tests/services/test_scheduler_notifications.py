"""Tests for the scheduler notification layer built in Task 6:

- Three new copy templates (no em-dashes, non-empty).
- ``dispatch_notification_to_org_members`` dual-channel fan-out: writes
  in-app rows AND sends best-effort email per active org member,
  respecting each channel's per-user preference. A member who opted
  out of BOTH ``org_activity`` channels receives neither.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.notification import Notification, NotificationCategory, UserNotificationPreferences
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import notification_service as ns
from app.services import notification_templates as nt


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator:
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
async def org_with_members(session_factory):
    """One org + three active MEMBER users.

    Two users have NO preference row (defaults apply → now ON for
    org_activity, both channels). One user has an explicit preference
    row with BOTH ``email_org_activity`` and ``in_app_org_activity``
    set to False (opted out of both channels for org_activity).

    Returns ``(org, all_user_ids, opted_out_id)``.
    """
    async with session_factory() as db:
        org = Organization(name="SchedOrg", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        default_a = User(
            org_id=org.id,
            username="member-a",
            email="member-a@test.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_active=True,
            email_verified=True,
        )
        default_b = User(
            org_id=org.id,
            username="member-b",
            email="member-b@test.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_active=True,
            email_verified=True,
        )
        opted_out = User(
            org_id=org.id,
            username="member-c",
            email="member-c@test.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_active=True,
            email_verified=True,
        )
        db.add_all([default_a, default_b, opted_out])
        await db.commit()

        opted_out_id = opted_out.id

        prefs = ns._default_preferences(opted_out_id)
        prefs.email_org_activity = False
        prefs.in_app_org_activity = False
        db.add(prefs)
        await db.commit()

        all_user_ids = [default_a.id, default_b.id, opted_out_id]

    return org, all_user_ids, opted_out_id


# ── template tests ────────────────────────────────────────────────────────────


def test_templates_have_no_em_dashes():
    for title, body, _ in (
        nt.scheduler_recurring_generated(generated=2, settled=1),
        nt.scheduler_billing_close_reminder(close_date=datetime.date(2026, 8, 1), days_until=3),
        nt.scheduler_billing_closed(new_period_start=datetime.date(2026, 8, 1)),
    ):
        assert "—" not in title and "—" not in body
        assert title and body


# ── fan-out tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fanout_dual_channel_respects_optout(session_factory, org_with_members, monkeypatch):
    org, all_user_ids, opted_out_id = org_with_members  # 3 users; 1 opted out of both channels
    emails_sent = []

    async def _fake_email(to, *, title, body, link_url=None):
        emails_sent.append(to)
        return True

    monkeypatch.setattr(ns, "send_notification_email", _fake_email)

    async with session_factory() as db:
        written = await ns.dispatch_notification_to_org_members(
            db, org_id=org.id, category=NotificationCategory.ORG_ACTIVITY,
            event_type="scheduler.recurring_generation.success",
            title="Recurring transactions generated", body="body",
        )
        await db.commit()

    # in-app: 2 rows written (the opted-out user's in_app pref is False)
    assert written == 2
    # email: 2 sent (opted-out user's email pref is False)
    assert len(emails_sent) == 2
    assert "member-c@test.io" not in emails_sent

    async with session_factory() as db:
        count = (await db.execute(select(func.count()).select_from(Notification))).scalar_one()
    assert count == 2
