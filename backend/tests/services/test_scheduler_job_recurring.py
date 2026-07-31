from __future__ import annotations

import datetime
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.user import Organization
from app.services.scheduler.base import OUTCOME_NOOP, OUTCOME_SUCCESS, JobResult
from app.services.scheduler.jobs.recurring_generation import RecurringGenerationJob


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def test_not_due_when_no_templates(session_factory, monkeypatch):
    job = RecurringGenerationJob()
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org); await db.commit(); await db.refresh(org)
        assert await job.is_due(db, org, datetime.date(2026, 7, 4)) is False


async def test_run_noop_writes_no_audit_and_no_notify(session_factory, monkeypatch):
    job = RecurringGenerationJob()
    calls = {"audit": 0, "notify": 0}
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.generate_due_transactions",
        _fake_generate(generated=0, settled=0),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.record_run",
        _counter(calls, "audit"),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.dispatch_notification_to_org_members",
        _counter(calls, "notify"),
    )
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org); await db.commit(); await db.refresh(org)
        res = await job.run(db, org, datetime.date(2026, 7, 4))
    assert res.outcome == OUTCOME_NOOP
    assert calls == {"audit": 0, "notify": 0}


async def test_run_success_records_and_notifies(session_factory, monkeypatch):
    job = RecurringGenerationJob()
    calls = {"audit": 0, "notify": 0}
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.generate_due_transactions",
        _fake_generate(generated=2, settled=1),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.record_run",
        _counter(calls, "audit", returns=42),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.dispatch_notification_to_org_members",
        _counter(calls, "notify"),
    )
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org); await db.commit(); await db.refresh(org)
        res = await job.run(db, org, datetime.date(2026, 7, 4))
    assert res.outcome == OUTCOME_SUCCESS
    assert res.counts == {"generated": 2, "settled": 1, "pending": 0}
    assert calls == {"audit": 1, "notify": 1}


# ─────────────────────────────────────────────────────────────────────────────
# TBD-284 — ONE TICK, ONE CLOCK.
# ─────────────────────────────────────────────────────────────────────────────

async def test_run_threads_the_ticks_clock_into_generation(session_factory, monkeypatch):
    """FENCE — ``run`` must hand the tick's ``today`` to the generation service.

    ``run`` used to call ``generate_due_transactions(db, org.id)`` with no
    ``today``, so the service re-read the wall clock. A tick that starts at
    23:59:59 then decides "there is work" against one day and MATERIALISES THE
    ROWS against the next -- a money row in the wrong billing period.

    The asserted value is a date the wall clock can never equal, so this fence
    cannot rot into a tautology and cannot become a date bomb.

    Wrong implementation killed: ``generate_due_transactions(db, org.id)``
    -> the fake records None and this goes red on the value.
    """
    seen: list = []
    job = RecurringGenerationJob()
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.generate_due_transactions",
        _fake_generate(generated=1, settled=0, sink=seen),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.record_run",
        _counter({"a": 0}, "a", returns=1),
    )
    monkeypatch.setattr(
        "app.services.scheduler.jobs.recurring_generation.dispatch_notification_to_org_members",
        _counter({"n": 0}, "n"),
    )
    tick_day = datetime.date(2099, 3, 17)
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org); await db.commit(); await db.refresh(org)
        await job.run(db, org, tick_day)

    assert seen == [tick_day], (
        f"generation ran against {seen!r}, not the tick's clock {tick_day!r}"
    )


async def test_one_tick_hands_the_same_clock_to_is_due_and_run(session_factory):
    """FENCE — the runner resolves the clock ONCE per tick.

    This is the other half of the straddle: threading ``today`` through ``run``
    is worthless if the runner resolves a fresh clock between deciding and
    doing. Uses a stub job so it fences the RUNNER contract, independently of
    any one job's implementation.

    Wrong implementation killed: ``run_all_due`` calling ``date.today()`` again
    for the ``run`` leg instead of reusing its ``today`` parameter.
    """
    from app.services.scheduler import runner as runner_mod

    received: dict[str, list] = {"is_due": [], "run": []}

    class _SpyJob:
        job_type = "spy"
        setting_key = "spy_enabled"

        async def is_due(self, db, org, today):
            received["is_due"].append(today)
            return True

        async def run(self, db, org, today):
            received["run"].append(today)
            return JobResult.noop()

    factory = session_factory
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org); await db.commit()

    async def _always_on(db, org_id, key):
        return True

    tick_day = datetime.date(2099, 3, 17)
    import unittest.mock as _mock
    with _mock.patch.object(runner_mod.org_settings, "get_bool", _always_on):
        await runner_mod.run_all_due(
            tick_day, session_factory=factory, registry=[_SpyJob()]
        )

    assert received["is_due"] == [tick_day]
    assert received["run"] == [tick_day]
    # The property, stated directly: one tick, one clock.
    assert received["is_due"] == received["run"]


def _fake_generate(*, generated, settled, sink=None):
    # ``today`` defaults to None ON PURPOSE (TBD-284). If the job stops passing
    # the tick's clock, this fake still accepts the call and records None, so
    # the fence fails on the VALUE. A fake with a REQUIRED ``today`` would go
    # red with a TypeError for any signature change and green for a job that
    # passed the WRONG date -- which is the failure this ticket is about.
    async def _f(db, org_id, today=None):
        if sink is not None:
            sink.append(today)
        return {"generated": generated, "settled": settled, "pending": 0,
                "period_end": "2026-07-31"}
    return _f


def _counter(store, key, returns=None):
    async def _f(*a, **k):
        store[key] += 1
        return returns
    return _f
