"""BE-10: the scheduler deletes CSP violation audit rows after 30 days.

* Only ``security.csp_violation`` rows older than 30 days go; newer CSP rows
  and every other event type, however old, stay.
* Deletes run in bounded batches, each committed on its own, and a tick stops
  after ``MAX_BATCHES``.
* The job logs a count, never row contents, and never raises.
"""
from __future__ import annotations

import datetime
import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.services.audit_service import CSP_VIOLATION_EVENT_TYPE
from app.services.scheduler.jobs import csp_audit_purge as purge_module
from app.services.scheduler.jobs.csp_audit_purge import run_csp_audit_purge

NOW = datetime.datetime(2026, 10, 24, 12, 0, 0)
OLD = NOW - datetime.timedelta(days=30, minutes=5)
RECENT = NOW - datetime.timedelta(days=29, hours=23, minutes=55)
SECRET = "https://app.example/reset-password-marker"


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


def _row(event_type: str, created_at: datetime.datetime) -> AuditEvent:
    return AuditEvent(
        event_type=event_type,
        actor_email="anonymous",
        outcome=AuditOutcome.FAILURE,
        detail={"document_uri": SECRET},
        created_at=created_at,
    )


async def _seed(factory, rows: list[AuditEvent]) -> None:
    async with factory() as db:
        db.add_all(rows)
        await db.commit()


async def _remaining(factory) -> list[tuple[str, datetime.datetime]]:
    async with factory() as db:
        res = await db.execute(select(AuditEvent.event_type, AuditEvent.created_at).order_by(AuditEvent.id))
        return [tuple(r) for r in res.all()]


@pytest.mark.parametrize("clock", [
    NOW,
    # The same instant as an aware non-UTC clock: dropping the UTC conversion shifts the cutoff 2h.
    datetime.datetime(2026, 10, 24, 14, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=2))),
])
async def test_only_csp_rows_older_than_30_days_are_deleted(factory, clock) -> None:
    """FENCE. Wrong implementations: another window (the 29d23h55m CSP row
    goes, or the 30d05m one stays), a purge that is not scoped to the CSP
    event type (the old login, reuse and rotation rows go), skipping the
    aware-to-naive UTC conversion (the aware case shifts)."""
    ancient = NOW - datetime.timedelta(days=400)
    await _seed(factory, [
        _row(CSP_VIOLATION_EVENT_TYPE, OLD),
        _row(CSP_VIOLATION_EVENT_TYPE, ancient),
        _row(CSP_VIOLATION_EVENT_TYPE, RECENT),
        _row("user.login.success", ancient),
        _row("auth.session.reuse_detected", OLD),
        _row("admin.org.delete", ancient),
    ])
    assert await run_csp_audit_purge(factory, now=clock) == 2
    assert await _remaining(factory) == [
        (CSP_VIOLATION_EVENT_TYPE, RECENT),
        ("user.login.success", ancient),
        ("auth.session.reuse_detected", OLD),
        ("admin.org.delete", ancient),
    ]


async def test_batches_are_bounded_and_a_tick_stops_after_max_batches(factory, monkeypatch) -> None:
    monkeypatch.setattr(purge_module, "BATCH_SIZE", 3)
    monkeypatch.setattr(purge_module, "MAX_BATCHES", 2)
    await _seed(factory, [_row(CSP_VIOLATION_EVENT_TYPE, OLD) for _ in range(10)])

    assert await run_csp_audit_purge(factory, now=NOW) == 6
    assert len(await _remaining(factory)) == 4
    assert await run_csp_audit_purge(factory, now=NOW) == 4
    assert await run_csp_audit_purge(factory, now=NOW) == 0
    assert await _remaining(factory) == []


async def test_each_batch_commits_on_its_own(factory, monkeypatch, log_lines) -> None:
    """A failure in the second batch keeps the first batch's deletes: one
    short transaction per batch, not one transaction for the whole run."""
    monkeypatch.setattr(purge_module, "BATCH_SIZE", 3)
    await _seed(factory, [_row(CSP_VIOLATION_EVENT_TYPE, OLD) for _ in range(5)])
    opened = {"n": 0}

    def flaky():
        opened["n"] += 1
        if opened["n"] == 2:
            raise OperationalError("SELECT audit_events", (SECRET,), Exception(SECRET))
        return factory()

    assert await run_csp_audit_purge(flaky, now=NOW) == 3
    assert len(await _remaining(factory)) == 2
    (line,) = [ln for ln in log_lines() if ln["msg"] == "scheduler.csp_audit_purge.failed"]
    assert line["error"] == "OperationalError"
    assert line["rows"] == 3
    assert SECRET not in json.dumps(log_lines())


async def test_logs_a_count_and_never_row_contents(factory, log_lines) -> None:
    await _seed(factory, [_row(CSP_VIOLATION_EVENT_TYPE, OLD) for _ in range(4)])
    assert await run_csp_audit_purge(factory, now=NOW) == 4
    (line,) = [ln for ln in log_lines() if ln["msg"] == "scheduler.csp_audit_purge.complete"]
    assert line["rows"] == 4
    assert SECRET not in json.dumps(log_lines())


async def test_nothing_to_purge_logs_nothing(factory, log_lines) -> None:
    await _seed(factory, [_row(CSP_VIOLATION_EVENT_TYPE, RECENT)])
    assert await run_csp_audit_purge(factory, now=NOW) == 0
    assert not [ln for ln in log_lines() if ln["msg"].startswith("scheduler.csp_audit_purge")]


async def test_never_raises_and_logs_only_the_error_class(log_lines) -> None:
    def broken():
        raise OperationalError("DELETE FROM audit_events", (SECRET,), Exception(SECRET))

    assert await run_csp_audit_purge(broken, now=NOW) == 0
    (line,) = [ln for ln in log_lines() if ln["msg"] == "scheduler.csp_audit_purge.failed"]
    assert line["error"] == "OperationalError"
    assert SECRET not in json.dumps(log_lines())
