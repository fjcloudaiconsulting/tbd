"""Tests for the platform-level PAT expiry-reminder job (Task 6, spec §10).

The job is NOT part of the per-org ``REGISTRY`` runner: PAT tokens belong to a
superadmin and have no org dimension, so it runs once per tick (under the
existing tick lock) and is gated on a global ``SystemSetting`` flag rather than
a per-org ``scheduler.`` toggle.

Behavior under test:

- Each threshold (14d / 3d / on-or-after expiry) fires exactly once and advances
  ``reminder_stage`` (0 → 1 → 2 → 3), sending exactly one email + one in-app row.
- A second run at the same wall-clock does NOT re-notify (idempotent stage-advance).
- Tokens with a null owner (``created_by_user_id IS NULL``) are skipped.
- Revoked tokens are skipped.
- Flag OFF (no ``SystemSetting`` row / value != on) → no-op.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from datetime import timezone

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.api_token import ApiToken
from app.models.notification import Notification
from app.models.system_setting import SystemSetting
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services.scheduler.jobs import api_token_expiry as job


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
async def superadmin(session_factory) -> int:
    """One superadmin owner; returns the user id."""
    async with session_factory() as db:
        org = Organization(name="PlatformOrg", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username="root",
            email="root@test.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_active=True,
            email_verified=True,
            is_superadmin=True,
        )
        db.add(user)
        await db.commit()
        return user.id


# ── helpers ──────────────────────────────────────────────────────────────────


NOW = datetime.datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


async def _enable_flag(session_factory) -> None:
    async with session_factory() as db:
        db.add(SystemSetting(key=job.FLAG_KEY, value="on"))
        await db.commit()


async def _mk_token(
    session_factory,
    *,
    owner_id: int | None,
    expires_at: datetime.datetime,
    reminder_stage: int = 0,
    revoked_at: datetime.datetime | None = None,
    prefix: str = "pat_aaaaaaaaaa",
) -> int:
    async with session_factory() as db:
        tok = ApiToken(
            token_hash=prefix + "hash",
            token_prefix=prefix,
            name="ci-token",
            scope="read",
            created_by_user_id=owner_id,
            created_by_email="root@test.io",
            expires_at=expires_at.replace(tzinfo=None),  # stored naive-UTC
            reminder_stage=reminder_stage,
            revoked_at=None if revoked_at is None else revoked_at.replace(tzinfo=None),
        )
        db.add(tok)
        await db.commit()
        return tok.id


async def _stage(session_factory, token_id: int) -> int:
    async with session_factory() as db:
        return (
            await db.execute(
                select(ApiToken.reminder_stage).where(ApiToken.id == token_id)
            )
        ).scalar_one()


async def _inapp_count(session_factory) -> int:
    async with session_factory() as db:
        return (
            await db.execute(select(func.count()).select_from(Notification))
        ).scalar_one()


async def _last_notification(session_factory) -> Notification:
    async with session_factory() as db:
        return (
            await db.execute(
                select(Notification).order_by(Notification.id.desc()).limit(1)
            )
        ).scalar_one()


@pytest.fixture
def sent_emails(monkeypatch):
    box: list[str] = []

    async def _fake_email(to, *, title, body, link_url=None):
        box.append(to)
        return True

    monkeypatch.setattr(job, "send_notification_email", _fake_email)
    return box


@pytest.fixture
def sent_email_links(monkeypatch):
    """Like ``sent_emails`` but captures the ``link_url`` kwarg for assertion."""
    box: list[str | None] = []

    async def _fake_email(to, *, title, body, link_url=None):
        box.append(link_url)
        return True

    monkeypatch.setattr(job, "send_notification_email", _fake_email)
    return box


# ── threshold tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_14d_threshold_fires_once_and_is_idempotent(session_factory, superadmin, sent_emails):
    await _enable_flag(session_factory)
    tid = await _mk_token(
        session_factory, owner_id=superadmin, expires_at=NOW + datetime.timedelta(days=14)
    )

    await job.run_api_token_expiry_reminders(session_factory, now=NOW)

    assert await _stage(session_factory, tid) == 1
    assert len(sent_emails) == 1
    assert await _inapp_count(session_factory) == 1

    # Second run at the same wall-clock: stage 1 already sent, and 14 days is not
    # <= 3, so no new notification.
    await job.run_api_token_expiry_reminders(session_factory, now=NOW)
    assert await _stage(session_factory, tid) == 1
    assert len(sent_emails) == 1
    assert await _inapp_count(session_factory) == 1


@pytest.mark.asyncio
async def test_3d_threshold_fires_once_and_is_idempotent(session_factory, superadmin, sent_emails):
    await _enable_flag(session_factory)
    tid = await _mk_token(
        session_factory,
        owner_id=superadmin,
        expires_at=NOW + datetime.timedelta(days=3),
        reminder_stage=1,  # 14d already sent
    )

    await job.run_api_token_expiry_reminders(session_factory, now=NOW)

    assert await _stage(session_factory, tid) == 2
    assert len(sent_emails) == 1
    assert await _inapp_count(session_factory) == 1

    await job.run_api_token_expiry_reminders(session_factory, now=NOW)
    assert await _stage(session_factory, tid) == 2
    assert len(sent_emails) == 1


@pytest.mark.asyncio
async def test_expiry_threshold_fires_once_and_is_terminal(session_factory, superadmin, sent_emails):
    await _enable_flag(session_factory)
    tid = await _mk_token(
        session_factory,
        owner_id=superadmin,
        expires_at=NOW - datetime.timedelta(hours=1),  # already past expiry
        reminder_stage=2,  # 3d already sent
    )

    await job.run_api_token_expiry_reminders(session_factory, now=NOW)

    assert await _stage(session_factory, tid) == 3
    assert len(sent_emails) == 1
    assert await _inapp_count(session_factory) == 1

    # Stage 3 is terminal (fully reminded) — never re-notify.
    await job.run_api_token_expiry_reminders(session_factory, now=NOW)
    assert await _stage(session_factory, tid) == 3
    assert len(sent_emails) == 1


@pytest.mark.asyncio
async def test_full_progression_across_ticks(session_factory, superadmin, sent_emails):
    await _enable_flag(session_factory)
    tid = await _mk_token(
        session_factory, owner_id=superadmin, expires_at=NOW + datetime.timedelta(days=14)
    )
    # 14d tick
    await job.run_api_token_expiry_reminders(session_factory, now=NOW)
    assert await _stage(session_factory, tid) == 1
    # 3d tick (11 days later)
    await job.run_api_token_expiry_reminders(session_factory, now=NOW + datetime.timedelta(days=11))
    assert await _stage(session_factory, tid) == 2
    # expiry tick (on expiry)
    await job.run_api_token_expiry_reminders(session_factory, now=NOW + datetime.timedelta(days=14))
    assert await _stage(session_factory, tid) == 3
    assert len(sent_emails) == 3
    assert await _inapp_count(session_factory) == 3


# ── copy tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stage1_copy_reflects_real_remaining_days_not_14d_bucket(
    session_factory, superadmin, sent_emails
):
    """A token minted with a ~7-day expiry fires stage 1 (crosses the 14d
    threshold immediately) but the copy must say ~7 days, not the hardcoded
    "14 days" stage-window label (the review finding this fix addresses).
    """
    await _enable_flag(session_factory)
    tid = await _mk_token(
        session_factory, owner_id=superadmin, expires_at=NOW + datetime.timedelta(days=7)
    )

    await job.run_api_token_expiry_reminders(session_factory, now=NOW)

    assert await _stage(session_factory, tid) == 1
    note = await _last_notification(session_factory)
    assert "7 days" in note.body
    assert "14" not in note.body
    assert "14" not in note.title


@pytest.mark.asyncio
async def test_stage1_copy_singular_day(session_factory, superadmin, sent_emails):
    """Exactly 1 day remaining renders singular ("1 day"), not "1 days"."""
    await _enable_flag(session_factory)
    tid = await _mk_token(
        session_factory, owner_id=superadmin, expires_at=NOW + datetime.timedelta(days=1)
    )

    await job.run_api_token_expiry_reminders(session_factory, now=NOW)

    assert await _stage(session_factory, tid) == 1
    note = await _last_notification(session_factory)
    assert "1 day" in note.body
    assert "1 days" not in note.body


@pytest.mark.asyncio
async def test_stage1_copy_sub_day_says_less_than_a_day(
    session_factory, superadmin, sent_emails
):
    """A few hours from expiry (rounds to 0 whole days) never shows "0 days"
    or a negative count for a pre-expiry stage."""
    await _enable_flag(session_factory)
    tid = await _mk_token(
        session_factory, owner_id=superadmin, expires_at=NOW + datetime.timedelta(hours=6)
    )

    await job.run_api_token_expiry_reminders(session_factory, now=NOW)

    assert await _stage(session_factory, tid) == 1
    note = await _last_notification(session_factory)
    assert "less than a day" in note.body
    assert "0 day" not in note.body


@pytest.mark.asyncio
async def test_expiry_stage_copy_says_expired(session_factory, superadmin, sent_emails):
    await _enable_flag(session_factory)
    tid = await _mk_token(
        session_factory,
        owner_id=superadmin,
        expires_at=NOW - datetime.timedelta(hours=1),
        reminder_stage=2,
    )

    await job.run_api_token_expiry_reminders(session_factory, now=NOW)

    assert await _stage(session_factory, tid) == 3
    note = await _last_notification(session_factory)
    assert "has expired" in note.body


@pytest.mark.asyncio
async def test_reminder_link_points_at_real_system_page(
    session_factory, superadmin, sent_email_links
):
    """The reminder must deep-link to the real superadmin page
    (``/system/api-tokens``), not the 404'ing ``/admin/api-tokens``."""
    await _enable_flag(session_factory)
    tid = await _mk_token(
        session_factory, owner_id=superadmin, expires_at=NOW + datetime.timedelta(days=14)
    )

    await job.run_api_token_expiry_reminders(session_factory, now=NOW)

    assert await _stage(session_factory, tid) == 1
    note = await _last_notification(session_factory)
    assert note.link_url == "/system/api-tokens"
    assert sent_email_links == ["/system/api-tokens"]


# ── skip tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_not_yet_due_no_notify(session_factory, superadmin, sent_emails):
    await _enable_flag(session_factory)
    tid = await _mk_token(
        session_factory, owner_id=superadmin, expires_at=NOW + datetime.timedelta(days=20)
    )
    await job.run_api_token_expiry_reminders(session_factory, now=NOW)
    assert await _stage(session_factory, tid) == 0
    assert len(sent_emails) == 0
    assert await _inapp_count(session_factory) == 0


@pytest.mark.asyncio
async def test_null_owner_skipped(session_factory, sent_emails):
    await _enable_flag(session_factory)
    tid = await _mk_token(
        session_factory, owner_id=None, expires_at=NOW + datetime.timedelta(days=1)
    )
    await job.run_api_token_expiry_reminders(session_factory, now=NOW)
    assert await _stage(session_factory, tid) == 0
    assert len(sent_emails) == 0
    assert await _inapp_count(session_factory) == 0


@pytest.mark.asyncio
async def test_revoked_token_skipped(session_factory, superadmin, sent_emails):
    await _enable_flag(session_factory)
    tid = await _mk_token(
        session_factory,
        owner_id=superadmin,
        expires_at=NOW + datetime.timedelta(days=1),
        revoked_at=NOW - datetime.timedelta(days=1),
    )
    await job.run_api_token_expiry_reminders(session_factory, now=NOW)
    assert await _stage(session_factory, tid) == 0
    assert len(sent_emails) == 0
    assert await _inapp_count(session_factory) == 0


@pytest.mark.asyncio
async def test_flag_off_is_noop(session_factory, superadmin, sent_emails):
    # No SystemSetting row written → flag defaults OFF.
    tid = await _mk_token(
        session_factory, owner_id=superadmin, expires_at=NOW + datetime.timedelta(days=1)
    )
    await job.run_api_token_expiry_reminders(session_factory, now=NOW)
    assert await _stage(session_factory, tid) == 0
    assert len(sent_emails) == 0
    assert await _inapp_count(session_factory) == 0
