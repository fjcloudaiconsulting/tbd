"""Pydantic schemas for the notification substrate.

Architect-locked rules baked into this module (specs
``2026-05-21-notification-system-sensitive-ops.md`` +
``2026-05-22-notification-system-2nd-arch-pass.md``):

- ``NotificationPreferencesUpdate.email_security`` is a plain
  ``bool``. The 400 rejection for ``email_security=False`` lives in
  the route handler, NOT in a field validator. A field validator
  raising ``ValueError`` is caught by FastAPI's request-validation
  layer and produces a 422, never the 400
  ``{code: "security_emails_required", ...}`` envelope the frontend
  keys off. See 2nd-arch delta section 4.
- Notification read shape mirrors the DB row; ``link_url`` and
  ``audit_event_id`` are nullable.
- Cursor pagination response carries ``items`` + ``next_cursor``.
  Cursor opaque to the client; encoded server-side as
  ``"<created_at_iso>__<id>"``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.models.notification import NotificationCategory


class NotificationResponse(BaseModel):
    """Single-row read shape returned by GET /notifications and the
    mark-read PATCH endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    category: NotificationCategory
    event_type: str
    title: str
    body: str
    link_url: Optional[str] = None
    seen_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    audit_event_id: Optional[int] = None
    created_at: datetime


class NotificationListResponse(BaseModel):
    """Cursor-paginated list shape for GET /notifications.

    ``next_cursor`` is ``None`` when the page is the last page.
    Otherwise it is an opaque token the client passes back as the
    ``cursor`` query parameter on the next request. The encoding is
    ``"<created_at_iso>__<id>"`` — server-side only; clients must
    treat it as opaque.
    """

    items: list[NotificationResponse]
    next_cursor: Optional[str] = None


class NotificationPreferencesResponse(BaseModel):
    """Full preference shape for the current user."""

    model_config = ConfigDict(from_attributes=True)

    email_security: bool
    email_account: bool
    email_org_admin: bool
    email_org_activity: bool
    in_app_security: bool
    in_app_account: bool
    in_app_org_admin: bool
    in_app_org_activity: bool


class NotificationPreferencesUpdate(BaseModel):
    """Update payload for PUT /notifications/preferences.

    ``email_security`` is a plain ``bool`` — NOT a constrained type
    or a validated field. The 400 rejection for ``email_security=False``
    lives in the route handler. See module docstring + 2nd-arch
    delta section 4 for the reasoning.
    """

    model_config = ConfigDict(extra="forbid")

    email_security: bool
    email_account: bool
    email_org_admin: bool
    email_org_activity: bool
    in_app_security: bool
    in_app_account: bool
    in_app_org_admin: bool
    in_app_org_activity: bool
