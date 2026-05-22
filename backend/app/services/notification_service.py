"""Persistence + service layer for the notification substrate.

Per the 2nd-arch delta (G7 — future-queue readiness) and the parent
spec, this layer is the single home for:

- ``dispatch_notification`` — write a single notification row for a
  user. PR3 consults the user's in-app preferences for non-security
  categories before writing; ``security`` is force-on and always
  writes (architect-locked).
- ``dispatch_notification_to_org_admins`` — fanout helper for
  org-broadcast events (plan change today, role + rename + reset in
  PR4). One SELECT for the admin set, then per-user dispatch.
  Per-user failure does NOT abort the fanout — the contract is
  best-effort, log-and-continue.
- ``mark_seen`` — bell-open clears the badge for all the user's
  unseen rows. Idempotent.
- ``mark_read`` — row-click clears a single row's unread state.
  Idempotent.
- ``list_for_user`` — cursor-paginated inbox feed. Ordered newest
  first by ``(created_at, id)``.
- ``get_preferences`` — auto-creates the user's preference row on
  first access. ``email_security`` is forced TRUE on auto-create
  per the locked default.
- ``update_preferences`` — applies a typed payload. The 400 for
  ``email_security=False`` is the route's job; this function trusts
  its caller.

PR3 scope intentionally excludes:

- Email scheduling. The 5 sensitive-op routes write the in-app row
  only in PR3; the Mailgun side wires in PR5.
- ``/settings/notifications`` UI — PR5.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app._time import utcnow_naive
from app.models.notification import (
    Notification,
    NotificationCategory,
    UserNotificationPreferences,
)
from app.models.user import Role, User


logger = structlog.stdlib.get_logger()


# Page size cap mirrors the parent spec's pagination decision (G3):
# default 50, hard ceiling 100. The route layer enforces the ceiling
# by clamping the ``limit`` query parameter before it reaches here.
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100


@dataclass(slots=True)
class NotificationPage:
    """Result tuple for cursor-paginated list calls."""

    items: list[Notification]
    next_cursor: Optional[str]


# ── dispatch ──────────────────────────────────────────────────────


# In-app category → preference-column mapping. Used by
# ``_in_app_preference_allows`` to decide whether to skip a row write
# when the user has opted out of the category. ``security`` is
# intentionally absent — that category is force-on and never
# consults the preference row.
_IN_APP_PREF_FIELD: dict[NotificationCategory, str] = {
    NotificationCategory.ACCOUNT: "in_app_account",
    NotificationCategory.ORG_ADMIN: "in_app_org_admin",
    NotificationCategory.ORG_ACTIVITY: "in_app_org_activity",
}


async def _in_app_preference_allows(
    db: AsyncSession, *, user_id: int, category: NotificationCategory
) -> bool:
    """Return whether the in-app row should be written for ``user_id``.

    Always True for ``security`` — architect-locked force-on
    (see parent spec "Architect resolutions" + 2nd-arch delta
    section 4). For the other three categories, consult the user's
    preference row; ``True`` (default) means write, ``False`` means
    skip.

    A missing preference row is treated as the defaults (security +
    account + org_admin allowed, org_activity not). ``get_preferences``
    auto-creates rows on first read, but during dispatch we avoid
    inserting a preference row purely for a dispatch decision — a
    direct SELECT is cheaper and the default-allow path mirrors
    ``_default_preferences``.
    """
    if category == NotificationCategory.SECURITY:
        return True

    field = _IN_APP_PREF_FIELD.get(category)
    if field is None:
        # Defensive: a future category that forgets to register here
        # falls through as "allowed" rather than silently dropping
        # every dispatch.
        return True

    stmt = select(UserNotificationPreferences).where(
        UserNotificationPreferences.user_id == user_id
    )
    result = await db.execute(stmt)
    prefs = result.scalar_one_or_none()
    if prefs is None:
        # No preference row → defaults apply. account + org_admin
        # default-on; org_activity default-off.
        if category == NotificationCategory.ORG_ACTIVITY:
            return False
        return True
    return bool(getattr(prefs, field))


async def dispatch_notification(
    db: AsyncSession,
    *,
    user_id: int,
    category: NotificationCategory,
    event_type: str,
    title: str,
    body: str,
    link_url: Optional[str] = None,
    audit_event_id: Optional[int] = None,
) -> Optional[Notification]:
    """Write a single notification row for ``user_id``.

    Caller is responsible for commit semantics — this matches the
    parent spec's "persistence-first, dispatch-after" guardrail (the
    notification ROW write goes through the request's ``AsyncSession``
    so it commits atomically with the action that caused it).

    Preference contract (PR3):

    - ``category == security`` → ALWAYS write the row. Architect-locked
      force-on. The user cannot opt out of security signals via the
      in-app channel.
    - ``category in {account, org_admin, org_activity}`` → consult the
      user's ``in_app_{category}`` preference. If the toggle is False
      the row is NOT written; a ``notification.skipped_by_pref``
      structlog event fires so an operator can spot the skip.

    Returns the persisted ``Notification`` row, or ``None`` when the
    preference check skipped the write. Callers that need to chain
    the id (e.g. for analytics) should null-check.

    Per G8 idempotency note: callers must invoke this at most once
    per request per ``(user_id, event_type)`` pair. There is no
    DB-level dedup; a duplicate invocation produces a duplicate row.
    """
    allowed = await _in_app_preference_allows(
        db, user_id=user_id, category=category
    )
    if not allowed:
        await logger.ainfo(
            "notification.skipped_by_pref",
            user_id=user_id,
            category=category.value,
            event_type=event_type,
            audit_event_id=audit_event_id,
        )
        return None

    # Set created_at explicitly in Python rather than relying on the
    # server_default (``func.now(6)`` on MySQL, plain CURRENT_TIMESTAMP
    # on SQLite). The dialect mismatch matters for cursor pagination:
    # SQLite stores timestamps without fractional seconds when the
    # default fires, but SQLAlchemy renders bound DateTime values
    # WITH ``.000000`` microsecond padding when the same value is
    # used in a WHERE clause — leading to text comparisons that go
    # the wrong way. Setting the column from Python gives us a
    # consistent shape on both backends and guarantees that two rows
    # written in the same transaction still get monotonically
    # increasing timestamps (utcnow_naive carries microsecond
    # precision).
    row = Notification(
        user_id=user_id,
        category=category,
        event_type=event_type,
        title=title,
        body=body,
        link_url=link_url,
        audit_event_id=audit_event_id,
        created_at=utcnow_naive(),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def dispatch_notification_to_org_admins(
    db: AsyncSession,
    *,
    org_id: int,
    category: NotificationCategory,
    event_type: str,
    title: str,
    body: str,
    link_url: Optional[str] = None,
    audit_event_id: Optional[int] = None,
) -> int:
    """Fan out a notification to every active org admin of ``org_id``.

    "Org admin" today = ``Role.OWNER`` ∪ ``Role.ADMIN`` (matches
    ``auth/org_permissions.py``'s ``require_admin``). Only active
    users (``is_active=True``) receive the notification.

    Uses ONE SQL SELECT to fetch the admin set, then iterates and
    calls ``dispatch_notification`` per recipient inside a SAVEPOINT.
    Per-user failure is logged and swallowed so one user's bad row
    write doesn't kill the entire fanout (best-effort contract).
    Preference-aware writes still happen per-user: an admin who has
    opted out of ``org_admin`` in-app notifications gets skipped.

    The savepoint wrap is load-bearing: SQLAlchemy async sessions
    are "poisoned" once a flush fails inside them — any subsequent
    ORM operation or commit raises ``InvalidRequestError`` until the
    transaction is rolled back. Wrapping each per-recipient dispatch
    in ``db.begin_nested()`` scopes the failure to the savepoint,
    so the outer transaction (and the next recipient's flush, and
    the caller's eventual commit) stays clean. Without this, the
    "best-effort" log was technically swallowed but the eventual
    commit would still fail downstream.

    Returns the count of notification rows actually written (skips
    via preference and per-user failures both count against the
    total). The caller logs this count for the audit-correlation UI
    (future PR).
    """
    stmt = select(User).where(
        User.org_id == org_id,
        User.role.in_((Role.OWNER, Role.ADMIN)),
        User.is_active.is_(True),
    )
    result = await db.execute(stmt)
    admins = list(result.scalars().all())

    written = 0
    failures = 0
    for admin in admins:
        savepoint = await db.begin_nested()
        try:
            row = await dispatch_notification(
                db,
                user_id=admin.id,
                category=category,
                event_type=event_type,
                title=title,
                body=body,
                link_url=link_url,
                audit_event_id=audit_event_id,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort fanout
            # Roll the savepoint back BEFORE logging so the outer
            # transaction is clean by the time control returns to
            # the loop head. A failed flush leaves the session in
            # "rollback-required" state; without this rollback the
            # next recipient's flush would raise InvalidRequestError
            # and the caller's commit would fail too.
            await savepoint.rollback()
            await logger.awarning(
                "notification.dispatch.fanout.recipient_failed",
                org_id=org_id,
                recipient_user_id=admin.id,
                event_type=event_type,
                error=str(exc),
                error_class=type(exc).__name__,
            )
            failures += 1
            # Continue to the next admin — one user's failure must
            # not poison the rest of the broadcast.
            continue
        else:
            # Commit the savepoint; the row stays pending in the
            # outer transaction (so the caller still owns commit).
            await savepoint.commit()
            if row is not None:
                written += 1

    await logger.ainfo(
        "notification.dispatch.fanout.complete",
        org_id=org_id,
        event_type=event_type,
        admin_count=len(admins),
        rows_written=written,
        failures=failures,
    )
    return written


# ── reads ─────────────────────────────────────────────────────────


def _encode_cursor(row: Notification) -> str:
    """Server-side opaque cursor token.

    Encoded as ``"<created_at_iso>__<id>"``. Microsecond precision
    is preserved (the column is ``DATETIME(6)`` on MySQL via the
    ``func.now(6)`` server default) so two rows written in the same
    transaction still order deterministically.
    """
    return f"{row.created_at.isoformat()}__{row.id}"


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    """Parse ``"<created_at_iso>__<id>"`` back into the boundary tuple.

    Raises ``ValueError`` on malformed input. The route layer wraps
    this in a 400 so a bad cursor surfaces cleanly instead of
    bubbling up as a 500.
    """
    try:
        ts_str, id_str = cursor.rsplit("__", 1)
        ts = datetime.fromisoformat(ts_str)
        rid = int(id_str)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc
    return ts, rid


async def list_for_user(
    db: AsyncSession,
    *,
    user_id: int,
    cursor: Optional[str] = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> NotificationPage:
    """Cursor-paginated inbox feed for ``user_id``.

    Ordered newest first by ``(created_at DESC, id DESC)``. The
    cursor encodes the LAST row of the previous page; the next page
    starts at the row strictly older than that boundary. This is
    inclusive-of-(created_at, id) safe because we use a strict
    ``<`` comparison.

    Returns ``NotificationPage(items, next_cursor)``. ``next_cursor``
    is ``None`` when this page IS the last page.
    """
    effective_limit = max(1, min(limit, MAX_LIST_LIMIT))

    stmt = (
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
    )
    if cursor is not None:
        boundary_ts, boundary_id = _decode_cursor(cursor)
        # (created_at, id) < (boundary_ts, boundary_id) ordered
        # tuple comparison, expressed as the equivalent disjunction
        # so it runs on every dialect.
        stmt = stmt.where(
            or_(
                Notification.created_at < boundary_ts,
                and_(
                    Notification.created_at == boundary_ts,
                    Notification.id < boundary_id,
                ),
            )
        )

    # Pull limit+1 to detect "is there a next page". The extra row
    # is sliced off the response but its existence drives next_cursor.
    stmt = stmt.limit(effective_limit + 1)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())

    if len(rows) > effective_limit:
        items = rows[:effective_limit]
        next_cursor = _encode_cursor(items[-1])
    else:
        items = rows
        next_cursor = None
    return NotificationPage(items=items, next_cursor=next_cursor)


async def get_unseen_count(db: AsyncSession, *, user_id: int) -> int:
    """Return the number of unseen notifications for ``user_id``.

    Lightweight ``SELECT COUNT(*) ... WHERE seen_at IS NULL`` — does
    NOT load row payloads. Backs the bell badge so the count stays
    truthful even when unseen rows exceed the popover's preview page
    size (which caps at 10).
    """
    stmt = (
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id)
        .where(Notification.seen_at.is_(None))
    )
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)


# ── mark seen / mark read ─────────────────────────────────────────


async def mark_seen(db: AsyncSession, *, user_id: int) -> int:
    """Clear the bell badge for ``user_id``.

    Sets ``seen_at = NOW()`` for every unseen row owned by the user.
    Returns the number of rows touched. Idempotent — a second call
    in the same second is a no-op (the row already has ``seen_at``
    set and the WHERE clause excludes it).

    Does NOT touch ``read_at``. Per the parent spec's G1 (read vs
    seen) the two columns are distinct events.
    """
    now = utcnow_naive()
    stmt = (
        update(Notification)
        .where(Notification.user_id == user_id)
        .where(Notification.seen_at.is_(None))
        .values(seen_at=now)
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount or 0


async def mark_read(
    db: AsyncSession, *, user_id: int, notification_id: int
) -> Optional[Notification]:
    """Mark a single notification row as read.

    Returns the updated row, or ``None`` when the row does not exist
    or belongs to a different user (the route returns 404 in both
    cases to avoid leaking notification ids across users).

    Sets ``read_at`` only when it is currently NULL; a second call
    leaves the original ``read_at`` timestamp intact (idempotent).
    """
    stmt = select(Notification).where(
        Notification.id == notification_id,
        Notification.user_id == user_id,
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return None
    if row.read_at is None:
        row.read_at = utcnow_naive()
        await db.flush()
        await db.refresh(row)
    return row


# ── preferences ───────────────────────────────────────────────────


def _default_preferences(user_id: int) -> UserNotificationPreferences:
    """Construct the default preference row for a new user.

    ``email_security`` is forced TRUE — the API layer rejects writes
    that flip it OFF, but the column shape is preserved so a future
    "really opt me out" exception is a one-line change. The other
    defaults mirror the parent spec's "noisy by nature" call on
    ``org_activity``.
    """
    return UserNotificationPreferences(
        user_id=user_id,
        email_security=True,
        email_account=True,
        email_org_admin=True,
        email_org_activity=False,
        in_app_security=True,
        in_app_account=True,
        in_app_org_admin=True,
        in_app_org_activity=False,
    )


async def get_preferences(
    db: AsyncSession, *, user_id: int
) -> UserNotificationPreferences:
    """Return the user's preference row.

    Auto-creates the row on first read with the default values when
    no row exists. Commits the insert through the request's session
    so the row survives the request even though the caller doesn't
    explicitly commit (FastAPI's ``get_db`` dep commits at request
    end on success).
    """
    stmt = select(UserNotificationPreferences).where(
        UserNotificationPreferences.user_id == user_id
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is not None:
        # Force email_security = True at READ time as well. The column
        # value should always be True under normal operation (the PUT
        # endpoint rejects False), but a stale row from a future
        # migration / direct DB poke must not leak through.
        if not row.email_security:
            row.email_security = True
            await db.flush()
        return row

    row = _default_preferences(user_id)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def update_preferences(
    db: AsyncSession,
    *,
    user_id: int,
    payload,
) -> UserNotificationPreferences:
    """Apply ``payload`` to the user's preference row.

    The route handler is responsible for the 400 ``security_emails_required``
    rejection when ``payload.email_security`` is False. This function
    trusts the caller; ``email_security`` is force-coerced to True
    here as a defense-in-depth backstop in case an internal call site
    forgets the route check.

    Auto-creates the row when missing (same path as ``get_preferences``)
    so a PUT-before-GET workflow Just Works.
    """
    row = await get_preferences(db, user_id=user_id)
    row.email_security = True  # defense in depth — the API gate is the real check
    row.email_account = payload.email_account
    row.email_org_admin = payload.email_org_admin
    row.email_org_activity = payload.email_org_activity
    row.in_app_security = payload.in_app_security
    row.in_app_account = payload.in_app_account
    row.in_app_org_admin = payload.in_app_org_admin
    row.in_app_org_activity = payload.in_app_org_activity
    await db.flush()
    await db.refresh(row)
    return row
