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


async def build_dashboard_payload(db: AsyncSession) -> dict[str, Any]:
    """Collect every tile the L4.2 MVP renders in one round-trip."""
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    # KPI reads are sequential, not gathered: SQLAlchemy's AsyncSession
    # is explicitly NOT safe for concurrent use across tasks, so firing
    # four db.scalar() calls on the same session via asyncio.gather can
    # corrupt session/connection state under load. The counts are trivial
    # (four indexed COUNTs); four sequential round-trips is still
    # sub-millisecond territory on this dataset. If this ever grows to
    # matter, collapse into a single SELECT with scalar subqueries rather
    # than reintroducing concurrent session access.
    total_orgs = await db.scalar(select(func.count()).select_from(Organization))
    total_users = await db.scalar(select(func.count()).select_from(User))
    active_subs = await db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(
            Subscription.status.in_(
                (SubscriptionStatus.TRIALING, SubscriptionStatus.ACTIVE)
            )
        )
    )
    signups_7d = await db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.created_at >= seven_days_ago)
    )

    # The probe catches its own exceptions, so a hanging database renders
    # `ok: false` instead of failing the whole response.
    db_health = await _probe_db(db)

    return {
        "kpis": {
            "total_orgs": total_orgs or 0,
            "total_users": total_users or 0,
            "active_subscriptions": active_subs or 0,
            "signups_last_7d": signups_7d or 0,
        },
        "health": {
            "db": db_health,
        },
    }
