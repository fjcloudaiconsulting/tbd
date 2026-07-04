import asyncio
import pytest
from app import main as app_main


async def test_lifespan_starts_and_stops_scheduler(monkeypatch):
    started = {"n": 0}
    async def _fake_loop(stop_event, *, tick_seconds, lock_ttl):
        started["n"] += 1
        await stop_event.wait()  # block until shutdown sets the event
    monkeypatch.setattr(app_main, "scheduler_loop", _fake_loop)
    monkeypatch.setattr(app_main.app_settings, "scheduler_enabled", True)
    # Skip dev migrations for this test.
    monkeypatch.setattr(app_main.app_settings, "app_env", "production")

    async with app_main.lifespan(app_main.app):
        # inside the context the task should be running
        assert started["n"] == 1
        assert not app_main.app.state.scheduler_task.done()
    # after exit the task is finished
    assert app_main.app.state.scheduler_task.done()
