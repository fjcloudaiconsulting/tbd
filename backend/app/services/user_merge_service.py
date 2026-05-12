"""Merge one ``users`` row into another (recovery for duplicate accounts).

Background. An earlier version of ``POST /api/v1/auth/google/callback``
did not deduplicate by email, so a local-password user who then
signed in with Google at the same address ended up as two rows in
``users`` — one with the original ``password_hash`` and one with
``password_set=False`` and the SSO profile fields. The current
callback merges by email, but the data already on disk has dupes.

This service reassigns every ``users.id`` FK reference from the
source row to the target row and then deletes the source row. It
is **only** used by the superadmin-only
``POST /api/v1/admin/users/merge`` endpoint.

FK enumeration. All five tables carrying a ``users.id`` FK are
handled below. The list mirrors the model definitions; adding a
new FK to ``users.id`` must come with an update here. The
``CASCADE`` / ``SET NULL`` semantics on each column govern what
happens on the final ``DELETE FROM users WHERE id = source``, but
we explicitly reassign every row first so no data is lost to a
``SET NULL`` cascade and no row is destroyed by a ``CASCADE``.

The same-org constraint is intentional. Merging across orgs would
silently transfer ownership of admin permissions and audit trails
to a different tenant — that needs an org-membership-transfer
workflow we don't have yet. Out of scope here.
"""
from __future__ import annotations

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_event import AuditEvent
from app.models.feature_override import OrgFeatureOverride
from app.models.invitation import Invitation
from app.models.org_data_reset_lock import OrgDataResetLock
from app.models.tag import Tag
from app.models.user import Role, User
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


logger = structlog.stdlib.get_logger()


async def merge_users(
    db: AsyncSession,
    *,
    source_user_id: int,
    target_user_id: int,
) -> dict[str, int]:
    """Reassign every reference from ``source_user_id`` to
    ``target_user_id`` and delete the source row.

    Returns a per-table count of rows reassigned. All work runs
    inside the caller's transaction — the caller commits.

    Raises:
        ValidationError: source and target are the same id.
        NotFoundError: source or target row does not exist.
        ConflictError: source and target are in different orgs
            (cross-org merge unsupported) or source is a superadmin
            (refuse to lose the superadmin bit silently).
    """
    if source_user_id == target_user_id:
        raise ValidationError("source and target must be different users")

    source = await db.scalar(select(User).where(User.id == source_user_id))
    if source is None:
        raise NotFoundError(f"source user {source_user_id} not found")
    target = await db.scalar(select(User).where(User.id == target_user_id))
    if target is None:
        raise NotFoundError(f"target user {target_user_id} not found")

    if source.org_id != target.org_id:
        raise ConflictError(
            "cross-org merge is not supported; both users must belong to "
            "the same organization"
        )

    if source.is_superadmin and not target.is_superadmin:
        # Refusing rather than silently promoting target. If the operator
        # really wants this, flip target.is_superadmin via the admin API
        # first, then re-run the merge.
        raise ConflictError(
            "source user is a superadmin; promote target first or pick a "
            "different target"
        )

    # Last-active-owner invariant. Deleting source must not leave its
    # org without an active OWNER. Mirrors the guard
    # ``invitation_service.remove_member`` enforces at the org-member
    # endpoint level; an admin must not be able to do via merge what
    # they can't do via the regular member removal flow.
    #
    # Scope is source.org_id only. Target's org membership preserves
    # the invariant naturally when target is in the SAME org AND is
    # an active OWNER. Otherwise, after the delete, source's org is
    # ownerless — even if target happens to be an owner of a different
    # org. Refuse with 409 so the operator promotes another user
    # first.
    if source.role == Role.OWNER and source.is_active:
        other_active_owners_in_source_org = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.org_id == source.org_id,
                User.role == Role.OWNER,
                User.is_active.is_(True),
                User.id != source.id,
            )
        )
        target_preserves_invariant = (
            target.org_id == source.org_id
            and target.role == Role.OWNER
            and target.is_active
        )
        if (other_active_owners_in_source_org or 0) == 0 and not target_preserves_invariant:
            raise ConflictError(
                f"Cannot merge: source is the only active owner of org "
                f"{source.org_id}. Promote another user to owner in that "
                f"org first, then retry."
            )

    counts: dict[str, int] = {}

    # 1. audit_events.actor_user_id (SET NULL on user delete).
    # Reassign so the audit trail attributes survive the source's
    # deletion. The snapshot email column stays as-is — it pins the
    # value at event time, which is the correct historical record.
    res = await db.execute(
        update(AuditEvent)
        .where(AuditEvent.actor_user_id == source_user_id)
        .values(actor_user_id=target_user_id)
    )
    counts["audit_events"] = res.rowcount or 0

    # 2. invitations.created_by (no ondelete; would raise FK error
    # on source delete if left dangling). Reassign attribution.
    res = await db.execute(
        update(Invitation)
        .where(Invitation.created_by == source_user_id)
        .values(created_by=target_user_id)
    )
    counts["invitations"] = res.rowcount or 0

    # 3. org_feature_overrides.set_by (SET NULL). Reassign so we
    # preserve who set the override.
    res = await db.execute(
        update(OrgFeatureOverride)
        .where(OrgFeatureOverride.set_by == source_user_id)
        .values(set_by=target_user_id)
    )
    counts["org_feature_overrides"] = res.rowcount or 0

    # 4. tags.created_by_user_id (SET NULL). Reassign so the
    # creator attribution survives.
    res = await db.execute(
        update(Tag)
        .where(Tag.created_by_user_id == source_user_id)
        .values(created_by_user_id=target_user_id)
    )
    counts["tags"] = res.rowcount or 0

    # 5. org_data_reset_lock.acquired_by_user_id (CASCADE). This
    # lock is short-lived and held by exactly one user — if the
    # source row holds the lock, transferring it lets the target
    # complete or release it cleanly. Otherwise CASCADE would drop
    # the lock and silently free another concurrent caller.
    res = await db.execute(
        update(OrgDataResetLock)
        .where(OrgDataResetLock.acquired_by_user_id == source_user_id)
        .values(acquired_by_user_id=target_user_id)
    )
    counts["org_data_reset_lock"] = res.rowcount or 0

    # Carry over the email-verified bit if the source row had it
    # and target didn't — the source row in the duplicate scenario
    # is typically the SSO row (email_verified=True from Google),
    # and the target is the original local user that may have
    # never verified. Mirrors the merge-into-existing behavior of
    # the Google callback.
    if source.email_verified and not target.email_verified:
        target.email_verified = True

    # Finally delete the source row. With all references reassigned,
    # the delete succeeds without tripping any FK constraint or
    # silently nulling out an audit/tag attribution.
    await db.execute(delete(User).where(User.id == source_user_id))

    await logger.ainfo(
        "admin.user.merge",
        source_user_id=source_user_id,
        target_user_id=target_user_id,
        counts=counts,
    )

    return counts
