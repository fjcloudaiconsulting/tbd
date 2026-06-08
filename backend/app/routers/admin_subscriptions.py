"""Admin subscription & revenue view router (L4.5).

Mounted at ``/api/v1/admin/subscriptions``. Read-only by design — the
override / mutation flow lives on ``/admin/orgs/[id]`` (L4.3 + L4.11)
and L4.5 deliberately does not duplicate it.

Auth via the platform ``subscriptions.view`` permission (superadmin
short-circuits today; fine-grained roles can land later via L4.8
without touching this file).

Audit policy: every list hit emits a structlog
``admin.subscriptions.viewed`` event and every detail hit emits
``admin.subscriptions.detail.viewed``; the durable
``audit_events`` row is **rate-throttled** to at most once per admin
per event-type per minute. List and detail use distinct event types
so a recent list view does not suppress the detail audit row (the
detail row carries ``target_org_id`` and must not be lost). Otherwise
an admin paging through 5,000 subscriptions would write 100 audit
rows for the same intent. The throttle uses Redis ``SET NX EX 60``;
when Redis is unconfigured (dev / tests) we fall **open** — emit
every call — so test assertions can pin the event without depending
on Redis being up.

Privacy: the raw search ``q`` is NEVER stored in the durable audit
detail. Only ``query_length`` (and ``has_query``) go in, mirroring
the cross-org user-search contract on ``admin_users.py``.
"""
from __future__ import annotations

from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.permissions import require_permission
from app.database import get_db
from app.deps import get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip
from app.redis_client import get_client as get_redis_client
from app.schemas.admin_subscriptions import (
    SubscriptionDetail,
    SubscriptionKPIs,
    SubscriptionListResponse,
)
from app.services import admin_subscription_service, audit_service
from app.services.exceptions import NotFoundError, ValidationError


logger = structlog.stdlib.get_logger()

router = APIRouter(
    prefix="/api/v1/admin/subscriptions", tags=["admin-subscriptions"]
)


# Audit throttle window: one durable audit row per (actor, event) per
# this many seconds. Locked at 60s to match the L4.5 spec ("once per
# admin per minute is enough, don't log every page-flip").
AUDIT_THROTTLE_SECONDS = 60


SubscriptionStatusFilter = Literal[
    "trialing", "active", "past_due", "canceled"
]


def _request_id() -> Optional[str]:
    """Pull the per-request id bound by RequestContextMiddleware (L4.9)."""
    return structlog.contextvars.get_contextvars().get("request_id")


async def _should_persist_audit(
    *, actor_user_id: Optional[int], event_type: str
) -> bool:
    """Return True if this hit should write a durable audit row.

    Fail-open semantics:

    - No Redis configured → return True every call (dev / unit tests
      where the stub Redis client is absent). The structlog event
      still emits unconditionally, so triage information is never
      lost; only the durable row is gated.
    - Redis error → return True (don't lose audit evidence because of
      a Redis blip).
    - Successful ``SET NX EX``: this is the first hit in the window,
      return True.
    - ``SET NX`` returned None (already set): we already wrote an
      audit row in the window, return False.
    """
    client = get_redis_client()
    if client is None or actor_user_id is None:
        return True
    key = f"admin.subscriptions.audit:{event_type}:{actor_user_id}"
    try:
        result = await client.set(key, "1", nx=True, ex=AUDIT_THROTTLE_SECONDS)
    except Exception as exc:  # noqa: BLE001 — defensive, never block on Redis.
        await logger.awarning(
            "admin.subscriptions.audit_throttle.error",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return True
    # Redis returns True when SET NX takes effect, None / False otherwise.
    return bool(result)


async def _emit_view_audit(
    *,
    event_type: str,
    actor: User,
    request: Request,
    session_factory: async_sessionmaker[AsyncSession],
    detail: Optional[dict] = None,
    target_org_id: Optional[int] = None,
    target_org_name: Optional[str] = None,
) -> None:
    """Emit both the structlog event (always) and the durable audit row
    (rate-throttled). Never raises — failures are logged and swallowed
    so a Redis or audit-write blip never reaches the caller."""
    await logger.ainfo(
        event_type,
        actor_user_id=actor.id,
        actor_email=actor.email,
        target_org_id=target_org_id,
        target_org_name=target_org_name,
    )
    try:
        should_persist = await _should_persist_audit(
            actor_user_id=actor.id, event_type=event_type
        )
    except Exception:  # noqa: BLE001 — defensive
        should_persist = True
    if not should_persist:
        return
    await audit_service.record_audit_event(
        session_factory,
        event_type=event_type,
        actor_user_id=actor.id,
        actor_email=actor.email,
        target_org_id=target_org_id,
        target_org_name=target_org_name,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=detail,
    )


@router.get(
    "/kpis",
    response_model=SubscriptionKPIs,
    dependencies=[Depends(require_permission("subscriptions.view"))],
)
async def get_kpis(
    db: AsyncSession = Depends(get_db),
) -> SubscriptionKPIs:
    """Pulse-strip totals (counts + per-plan distribution + mock $$).

    Deliberately not audited — KPIs are coarse-grain page-render data,
    not per-subscription drill-downs. The list and detail routes carry
    the audit signal.
    """
    payload = await admin_subscription_service.aggregate_revenue_kpis(db)
    return SubscriptionKPIs.model_validate(payload)


@router.get(
    "",
    response_model=SubscriptionListResponse,
    dependencies=[Depends(require_permission("subscriptions.view"))],
)
async def list_subscriptions(
    request: Request,
    current_user: User = Depends(require_permission("subscriptions.view")),
    status_filter: Optional[SubscriptionStatusFilter] = Query(
        default=None, alias="status"
    ),
    plan: Optional[str] = Query(default=None, max_length=80),
    q: Optional[str] = Query(default=None, max_length=120),
    sort_by: Optional[str] = Query(default=None),
    sort_dir: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> SubscriptionListResponse:
    try:
        payload = await admin_subscription_service.list_subscriptions(
            db,
            status_filter=status_filter,
            plan_filter=plan,
            q=q,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    await _emit_view_audit(
        event_type="admin.subscriptions.viewed",
        actor=current_user,
        request=request,
        session_factory=session_factory,
        detail={
            "view": "list",
            "filters": {
                "status": status_filter,
                "plan": plan,
                # Privacy: never store raw ``q`` (see admin_users.py
                # pattern). Only the length / presence flag is durable.
                "query_length": len(q) if q else 0,
                "has_query": bool(q),
                "limit": limit,
                "offset": offset,
            },
            "result_total": payload["total"],
        },
    )
    return SubscriptionListResponse.model_validate(payload)


@router.get(
    "/{subscription_id}",
    response_model=SubscriptionDetail,
    dependencies=[Depends(require_permission("subscriptions.view"))],
)
async def get_subscription_detail(
    subscription_id: int,
    request: Request,
    current_user: User = Depends(require_permission("subscriptions.view")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> SubscriptionDetail:
    try:
        payload = await admin_subscription_service.get_subscription_detail(
            db, subscription_id=subscription_id
        )
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )
    await _emit_view_audit(
        # Distinct event type from the list handler so the throttle
        # (keyed on event_type + actor) doesn't suppress this row when
        # the admin drills in within 60s of the list view. The detail
        # row carries ``target_org_id`` and must always be persisted.
        event_type="admin.subscriptions.detail.viewed",
        actor=current_user,
        request=request,
        session_factory=session_factory,
        target_org_id=payload["org"]["id"],
        target_org_name=payload["org"]["name"],
        detail={
            "view": "detail",
            "subscription_id": subscription_id,
        },
    )
    return SubscriptionDetail.model_validate(payload)
