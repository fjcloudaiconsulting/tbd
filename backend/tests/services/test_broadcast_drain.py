"""Tests for the send-drain engine (Task 4, spec
``2026-07-18-admin-email-broadcast-design.md``).

Covers, with ``send_email`` mocked and pacing monkeypatched to 0:
- a fresh drain sends every ``pending`` recipient, marks them ``sent`` and
  bumps the broadcast counters + ``status=completed``;
- a user who lapsed (deactivated) after materialization is ``skipped``
  (Ruling 9), the rest still ``sent``;
- one ``send_email`` returning False marks that row ``failed`` while the rest
  ``sent`` and the drain still completes (Ruling 12);
- concurrency (Ruling 14a): ``launch_drain`` twice for the same id runs one
  drain, so ``send_email`` is called exactly once per distinct pending row —
  the in-process registry blocks the second launch;
- attempts cap (Ruling 14b): ``resume_pending`` does not retry a ``pending``
  row already at ``broadcast_max_attempts``;
- HTML escape (Ruling 14d): a ``first_name`` containing ``<``/``&`` is escaped
  in the HTML arg passed to ``send_email``;
- drain-raise observed (Ruling 14e): an unhandled drain error is logged by the
  done-callback, the task is removed from ``_DRAIN_TASKS`` and the broadcast
  ``status`` is ``failed``.

Uses an in-memory aiosqlite engine (same fixture pattern as the other
broadcast service tests) so no running MySQL / docker-compose stack is
required.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import event, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.email_broadcast import (
    SEGMENT_ACTIVE_VERIFIED,
    BroadcastStatus,
    EmailBroadcast,
    EmailBroadcastRecipient,
    RecipientStatus,
)
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import broadcast_service


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fast_pacing(monkeypatch):
    """No real sleeping between sends."""
    monkeypatch.setattr(broadcast_service.settings, "broadcast_pacing_seconds", 0)


@pytest.fixture(autouse=True)
def _clean_registries():
    """Isolate the module-level drain registries between tests."""
    broadcast_service._ACTIVE_DRAINS.clear()
    broadcast_service._DRAIN_TASKS.clear()
    yield
    broadcast_service._ACTIVE_DRAINS.clear()
    broadcast_service._DRAIN_TASKS.clear()


async def _seed(session_factory, recipients, *, subject="Hi", body="Hi {first_name},"):
    """Create an Org, one User per recipient spec, a SENDING broadcast, and a
    PENDING recipient row per user.

    ``recipients`` is a list of dicts with keys: ``first_name`` (str|None),
    ``is_active`` (bool), ``email_verified`` (bool), optional ``attempts``.
    Returns ``(broadcast_id, [user_ids], [recipient_ids])``.
    """
    async with session_factory() as db:
        org = Organization(name="TestOrg", billing_cycle_day=1)
        db.add(org)
        await db.flush()

        users = []
        for i, spec in enumerate(recipients):
            user = User(
                username=f"user{i}",
                email=f"user{i}@x.io",
                first_name=spec.get("first_name", f"User{i}"),
                password_hash=hash_password("pw-1234567"),
                org_id=org.id,
                role=Role.MEMBER if i else Role.OWNER,
                is_active=spec.get("is_active", True),
                email_verified=spec.get("email_verified", True),
            )
            users.append(user)
        db.add_all(users)
        await db.flush()

        broadcast = EmailBroadcast(
            subject=subject,
            body_template=body,
            segment=SEGMENT_ACTIVE_VERIFIED,
            status=BroadcastStatus.SENDING,
        )
        db.add(broadcast)
        await db.flush()

        recipient_rows = []
        for user, spec in zip(users, recipients):
            r = EmailBroadcastRecipient(
                broadcast_id=broadcast.id,
                user_id=user.id,
                email=user.email,
                first_name=user.first_name,
                status=RecipientStatus.PENDING,
                attempts=spec.get("attempts", 0),
            )
            recipient_rows.append(r)
        db.add_all(recipient_rows)
        broadcast.total_recipients = len(recipient_rows)
        await db.commit()

        return (
            broadcast.id,
            [u.id for u in users],
            [r.id for r in recipient_rows],
        )


async def _get_broadcast(session_factory, broadcast_id):
    async with session_factory() as db:
        return (
            await db.execute(
                select(EmailBroadcast).where(EmailBroadcast.id == broadcast_id)
            )
        ).scalar_one()


async def _recipient_statuses(session_factory, broadcast_id):
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(EmailBroadcastRecipient.email, EmailBroadcastRecipient.status)
                .where(EmailBroadcastRecipient.broadcast_id == broadcast_id)
                .order_by(EmailBroadcastRecipient.id)
            )
        ).all()
    return {email: status for email, status in rows}


@pytest.mark.asyncio
async def test_drain_sends_all_pending(session_factory, monkeypatch):
    monkeypatch.setattr(
        broadcast_service, "send_email", AsyncMock(return_value=True)
    )
    broadcast_id, _users, _recips = await _seed(
        session_factory, [{}, {}, {}]
    )

    await broadcast_service._drain(session_factory, broadcast_id)

    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert all(s == RecipientStatus.SENT for s in statuses.values())
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 3
    assert b.failed_count == 0
    assert b.skipped_count == 0
    assert b.status == BroadcastStatus.COMPLETED
    assert b.completed_at is not None
    assert broadcast_service.send_email.await_count == 3


@pytest.mark.asyncio
async def test_drain_skips_lapsed_user(session_factory, monkeypatch):
    monkeypatch.setattr(
        broadcast_service, "send_email", AsyncMock(return_value=True)
    )
    # user index 1 is deactivated AFTER materialization (row still PENDING).
    broadcast_id, user_ids, _recips = await _seed(
        session_factory, [{}, {}, {}]
    )
    async with session_factory() as db:
        await db.execute(
            update(User).where(User.id == user_ids[1]).values(is_active=False)
        )
        await db.commit()

    await broadcast_service._drain(session_factory, broadcast_id)

    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert statuses["user1@x.io"] == RecipientStatus.SKIPPED
    assert statuses["user0@x.io"] == RecipientStatus.SENT
    assert statuses["user2@x.io"] == RecipientStatus.SENT
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 2
    assert b.skipped_count == 1
    assert b.status == BroadcastStatus.COMPLETED
    # send_email was never called for the skipped user.
    assert broadcast_service.send_email.await_count == 2


@pytest.mark.asyncio
async def test_drain_marks_failed_on_send_false(session_factory, monkeypatch):
    async def _fake_send(to, subject, body_html, body_text=None):
        return to != "user1@x.io"

    monkeypatch.setattr(
        broadcast_service, "send_email", AsyncMock(side_effect=_fake_send)
    )
    broadcast_id, _users, _recips = await _seed(
        session_factory, [{}, {}, {}]
    )

    await broadcast_service._drain(session_factory, broadcast_id)

    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert statuses["user1@x.io"] == RecipientStatus.FAILED
    assert statuses["user0@x.io"] == RecipientStatus.SENT
    assert statuses["user2@x.io"] == RecipientStatus.SENT
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 2
    assert b.failed_count == 1
    # One recipient's failure never halts the batch; drain still completes.
    assert b.status == BroadcastStatus.COMPLETED


@pytest.mark.asyncio
async def test_launch_drain_twice_runs_single_drain(session_factory, monkeypatch):
    monkeypatch.setattr(
        broadcast_service, "send_email", AsyncMock(return_value=True)
    )
    broadcast_id, _users, _recips = await _seed(
        session_factory, [{}, {}, {}, {}]
    )

    broadcast_service.launch_drain(session_factory, broadcast_id)
    # Second launch for the same id must be an idempotent no-op (registry).
    broadcast_service.launch_drain(session_factory, broadcast_id)

    tasks = list(broadcast_service._DRAIN_TASKS)
    assert len(tasks) == 1
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)  # let the done-callback flush

    # Exactly one send per distinct pending recipient (no double-send).
    assert broadcast_service.send_email.await_count == 4
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 4
    assert b.status == BroadcastStatus.COMPLETED
    # Registry cleaned up by the done-callback.
    assert broadcast_id not in broadcast_service._ACTIVE_DRAINS


@pytest.mark.asyncio
async def test_resume_skips_rows_at_attempts_cap(session_factory, monkeypatch):
    monkeypatch.setattr(
        broadcast_service, "send_email", AsyncMock(return_value=True)
    )
    max_attempts = broadcast_service.settings.broadcast_max_attempts
    # user0 pending at the cap (must NOT be retried); user1 pending, retryable.
    broadcast_id, _users, recip_ids = await _seed(
        session_factory,
        [{"attempts": max_attempts}, {"attempts": 0}],
    )

    await broadcast_service.resume_pending(session_factory, broadcast_id)

    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert statuses["user0@x.io"] == RecipientStatus.PENDING  # untouched
    assert statuses["user1@x.io"] == RecipientStatus.SENT
    # Only the retryable row was sent.
    assert broadcast_service.send_email.await_count == 1
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 1
    # A pending (capped) row remains, so the broadcast stays SENDING.
    assert b.status == BroadcastStatus.SENDING


@pytest.mark.asyncio
async def test_drain_html_escapes_first_name(session_factory, monkeypatch):
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(broadcast_service, "send_email", send_mock)
    broadcast_id, _users, _recips = await _seed(
        session_factory, [{"first_name": "<x>&"}]
    )

    await broadcast_service._drain(session_factory, broadcast_id)

    assert send_mock.await_count == 1
    call = send_mock.await_args_list[0]
    # send_email(to, subject, body_html, body_text)
    body_html = call.args[2]
    body_text = call.args[3]
    assert "&lt;x&gt;" in body_html
    assert "<x>" not in body_html
    # The plain-text part keeps the raw name.
    assert "<x>&" in body_text


@pytest.mark.asyncio
async def test_drain_raise_observed_by_done_callback(session_factory, monkeypatch):
    monkeypatch.setattr(
        broadcast_service, "send_email", AsyncMock(return_value=True)
    )
    # Force an unhandled drain error: render happens outside the per-row send
    # try/except, so a raising render_email crashes the whole drain.
    def _boom(*_a, **_k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(broadcast_service, "render_email", _boom)
    fake_logger = Mock()
    monkeypatch.setattr(broadcast_service, "logger", fake_logger)

    broadcast_id, _users, _recips = await _seed(session_factory, [{}])

    broadcast_service.launch_drain(session_factory, broadcast_id)
    task = next(iter(broadcast_service._DRAIN_TASKS))
    with pytest.raises(RuntimeError, match="render exploded"):
        await task
    await asyncio.sleep(0)  # let the done-callback run

    # Done-callback logged the failure and cleaned up the registries.
    fake_logger.error.assert_called_once()
    assert task not in broadcast_service._DRAIN_TASKS
    assert broadcast_id not in broadcast_service._ACTIVE_DRAINS
    # The wrapper flipped the broadcast to FAILED before re-raising.
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.status == BroadcastStatus.FAILED
