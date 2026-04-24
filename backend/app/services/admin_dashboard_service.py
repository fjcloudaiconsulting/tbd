"""Aggregator for the L4.2 admin dashboard.

Goal: one round-trip that populates every tile on `/admin`. KPI reads
and health probes are deliberately in SEPARATE ``asyncio.gather`` blocks
so a stuck dependency can't stall the whole request and a failed probe
doesn't blank the KPIs (or vice-versa).

Design rationale: see docs/decisions/2026-04-24-admin-dashboard-home.md
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import Organization, User
from app.redis_client import get_client as get_redis_client


# Short enough that a dead dependency can't gate the page, long enough
# to absorb normal jitter on container cold-start / fork.
PROBE_TIMEOUT_SECONDS = 2.0


async def _probe_db(db: AsyncSession) -> dict[str, Any]:
    """Round-trip a trivial SELECT 1 and measure wall time."""
    start = time.perf_counter()
    try:
        await asyncio.wait_for(
            db.execute(text("SELECT 1")), timeout=PROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:
        # Truncate any driver-side error — we never want a stack trace
        # or credential fragment leaking into the response body.
        return {"ok": False, "error": type(exc).__name__}
    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    return {"ok": True, "latency_ms": latency_ms}


async def _probe_redis() -> dict[str, Any]:
    """PING Redis if configured; report `not_configured` otherwise."""
    client = get_redis_client()
    if client is None:
        return {"ok": False, "error": "not_configured"}
    start = time.perf_counter()
    try:
        await asyncio.wait_for(client.ping(), timeout=PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    return {"ok": True, "latency_ms": latency_ms}


async def build_dashboard_payload(db: AsyncSession) -> dict[str, Any]:
    """Collect every tile the L4.2 MVP renders in one round-trip."""
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    # KPI reads first, independent of health. If any of these blow up
    # the DB is broken badly enough that we want a 500 — the endpoint's
    # purpose is moot if COUNT() doesn't work.
    total_orgs, total_users, active_subs, signups_7d = await asyncio.gather(
        db.scalar(select(func.count()).select_from(Organization)),
        db.scalar(select(func.count()).select_from(User)),
        db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.status.in_(
                    (SubscriptionStatus.TRIALING, SubscriptionStatus.ACTIVE)
                )
            )
        ),
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.created_at >= seven_days_ago)
        ),
    )

    # Health probes gathered independently. Each coroutine catches its
    # own exceptions so one hanging dependency can't tank the whole
    # response — at worst the corresponding cell renders `ok: false`.
    db_health, redis_health = await asyncio.gather(_probe_db(db), _probe_redis())

    return {
        "kpis": {
            "total_orgs": total_orgs or 0,
            "total_users": total_users or 0,
            "active_subscriptions": active_subs or 0,
            "signups_last_7d": signups_7d or 0,
        },
        "health": {
            "db": db_health,
            "redis": redis_health,
        },
    }
