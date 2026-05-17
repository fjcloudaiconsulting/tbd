"""System-level user administration (hard delete).

Companion to the read-only ``admin_users_search_service``. Hosts the
destructive system-level endpoint that hard-deletes a ``User`` row.

Membership-model note. ``users.org_id`` is a direct NOT NULL FK
to ``organizations.id`` — every user belongs to exactly one org at
all times. There is no join table. As a result, the operator
contract "remove the user when they have no org association" can
only mean "delete the User row entirely". Detaching the row from
its org without deleting is not expressible in the current schema
(would require making ``org_id`` nullable + a "no-org limbo" state
across every org-scoped query in the app). See PR body for the
architect call-out.

Hard-delete preconditions (server-enforced, every branch raises
``ConflictError`` with a stable ``code``):

- ``user_is_superadmin`` — superadmins are never deletable via
  this endpoint. Demote via direct DB intervention if genuinely
  required.
- ``user_still_active`` — target must be ``is_active=False``.
  Forces operators through the existing deactivate path first, so
  active-user deletes are impossible by mistake.
- ``user_is_self`` — actor cannot delete their own user row.

There is no ``user_has_org_memberships`` precondition in this
schema because the single-FK shape makes "has zero memberships"
unrepresentable. If a future migration introduces a join table,
add the membership-count check before the delete.

FK cleanup. ``users.id`` is referenced from seven tables. The
delete handler walks them explicitly so we never rely on implicit
``ON DELETE`` semantics for correctness:

  1. ``audit_events.actor_user_id`` — ``ON DELETE SET NULL``. The
     historical actor_email column preserves attribution. We let
     the cascade run.
  2. ``feedback_entries.user_id`` — ``ON DELETE SET NULL``. Same
     treatment.
  3. ``tags.created_by_user_id`` — ``ON DELETE SET NULL``. Same.
  4. ``org_feature_overrides.set_by`` — ``ON DELETE SET NULL``. Same.
  5. ``org_data_reset_lock.acquired_by_user_id`` — ``ON DELETE
     CASCADE``. Lock is short-lived; cascading destroys the lock
     row, which is correct (deleting the user releases any lock
     they were holding).
  6. ``invitations.created_by`` — NO ``ondelete``. We DELETE all
     rows where ``created_by = target.id`` before deleting the
     user. Invitations attributed to a deleted user have no
     remaining actor, and we never want to re-activate a pending
     invite issued by a removed account.
  7. ``import_batches.created_by_user_id`` — NO ``ondelete``. We
     DELETE all rows where ``created_by_user_id = target.id`` before
     deleting the user. The transactions an import batch produced
     remain on the org (the FK from transactions.import_batch_id
     to import_batches.id has ON DELETE SET NULL).

The 6 + 7 explicit deletes mirror the FK-ordered delete pattern
``org_data_service.wipe_org_data`` established for the org-wipe
path. See ``reference_truncate_org_scoped.md`` for the broader
"don't leave orphaned rows" rule.

Audit. Success emits ``admin.user.deleted`` via the independent
session (the User row is gone by the time the audit row is
written, so we can't stage it on the business session — same
shape as ``admin.user.merged``). Failure paths emit
``admin.user.delete.failed`` with the precondition ``code`` so
the audit row is self-explanatory.
"""
from __future__ import annotations

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.import_batch import ImportBatch
from app.models.invitation import Invitation
from app.models.user import User
from app.services.exceptions import ConflictError, NotFoundError


logger = structlog.stdlib.get_logger()


# Stable machine-readable codes the router surfaces back to the
# client. Keep these in sync with the frontend strings checked by
# the admin user detail page's delete button.
CODE_USER_IS_SUPERADMIN = "user_is_superadmin"
CODE_USER_STILL_ACTIVE = "user_still_active"
CODE_USER_IS_SELF = "user_is_self"


async def delete_user(
    db: AsyncSession,
    *,
    target_user_id: int,
    actor_user_id: int,
) -> dict:
    """Hard-delete ``target_user_id``.

    Caller commits the session on success. Raises ``NotFoundError``
    when the user does not exist; raises ``ConflictError`` with a
    populated ``code`` attribute when a precondition fails.

    Returns a small dict the router can fold into the success audit
    detail and the response body.
    """
    target = await db.get(User, target_user_id)
    if target is None:
        raise NotFoundError(f"User {target_user_id}")

    # Snapshot identifying fields BEFORE any work. Once the row is
    # deleted, attribute access on the ORM instance can lazy-load,
    # which throws MissingGreenlet under async.
    snapshot = {
        "id": target.id,
        "email": target.email,
        "username": target.username,
        "org_id": target.org_id,
        "is_active": target.is_active,
        "is_superadmin": target.is_superadmin,
    }

    if target.id == actor_user_id:
        # Self-target is a footgun, not a security issue. A
        # superadmin removing themselves while logged in would
        # destroy their own session + audit trail in one shot.
        raise ConflictError(
            "You cannot delete your own user via this endpoint",
            code=CODE_USER_IS_SELF,
        )

    if target.is_superadmin:
        raise ConflictError(
            "Cannot delete a platform superadmin via this endpoint",
            code=CODE_USER_IS_SUPERADMIN,
        )

    if target.is_active:
        raise ConflictError(
            "Target user must be deactivated before deletion. "
            "Deactivate them first via the org members page.",
            code=CODE_USER_STILL_ACTIVE,
        )

    # ── FK cleanup before the user delete ────────────────────────────
    #
    # See the module docstring for the rationale on which FKs need
    # explicit handling vs which can ride the cascade. The two
    # tables touched here both have NO ``ondelete`` on their FK,
    # so a bare DELETE FROM users would raise an FK violation if
    # any row attributes its creation to the target.

    inv_result = await db.execute(
        delete(Invitation).where(Invitation.created_by == target_user_id)
    )
    batches_result = await db.execute(
        delete(ImportBatch).where(
            ImportBatch.created_by_user_id == target_user_id
        )
    )

    fk_cleanup_counts = {
        "invitations": inv_result.rowcount or 0,
        "import_batches": batches_result.rowcount or 0,
    }

    # Finally, the user row itself. SET NULL FKs (audit_events,
    # feedback, tags, feature_overrides) and the CASCADE FK
    # (org_data_reset_lock) all run as part of the same statement.
    await db.execute(delete(User).where(User.id == target_user_id))

    await logger.ainfo(
        "admin.user.delete",
        target_user_id=target_user_id,
        target_email=snapshot["email"],
        target_org_id=snapshot["org_id"],
        actor_user_id=actor_user_id,
        fk_cleanup_counts=fk_cleanup_counts,
    )

    return {
        "snapshot": snapshot,
        "fk_cleanup_counts": fk_cleanup_counts,
    }
