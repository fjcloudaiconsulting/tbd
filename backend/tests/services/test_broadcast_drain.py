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
- rejected batch: one batch is CONCLUSIVELY refused → those rows ``failed``,
  the rest ``sent``, drain still ``completed``; a later ``resume`` re-batches the
  failed rows (below the attempts cap) and, on success, they become ``sent``;
- indeterminate batch (TBD-330): an UNANSWERED send leaves its rows ``sent``
  with the reason in ``error``, and ``resume`` will not re-send them; the
  resume predicate additionally refuses any row a Mailgun webhook has already
  reported on (``delivery_status IS NOT NULL``), in the SELECT *and* in the
  claim UPDATE;
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
from app.services.email_service import BatchSendResult, SendDisposition

# ``send_batch`` returns a TYPED result, not a bool (TBD-330). Every mock
# here hands back one of these three. A stale mock still returning a bare
# ``True`` raises ``AttributeError`` on ``.disposition`` rather than silently
# reading as a success — that loudness is the whole point of the typed shape.
ACCEPTED = BatchSendResult(SendDisposition.ACCEPTED)
REJECTED = BatchSendResult(SendDisposition.REJECTED, "mailgun refused it")
INDETERMINATE = BatchSendResult(
    SendDisposition.INDETERMINATE, "no answer from Mailgun within the deadline"
)


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
    optional ``status`` (a ``RecipientStatus``, default ``PENDING``),
    optional ``delivery_status`` (str|None, default None — the state of a
    row no Mailgun webhook has reported on yet), optional ``error`` (str|None,
    default None — a verdict left behind by a PREVIOUS drain attempt).
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
                delivery_status=spec.get("delivery_status"),
                error=spec.get("error"),
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


async def _recipient_rows(session_factory, broadcast_id):
    """``{email: EmailBroadcastRecipient}`` — for fences that need to read
    ``error`` / ``attempts`` / ``delivery_status``, not just ``status``."""
    async with session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(EmailBroadcastRecipient)
                    .where(EmailBroadcastRecipient.broadcast_id == broadcast_id)
                    .order_by(EmailBroadcastRecipient.id)
                )
            )
            .scalars()
            .all()
        )
    return {r.email: r for r in rows}


@pytest.mark.asyncio
async def test_drain_single_batch_sends_all_pending(session_factory, monkeypatch):
    send_mock = AsyncMock(return_value=ACCEPTED)
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
    send_mock = AsyncMock(return_value=ACCEPTED)
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
    """A batch Mailgun CONCLUSIVELY REJECTED marks those rows ``failed``
    while the rest are ``sent`` and the drain still completes; a later ``resume``
    re-batches the failed rows (below cap) and, on success, they become
    ``sent``.

    TBD-330 narrowed this test's premise. It used to say "returns False",
    which covered both a conclusive refusal and an unanswered send; only the
    first licenses ``failed``. The unanswered case is fenced separately by
    ``test_indeterminate_batch_stays_sent_and_resume_leaves_it_alone``.
    """
    monkeypatch.setattr(broadcast_service.settings, "broadcast_batch_size", 2)

    async def _fail_first_batch(to_list, *_a, **_k):
        # Batch [user0, user1] is refused; batch [user2] succeeds.
        return ACCEPTED if "user0@x.io" not in to_list else REJECTED

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
    resume_mock = AsyncMock(return_value=ACCEPTED)
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
    send_mock = AsyncMock(return_value=ACCEPTED)
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
    send_mock = AsyncMock(return_value=ACCEPTED)
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
    by ``resume``.

    G2 — REGRESSION GUARD, not a fence. ``delivery_status`` is seeded
    explicitly as ``None`` so the attempts cap is what is under test here:
    TBD-330 added a second suppressing term to the same predicate, and a
    reader must be able to see at a glance that this row is being held back
    by the cap and not by that term.
    """
    send_mock = AsyncMock(return_value=ACCEPTED)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    max_attempts = broadcast_service.settings.broadcast_max_attempts
    # user0 FAILED at the cap (must NOT be re-batched); user1 FAILED below cap.
    broadcast_id, _users, _recips = await _seed(
        session_factory,
        [
            {
                "status": RecipientStatus.FAILED,
                "attempts": max_attempts,
                "delivery_status": None,
            },
            {
                "status": RecipientStatus.FAILED,
                "attempts": 1,
                "delivery_status": None,
            },
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
    broadcast in ``sending`` (there is still an un-sent recipient).

    G2 — REGRESSION GUARD, not a fence. See the sibling above for why
    ``delivery_status`` is seeded explicitly.
    """
    send_mock = AsyncMock(return_value=ACCEPTED)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    max_attempts = broadcast_service.settings.broadcast_max_attempts
    broadcast_id, _users, _recips = await _seed(
        session_factory,
        [
            {"attempts": max_attempts, "delivery_status": None},
            {"attempts": 0, "delivery_status": None},
        ],
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
        return ACCEPTED

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
    send_mock = AsyncMock(return_value=ACCEPTED)
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
    send_mock = AsyncMock(return_value=ACCEPTED)
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
        broadcast_service, "send_batch", AsyncMock(return_value=ACCEPTED)
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


# ─── TBD-330: an UNANSWERED batch is not a failed batch ─────────────────
#
# ``failed`` is an assertion that Mailgun did not accept the message. The
# drain used to write it for every falsy ``send_batch`` return, including
# the 20s aggregate deadline — a state informationally identical to a crash
# mid-call, which R2 already rules is NOT retried by ``resume``. The result
# was: 1000 rows reverted to ``failed`` with a permanently-NULL
# ``delivery_status``, an operator clicking Resume, and 1000 duplicates.
#
# F2 and F3 are a PAIR and must be read together. F2 alone is satisfied by
# an implementation that never reverts anything; F3 is what kills that.


@pytest.mark.asyncio
async def test_indeterminate_batch_stays_sent_and_resume_leaves_it_alone(
    session_factory, monkeypatch
):
    """F2. An INDETERMINATE batch leaves its rows ``sent``, records the
    specific reason in ``error``, and a later resume does not touch them."""
    fake_logger = Mock()
    monkeypatch.setattr(broadcast_service, "logger", fake_logger)
    monkeypatch.setattr(
        broadcast_service, "send_batch", AsyncMock(return_value=INDETERMINATE)
    )
    broadcast_id, _users, _recips = await _seed(session_factory, [{}, {}, {}])

    await broadcast_service._drain(session_factory, broadcast_id)

    rows = await _recipient_rows(session_factory, broadcast_id)
    assert [r.status for r in rows.values()] == [RecipientStatus.SENT] * 3
    # Claimed exactly once. Not reverted, so not re-claimable either.
    assert [r.attempts for r in rows.values()] == [1, 1, 1]
    # The typed reason reaches a queryable column, not just structlog. This
    # is the assertion an implementation that merely "does not revert" fails:
    # leaving the rows alone is necessary but not sufficient, the operator
    # still has to be able to find out WHICH batch is unresolved.
    for r in rows.values():
        assert r.error == INDETERMINATE.reason

    # F8: emitted at ERROR, carrying broadcast_id + count, and NO addresses.
    # ``info`` would sort it with ``broadcast_batch_sent`` — an unresolved
    # delivery question must not look like a success.
    events = [
        c
        for c in fake_logger.error.call_args_list
        if c.args and c.args[0] == "broadcast_batch_indeterminate"
    ]
    assert len(events) == 1
    assert events[0].kwargs["broadcast_id"] == broadcast_id
    assert events[0].kwargs["count"] == 3
    # MA5: counts, never addresses.
    for addr in ("user0@x.io", "user1@x.io", "user2@x.io"):
        assert addr not in repr(events[0].kwargs)
    assert [
        c
        for c in fake_logger.info.call_args_list
        if c.args and c.args[0] == "broadcast_batch_indeterminate"
    ] == []

    # And the headline: a Resume click cannot re-send them.
    resume_mock = AsyncMock(return_value=ACCEPTED)
    monkeypatch.setattr(broadcast_service, "send_batch", resume_mock)

    await broadcast_service.resume_pending(session_factory, broadcast_id)

    assert resume_mock.await_count == 0
    rows = await _recipient_rows(session_factory, broadcast_id)
    assert [r.status for r in rows.values()] == [RecipientStatus.SENT] * 3
    assert [r.attempts for r in rows.values()] == [1, 1, 1]


@pytest.mark.asyncio
async def test_rejected_batch_still_reverts_and_still_resumes(
    session_factory, monkeypatch
):
    """F3 — MANDATORY CONTROL, not a coverage claim.

    Its job is to kill the implementation that passes F2 by never reverting
    anything at all. A CONCLUSIVE rejection (Mailgun parsed the batch and
    refused it, or MA2 refused to issue the request) still has to revert
    ``sent → failed`` and still has to be re-sendable by a resume — that is
    the behaviour TBD-330 must preserve, not remove.

    Note this control CANNOT be green against unmodified ``main`` as the
    spec's fence table assumed: once ``send_batch`` returns a typed result,
    ``main``'s drain reads the object as truthy and never reverts. The
    control's value is unchanged — it constrains this branch's
    implementation from the opposite side of F2 — but it is a fence on the
    new contract, not a replay of the old one.
    """
    monkeypatch.setattr(
        broadcast_service, "send_batch", AsyncMock(return_value=REJECTED)
    )
    broadcast_id, _users, _recips = await _seed(session_factory, [{}, {}, {}])

    await broadcast_service._drain(session_factory, broadcast_id)

    rows = await _recipient_rows(session_factory, broadcast_id)
    assert [r.status for r in rows.values()] == [RecipientStatus.FAILED] * 3
    # The typed reason replaces the old generic "send_batch returned a
    # falsy result", which said nothing about WHY.
    for r in rows.values():
        assert r.error == REJECTED.reason

    resume_mock = AsyncMock(return_value=ACCEPTED)
    monkeypatch.setattr(broadcast_service, "send_batch", resume_mock)

    await broadcast_service.resume_pending(session_factory, broadcast_id)

    assert resume_mock.await_count == 1
    assert set(resume_mock.await_args.args[0]) == {
        "user0@x.io",
        "user1@x.io",
        "user2@x.io",
    }
    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert all(s == RecipientStatus.SENT for s in statuses.values())


# ─── TBD-330: ``error`` carries the CURRENT attempt's verdict, never a
#     stale one ────────────────────────────────────────────────────────
#
# The whole tri-state design leans on ``error``: an INDETERMINATE batch
# changes no status, no counter and no UI element, so the ONLY way an
# operator learns a batch is unresolved is the query
# ``status='sent' AND error IS NOT NULL`` (that is what the comment on
# ``RecipientResponse.error`` promises).
#
# Which makes a stale ``error`` a correctness bug, not cosmetics. The
# ACCEPTED path writes nothing at all, so unless the CLAIM clears the
# column, this sequence is possible:
#
#   REJECTED  → row ``failed`` + "mailgun refused it"
#   operator fixes the cause, clicks Resume
#   re-claimed → ``sent``
#   ACCEPTED  → nothing written
#   ⇒ final row: ``status='sent'``, ``error='mailgun refused it'``
#
# which is BYTE-IDENTICAL to a genuinely indeterminate row and puts a
# false positive into the one query that is supposed to answer the
# question. The pair below pins both directions: the stale verdict must be
# gone, and the CURRENT attempt's verdict must survive.


@pytest.mark.asyncio
async def test_reclaim_clears_the_previous_attempts_error(
    session_factory, monkeypatch
):
    """A rejection reason must not outlive the attempt that produced it.

    Direction 1 of the pair. Seeds the post-REJECTED state directly (three
    ``failed`` rows each carrying a rejection reason), resumes into an
    ACCEPTED send, and asserts the reason is GONE — including via the
    literal unresolved-population query an operator would run.
    """
    monkeypatch.setattr(
        broadcast_service, "send_batch", AsyncMock(return_value=ACCEPTED)
    )
    stale = "mailgun refused it"
    broadcast_id, _users, _recips = await _seed(
        session_factory,
        [
            {"status": RecipientStatus.FAILED, "attempts": 1, "error": stale},
            {"status": RecipientStatus.FAILED, "attempts": 1, "error": stale},
            {"status": RecipientStatus.FAILED, "attempts": 1, "error": stale},
        ],
    )

    await broadcast_service.resume_pending(session_factory, broadcast_id)

    rows = await _recipient_rows(session_factory, broadcast_id)
    assert [r.status for r in rows.values()] == [RecipientStatus.SENT] * 3
    assert [r.attempts for r in rows.values()] == [2, 2, 2]
    for r in rows.values():
        assert r.error is None, (
            "the previous attempt's rejection reason survived a successful "
            f"re-send: {r.error!r}"
        )

    # The operator's actual query for "which batches are unresolved?". A
    # stale reason makes this return rows that were in fact delivered
    # cleanly, which is the failure mode that matters.
    async with session_factory() as db:
        unresolved = (
            (
                await db.execute(
                    select(EmailBroadcastRecipient.email).where(
                        EmailBroadcastRecipient.broadcast_id == broadcast_id,
                        EmailBroadcastRecipient.status == RecipientStatus.SENT,
                        EmailBroadcastRecipient.error.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert list(unresolved) == []


@pytest.mark.asyncio
async def test_reclaim_does_not_erase_the_reason_the_current_attempt_wrote(
    session_factory, monkeypatch
):
    """MANDATORY CONTROL — direction 2 of the pair.

    Its job is to kill the implementation that satisfies the test above by
    clearing ``error`` unconditionally in the WRONG place (after the send,
    or on every write). The clear belongs at the CLAIM, which commits
    BEFORE the Mailgun call, so it cannot race the reason the same attempt
    is about to write. Same seeded stale reason, but the resumed send comes
    back INDETERMINATE: the stale string must be gone AND the fresh one
    must be present.

    Without this control, ``.values(error=None)`` placed anywhere at all
    passes — including somewhere that silently discards every reason the
    tri-state design exists to record.
    """
    monkeypatch.setattr(
        broadcast_service, "send_batch", AsyncMock(return_value=INDETERMINATE)
    )
    stale = "mailgun refused it"
    broadcast_id, _users, _recips = await _seed(
        session_factory,
        [
            {"status": RecipientStatus.FAILED, "attempts": 1, "error": stale},
            {"status": RecipientStatus.FAILED, "attempts": 1, "error": stale},
        ],
    )

    await broadcast_service.resume_pending(session_factory, broadcast_id)

    rows = await _recipient_rows(session_factory, broadcast_id)
    assert [r.status for r in rows.values()] == [RecipientStatus.SENT] * 2
    for r in rows.values():
        assert r.error == INDETERMINATE.reason
        # Belt and braces: the two strings are genuinely different, so the
        # assertion above cannot be satisfied by the stale value surviving.
        assert r.error != stale


# ─── TBD-330: a row Mailgun has already reported on is never re-sent ────
#
# ``delivery_status`` has exactly one producer, ``mailgun_webhook.map_event``,
# and its value set is closed: delivered / bounced_temporary /
# bounced_permanent / complained, or NULL for "no event recorded". EVERY
# non-NULL value suppresses a re-send — ``delivered`` would duplicate,
# ``bounced_temporary`` would stack a second message on top of Mailgun's own
# internal retry, ``bounced_permanent`` is the fastest way to get a sending
# domain throttled, and ``complained`` is the worst available action. The
# "safe to re-send" set is empty, so the discriminator is NON-NULLNESS, not
# membership in some subset of values.
#
# ⚠ Which is why the term must be ``IS NULL`` and never a value list. A
# ``delivery_status NOT IN ('delivered', ...)`` predicate evaluates to SQL
# NULL for a NULL row, so the row is EXCLUDED and resume sends to nobody —
# a bug that passes any naive "no duplicates" assertion. F4(b) exists for
# exactly that, and asserts the exact ``to_list`` SET rather than a count.


@pytest.mark.asyncio
async def test_resume_sends_only_the_row_no_webhook_has_reported_on(
    session_factory, monkeypatch
):
    """F4. Four ``failed`` rows, one per ``delivery_status`` state, resumed
    in ONE call. The NULL row IS re-sent (the control that keeps the
    predicate from being polarity-flipped); the three reported-on rows are
    not. Asserts the exact address set — the count alone is identical for
    'resumed everybody' and 'resumed the right one'."""
    send_mock = AsyncMock(return_value=ACCEPTED)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    broadcast_id, _users, _recips = await _seed(
        session_factory,
        [
            {"status": RecipientStatus.FAILED, "attempts": 1, "delivery_status": None},
            {
                "status": RecipientStatus.FAILED,
                "attempts": 1,
                "delivery_status": "delivered",
            },
            {
                "status": RecipientStatus.FAILED,
                "attempts": 1,
                "delivery_status": "complained",
            },
            {
                "status": RecipientStatus.FAILED,
                "attempts": 1,
                "delivery_status": "bounced_permanent",
            },
        ],
    )

    await broadcast_service.resume_pending(session_factory, broadcast_id)

    assert send_mock.await_count == 1
    assert set(send_mock.await_args.args[0]) == {"user0@x.io"}

    rows = await _recipient_rows(session_factory, broadcast_id)
    # DoD-4 control: the genuinely-unreported row IS still re-sent.
    assert rows["user0@x.io"].status == RecipientStatus.SENT
    assert rows["user0@x.io"].attempts == 2
    # The reported-on rows are untouched — not re-sent, not re-claimed, and
    # (TBD-330 §4) not "reconciled" to some other status either.
    for email in ("user1@x.io", "user2@x.io", "user3@x.io"):
        assert rows[email].status == RecipientStatus.FAILED
        assert rows[email].attempts == 1


@pytest.mark.asyncio
async def test_claim_refuses_a_row_that_gained_delivery_status_mid_flight(
    session_factory, monkeypatch
):
    """F5 — the half-fix killer.

    The SELECT, the per-row segment re-check loop (one await plus a commit
    per row, up to 1000 round-trips) and the claim UPDATE are separated by
    real time, during which the webhook sink keeps accepting events. A fix
    that adds the term to the SELECT only is internally consistent and
    passes F4 in full, while leaving the door wide open.

    The existing rowcount-mismatch guard does NOT save a SELECT-only fix:
    without the term on the claim, the UPDATE matches every ``survivor_id``
    regardless of ``delivery_status``, so ``claimed == len(survivor_ids)``,
    the guard never fires, and the just-delivered row is mailed.
    """
    send_mock = AsyncMock(return_value=ACCEPTED)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    fake_logger = Mock()
    monkeypatch.setattr(broadcast_service, "logger", fake_logger)

    broadcast_id, _users, recipient_ids = await _seed(
        session_factory,
        [
            {"status": RecipientStatus.FAILED, "attempts": 1},
            {"status": RecipientStatus.FAILED, "attempts": 1},
        ],
    )

    # Land a ``delivered`` webhook in the window between the SELECT and the
    # claim, by hooking the segment re-check which runs in exactly that gap.
    real_check = broadcast_service._user_still_targetable
    landed = False

    async def _land_webhook_then_check(db, user_id):
        nonlocal landed
        result = await real_check(db, user_id)
        if not landed:
            landed = True
            async with session_factory() as other:
                await other.execute(
                    update(EmailBroadcastRecipient)
                    .where(EmailBroadcastRecipient.id == recipient_ids[0])
                    .values(delivery_status="delivered")
                )
                await other.commit()
        return result

    monkeypatch.setattr(
        broadcast_service, "_user_still_targetable", _land_webhook_then_check
    )

    await broadcast_service.resume_pending(session_factory, broadcast_id)

    # Nothing was handed to Mailgun at all: the claim came up one row short,
    # so the whole batch was rolled back and skipped.
    assert send_mock.await_count == 0
    logged = [
        c
        for c in fake_logger.error.call_args_list
        if c.args and c.args[0] == "broadcast_batch_claim_mismatch"
    ]
    assert len(logged) == 1
    assert logged[0].kwargs["expected"] == 2
    assert logged[0].kwargs["actual"] == 1

    rows = await _recipient_rows(session_factory, broadcast_id)
    # The delivered row keeps its outcome and is never re-sent.
    assert rows["user0@x.io"].delivery_status == "delivered"
    assert rows["user0@x.io"].attempts == 1
    # The innocent row was NOT consumed: the claim rolled back, so it is
    # still eligible for the next resume. That is the documented liveness
    # cost of putting the term on the claim, not a lost recipient.
    assert rows["user1@x.io"].status == RecipientStatus.FAILED
    assert rows["user1@x.io"].attempts == 1


@pytest.mark.asyncio
async def test_failed_row_with_a_delivery_status_is_left_exactly_as_it_is(
    session_factory, monkeypatch
):
    """F7. Pins TBD-330 §4 in BOTH directions.

    (a) The ``failed`` + ``delivered`` row must not be re-sent — deleting
        the ``is_(None)`` term reddens this.
    (b) Its ``status`` must ALSO be left alone. The ruling is that no
        reconcile UPDATE ships: ``status`` has exactly one writer (this
        drain) and ``delivery_status`` has exactly one writer (the webhook),
        and a reconcile would add a third writer to ``status`` for a row
        population that is verifiably zero. Adding one reddens this.
    """
    send_mock = AsyncMock(return_value=ACCEPTED)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    broadcast_id, _users, _recips = await _seed(
        session_factory,
        [
            {
                "status": RecipientStatus.FAILED,
                "attempts": 1,
                "delivery_status": "delivered",
            },
            {"status": RecipientStatus.FAILED, "attempts": 1},
        ],
    )

    await broadcast_service.resume_pending(session_factory, broadcast_id)

    # The paired positive: the NULL row WAS sent, so this is not passing
    # because nothing happened at all.
    assert send_mock.await_count == 1
    assert set(send_mock.await_args.args[0]) == {"user1@x.io"}

    rows = await _recipient_rows(session_factory, broadcast_id)
    assert rows["user0@x.io"].status == RecipientStatus.FAILED
    assert rows["user0@x.io"].delivery_status == "delivered"
    assert rows["user0@x.io"].attempts == 1
    assert rows["user1@x.io"].status == RecipientStatus.SENT


@pytest.mark.asyncio
async def test_fresh_drain_still_sends_rows_with_no_delivery_status(
    session_factory, monkeypatch
):
    """G1 — REGRESSION GUARD, **not a fence**. Do not count it as coverage.

    On the fresh-send path the ``delivery_status IS NULL`` term is vacuous
    by construction: a ``pending`` row was never claimed, so no Mailgun
    message exists for a webhook to report on, so the column is always NULL
    there. The term ships on that path as an invariant only. This guard
    exists solely to catch a future edit that inverts the term's polarity
    and silently stops every fresh send.
    """
    send_mock = AsyncMock(return_value=ACCEPTED)
    monkeypatch.setattr(broadcast_service, "send_batch", send_mock)
    broadcast_id, _users, _recips = await _seed(session_factory, [{}, {}])

    await broadcast_service._drain(session_factory, broadcast_id)

    assert set(send_mock.await_args.args[0]) == {"user0@x.io", "user1@x.io"}
    statuses = await _recipient_statuses(session_factory, broadcast_id)
    assert all(s == RecipientStatus.SENT for s in statuses.values())
