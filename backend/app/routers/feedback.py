"""In-app feedback router.

Mounted at ``/api/v1/feedback``. Single POST endpoint — submission.
Admin read endpoints are out of scope for v1 (separate L4.x slice
when prioritized).

Rate limit: 5/hour per client IP. The K8S-1 Redis-backed slowapi
storage (PR #245) makes this cross-replica accurate; before that,
each replica enforced its own private bucket. 5/hour is loose enough
that real users won't trip it but tight enough to swallow accidental
spam (browser-extension submission loops, etc.).

Audit: a successful submission writes a `feedback.submitted` event
on an independent session via `record_audit_event`. The audit detail
carries the category and whether identity was attached, but NOT the
message body — the audit table is a who-did-what trail, not a
feedback archive.
"""
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip, limiter
from app.schemas.feedback import FeedbackCreate, FeedbackResponse
from app.services import audit_service, feedback_service


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/feedback", tags=["feedback"])


def _request_id() -> Optional[str]:
    return structlog.contextvars.get_contextvars().get("request_id")


@router.post("", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/hour")
async def submit_feedback(
    body: FeedbackCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Submit a feedback entry. Auth required.

    Identity is stored ONLY when `body.include_identity` is True. The
    default is False, which is the documented privacy posture.
    """
    # Snapshot identity-shaping fields before any await so they remain
    # valid even if `current_user` is later detached by a rollback.
    actor_user_id = current_user.id
    actor_org_id = current_user.org_id
    actor_email = current_user.email

    entry = await feedback_service.create_feedback_entry(
        db,
        user_id=actor_user_id,
        org_id=actor_org_id,
        message=body.message,
        category=body.category,
        context=body.context,
        include_identity=body.include_identity,
    )
    await db.commit()
    await db.refresh(entry)

    await audit_service.record_audit_event(
        session_factory,
        event_type="feedback.submitted",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=actor_org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "category": body.category.value,
            # Whether identity was attached on the row. The audit
            # trail itself always carries the actor — that's the
            # whole point of an audit log — but knowing that the
            # FEEDBACK row is anonymous is useful for triage.
            "identity_attached": body.include_identity,
            "message_length": len(body.message),
        },
    )

    await logger.ainfo(
        "feedback.submitted",
        category=body.category.value,
        identity_attached=body.include_identity,
        message_length=len(body.message),
    )

    return entry
