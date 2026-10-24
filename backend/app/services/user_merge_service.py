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

FK enumeration (BE-29). Every ``users.id`` FK is either moved to the
target (``REASSIGNED``, plus ``dashboard_layouts`` handled on its own)
or deliberately left to its ``ON DELETE`` (``LEFT_TO_ONDELETE``, each
with its reason). A new FK to ``users.id`` must be added to one of them:
``tests/services/test_user_merge_service.py`` compares both against the
model metadata and fails otherwise. Reassigning first means no data is
lost to a ``SET NULL`` or ``CASCADE``, and a ``RESTRICT`` or plain FK
never blocks the final ``DELETE FROM users WHERE id = source``.

The same-org constraint is intentional. Merging across orgs would
silently transfer ownership of admin permissions and audit trails
to a different tenant — that needs an org-membership-transfer
workflow we don't have yet. Out of scope here.
"""
from __future__ import annotations

import logging
import secrets
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.announcement import Announcement
from app.models.audit_event import AuditEvent
from app.models.dashboard import DashboardLayout
from app.models.email_broadcast import EmailBroadcast
from app.models.feature_override import OrgFeatureOverride
from app.models.feedback import FeedbackEntry
from app.models.import_batch import ImportBatch
from app.models.limit_override import OrgLimitOverride
from app.models.invitation import Invitation
from app.models.notification import Notification
from app.models.org_ai_consent import OrgAIConsent
from app.models.org_data_reset_lock import OrgDataResetLock
from app.models.rate_limit_override import RateLimitOverride
from app.models.report import Report
from app.models.scenario import Scenario
from app.models.tag import Tag
from app.models.user import Role, User
from app.security import hash_password
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


logger = logging.getLogger(__name__)


# (column, key in the returned counts). Each moves from source to target.
REASSIGNED = (
    # audit_events.actor_user_id (SET NULL): the audit trail keeps its actor;
    # the snapshot email stays as recorded at event time.
    (AuditEvent.actor_user_id, "audit_events"),
    # invitations.created_by (no ondelete): would block the delete.
    (Invitation.created_by, "invitations"),
    # Override attribution (SET NULL): keep who set it (TBD-585).
    (OrgFeatureOverride.set_by, "org_feature_overrides"),
    (OrgLimitOverride.set_by, "org_limit_overrides"),
    (Tag.created_by_user_id, "tags"),
    # org_data_reset_locks (CASCADE): a lock held by source passes to target,
    # so it can be completed or released instead of silently freed.
    (OrgDataResetLock.acquired_by_user_id, "org_data_reset_lock"),
    # import_batches.created_by_user_id (no ondelete) and reports
    # (RESTRICT): both would block the delete.
    (ImportBatch.created_by_user_id, "import_batches"),
    (Report.owner_user_id, "reports"),
    # The same person's own data (CASCADE would destroy it).
    (Scenario.user_id, "scenarios"),
    (Notification.user_id, "notifications"),
    # Attribution (SET NULL).
    (FeedbackEntry.user_id, "feedback_entries"),
    (Announcement.created_by_user_id, "announcements"),
    (EmailBroadcast.created_by_user_id, "email_broadcasts"),
    (OrgAIConsent.consented_by_user_id, "org_ai_consents"),
    (RateLimitOverride.created_by_user_id, "rate_limit_overrides"),
)

# FKs whose ON DELETE is the intended merge outcome.
LEFT_TO_ONDELETE: dict[str, str] = {
    "agent_pending_actions.user_id": (
        "CASCADE: proposals have a 10 minute confirm window and are purged after 48h"
    ),
    "api_tokens.created_by_user_id": (
        "SET NULL: a token never moves to another identity; without an owner "
        "it fails to authenticate"
    ),
    "email_broadcast_recipients.user_id": (
        "SET NULL: delivery history keeps its snapshot email, and "
        "(broadcast_id, user_id) is unique"
    ),
    "rate_limit_overrides.user_id": (
        "CASCADE: a per-user limit is not granted to another identity silently"
    ),
    "user_dismissed_announcements.user_id": (
        "CASCADE: one row per (user, announcement); the target keeps its own"
    ),
    "user_notification_preferences.user_id": (
        "CASCADE: one row per user; the target keeps its own preferences"
    ),
}


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

    for column, key in REASSIGNED:
        res = await db.execute(
            update(column.class_)
            .where(column == source_user_id)
            .values({column.key: target_user_id})
        )
        counts[key] = res.rowcount or 0

    # dashboard_layouts.owner_user_id (RESTRICT, one per user): the target
    # keeps its own layout when it has one, else takes over the source's.
    target_has_layout = await db.scalar(
        select(func.count())
        .select_from(DashboardLayout)
        .where(DashboardLayout.owner_user_id == target_user_id)
    )
    if target_has_layout:
        res = await db.execute(
            delete(DashboardLayout).where(
                DashboardLayout.owner_user_id == source_user_id
            )
        )
    else:
        res = await db.execute(
            update(DashboardLayout)
            .where(DashboardLayout.owner_user_id == source_user_id)
            .values(owner_user_id=target_user_id)
        )
    counts["dashboard_layouts"] = res.rowcount or 0

    # Carry over the email-verified bit if the source row had it
    # and target didn't — the source row in the duplicate scenario
    # is typically the SSO row (email_verified=True from Google),
    # and the target is the original local user that may have
    # never verified. Mirrors the merge-into-existing behavior of
    # the Google callback.
    if source.email_verified and not target.email_verified:
        target.email_verified = True
        # TBD-651: nobody proved the target's password belongs to the email
        # owner, so verifying the row must not keep it (same as Google link).
        target.password_hash = hash_password(secrets.token_urlsafe(32))
        target.password_set = False

    # Finally delete the source row. With every reference reassigned or
    # left to its ON DELETE on purpose (LEFT_TO_ONDELETE), the delete
    # trips no FK constraint and nulls no attribution by accident.
    await db.execute(delete(User).where(User.id == source_user_id))

    logger.info(
        "admin.user.merge",
        extra={
            "source_user_id": source_user_id,
            "target_user_id": target_user_id,
            "counts": counts,
        },
    )

    return counts
