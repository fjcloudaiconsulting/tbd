"""Tests for ``CcStatementCloseJob`` (Task 9, CC Statement Alerts V1).

The close job fires once a credit card's statement cycle has actually
closed (``most_recent_closed_cycle`` is not ``None``) and reports the
carried balance -- if any -- via ``statement_outstanding``. Unlike the
reminder job (Task 8, in-app only), this job dual-dispatches: the in-app
body may state the amount due, the email body never does, and a $0
outstanding balance suppresses the email entirely while still writing
the dedup marker (so ``is_due`` doesn't re-evaluate the same closed
cycle every tick). Coverage:

  - ``is_due`` True once a cycle has closed and is unsent; False when
    ``most_recent_closed_cycle`` returns ``None`` (backfill guard) or the
    close date already has a dedup marker.
  - ``run`` computes ``owed`` via the real ``statement_outstanding``
    ledger reconstruction, builds an in-app body with the amount + due
    date and an email body that omits the amount, and dispatches with
    ``send_email=True``.
  - $0 outstanding -> ``send_email=False``, in-app body says "nothing
    due", and the dedup marker is STILL written.
  - A second ``run`` for the same closed cycle is a no-op.
  - A card whose dispatch raises mid-run gets rolled back and audited as
    a failure, but the other due card on the same org still gets its
    marker + notification (shared-session isolation, same idiom as
    Task 8's test).

Real ``active_cc_accounts`` / ``most_recent_closed_cycle`` /
``statement_outstanding`` / ``cc_alerts_sent_since`` run against an
in-memory SQLite session (same idiom as ``test_cc_statement_amount.py``
and ``test_cc_statement_reminder_job.py``); ``record_cc_alert`` writes
via its own session, so ``app.services.scheduler.audit.async_session``
is monkeypatched to the test factory. Only the notification dispatch
itself is faked, to assert call kwargs and to inject a mid-run failure.
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
from app.models.category import Category, CategoryType
from app.models.notification import NotificationCategory
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.services.scheduler import audit as sched_audit
from app.services.scheduler.jobs import cc_statement_close as job_mod
from app.services.scheduler.jobs.cc_statement_close import CcStatementCloseJob

TODAY = datetime.date(2026, 7, 23)
LONG_AGO = datetime.datetime(2020, 1, 1)
CLOSE_DAY = 20
CLOSE_DATE = datetime.date(2026, 7, 20)  # most-recent CLOSED cycle as of TODAY
PAYMENT_DATE = datetime.date(2026, 8, 1)  # default payment_day=1, +1 month


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
    session_factory,
    org: Organization,
    *,
    name: str,
    close_day: int = CLOSE_DAY,
    is_active: bool = True,
    opening_balance: Decimal = Decimal("0.00"),
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
            opening_balance=opening_balance,
            created_at=LONG_AGO,
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)
        return account


async def _charge(session_factory, org: Organization, account: Account, *, amount: str, on: datetime.date) -> None:
    async with session_factory() as db:
        cat = (
            await db.execute(
                select(Category).where(Category.org_id == org.id, Category.slug == "groceries")
            )
        ).scalar_one_or_none()
        if cat is None:
            cat = Category(org_id=org.id, name="Groceries", slug="groceries", type=CategoryType.EXPENSE)
            db.add(cat)
            await db.flush()
        db.add(
            Transaction(
                org_id=org.id,
                account_id=account.id,
                category_id=cat.id,
                amount=Decimal(amount),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED,
                date=on,
                settled_date=on,
                description="x",
                is_imported=False,
                is_manual_adjustment=False,
            )
        )
        await db.commit()


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
                    AuditEvent.event_type == sched_audit.CC_CLOSED_EVENT_TYPE,
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
                    AuditEvent.event_type == "scheduler.cc_statement_closed.failure",
                    AuditEvent.target_org_id == org_id,
                )
            )
        ).scalars().all()
        return list(rows)


# ── is_due ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_due_true_once_cycle_closed_and_unsent(session_factory):
    org = await _seed_org(session_factory)
    await _seed_cc_account(session_factory, org, name="Amex Gold")
    job = CcStatementCloseJob()
    async with session_factory() as db:
        assert await job.is_due(db, org, TODAY) is True


@pytest.mark.asyncio
async def test_is_due_false_backfill_guard(session_factory):
    org = await _seed_org(session_factory)
    # Account created AFTER its most-recent-closeable cycle -> backfill guard.
    async with session_factory() as db:
        cc_type = AccountType(org_id=org.id, name="Credit Card", slug="credit_card", is_system=True)
        db.add(cc_type)
        await db.flush()
        account = Account(
            org_id=org.id,
            name="Brand New Card",
            account_type_id=cc_type.id,
            balance=Decimal("0.00"),
            currency="EUR",
            is_active=True,
            close_day=CLOSE_DAY,
            created_at=datetime.datetime(2026, 7, 22),  # after CLOSE_DATE (07-20)
        )
        db.add(account)
        await db.commit()

    job = CcStatementCloseJob()
    async with session_factory() as db:
        assert await job.is_due(db, org, TODAY) is False


@pytest.mark.asyncio
async def test_is_due_false_when_already_sent(session_factory):
    org = await _seed_org(session_factory)
    account = await _seed_cc_account(session_factory, org, name="Amex Gold")

    await sched_audit.record_cc_alert(
        org=org,
        account_id=account.id,
        close_date=CLOSE_DATE,
        event_type=sched_audit.CC_CLOSED_EVENT_TYPE,
        detail={},
    )

    job = CcStatementCloseJob()
    async with session_factory() as db:
        assert await job.is_due(db, org, TODAY) is False


@pytest.mark.asyncio
async def test_is_due_false_when_no_cc_accounts(session_factory):
    org = await _seed_org(session_factory)
    job = CcStatementCloseJob()
    async with session_factory() as db:
        assert await job.is_due(db, org, TODAY) is False


# ── run: carried balance ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_carried_balance_dispatches_with_email(session_factory, dispatch_calls):
    org = await _seed_org(session_factory)
    account = await _seed_cc_account(session_factory, org, name="Amex Gold")
    await _charge(session_factory, org, account, amount="240.00", on=datetime.date(2026, 7, 15))

    job = CcStatementCloseJob()
    async with session_factory() as db:
        result = await job.run(db, org, TODAY)

    assert result.outcome == "success"
    assert result.counts["dispatched_account_ids"] == [account.id]

    assert len(dispatch_calls) == 1
    call = dispatch_calls[0]
    assert call["send_email"] is True
    assert call["category"] == NotificationCategory.CC_STATEMENT
    assert call["org_id"] == org.id
    assert call["link_url"] == f"/accounts?edit={account.id}"
    assert "240.00 EUR is due on 2026-08-01" in call["body"]
    assert "240.00" not in call["email_body"]
    assert "EUR" not in call["email_body"]

    markers = await _sent_markers(session_factory, org.id)
    assert len(markers) == 1
    assert markers[0].detail["account_id"] == account.id
    assert markers[0].detail["close_date"] == CLOSE_DATE.isoformat()


@pytest.mark.asyncio
async def test_run_zero_owed_in_app_only_marker_still_written(session_factory, dispatch_calls):
    org = await _seed_org(session_factory)
    account = await _seed_cc_account(session_factory, org, name="Paid Off Card")
    # No charges -> statement_outstanding returns 0.

    job = CcStatementCloseJob()
    async with session_factory() as db:
        result = await job.run(db, org, TODAY)

    assert result.outcome == "success"
    assert len(dispatch_calls) == 1
    call = dispatch_calls[0]
    assert call["send_email"] is False
    assert "nothing due" in call["body"]

    markers = await _sent_markers(session_factory, org.id)
    assert len(markers) == 1
    assert markers[0].detail["account_id"] == account.id


# ── run: dedup + noop ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_run_same_closed_cycle_is_noop(session_factory, dispatch_calls):
    org = await _seed_org(session_factory)
    await _seed_cc_account(session_factory, org, name="Amex Gold")

    job = CcStatementCloseJob()
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
    job = CcStatementCloseJob()
    async with session_factory() as db:
        result = await job.run(db, org, TODAY)

    assert result.outcome == "noop"
    assert dispatch_calls == []


# ── run: partial failure isolation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_card_failure_does_not_block_the_other(session_factory, monkeypatch):
    org = await _seed_org(session_factory)
    bad = await _seed_cc_account(session_factory, org, name="Bad Card")
    good = await _seed_cc_account(session_factory, org, name="Good Card")

    calls: list[dict] = []

    async def _flaky_dispatch(db, **kwargs):
        if kwargs["title"].startswith("Bad Card"):
            raise RuntimeError("boom")
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(job_mod, "dispatch_notification_to_org_members", _flaky_dispatch)

    job = CcStatementCloseJob()
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
