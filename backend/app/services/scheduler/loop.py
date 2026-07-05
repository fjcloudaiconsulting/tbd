from __future__ import annotations

import asyncio
import datetime

import structlog

from app import redis_client
from app.services.scheduler.runner import run_all_due

logger = structlog.get_logger(__name__)

LOCK_KEY = "scheduler:tick:lock"


async def acquire_tick_lock(ttl_seconds: int) -> bool:
    client = redis_client.get_client()
    if client is None:
        # Dev / no-redis: single process, no contention to guard against.
        return True
    got = await client.set(LOCK_KEY, "1", nx=True, ex=ttl_seconds)
    return bool(got)


async def run_one_tick(today: datetime.date, *, lock_ttl: int) -> bool:
    if not await acquire_tick_lock(lock_ttl):
        await logger.ainfo("scheduler.tick.skip_locked")
        return False
    await logger.ainfo("scheduler.tick.start")
    await run_all_due(today)
    await logger.ainfo("scheduler.tick.complete")
    return True


async def scheduler_loop(stop_event: asyncio.Event, *, tick_seconds: int, lock_ttl: int) -> None:
    while not stop_event.is_set():
        try:
            await run_one_tick(datetime.date.today(), lock_ttl=lock_ttl)
        except Exception as exc:  # noqa: BLE001 — never let the ticker die
            await logger.aerror("scheduler.tick.error", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass
