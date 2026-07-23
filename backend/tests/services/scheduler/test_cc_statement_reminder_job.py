"""Tests for ``CcStatementReminderJob`` (Task 8, CC Statement Alerts V1).

The reminder job fans out per credit-card account within one org: each
card has its own cycle, so ``is_due``/``run`` must evaluate every active
CC account rather than the org as a whole. Coverage:

  - ``is_due`` True exactly at the lead-day boundary, False one day past
    it, and False once the current cycle's close date already has a
    dedup marker.
  - ``run`` dispatches in-app only (``send_email=False``,
    ``category=CC_STATEMENT``), writes the dedup marker via
    ``record_cc_alert``, and commits.
  - A second ``run`` for the same cycle is a no-op (``JobResult.noop()``)
    because the marker from the first run already covers it.
  - A card whose dispatch raises mid-run gets rolled back and audited as
    a failure, but the OTHER due card on the same org still gets its
    marker + notification (shared-session isolation).

Real ``active_cc_accounts`` / ``resolve_cycle_for_account`` /
``cc_alerts_sent_since`` run against an in-memory SQLite session (same
idiom as ``test_cc_statement_common.py``); ``record_cc_alert`` writes
via its own session, so ``app.services.scheduler.audit.async_session``
is monkeypatched to the test factory (same idiom as
``test_cc_alert_dedup.py``). Only the notification dispatch itself is
faked, to assert call kwargs and to inject a mid-run failure.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.audit_event import AuditEvent
from app.models.notification import NotificationCategory
from app.models.user import Organization
from app.services.scheduler import audit as sched_audit
from app.services.scheduler.jobs import cc_statement_reminder as job_mod
from app.services.scheduler.jobs.cc_statement_reminder import CcStatementReminderJob

TODAY = datetime.date(2026, 7, 23)
LONG_AGO = datetime.datetime(2020, 1, 1)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session_factory(monkeypatch):
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
    # record_cc_alert / record_run open their OWN session via
    # app.database.async_session; point that at the test engine.
    monkeypatch.setattr(sched_audit, "async_session", factory)
    yield factory
    await engine.dispose()


async def _seed_org(session_factory) -> Organization:
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        await db.refresh(org)
        return org


async def _seed_cc_account(
    session_factory, org: Organization, *, name: str, close_day: int, is_active: bool = True
) -> Account:
    async with session_factory() as db:
        cc_type = (
            await db.execute(
                select(AccountType).where(
                    AccountType.org_id == org.id, AccountType.slug == "credit_card"
                )
            )
        ).scalar_one_or_none()
        if cc_type is None:
            cc_type = AccountType(
                org_id=org.id, name="Credit Card", slug="credit_card", is_system=True
            )
            db.add(cc_type)
            await db.flush()
        account = Account(
            org_id=org.id,
            name=name,
            account_type_id=cc_type.id,
            balance=Decimal("0.00"),
            currency="EUR",
            is_active=is_active,
            close_day=close_day,
            created_at=LONG_AGO,
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)
        return account


@pytest.fixture
def dispatch_calls(monkeypatch):
    calls: list[dict] = []

    async def _fake_dispatch(db, **kwargs):
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(job_mod, "dispatch_notification_to_org_members", _fake_dispatch)
    return calls


async def _sent_markers(session_factory, org_id: int) -> list[AuditEvent]:
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == sched_audit.CC_REMINDER_EVENT_TYPE,
                    AuditEvent.target_org_id == org_id,
                )
            )
        ).scalars().all()
        return list(rows)


async def _failure_markers(session_factory, org_id: int) -> list[AuditEvent]:
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "scheduler.cc_statement_reminder.failure",
                    AuditEvent.target_org_id == org_id,
                )
            )
        ).scalars().all()
        return list(rows)


# ── is_due ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_due_true_exactly_at_lead_days(session_factory):
    org = await _seed_org(session_factory)
    # close_day=25, today=23 -> days_until=2, default lead=2 -> due.
    await _seed_cc_account(session_factory, org, name="Amex Gold", close_day=25)
    job = CcStatementReminderJob()
    async with session_factory() as db:
        assert await job.is_due(db, org, TODAY) is True


@pytest.mark.asyncio
async def test_is_due_false_at_lead_plus_one(session_factory):
    org = await _seed_org(session_factory)
    # close_day=25, today=22 -> days_until=3, default lead=2 -> not due.
    await _seed_cc_account(session_factory, org, name="Amex Gold", close_day=25)
    job = CcStatementReminderJob()
    async with session_factory() as db:
        assert await job.is_due(db, org, datetime.date(2026, 7, 22)) is False


@pytest.mark.asyncio
async def test_is_due_false_when_already_sent(session_factory):
    org = await _seed_org(session_factory)
    account = await _seed_cc_account(session_factory, org, name="Amex Gold", close_day=25)

    # Pre-seed the dedup marker for this exact (account, close_date) pair.
    await sched_audit.record_cc_alert(
        org=org,
        account_id=account.id,
        close_date=datetime.date(2026, 7, 25),
        event_type=sched_audit.CC_REMINDER_EVENT_TYPE,
        detail={"days_until": 2},
    )

    job = CcStatementReminderJob()
    async with session_factory() as db:
        assert await job.is_due(db, org, TODAY) is False


@pytest.mark.asyncio
async def test_is_due_false_when_no_cc_accounts(session_factory):
    org = await _seed_org(session_factory)
    job = CcStatementReminderJob()
    async with session_factory() as db:
        assert await job.is_due(db, org, TODAY) is False


# ── run: happy path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_dispatches_in_app_only_and_writes_marker(session_factory, dispatch_calls):
    org = await _seed_org(session_factory)
    account = await _seed_cc_account(session_factory, org, name="Amex Gold", close_day=25)

    job = CcStatementReminderJob()
    async with session_factory() as db:
        result = await job.run(db, org, TODAY)

    assert result.outcome == "success"
    assert result.counts["dispatched_account_ids"] == [account.id]

    assert len(dispatch_calls) == 1
    call = dispatch_calls[0]
    assert call["send_email"] is False
    assert call["category"] == NotificationCategory.CC_STATEMENT
    assert call["org_id"] == org.id
    assert call["link_url"] == f"/accounts?edit={account.id}"

    markers = await _sent_markers(session_factory, org.id)
    assert len(markers) == 1
    assert markers[0].detail["account_id"] == account.id
    assert markers[0].detail["close_date"] == "2026-07-25"


@pytest.mark.asyncio
async def test_second_run_same_cycle_is_noop(session_factory, dispatch_calls):
    org = await _seed_org(session_factory)
    await _seed_cc_account(session_factory, org, name="Amex Gold", close_day=25)

    job = CcStatementReminderJob()
    async with session_factory() as db:
        first = await job.run(db, org, TODAY)
    assert first.outcome == "success"
    assert len(dispatch_calls) == 1

    async with session_factory() as db:
        second = await job.run(db, org, TODAY)
    assert second.outcome == "noop"
    assert len(dispatch_calls) == 1  # no additional dispatch


@pytest.mark.asyncio
async def test_run_returns_noop_when_nothing_due(session_factory, dispatch_calls):
    org = await _seed_org(session_factory)
    # close_day=25, today=22 -> 3 days out, outside default lead=2.
    await _seed_cc_account(session_factory, org, name="Amex Gold", close_day=25)

    job = CcStatementReminderJob()
    async with session_factory() as db:
        result = await job.run(db, org, datetime.date(2026, 7, 22))

    assert result.outcome == "noop"
    assert dispatch_calls == []


# ── run: partial failure isolation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_card_failure_does_not_block_the_other(session_factory, monkeypatch):
    org = await _seed_org(session_factory)
    bad = await _seed_cc_account(session_factory, org, name="Bad Card", close_day=25)
    good = await _seed_cc_account(session_factory, org, name="Good Card", close_day=25)

    calls: list[dict] = []

    async def _flaky_dispatch(db, **kwargs):
        if kwargs["title"].startswith("Bad Card"):
            raise RuntimeError("boom")
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(job_mod, "dispatch_notification_to_org_members", _flaky_dispatch)

    job = CcStatementReminderJob()
    async with session_factory() as db:
        result = await job.run(db, org, TODAY)
        # The shared session must still be usable after the mid-loop
        # rollback -- proves run() rolled back before continuing rather
        # than leaving db in "rollback-required" state.
        await db.execute(select(Account).where(Account.id == good.id))

    assert result.outcome == "success"
    assert result.counts["dispatched_account_ids"] == [good.id]
    assert len(calls) == 1

    failures = await _failure_markers(session_factory, org.id)
    assert len(failures) == 1
    assert failures[0].detail["account_id"] == bad.id
    assert "boom" in failures[0].detail["error"]
