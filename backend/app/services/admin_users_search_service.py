"""Cross-org user search service (L4.4 slice).

Backs the admin ``/api/v1/admin/users`` list and detail surface that
lets a superadmin discover a user across the whole platform regardless
of their org. Sits alongside ``user_merge_service`` (the recovery
endpoint) but stays read-only. No mutating ops live here.

Privacy: this service does NOT log raw search input. Callers (router)
should log ``query_length`` / ``result_count`` only, never ``q``. This
mirrors the description-suggestions contract (Wave 2A section 5.4).

Shape decisions:

- Search semantics: ``q`` is matched case-insensitively as a prefix
  against ``email`` / ``username`` and as a substring against the
  composed ``first_name + last_name`` display name. Prefix-then-
  substring keeps the typical "I have an email or handle" case fast
  without ruling out "I half-remember the name".
- A user has exactly one org today (``users.org_id`` is single-FK),
  so the returned ``orgs`` field is a single-element array. The shape
  is pre-multi-org so the frontend can fan out later without a schema
  break.
- Recent audit events: filtered by ``actor_user_id`` (the events this
  user performed). ``audit_events`` has no ``target_user_id`` column,
  so we don't attempt the "events about this user" cut here. That is
  an L4.7 expansion of the audit schema.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.services.exceptions import NotFoundError
from app.services.list_query import resolve_order_by


# Status options the router exposes. Matches the User flags we cut on.
_STATUS_VALUES = frozenset({"active", "inactive", "unverified", "superadmin"})

# Closed whitelist of sortable columns for the admin users list. Keys are
# the public sort tokens the frontend sends; values are the column to order
# by. ``org_name`` sorts on the joined Organization.name. Anything not here
# is a 400 (see ``list_query.resolve_order_by``).
_SORTABLE = {
    "created_at": User.created_at,
    "email": User.email,
    "username": User.username,
    # role sorts lexically by the stored enum value (admin/member/owner),
    # NOT by privilege hierarchy — intentional, not a bug.
    "role": User.role,
    # NULL ordering for org_name follows the DB default (NULLs last on
    # MySQL, NULLs first on PostgreSQL/SQLite). Acceptable today because
    # org_id is always set (non-nullable FK); add nullslast() here if a
    # nullable org column is ever added to this sort surface.
    "org_name": Organization.name,
}


def _normalize_like(q: str) -> str:
    """Escape LIKE metacharacters so a raw user query can't widen the match.

    Same shape as ``transaction_suggestions_service._normalize_prefix``.
    """
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _compose_display_name(user: User) -> Optional[str]:
    parts = [p for p in (user.first_name, user.last_name) if p]
    return " ".join(parts) if parts else None


def _serialize_org(org: Organization, role: Role) -> dict:
    return {
        "org_id": org.id,
        "name": org.name,
        "role": role.value,
    }


def _serialize_user_row(user: User, org: Optional[Organization]) -> dict:
    """Common list-row payload. Caller embeds ``orgs`` as a list."""
    return {
        "id": user.id,
        "email": user.email,
        "username": user.username,
        "display_name": _compose_display_name(user),
        "is_superadmin": user.is_superadmin,
        "is_active": user.is_active,
        "email_verified": user.email_verified,
        "mfa_enabled": user.mfa_enabled,
        "password_changed_at": (
            user.password_changed_at.isoformat()
            if user.password_changed_at else None
        ),
        "onboarded_at": (
            user.onboarded_at.isoformat() if user.onboarded_at else None
        ),
        "created_at": (
            user.created_at.isoformat() if user.created_at else None
        ),
        "orgs": [_serialize_org(org, user.role)] if org is not None else [],
    }


async def list_users(
    db: AsyncSession,
    *,
    q: Optional[str] = None,
    org_filter: Optional[int] = None,
    role_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Paginated cross-org user list for the admin table.

    ``q`` matches case-insensitively as prefix against ``email`` /
    ``username`` and substring against ``first_name || ' ' || last_name``.

    ``org_filter`` narrows to a single org id (drives the "user in org X"
    drill-in from the orgs page once it lands). ``role_filter`` and
    ``status_filter`` cut against the user's role/active/verified flags.

    ``sort_by`` is resolved against a closed whitelist (see ``_SORTABLE``);
    an unknown key raises ``ValidationError`` (router → 400). When omitted,
    defaults to ``created_at`` desc. ``id`` desc is always appended as a
    stable tiebreaker so pagination is deterministic.

    Returns ``{items, total, limit, offset}`` for direct JSON dump.
    """
    base = select(User, Organization).outerjoin(
        Organization, Organization.id == User.org_id
    )
    count_base = select(func.count()).select_from(User)

    where_clauses = []

    if q:
        q_lower = q.lower()
        like_prefix_lower = _normalize_like(q_lower) + "%"
        like_substr_lower = "%" + _normalize_like(q_lower) + "%"
        # MySQL utf8mb4_0900_ai_ci is case-insensitive; SQLite LIKE is
        # case-insensitive for ASCII. Lowering both sides keeps locale
        # quirks (e.g. the email-as-CS column added by migration 040)
        # from leaking case sensitivity into the search.
        where_clauses.append(
            or_(
                func.lower(User.email).like(like_prefix_lower, escape="\\"),
                func.lower(User.username).like(like_prefix_lower, escape="\\"),
                func.lower(
                    func.coalesce(User.first_name, "")
                    + " "
                    + func.coalesce(User.last_name, "")
                ).like(like_substr_lower, escape="\\"),
            )
        )

    if org_filter is not None:
        where_clauses.append(User.org_id == org_filter)

    if role_filter:
        try:
            role_enum = Role(role_filter)
        except ValueError:
            # Unknown role: return zero rows rather than 500.
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        where_clauses.append(User.role == role_enum)

    if status_filter:
        status_norm = status_filter.lower()
        if status_norm not in _STATUS_VALUES:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        if status_norm == "active":
            where_clauses.append(User.is_active.is_(True))
        elif status_norm == "inactive":
            where_clauses.append(User.is_active.is_(False))
        elif status_norm == "unverified":
            where_clauses.append(User.email_verified.is_(False))
        elif status_norm == "superadmin":
            where_clauses.append(User.is_superadmin.is_(True))

    for clause in where_clauses:
        base = base.where(clause)
        count_base = count_base.where(clause)

    total = (await db.scalar(count_base)) or 0

    order_by = resolve_order_by(
        sort_by,
        sort_dir,
        allowed=_SORTABLE,
        default_key="created_at",
        default_dir="desc",
        tiebreaker=User.id.desc(),
    )

    rows = (
        await db.execute(
            base.order_by(*order_by).limit(limit).offset(offset)
        )
    ).all()

    items = [_serialize_user_row(user, org) for user, org in rows]

    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def get_user_detail(
    db: AsyncSession,
    *,
    user_id: int,
    audit_limit: int = 10,
) -> dict:
    """Full user payload + org memberships + recent audit events.

    Raises ``NotFoundError`` if the user id doesn't exist.
    """
    row = (
        await db.execute(
            select(User, Organization)
            .outerjoin(Organization, Organization.id == User.org_id)
            .where(User.id == user_id)
        )
    ).first()
    if row is None:
        raise NotFoundError(f"User {user_id}")
    user, org = row

    payload = _serialize_user_row(user, org)
    # Detail-only fields (kept off the list payload to stay lean).
    payload["password_set"] = user.password_set
    payload["sessions_invalidated_at"] = (
        user.sessions_invalidated_at.isoformat()
        if user.sessions_invalidated_at else None
    )
    payload["phone"] = user.phone

    # Recent audit events authored by this user. Stable ordering by
    # ``created_at DESC, id DESC`` mirrors ``list_audit_events``.
    events_result = await db.execute(
        select(AuditEvent)
        .where(AuditEvent.actor_user_id == user_id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(audit_limit)
    )
    audit_rows = list(events_result.scalars().all())
    payload["recent_audit_events"] = [
        {
            "id": row.id,
            "event_type": row.event_type,
            "outcome": row.outcome.value,
            "target_org_id": row.target_org_id,
            "target_org_name": row.target_org_name,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in audit_rows
    ]

    return payload
