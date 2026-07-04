"""Customer-facing notification substrate router (specs
``2026-05-21-notification-system-sensitive-ops.md`` +
``2026-05-22-notification-system-2nd-arch-pass.md``).

Mounted at ``/api/v1/notifications``. Six endpoints:

- ``GET /notifications`` — cursor-paginated inbox feed for the
  current user. Newest first.
- ``GET /notifications/unseen-count`` — lightweight ``{count: int}``
  for the bell badge. ``SELECT COUNT(*) WHERE seen_at IS NULL``;
  does not load row payloads. The bell polls this on its 60s
  cadence so the badge stays truthful even when unseen > the
  popover's display limit.
- ``POST /notifications/mark-seen`` — clears the unseen-count badge
  for the bell icon. Idempotent.
- ``PATCH /notifications/{id}`` — marks a single row as read. Returns
  the updated row. Cross-user access returns 404 (does not leak ids).
- ``GET /notifications/preferences`` — current user's preferences,
  auto-creating the row on first read with the locked defaults.
- ``PUT /notifications/preferences`` — replaces every preference
  toggle. Rejects ``email_security=false`` with
  ``400 {"code": "security_emails_required", ...}`` — the check
  lives here (NOT in a Pydantic validator) per 2nd-arch delta
  section 4, because a body-model validator would surface as 422
  and miss the custom envelope.
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.notification import (
    NotificationListResponse,
    NotificationPreferencesResponse,
    NotificationPreferencesUpdate,
    NotificationResponse,
    NotificationUnseenCountResponse,
)
from app.services import notification_service


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(
        default=notification_service.DEFAULT_LIST_LIMIT,
        ge=1,
        le=notification_service.MAX_LIST_LIMIT,
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cursor-paginated inbox feed for the current user."""
    try:
        page = await notification_service.list_for_user(
            db,
            user_id=current_user.id,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as exc:
        # Malformed cursor — surface as a 400 with a structured code
        # so the FE can clear its stored cursor and retry.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_cursor",
                "message": "Notification cursor is malformed.",
            },
        ) from exc
    return NotificationListResponse(
        items=[NotificationResponse.model_validate(row) for row in page.items],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/unseen-count",
    response_model=NotificationUnseenCountResponse,
)
async def get_unseen_count(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the count of unseen notifications for the current user.

    Lightweight ``SELECT COUNT(*) WHERE seen_at IS NULL`` — does NOT
    load row payloads. The bell badge polls this on its 60s cadence
    so the displayed count stays truthful even when the unseen total
    exceeds the popover's preview page size.

    Returns the raw count. The bell caps the rendered label at
    ``99+`` client-side; the wire payload is uncapped so a future
    "show 250" tweak is frontend-only.
    """
    count = await notification_service.get_unseen_count(
        db, user_id=current_user.id
    )
    return NotificationUnseenCountResponse(count=count)


@router.post(
    "/mark-seen",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def mark_all_seen(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bell-open: clear the unseen-count badge for the current user.

    Sets ``seen_at = NOW()`` on every unseen row owned by the user.
    Idempotent — a second call within the same second touches zero
    rows. Does NOT change ``read_at``; the inbox list's unread state
    is unaffected.
    """
    await notification_service.mark_seen(db, user_id=current_user.id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{notification_id}",
    response_model=NotificationResponse,
)
async def mark_one_read(
    notification_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a single notification row as read.

    Cross-user access (row owned by a different user) returns 404 so
    the response does not leak the existence of ids the caller does
    not own. A row that is already read is left untouched (the
    original ``read_at`` timestamp is preserved); the response shape
    is still the row.
    """
    row = await notification_service.mark_read(
        db, user_id=current_user.id, notification_id=notification_id
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )
    await db.commit()
    return NotificationResponse.model_validate(row)


@router.get(
    "/preferences",
    response_model=NotificationPreferencesResponse,
)
async def get_my_preferences(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current user's preferences.

    Auto-creates the row on first read with the locked defaults
    (security + account + org_admin = True, org_activity = True).
    The auto-create commits before returning so a subsequent GET hits
    the persisted row rather than re-creating.
    """
    row = await notification_service.get_preferences(db, user_id=current_user.id)
    await db.commit()
    return NotificationPreferencesResponse.model_validate(row)


@router.put(
    "/preferences",
    response_model=NotificationPreferencesResponse,
)
async def update_my_preferences(
    body: NotificationPreferencesUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace every preference toggle for the current user.

    Rejects ``email_security=False`` with
    ``400 {"code": "security_emails_required", ...}`` per the locked
    behavior. The check lives in the route handler (NOT in a Pydantic
    validator) because a body-model validator raising ``ValueError``
    surfaces as a default 422 ``RequestValidationError``, never the
    custom 400 envelope. Match the auth.py:241-247 envelope shape
    verbatim.
    """
    if body.email_security is False:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "security_emails_required",
                "message": "Security notifications cannot be disabled.",
            },
        )
    row = await notification_service.update_preferences(
        db, user_id=current_user.id, payload=body
    )
    await db.commit()
    return NotificationPreferencesResponse.model_validate(row)
