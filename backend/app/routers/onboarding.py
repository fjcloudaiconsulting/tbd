"""Onboarding endpoints (L3.3 first-run wizard).

Three endpoints, all auth-required, all scoped to the calling user
and their org:

- ``POST /api/v1/users/me/onboarding/complete``
  Sets ``users.onboarded_at`` to now (UTC). Idempotent — calling again
  re-stamps the timestamp but the frontend will not redirect to
  ``/onboarding`` once it is set.

- ``POST /api/v1/users/me/onboarding/seed-demo``
  Runs ``demo_seed_service.seed_org`` against the caller's org.
  Refuses with 409 ``org_has_data`` when the org already has user
  transactions OR the demo sentinel category is already present.
  Audit-logged on success and on refusal.

- ``POST /api/v1/users/me/onboarding/restart-tour``
  Records that the user requested to replay the dashboard tour
  overlay. Server-side this is an audit-only, rate-limited action:
  ``users.onboarded_at`` is intentionally **not** touched, because
  AppShell guards on a NULL ``onboarded_at`` to bounce first-run
  users to ``/onboarding`` — clearing it here would trap the user in
  a redirect loop. The frontend triggers the dashboard tour overlay
  via a sessionStorage flag and a ``/dashboard`` push; this endpoint
  is the recorded, rate-limited server side of that action. A full
  wizard restart is a separate explicit action handled outside this
  endpoint. Idempotent. Per-user. Audit event
  ``onboarding.tour.restarted`` on every call.

Org isolation: the service receives ``current_user.org_id`` directly;
no path or body parameter can override which org gets seeded.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip, limiter
from app.services import audit_service
from app.services.demo_seed_service import (
    DemoSeedAlreadyApplied,
    seed_org,
)


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/users/me/onboarding", tags=["onboarding"])


class OnboardingCompleteResponse(BaseModel):
    onboarded_at: str


class SeedDemoResponse(BaseModel):
    accounts_created: int
    transactions_created: int
    categories_created: int


class RestartTourResponse(BaseModel):
    onboarded_at: Optional[str]


def _request_id() -> Optional[str]:
    return structlog.contextvars.get_contextvars().get("request_id")


@router.post("/complete", response_model=OnboardingCompleteResponse)
@limiter.limit("10/hour")
async def complete_onboarding(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark the caller as having finished the first-run wizard.

    Idempotent: re-stamps ``onboarded_at`` if called twice. The
    frontend treats any non-NULL value as "done" and stops bouncing
    the user to ``/onboarding``.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Re-fetch the user via the request-scoped session so the mutation
    # rides on the session that actually commits. `current_user` may
    # come from a different session in tests (and in any future
    # auth-dependency that lifts the user out of band).
    user = (
        await db.execute(select(User).where(User.id == current_user.id))
    ).scalar_one()
    user.onboarded_at = now
    await db.commit()
    return OnboardingCompleteResponse(onboarded_at=now.isoformat())


@router.post("/seed-demo", response_model=SeedDemoResponse)
@limiter.limit("3/hour")
async def seed_demo(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Populate the caller's org with the demo dataset (idempotent).

    Refuses with 409 ``org_has_data`` when the org already has user
    transactions, or with 409 ``demo_already_applied`` when the demo
    sentinel category is already present.
    """
    # Snapshot the audit-relevant fields BEFORE any commit or rollback.
    # SQLAlchemy expires ORM attributes after commit / rollback by default
    # and the audit emit happens AFTER that boundary. Touching
    # ``current_user.id`` on the failure path would otherwise trigger a
    # lazy reload that crashes with MissingGreenlet because the request
    # session is no longer green-thread safe by that point.
    actor_user_id = current_user.id
    actor_email = current_user.email
    org_id = current_user.org_id

    try:
        result = await seed_org(db, org_id)
        await db.commit()
    except DemoSeedAlreadyApplied as exc:
        await db.rollback()
        # Audit the refusal so a curious admin can see "user tried to
        # seed and we said no" without blocking the user with a 5xx.
        await audit_service.record_audit_event(
            session_factory,
            event_type="onboarding.seed_demo.refused",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=org_id,
            target_org_name=None,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="failure",
            detail={"reason": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="org_has_data",
        )

    await audit_service.record_audit_event(
        session_factory,
        event_type="onboarding.seed_demo.applied",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "accounts": result.accounts_created,
            "transactions": result.transactions_created,
            "categories": result.categories_created,
        },
    )

    return SeedDemoResponse(
        accounts_created=result.accounts_created,
        transactions_created=result.transactions_created,
        categories_created=result.categories_created,
    )


@router.post("/restart-tour", response_model=RestartTourResponse)
@limiter.limit("10/hour")
async def restart_tour(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Record a dashboard-tour replay request without mutating state.

    Replaying the dashboard tour is a client-side overlay that the
    frontend triggers via sessionStorage; the server's role is to
    audit and rate-limit the user action. ``users.onboarded_at`` is
    deliberately left unchanged — AppShell redirects authenticated
    users with ``onboarded_at IS NULL`` to ``/onboarding``, so
    clearing it here would trap the user in a redirect loop instead
    of letting them see the dashboard tour overlay.

    Idempotent: calling twice leaves ``onboarded_at`` untouched (be
    that NULL or a prior timestamp) and audits both calls. Per-user.
    """
    # Pre-snapshot the audit fields so a lazy reload after commit
    # cannot crash with MissingGreenlet. Same pattern as seed_demo.
    actor_user_id = current_user.id
    actor_email = current_user.email
    org_id = current_user.org_id

    # Re-fetch through the request-scoped session so we read the
    # canonical value the rest of the request will see, even when
    # `current_user` was hydrated from a different session in tests.
    user = (
        await db.execute(select(User).where(User.id == current_user.id))
    ).scalar_one()
    onboarded_at = user.onboarded_at

    await audit_service.record_audit_event(
        session_factory,
        event_type="onboarding.tour.restarted",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=None,
    )

    return RestartTourResponse(
        onboarded_at=onboarded_at.isoformat() if onboarded_at else None,
    )
