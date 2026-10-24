from __future__ import annotations

import asyncio
import datetime

import logging
from opentelemetry.trace import SpanKind

from app import state_db, tracing
from app.services.scheduler.jobs.agent_action_purge import run_agent_action_purge
from app.services.scheduler.jobs.api_token_expiry import run_api_token_expiry_reminders
from app.services.scheduler.jobs.csp_audit_purge import run_csp_audit_purge
from app.services.scheduler.jobs.oauth_client_purge import run_oauth_client_purge
from app.services.scheduler.jobs.platform_ai_catalog import run_platform_ai_catalog_sync
from app.services.scheduler.jobs.platform_ai_spend_watch import run_platform_ai_spend_watch
from app.services.scheduler.runner import run_all_due

logger = logging.getLogger(__name__)

LOCK_KEY = "scheduler:tick:lock"


async def acquire_tick_lock(ttl_seconds: int) -> bool:
    # A lease row, never released: the TTL also spaces ticks across replicas.
    return await state_db.acquire_lease(LOCK_KEY, ttl_seconds) is not None


async def run_one_tick(today: datetime.date, *, lock_ttl: int, max_orgs: int | None = None) -> bool:
    if not await acquire_tick_lock(lock_ttl):
        logger.info("scheduler.tick.skip_locked")
        return False
    logger.info("scheduler.tick.start")
    # TBD-640: agent proposals older than 48 hours. Never raises, and runs
    # first so a failing org sweep cannot starve the 48h retention promise.
    with tracing.span("job agent_action_purge", SpanKind.INTERNAL, {"job.kind": "agent_action_purge"}):
        await run_agent_action_purge()
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
    # BE-10: CSP violation audit rows older than 30 days. Never raises.
    with tracing.span("job csp_audit_purge", SpanKind.INTERNAL, {"job.kind": "csp_audit_purge"}):
        await run_csp_audit_purge()
    # TBD-631: daily OpenRouter catalog sync. Never raises.
    with tracing.span("job platform_ai_catalog", SpanKind.INTERNAL, {"job.kind": "platform_ai_catalog"}):
        await run_platform_ai_catalog_sync()
    # TBD-633: OpenRouter spend reconciliation and spike alarm. Never raises.
    with tracing.span("job platform_ai_spend_watch", SpanKind.INTERNAL, {"job.kind": "platform_ai_spend_watch"}):
        await run_platform_ai_spend_watch()
    logger.info("scheduler.tick.complete")
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
            logger.error("scheduler.tick.error", extra={"error": type(exc).__name__})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass
