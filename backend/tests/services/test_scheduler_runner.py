from __future__ import annotations

import datetime
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.user import Organization
from app.services.scheduler import runner as R
from app.services.scheduler.base import JobResult


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


class _Job:
    def __init__(self, job_type, setting_key, *, due=True, boom=False, noop=False, log=None):
        self.job_type = job_type; self.setting_key = setting_key
        self._due = due; self._boom = boom; self._noop = noop
        self._log = log if log is not None else []
    async def is_due(self, db, org, today): return self._due
    async def run(self, db, org, today):
        if self._boom: raise RuntimeError("kaboom")
        self._log.append(("ran", self.job_type))
        return JobResult.noop() if self._noop else JobResult.ok({})


async def test_disabled_job_is_skipped(session_factory, monkeypatch):
    log = []
    monkeypatch.setattr(R, "async_session", session_factory)
    async def _enabled(db, org_id, key): return key != "off_key"
    monkeypatch.setattr(R.org_settings, "get_bool", _enabled)
    async with session_factory() as db:
        db.add(Organization(name="A", billing_cycle_day=1)); await db.commit()
    reg = [_Job("j_on", "on_key", log=log), _Job("j_off", "off_key", log=log)]
    await R.run_all_due(datetime.date(2026, 7, 4), session_factory=session_factory, registry=reg)
    assert ("ran", "j_on") in log
    assert ("ran", "j_off") not in log


async def test_one_job_failure_does_not_abort_sweep(session_factory, monkeypatch):
    log = []
    failures = {"n": 0}
    monkeypatch.setattr(R, "async_session", session_factory)
    async def _enabled(db, org_id, key): return True
    async def _fail_audit(**k):
        if k.get("outcome") == "failure": failures["n"] += 1
        return 1
    monkeypatch.setattr(R.org_settings, "get_bool", _enabled)
    monkeypatch.setattr(R, "record_run", _fail_audit)
    async with session_factory() as db:
        db.add(Organization(name="A", billing_cycle_day=1)); await db.commit()
    reg = [_Job("boom", "k1", boom=True, log=log), _Job("good", "k2", log=log)]
    await R.run_all_due(datetime.date(2026, 7, 4), session_factory=session_factory, registry=reg)
    assert ("ran", "good") in log     # ran despite the earlier failure
    assert failures["n"] == 1          # failure was audited


async def _seed_orgs(session_factory, n):
    async with session_factory() as db:
        for i in range(n):
            db.add(Organization(name=f"Org{i}", billing_cycle_day=1))
        await db.commit()


async def test_max_orgs_per_tick_caps_orgs_that_did_work(session_factory, monkeypatch):
    # A fresh-deploy burst of 5 due orgs must drain at most `max_orgs` per tick.
    runs = {"n": 0}
    monkeypatch.setattr(R, "async_session", session_factory)
    async def _enabled(db, org_id, key): return True
    monkeypatch.setattr(R.org_settings, "get_bool", _enabled)
    await _seed_orgs(session_factory, 5)

    class _Counting(_Job):
        async def run(self, db, org, today):
            runs["n"] += 1
            return JobResult.ok({})

    reg = [_Counting("j", "k")]
    await R.run_all_due(datetime.date(2026, 7, 4), session_factory=session_factory,
                        registry=reg, max_orgs=2)
    assert runs["n"] == 2  # only 2 of 5 orgs processed this tick


async def test_noop_orgs_do_not_consume_budget(session_factory, monkeypatch):
    # Orgs whose only outcome is noop must NOT count against the per-tick budget,
    # so steady-state (few real mutations) never starves orgs past the cap.
    log = []
    monkeypatch.setattr(R, "async_session", session_factory)
    async def _enabled(db, org_id, key): return True
    monkeypatch.setattr(R.org_settings, "get_bool", _enabled)
    await _seed_orgs(session_factory, 3)
    reg = [_Job("noop_job", "k", noop=True, log=log)]
    await R.run_all_due(datetime.date(2026, 7, 4), session_factory=session_factory,
                        registry=reg, max_orgs=1)
    # All 3 orgs ran their (noop) job because none consumed the budget of 1.
    assert log.count(("ran", "noop_job")) == 3


async def test_none_cap_processes_all_orgs(session_factory, monkeypatch):
    # Default (no cap) preserves the pre-guard behavior: every org is processed.
    log = []
    monkeypatch.setattr(R, "async_session", session_factory)
    async def _enabled(db, org_id, key): return True
    monkeypatch.setattr(R.org_settings, "get_bool", _enabled)
    await _seed_orgs(session_factory, 4)
    reg = [_Job("j", "k", log=log)]
    await R.run_all_due(datetime.date(2026, 7, 4), session_factory=session_factory,
                        registry=reg, max_orgs=None)
    assert log.count(("ran", "j")) == 4


async def test_zero_cap_means_no_cap(session_factory, monkeypatch):
    # max_orgs=0 (the "disable the guard" sentinel from config) must NOT cap at
    # the first working org — it means unlimited.
    log = []
    monkeypatch.setattr(R, "async_session", session_factory)
    async def _enabled(db, org_id, key): return True
    monkeypatch.setattr(R.org_settings, "get_bool", _enabled)
    await _seed_orgs(session_factory, 4)
    reg = [_Job("j", "k", log=log)]
    await R.run_all_due(datetime.date(2026, 7, 4), session_factory=session_factory,
                        registry=reg, max_orgs=0)
    assert log.count(("ran", "j")) == 4
