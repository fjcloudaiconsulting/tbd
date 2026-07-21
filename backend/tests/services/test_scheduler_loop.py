import asyncio
import datetime
import pytest

from app.services.scheduler import loop as L


async def _patch_platform_job(monkeypatch):
    """Stub the platform PAT-expiry job so tick unit tests avoid a real DB."""
    async def _noop(*a, **k):
        return 0
    monkeypatch.setattr(L, "run_api_token_expiry_reminders", _noop)


async def test_tick_runs_when_lock_acquired(monkeypatch):
    ran = {"n": 0}
    async def _sweep(today, **k): ran["n"] += 1
    monkeypatch.setattr(L, "run_all_due", _sweep)
    await _patch_platform_job(monkeypatch)
    did = await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600)
    assert did is True
    assert ran["n"] == 1


async def test_second_tick_skipped_while_lock_held(monkeypatch):
    ran = {"n": 0}
    async def _sweep(today, **k): ran["n"] += 1
    monkeypatch.setattr(L, "run_all_due", _sweep)
    await _patch_platform_job(monkeypatch)
    first = await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600)
    second = await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600)
    assert first is True and second is False
    assert ran["n"] == 1  # lock still held -> second sweep skipped


async def test_platform_pat_job_runs_under_the_tick_lock(monkeypatch):
    """The platform PAT-expiry job runs once when the lock is acquired and is
    skipped entirely when the tick is locked out (same lock as the per-org sweep)."""
    async def _sweep(today, **k): pass
    monkeypatch.setattr(L, "run_all_due", _sweep)
    calls = {"n": 0}
    async def _job(*a, **k):
        calls["n"] += 1
        return 0
    monkeypatch.setattr(L, "run_api_token_expiry_reminders", _job)

    first = await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600)
    assert first is True
    assert calls["n"] == 1  # ran under the acquired lock

    second = await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600)
    assert second is False
    assert calls["n"] == 1  # lock held -> platform job skipped too
