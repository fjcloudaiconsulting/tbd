from __future__ import annotations

import asyncio
import datetime

import structlog
from opentelemetry.trace import SpanKind

from app import redis_client, tracing
from app.services.scheduler.jobs.api_token_expiry import run_api_token_expiry_reminders
from app.services.scheduler.jobs.oauth_client_purge import run_oauth_client_purge
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


async def run_one_tick(today: datetime.date, *, lock_ttl: int, max_orgs: int | None = None) -> bool:
    if not await acquire_tick_lock(lock_ttl):
        await logger.ainfo("scheduler.tick.skip_locked")
        return False
    await logger.ainfo("scheduler.tick.start")
    await run_all_due(today, max_orgs=max_orgs)
    # Platform-level PAT expiry reminders run once per tick under this same lock.
    # They are NOT part of the per-org registry (PAT tokens have no org
    # dimension) and are gated on their own global SystemSetting flag inside the
    # job. A tz-aware ``now`` drives the day-granularity threshold math.
    with tracing.span("job api_token_expiry", SpanKind.INTERNAL, {"job.kind": "api_token_expiry"}):
        await run_api_token_expiry_reminders(now=datetime.datetime.now(datetime.timezone.utc))
    # TBD-587: expired OAuth codes, then idle OAuth clients. Never raises.
    with tracing.span("job oauth_client_purge", SpanKind.INTERNAL, {"job.kind": "oauth_client_purge"}):
        await run_oauth_client_purge()
    await logger.ainfo("scheduler.tick.complete")
    return True


async def scheduler_loop(
    stop_event: asyncio.Event, *, tick_seconds: int, lock_ttl: int, max_orgs: int | None = None
) -> None:
    while not stop_event.is_set():
        try:
            # Inside the try: the span records the error class, then the loop swallows it.
            with tracing.span("scheduler.tick", SpanKind.INTERNAL, {}):
                await run_one_tick(datetime.date.today(), lock_ttl=lock_ttl, max_orgs=max_orgs)
        except Exception as exc:  # noqa: BLE001 — never let the ticker die
            await logger.aerror("scheduler.tick.error", error=type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass
