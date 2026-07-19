"""Tests for the Mailgun BATCH send-drain engine (2026-07-19 batch revision,
spec ``2026-07-18-admin-email-broadcast-design.md`` R1-R5 / MA1-MA7).

The drain now iterates BATCHES, not rows: each batch is claimed to ``sent``
BEFORE one ``send_batch`` Mailgun call (R2, claim-before-send), and a non-2xx
result reverts exactly that batch ``sent → failed``. ``send_batch`` is mocked
throughout (``app.services.broadcast_service.send_batch``); no real Mailgun.

Covers, with pacing monkeypatched to 0:
- single batch: every ``pending`` → ``sent``, ``send_batch`` called ONCE, its
  ``to_list`` == all recipient emails, ``recipient_variables`` keys == ``to_list``,
  counters + ``status=completed``;
- multi-batch: ``broadcast_batch_size`` = 2 with 5 recipients → ``send_batch``
  called 3 times, pacing awaited BETWEEN batches, all ``sent``;
- failed batch: one batch's ``send_batch`` returns False → those rows ``failed``,
  the rest ``sent``, drain still ``completed``; a later ``resume`` re-batches the
  failed rows (below the attempts cap) and, on success, they become ``sent``;
- lapsed user (Ruling 9): a user flipped inactive post-materialization is
  ``skipped`` and EXCLUDED from ``send_batch``'s ``to_list``;
- concurrency (Ruling 14a): ``launch_drain`` twice for the same id runs ONE
  drain — the distinct addresses across every ``send_batch`` call equal the
  distinct pending recipients (the in-process registry blocks the 2nd launch);
- attempts cap (Ruling 14b): a ``failed`` row at ``broadcast_max_attempts`` is
  not re-batched by ``resume``;
- claim-before-send (R2): rows are already ``sent`` in the DB by the time
  ``send_batch`` is invoked (the mock inspects the DB mid-call);
- drain-raise observed (Ruling 14e): an unhandled drain error is logged by the
  done-callback, the task is removed from ``_DRAIN_TASKS`` and the broadcast
  ``status`` is ``failed``.

Uses an in-memory aiosqlite engine (same fixture pattern as the other broadcast
service tests) so no running MySQL / docker-compose stack is required.
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
    """No real sleeping between batches."""
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
    recipient row per user.

    ``recipients`` is a list of dicts with keys: ``first_name`` (str|None),
    ``is_active`` (bool), ``email_verified`` (bool), optional ``attempts``,
    optional ``status`` (a ``RecipientStatus``, default ``PENDING``).
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
                status=spec.get("status", RecipientStatus.PENDING),
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
async def test_drain_single_batch_sends_all_pending(session_factory, monkeypatch):
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    broadcast_id, _users, _recips = await _seed(session_factory, [{}, {}, {}])

    await broadcast_service._drain(session_factory, broadcast_id)

    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert all(s == RecipientStatus.SENT for s in statuses.values())

    # One Mailgun batch call covering all three recipients.
    assert send_mock.await_count == 1
    call = send_mock.await_args
    to_list = call.args[0]
    recipient_variables = call.args[4]
    assert set(to_list) == {"user0@x.io", "user1@x.io", "user2@x.io"}
    # MA2: the recipient-variables map keys exactly match ``to_list``.
    assert set(recipient_variables.keys()) == set(to_list)
    # Body tokens (not a per-recipient render) are passed through.
    assert "%recipient.first_name_html%" in call.args[2]
    assert "%recipient.first_name_text%" in call.args[3]

    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 3
    assert b.failed_count == 0
    assert b.skipped_count == 0
    assert b.status == BroadcastStatus.COMPLETED
    assert b.completed_at is not None


@pytest.mark.asyncio
async def test_drain_multi_batch_paces_between_batches(session_factory, monkeypatch):
    """``broadcast_batch_size`` = 2 with 5 recipients → 3 batches, ``send_batch``
    called 3 times, and pacing awaited BETWEEN batches (2 sleeps, never after the
    last)."""
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    monkeypatch.setattr(broadcast_service.settings, "broadcast_batch_size", 2)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(broadcast_service.asyncio, "sleep", sleep_mock)

    broadcast_id, _users, _recips = await _seed(
        session_factory, [{}, {}, {}, {}, {}]
    )

    await broadcast_service._drain(session_factory, broadcast_id)

    # Three batches: [0,1] [2,3] [4].
    assert send_mock.await_count == 3
    batch_sizes = [len(c.args[0]) for c in send_mock.await_args_list]
    assert batch_sizes == [2, 2, 1]
    # Pacing awaited strictly BETWEEN batches: 3 batches → 2 sleeps.
    assert sleep_mock.await_count == 2

    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert all(s == RecipientStatus.SENT for s in statuses.values())
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 5
    assert b.status == BroadcastStatus.COMPLETED


@pytest.mark.asyncio
async def test_drain_failed_batch_then_resume(session_factory, monkeypatch):
    """A batch whose ``send_batch`` returns False marks those rows ``failed``
    while the rest are ``sent`` and the drain still completes; a later ``resume``
    re-batches the failed rows (below cap) and, on success, they become
    ``sent``."""
    monkeypatch.setattr(broadcast_service.settings, "broadcast_batch_size", 2)

    async def _fail_first_batch(to_list, *_a, **_k):
        # Batch [user0, user1] fails; batch [user2] succeeds.
        return "user0@x.io" not in to_list

    monkeypatch.setattr(
        broadcast_service, "send_batch", AsyncMock(side_effect=_fail_first_batch)
    )
    broadcast_id, _users, _recips = await _seed(session_factory, [{}, {}, {}])

    await broadcast_service._drain(session_factory, broadcast_id)

    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert statuses["user0@x.io"] == RecipientStatus.FAILED
    assert statuses["user1@x.io"] == RecipientStatus.FAILED
    assert statuses["user2@x.io"] == RecipientStatus.SENT
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 1
    assert b.failed_count == 2
    # A failed batch never halts the drain; no pending rows remain → completed.
    assert b.status == BroadcastStatus.COMPLETED

    # Resume: the two failed rows (attempts=1, below cap) are re-batched and now
    # succeed.
    resume_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(broadcast_service, "send_batch", resume_mock)

    await broadcast_service.resume_pending(session_factory, broadcast_id)

    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert all(s == RecipientStatus.SENT for s in statuses.values())
    # Only the failed pair was re-batched (user2 already sent, not re-listed).
    assert resume_mock.await_count == 1
    assert set(resume_mock.await_args.args[0]) == {"user0@x.io", "user1@x.io"}
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 3
    assert b.failed_count == 0
    assert b.status == BroadcastStatus.COMPLETED


@pytest.mark.asyncio
async def test_drain_skips_lapsed_user_excluded_from_batch(session_factory, monkeypatch):
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    # user index 1 is deactivated AFTER materialization (row still PENDING).
    broadcast_id, user_ids, _recips = await _seed(session_factory, [{}, {}, {}])
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

    # The lapsed user is EXCLUDED from the batch's ``to_list`` and vars map.
    assert send_mock.await_count == 1
    to_list = send_mock.await_args.args[0]
    recipient_variables = send_mock.await_args.args[4]
    assert "user1@x.io" not in to_list
    assert set(to_list) == {"user0@x.io", "user2@x.io"}
    assert set(recipient_variables.keys()) == set(to_list)

    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 2
    assert b.skipped_count == 1
    assert b.status == BroadcastStatus.COMPLETED


@pytest.mark.asyncio
async def test_launch_drain_twice_runs_single_drain(session_factory, monkeypatch):
    """``launch_drain`` twice for the same id → one drain; the distinct addresses
    across every ``send_batch`` call equal the distinct pending recipients (the
    registry blocks the 2nd launch, so no address is sent twice)."""
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
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

    # Every distinct address appears exactly once across all batch calls.
    all_addresses = [addr for c in send_mock.await_args_list for addr in c.args[0]]
    assert sorted(all_addresses) == [
        "user0@x.io",
        "user1@x.io",
        "user2@x.io",
        "user3@x.io",
    ]
    assert len(all_addresses) == len(set(all_addresses))  # no double-send

    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 4
    assert b.status == BroadcastStatus.COMPLETED
    # Registry cleaned up by the done-callback.
    assert broadcast_id not in broadcast_service._ACTIVE_DRAINS


@pytest.mark.asyncio
async def test_resume_does_not_rebatch_failed_at_cap(session_factory, monkeypatch):
    """A ``failed`` row already at ``broadcast_max_attempts`` is not re-batched
    by ``resume``."""
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    max_attempts = broadcast_service.settings.broadcast_max_attempts
    # user0 FAILED at the cap (must NOT be re-batched); user1 FAILED below cap.
    broadcast_id, _users, _recips = await _seed(
        session_factory,
        [
            {"status": RecipientStatus.FAILED, "attempts": max_attempts},
            {"status": RecipientStatus.FAILED, "attempts": 1},
        ],
    )

    await broadcast_service.resume_pending(session_factory, broadcast_id)

    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert statuses["user0@x.io"] == RecipientStatus.FAILED  # capped, untouched
    assert statuses["user1@x.io"] == RecipientStatus.SENT
    # Only the below-cap row was batched.
    assert send_mock.await_count == 1
    assert set(send_mock.await_args.args[0]) == {"user1@x.io"}
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 1
    assert b.failed_count == 1
    # No pending rows remain (capped row is FAILED), so the broadcast is done.
    assert b.status == BroadcastStatus.COMPLETED


@pytest.mark.asyncio
async def test_resume_leaves_pending_at_cap_and_stays_sending(session_factory, monkeypatch):
    """A ``pending`` row already at the cap is not re-batched and keeps the
    broadcast in ``sending`` (there is still an un-sent recipient)."""
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    max_attempts = broadcast_service.settings.broadcast_max_attempts
    broadcast_id, _users, _recips = await _seed(
        session_factory,
        [{"attempts": max_attempts}, {"attempts": 0}],
    )

    await broadcast_service.resume_pending(session_factory, broadcast_id)

    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert statuses["user0@x.io"] == RecipientStatus.PENDING  # capped, untouched
    assert statuses["user1@x.io"] == RecipientStatus.SENT
    assert send_mock.await_count == 1
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 1
    # A pending (capped) row remains, so the broadcast stays SENDING.
    assert b.status == BroadcastStatus.SENDING


@pytest.mark.asyncio
async def test_claim_before_send(session_factory, monkeypatch):
    """R2: the whole survivor set is claimed ``sent`` and COMMITTED before the
    Mailgun call — so a poller (here, the ``send_batch`` mock itself) opening a
    fresh session mid-call already sees the rows as ``sent``."""
    broadcast_id, _users, _recips = await _seed(session_factory, [{}, {}, {}])

    observed: list[RecipientStatus] = []

    async def _inspect(to_list, *_a, **_k):
        async with session_factory() as poll_db:
            rows = (
                await poll_db.execute(
                    select(EmailBroadcastRecipient.status).where(
                        EmailBroadcastRecipient.broadcast_id == broadcast_id
                    )
                )
            ).scalars().all()
        observed.extend(rows)
        return True

    monkeypatch.setattr(
        broadcast_service, "send_batch", AsyncMock(side_effect=_inspect)
    )

    await broadcast_service._drain(session_factory, broadcast_id)

    # Every row was already ``sent`` at the instant ``send_batch`` was invoked.
    assert observed == [RecipientStatus.SENT] * 3
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.sent_count == 3
    assert b.status == BroadcastStatus.COMPLETED


@pytest.mark.parametrize(
    "subject",
    [
        pytest.param("Enjoy 50% off this week", id="stray_percent"),
        pytest.param("Hi %recipient.bogus%", id="unknown_recipient_token"),
    ],
)
@pytest.mark.asyncio
async def test_drain_rejects_hazardous_subject_before_sending(
    session_factory, monkeypatch, subject
):
    """MA1 covers the SUBJECT too, not just the bodies.

    Mailgun substitutes recipient-variables across the whole payload, subject
    included, so a stray ``%`` or an unpopulated ``%recipient.X%`` there is
    the same hazard it is in the body. The drain must refuse before any
    Mailgun call happens.
    """
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    broadcast_id, _users, _recips = await _seed(
        session_factory, [{}, {}], subject=subject
    )

    with pytest.raises(ValueError):
        await broadcast_service._drain(session_factory, broadcast_id)

    assert send_mock.await_count == 0
    # Nothing was claimed: every row is still pending and retryable.
    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert all(s == RecipientStatus.PENDING for s in statuses.values())
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.status == BroadcastStatus.FAILED


@pytest.mark.asyncio
async def test_drain_skips_batch_when_claim_rowcount_short(
    session_factory, monkeypatch
):
    """R2 safety net: if the claim UPDATE does not cover every survivor, the
    batch is rolled back and SKIPPED rather than sent.

    Simulated by advancing one row's status out of the expected set between
    the SELECT and the claim — the claim's ``status IN (expected)`` predicate
    then matches one row fewer than we are about to hand Mailgun, which is
    exactly the 'send to someone we did not claim' hazard.
    """
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    fake_logger = Mock()
    monkeypatch.setattr(broadcast_service, "logger", fake_logger)

    broadcast_id, _users, recipient_ids = await _seed(session_factory, [{}, {}])

    # Slip a status change in between the SELECT and the claim UPDATE by
    # hooking the segment re-check, which runs in that exact window.
    real_check = broadcast_service._user_still_targetable
    advanced = False

    async def _advance_then_check(db, user_id):
        nonlocal advanced
        result = await real_check(db, user_id)
        if not advanced:
            advanced = True
            async with session_factory() as other:
                await other.execute(
                    update(EmailBroadcastRecipient)
                    .where(EmailBroadcastRecipient.id == recipient_ids[0])
                    .values(status=RecipientStatus.SENT)
                )
                await other.commit()
        return result

    monkeypatch.setattr(
        broadcast_service, "_user_still_targetable", _advance_then_check
    )

    await broadcast_service._drain(session_factory, broadcast_id)

    # The short claim means the batch is never handed to Mailgun.
    assert send_mock.await_count == 0
    logged = [c for c in fake_logger.error.call_args_list
              if c.args and c.args[0] == "broadcast_batch_claim_mismatch"]
    assert len(logged) == 1
    assert logged[0].kwargs["expected"] == 2
    assert logged[0].kwargs["actual"] == 1
    # The un-advanced row stayed pending (claim rolled back), so a resume can
    # still pick it up.
    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert statuses["user1@x.io"] == RecipientStatus.PENDING


@pytest.mark.asyncio
async def test_drain_raise_observed_by_done_callback(session_factory, monkeypatch):
    monkeypatch.setattr(
        broadcast_service, "send_batch", AsyncMock(return_value=True)
    )
    # Force an unhandled drain error: body tokenization happens once, up front,
    # outside any per-batch guard, so a raising build_batch_bodies crashes the
    # whole drain and the wrapper flips status=FAILED + re-raises.
    def _boom(*_a, **_k):
        raise RuntimeError("tokenize exploded")

    monkeypatch.setattr(broadcast_service, "build_batch_bodies", _boom)
    fake_logger = Mock()
    monkeypatch.setattr(broadcast_service, "logger", fake_logger)

    broadcast_id, _users, _recips = await _seed(session_factory, [{}])

    broadcast_service.launch_drain(session_factory, broadcast_id)
    task = next(iter(broadcast_service._DRAIN_TASKS))
    with pytest.raises(RuntimeError, match="tokenize exploded"):
        await task
    await asyncio.sleep(0)  # let the done-callback run

    # Done-callback logged the failure and cleaned up the registries.
    fake_logger.error.assert_called_once()
    assert task not in broadcast_service._DRAIN_TASKS
    assert broadcast_id not in broadcast_service._ACTIVE_DRAINS
    # The wrapper flipped the broadcast to FAILED before re-raising.
    b = await _get_broadcast(session_factory, broadcast_id)
    assert b.status == BroadcastStatus.FAILED
