"""Superadmin org-member management (L4.4 slice).

Escape-hatch endpoints for editing membership of any org. Mirrors the
owner-only ``invitation_service`` member ops but with the caller's
identity decoupled from the target org (the actor's ``org_id`` is the
superadmin's home org, not necessarily ``target_org_id``).

All mutations are caller-commit; the router owns the ``db.commit()``
boundary and the audit-event write (so the audit row sits in the same
transaction as the business write for org-delete-style guarantees,
and is also re-emitted on the independent session for failure paths).

Safety guards (every mutation enforces all that apply):

- Cannot operate on yourself via this endpoint. Footgun prevention;
  superadmins still manage their own user via /me / /admin/users.
- Cannot deactivate / demote / remove the LAST OWNER of the target
  org (count includes only active owners). The org would be left
  without an owner and the org-side admin tooling would have nothing
  to recover with — that's the inverse of the escape hatch.
- Cannot remove or deactivate a superadmin via this endpoint.
  Superadmin status is platform-level; their org membership is for
  data locality, not auth, and yanking it via the org sub-resource
  is the wrong control surface (use platform user admin when L4.4-B
  ships).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app._time import utcnow_naive
from app.models.user import Organization, Role, User
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


# Roles a superadmin can assign through this endpoint. Restricted to
# the org-scoped set; platform roles (e.g. superadmin) are managed on
# a different surface.
_ASSIGNABLE_ROLES = frozenset({Role.OWNER, Role.ADMIN, Role.MEMBER})


async def _load_target_member(
    db: AsyncSession, *, org_id: int, user_id: int
) -> User:
    target = (
        await db.execute(
            select(User).where(User.id == user_id, User.org_id == org_id)
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundError("Member")
    return target


async def _active_owner_count(db: AsyncSession, *, org_id: int) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.org_id == org_id,
                User.role == Role.OWNER,
                User.is_active.is_(True),
            )
        )
    ) or 0


async def list_members(db: AsyncSession, *, org_id: int) -> list[dict]:
    """Every member of the org (active and inactive), stable order.

    Superadmin-visibility shape — includes ``is_active`` and
    ``email_verified`` so the admin UI can render correctly without a
    second roundtrip.
    """
    org_exists = await db.scalar(
        select(Organization.id).where(Organization.id == org_id)
    )
    if org_exists is None:
        raise NotFoundError("Organization")

    rows = (
        await db.execute(
            select(User).where(User.org_id == org_id).order_by(User.username)
        )
    ).scalars().all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "role": u.role.value,
            "is_active": u.is_active,
            "email_verified": u.email_verified,
            "is_superadmin": u.is_superadmin,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in rows
    ]


async def update_member(
    db: AsyncSession,
    *,
    org_id: int,
    user_id: int,
    actor: User,
    role: Optional[Role] = None,
    is_active: Optional[bool] = None,
) -> tuple[User, dict, dict, list[str]]:
    """Apply a partial update to a member of ``org_id``.

    Returns ``(target, before, after, changes)`` where ``changes`` is
    the list of fields that actually changed (so the router emits one
    audit event per real change and skips audit/log noise for no-op
    PATCHes).

    Caller commits.
    """
    if role is None and is_active is None:
        raise ValidationError("No fields to update")

    if user_id == actor.id:
        raise ValidationError("You cannot modify your own membership here")

    if role is not None and role not in _ASSIGNABLE_ROLES:
        raise ValidationError(f"Cannot assign role {role.value!r}")

    target = await _load_target_member(db, org_id=org_id, user_id=user_id)

    if target.is_superadmin:
        raise ConflictError(
            "Cannot modify a platform superadmin via org-member admin"
        )

    before = {
        "role": target.role.value,
        "is_active": target.is_active,
    }

    # Determine which fields are actually changing and run the safety
    # guards against the effective post-update state. Counting only
    # active owners excludes already-removed accounts from "last
    # owner" arithmetic (a dormant inactive owner can't recover an
    # org by themselves).
    changes: list[str] = []
    will_become_inactive = (
        is_active is False and target.is_active is True
    )
    will_be_demoted = (
        role is not None and target.role == Role.OWNER and role != Role.OWNER
    )

    if (will_become_inactive or will_be_demoted) and target.role == Role.OWNER:
        active_owners = await _active_owner_count(db, org_id=org_id)
        if active_owners <= 1:
            raise ConflictError(
                "Cannot remove the last active owner of the organization"
            )

    if role is not None and role != target.role:
        target.role = role
        changes.append("role")
    if is_active is not None and is_active != target.is_active:
        target.is_active = is_active
        if is_active is False:
            # Force a token re-issue check on next request, so the
            # deactivated user can't continue an active session.
            target.sessions_invalidated_at = utcnow_naive()
        changes.append("is_active")

    after = {
        "role": target.role.value,
        "is_active": target.is_active,
    }

    if changes:
        await db.flush()

    return target, before, after, changes


async def remove_member(
    db: AsyncSession,
    *,
    org_id: int,
    user_id: int,
    actor: User,
) -> tuple[User, dict]:
    """Soft-delete a member from ``org_id`` (mirrors the org-side
    ``invitation_service.remove_member`` semantics — sets
    ``is_active=False`` and bumps ``sessions_invalidated_at``).

    Returns ``(target, snapshot)`` where ``snapshot`` is the
    pre-removal identifying fields for the audit detail. Caller
    commits.
    """
    if user_id == actor.id:
        raise ValidationError("You cannot remove yourself")

    target = await _load_target_member(db, org_id=org_id, user_id=user_id)

    if target.is_superadmin:
        raise ConflictError(
            "Cannot remove a platform superadmin via org-member admin"
        )

    snapshot = {
        "user_id": target.id,
        "username": target.username,
        "email": target.email,
        "role": target.role.value,
        "was_active": target.is_active,
    }

    if not target.is_active:
        # Already removed — idempotent. Returning the snapshot lets
        # the router emit a no-op audit event if it wants.
        return target, snapshot

    if target.role == Role.OWNER:
        active_owners = await _active_owner_count(db, org_id=org_id)
        if active_owners <= 1:
            raise ConflictError(
                "Cannot remove the last active owner of the organization"
            )

    target.is_active = False
    target.sessions_invalidated_at = utcnow_naive()
    await db.flush()
    return target, snapshot
