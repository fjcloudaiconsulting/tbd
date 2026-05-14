"""Onboarding endpoints (L3.3 first-run wizard).

Two endpoints, both auth-required, both scoped to the calling user
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

  Accepts an ``empty_org_only`` query parameter (default ``true``)
  that documents caller intent for the in-app reseed flow:
   - ``true``  → caller expects the org to already be empty. This
                 is the safe path used by the onboarding wizard and
                 the Settings "Load demo data" affordance.
   - ``false`` → caller has just wiped via
                 ``POST /api/v1/orgs/data/reset`` and expects the
                 seed to succeed. The server does NOT auto-wipe.
  Either way ``seed_org`` enforces emptiness server-side; the param
  is informational and ends up in the audit ``detail`` so we can see
  which flow the user came through.

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

from app.auth.org_permissions import require_org_owner
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
    empty_org_only: bool = True,
    current_user: User = Depends(require_org_owner),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Populate the caller's org with the demo dataset (idempotent).

    Refuses with 409 ``org_has_data`` when the org already has user
    transactions, or with 409 ``demo_already_applied`` when the demo
    sentinel category is already present.

    ``empty_org_only`` is the caller-supplied intent flag — see the
    module docstring. The server enforces emptiness regardless of the
    value (no auto-wipe), so passing ``False`` only signals "I have
    already wiped" for audit forensics; it does NOT relax the seed
    contract.
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
            detail={"reason": str(exc), "empty_org_only": empty_org_only},
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
            "empty_org_only": empty_org_only,
        },
    )

    return SeedDemoResponse(
        accounts_created=result.accounts_created,
        transactions_created=result.transactions_created,
        categories_created=result.categories_created,
    )
