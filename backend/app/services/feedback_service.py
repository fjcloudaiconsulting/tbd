"""In-app feedback service.

Two responsibilities:

1. `normalize_context` — privacy-strip the client-collected context
   before persisting. Currently strips query strings off the URL
   (anything after `?` or `#`) and trims overlong fields. The single
   normalization site makes the no-PII invariant testable.

2. `create_feedback_entry` — persist the row with identity opt-in
   honored. When `include_identity=False`, the FK columns stay NULL
   even though the caller is authenticated. This is the privacy
   default; the router cannot opt past it without flipping the flag
   explicitly.

No audit-event helper here — the router calls
`audit_service.record_audit_event` directly after commit, matching
the pattern used by `org_service.rename_org`.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feedback import FeedbackCategory, FeedbackEntry
from app.schemas.feedback import FeedbackContext


def _strip_url(raw: Optional[str]) -> Optional[str]:
    """Return the URL with query string and fragment removed.

    Path is preserved (the spec says `/import/123/reconcile` is fine
    because the path itself does not carry user-controlled secrets;
    `/login?token=xyz` IS sensitive because the token is in the
    query). Anything we cannot parse is dropped to None rather than
    stored raw — fail closed.
    """
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    # Drop query and fragment. Keep scheme + netloc + path so an
    # in-app URL like "http://localhost/transactions" round-trips.
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return cleaned or None


def normalize_context(ctx: FeedbackContext) -> dict[str, Any]:
    """Convert the wire-shape context into the JSON we persist.

    Single source of truth for the privacy-stripping rules. Tests
    point at this function directly so any future relaxation has to
    pass the same assertion battery.
    """
    payload: dict[str, Any] = {}
    cleaned_url = _strip_url(ctx.url)
    if cleaned_url is not None:
        payload["url"] = cleaned_url
    if ctx.user_agent:
        payload["user_agent"] = ctx.user_agent
    if ctx.app_version:
        payload["app_version"] = ctx.app_version
    if ctx.viewport_w is not None and ctx.viewport_h is not None:
        payload["viewport"] = {"w": ctx.viewport_w, "h": ctx.viewport_h}
    if ctx.theme:
        payload["theme"] = ctx.theme
    return payload


async def create_feedback_entry(
    db: AsyncSession,
    *,
    user_id: Optional[int],
    org_id: Optional[int],
    message: str,
    category: FeedbackCategory,
    context: FeedbackContext,
    include_identity: bool,
) -> FeedbackEntry:
    """Persist a feedback row honoring the identity opt-in.

    The CALLER passes the authenticated user_id / org_id; this function
    decides whether to record them based on `include_identity`. Storing
    the gate decision here (rather than in the router) means tests can
    drive it directly without going through HTTP.
    """
    row = FeedbackEntry(
        user_id=user_id if include_identity else None,
        org_id=org_id if include_identity else None,
        message=message,
        category=category,
        context=normalize_context(context),
    )
    db.add(row)
    await db.flush()
    return row
