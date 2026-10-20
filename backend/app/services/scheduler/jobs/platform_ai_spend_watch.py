"""Daily OpenRouter spend reconciliation and spend-spike alarm (TBD-633).

Platform-global like the catalog sync: called once per tick from ``run_one_tick`` under the tick
lease. Alarms are structured log events (the contract with the infra session's Grafana rules):
``platform_ai_spend.reconciled|drift|rebaselined|reconcile_failed|spike|spike_failed|failed``
(``failed`` is the outer except).

Once a day it compares OpenRouter's lifetime ``/key`` usage delta against the platform rows of
``ai_usage_ledger`` over the same window, and separately flags a day whose spend jumped against the
mean of the 7 days before it. Errors are logged, never raised (the ticker must not die).
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

import httpx
from opentelemetry import trace
from sqlalchemy import func, select

from app import state_db, tracing
from app.config import settings
from app.database import async_session
from app.models.ai_usage_ledger import AIUsageLedger
from app.models.system_setting import SystemSetting
from app.services import platform_ai
from app.services.scheduler.jobs.platform_ai_catalog import OPENROUTER_ROOT

logger = logging.getLogger(__name__)

DRIFT_PCT = 2
FLOOR_MICROS = 100_000  # a gap under $0.10 is noise whatever its percentage
SPIKE_RATIO = 3
ORG_FLOOR = 1_000_000
GLOBAL_FLOOR = 5_000_000
SNAPSHOT_KEY = "platform_ai_spend.snapshot"
SPIKE_DAY_KEY = "platform_ai_spend.spike_day"
DUE_AFTER = datetime.timedelta(hours=23)  # 23 not 24, so tick jitter cannot slip a day
ATTEMPT_LEASE = "platform_ai_spend:attempt"
ATTEMPT_LEASE_TTL = 3600  # never released: the TTL is the 1h retry backoff
FETCH_DEADLINE_SECONDS = 30
MAX_BODY_BYTES = 65_536
USAGE_CAP_MICROS = 10**15  # the counter is lifetime, so it is not bound by the per-call cap


class SpendFetchError(Exception):
    """OpenRouter's ``/key`` did not answer usably; carries the status, never the body."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _usage_counter_micros(value) -> int | None:
    """Lifetime USD counter -> micro-USD, half-even; None if unusable."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    if not d.is_finite() or d < 0 or d > Decimal(USAGE_CAP_MICROS) / 1_000_000:
        return None
    return int((d * 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


async def fetch_key_usage(*, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """``data`` of ``GET /key`` for the house key."""
    # identity encoding: the byte cap must bound decompressed bytes
    headers = {"Authorization": f"Bearer {settings.platform_ai_openrouter_api_key}", "Accept-Encoding": "identity"}
    async with asyncio.timeout(FETCH_DEADLINE_SECONDS):
        async with httpx.AsyncClient(timeout=FETCH_DEADLINE_SECONDS, transport=transport, trust_env=False) as client:
            async with client.stream("GET", f"{OPENROUTER_ROOT}/key", headers=headers) as r:
                if r.status_code != 200:
                    raise SpendFetchError("status", r.status_code)
                raw = b""
                async for chunk in r.aiter_bytes():
                    raw += chunk
                    if len(raw) > MAX_BODY_BYTES:
                        raise SpendFetchError("body too large")
    body = json.loads(raw)
    return body.get("data") if isinstance(body, dict) else None


async def _get(db, key: str) -> str | None:
    return await db.scalar(select(SystemSetting.value).where(SystemSetting.key == key))


async def _put(db, key: str, value: str) -> None:
    row = await db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if row is None:
        db.add(SystemSetting(key=key, value=value))
    else:
        row.value = value


def _snapshot(raw: str | None) -> dict | None:
    """The stored snapshot, or None when absent or malformed."""
    try:
        s = json.loads(raw)
        usage, at = s["usage_micros"], datetime.datetime.fromisoformat(s["at"])
        if (at.tzinfo is None and isinstance(usage, int) and not isinstance(usage, bool) and usage >= 0
                and isinstance(s["key_fp"], str)):
            return {"usage_micros": usage, "at": at, "key_fp": s["key_fp"]}
    except (TypeError, ValueError, KeyError):
        pass
    return None


def _spiked(day: int, window: int, floor: int) -> bool:
    return day >= floor and 7 * day > SPIKE_RATIO * window  # integer math, no truncated mean


async def _sums(db, start, end) -> dict[int, int]:
    q = (select(AIUsageLedger.org_id, func.sum(AIUsageLedger.cost_micros))
         .where(AIUsageLedger.billing_source == "platform",
                AIUsageLedger.dispatched_at >= start, AIUsageLedger.dispatched_at < end)
         .group_by(AIUsageLedger.org_id))
    return {o: int(v) for o, v in (await db.execute(q)).all()}


async def _spikes(session_factory, now: datetime.datetime) -> None:
    day = now.date() - datetime.timedelta(days=1)
    async with session_factory() as db:
        try:
            if datetime.date.fromisoformat(await _get(db, SPIKE_DAY_KEY)) == day:  # a later day = clock moved back: stale
                return
        except (TypeError, ValueError):
            pass
        start = datetime.datetime.combine(day, datetime.time.min)
        today = await _sums(db, start, start + datetime.timedelta(days=1))
        window = await _sums(db, start - datetime.timedelta(days=7), start)
        scopes = [("org", o, today[o], window.get(o, 0), ORG_FLOOR) for o in today]
        scopes.append(("global", None, sum(today.values()), sum(window.values()), GLOBAL_FLOOR))
        spikes = [{"scope": scope, "org_id": org_id, "day": day.isoformat(), "day_micros": d,
                   "baseline_micros": w // 7, "ratio_x100": d * 700 // w if w else None}
                  for scope, org_id, d, w, floor in scopes if _spiked(d, w, floor)]
        await _put(db, SPIKE_DAY_KEY, day.isoformat())
        await db.commit()  # commit before logging: a failed commit must not alarm twice
        for extra in spikes:
            logger.error("platform_ai_spend.spike", extra=extra)


async def _reconcile(session_factory, fetch, now: datetime.datetime) -> str:
    async with session_factory() as db:
        prev = _snapshot(await _get(db, SNAPSHOT_KEY))
    if prev is not None and now - prev["at"] < DUE_AFTER:
        return "fresh"
    if await state_db.acquire_lease(ATTEMPT_LEASE, ATTEMPT_LEASE_TTL) is None:
        return "backoff"
    try:
        data = await fetch()
        usage = _usage_counter_micros(data.get("usage") if isinstance(data, dict) else None)
        if usage is None:
            raise SpendFetchError("unusable usage")
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status", None)
        logger.error("platform_ai_spend.reconcile_failed", extra={
            "error": type(exc).__name__, "status": status,
            "hours_since_success": None if prev is None else round((now - prev["at"]).total_seconds() / 3600, 1)})
        return "fetch_failed"
    fp = hashlib.sha256(settings.platform_ai_openrouter_api_key.encode()).hexdigest()[:16]
    new = json.dumps({"usage_micros": usage, "at": now.isoformat(), "key_fp": fp})
    outcome = "baselined"
    events: list = []  # logged only after the snapshot commits
    if prev is None:
        events.append((logger.info, "platform_ai_spend.reconciled", {"openrouter_micros": usage, "baseline": True}))
    elif prev["key_fp"] != fp or usage < prev["usage_micros"]:
        outcome = "rebaselined"
        events.append((logger.warning, "platform_ai_spend.rebaselined", {
            "reason": "key_changed" if prev["key_fp"] != fp else "usage_decreased"}))
    else:
        async with session_factory() as db:
            ledger = int(await db.scalar(select(func.coalesce(func.sum(AIUsageLedger.cost_micros), 0)).where(
                AIUsageLedger.billing_source == "platform",
                AIUsageLedger.dispatched_at > prev["at"], AIUsageLedger.dispatched_at <= now)))
        or_delta = usage - prev["usage_micros"]
        gap, top = or_delta - ledger, max(or_delta, ledger)
        drift = abs(gap) >= FLOOR_MICROS and abs(gap) * 100 > DRIFT_PCT * top
        fields = {"openrouter_micros": or_delta, "ledger_micros": ledger, "gap_micros": gap,
                  "gap_bp": abs(gap) * 10_000 // top if top else 0,
                  "window_s": int((now - prev["at"]).total_seconds()), "drift": drift}
        events.append((logger.info, "platform_ai_spend.reconciled", fields))
        if drift:
            events.append((logger.error, "platform_ai_spend.drift", fields))
        outcome = "drift" if drift else "reconciled"
    async with session_factory() as db:
        await _put(db, SNAPSHOT_KEY, new)
        await db.commit()
    for log, name, extra in events:
        log(name, extra=extra)
    return outcome


async def run_platform_ai_spend_watch(
    session_factory=async_session, *, fetch=fetch_key_usage, now: datetime.datetime | None = None
) -> str:
    """One daily pass; returns an outcome word and never raises."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is not None:  # columns are naive UTC
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    now = now.replace(microsecond=0)  # the ledger column has no fractional seconds
    try:
        if not (settings.ai_native_enabled and platform_ai.platform_key_configured("openrouter")):
            return "off"
        try:
            await _spikes(session_factory, now)
        except Exception as exc:  # noqa: BLE001 -- a spike failure must not block the reconcile
            logger.error("platform_ai_spend.spike_failed", extra={"error": type(exc).__name__})
        return await _reconcile(session_factory, fetch, now)
    except Exception as exc:  # noqa: BLE001 -- never let the ticker die
        tracing.mark_error(trace.get_current_span(), exc)
        logger.error("platform_ai_spend.failed", extra={"error": type(exc).__name__})
        return "error"
