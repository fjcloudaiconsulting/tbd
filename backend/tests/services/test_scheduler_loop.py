import asyncio
import datetime
import pytest

from app.services.scheduler import loop as L


async def _patch_platform_job(monkeypatch):
    """Stub the platform PAT-expiry job so tick unit tests avoid a real DB."""
    async def _noop(*a, **k):
        return 0
    monkeypatch.setattr(L, "run_api_token_expiry_reminders", _noop)
    monkeypatch.setattr(L, "run_platform_ai_catalog_sync", _noop)
    monkeypatch.setattr(L, "run_platform_ai_spend_watch", _noop)
    monkeypatch.setattr(L, "run_agent_action_purge", _noop)
    monkeypatch.setattr(L, "run_csp_audit_purge", _noop)


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


async def test_tick_runs_again_once_the_lease_expires(monkeypatch):
    from tests.conftest import expire_lease

    ran = {"n": 0}
    async def _sweep(today, **k): ran["n"] += 1
    monkeypatch.setattr(L, "run_all_due", _sweep)
    await _patch_platform_job(monkeypatch)
    assert await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600) is True
    assert await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600) is False
    expire_lease(L.LOCK_KEY)
    assert await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600) is True
    assert ran["n"] == 2


async def test_catalog_sync_runs_once_under_the_tick_lock(monkeypatch):
    async def _sweep(today, **k): pass
    monkeypatch.setattr(L, "run_all_due", _sweep)
    await _patch_platform_job(monkeypatch)
    calls = {"n": 0}
    async def _job(*a, **k):
        calls["n"] += 1
        return "synced"
    monkeypatch.setattr(L, "run_platform_ai_catalog_sync", _job)
    assert await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600) is True
    assert calls["n"] == 1
    assert await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600) is False
    assert calls["n"] == 1


async def test_g3_spend_watch_runs_once_under_the_tick_lock(monkeypatch):
    async def _sweep(today, **k): pass
    monkeypatch.setattr(L, "run_all_due", _sweep)
    await _patch_platform_job(monkeypatch)
    calls = {"n": 0}
    async def _job(*a, **k):
        calls["n"] += 1
        return "reconciled"
    monkeypatch.setattr(L, "run_platform_ai_spend_watch", _job)
    assert await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600) is True
    assert calls["n"] == 1
    assert await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600) is False
    assert calls["n"] == 1


async def test_agent_action_purge_runs_once_under_the_tick_lock(monkeypatch):
    """FENCE (TBD-640): wired into the tick, and skipped with it when locked out."""
    async def _sweep(today, **k): pass
    monkeypatch.setattr(L, "run_all_due", _sweep)
    await _patch_platform_job(monkeypatch)
    calls = {"n": 0}
    async def _job(*a, **k):
        calls["n"] += 1
        return 0
    monkeypatch.setattr(L, "run_agent_action_purge", _job)
    assert await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600) is True
    assert calls["n"] == 1
    assert await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600) is False
    assert calls["n"] == 1


async def test_agent_action_purge_runs_even_when_the_org_sweep_raises(monkeypatch):
    """FENCE (TBD-640): the 48h promise must not depend on the org sweep succeeding."""
    async def _boom(today, **k):
        raise RuntimeError("sweep failed")
    monkeypatch.setattr(L, "run_all_due", _boom)
    await _patch_platform_job(monkeypatch)
    calls = {"n": 0}
    async def _job(*a, **k):
        calls["n"] += 1
        return 0
    monkeypatch.setattr(L, "run_agent_action_purge", _job)
    with pytest.raises(RuntimeError):
        await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600)
    assert calls["n"] == 1


async def test_csp_audit_purge_runs_once_under_the_tick_lock(monkeypatch):
    """FENCE (BE-10): the 30-day CSP retention is wired into the tick, and
    skipped with it when locked out."""
    async def _sweep(today, **k): pass
    monkeypatch.setattr(L, "run_all_due", _sweep)
    await _patch_platform_job(monkeypatch)
    calls = {"n": 0}
    async def _job(*a, **k):
        calls["n"] += 1
        return 0
    monkeypatch.setattr(L, "run_csp_audit_purge", _job)
    assert await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600) is True
    assert calls["n"] == 1
    assert await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600) is False
    assert calls["n"] == 1
