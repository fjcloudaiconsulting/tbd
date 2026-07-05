import asyncio
import datetime
import pytest

from app.services.scheduler import loop as L


async def test_tick_runs_when_lock_acquired(monkeypatch):
    ran = {"n": 0}
    async def _sweep(today, **k): ran["n"] += 1
    monkeypatch.setattr(L, "run_all_due", _sweep)
    did = await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600)
    assert did is True
    assert ran["n"] == 1


async def test_second_tick_skipped_while_lock_held(monkeypatch):
    ran = {"n": 0}
    async def _sweep(today, **k): ran["n"] += 1
    monkeypatch.setattr(L, "run_all_due", _sweep)
    first = await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600)
    second = await L.run_one_tick(datetime.date(2026, 7, 4), lock_ttl=600)
    assert first is True and second is False
    assert ran["n"] == 1  # lock still held -> second sweep skipped
