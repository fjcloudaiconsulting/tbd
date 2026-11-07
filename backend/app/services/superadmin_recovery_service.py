"""Operator break-glass: grant the platform superadmin flag (TBD-443).

An install whose last active superadmin is gone cannot be administered from
inside the app: every ``/admin`` route 403s and nothing in the request path
writes ``is_superadmin=True`` after construction. This module is the one
recovery writer. It is reachable ONLY from ``scripts/promote_superadmin.py``,
which runs inside the backend container, so whoever runs it already holds the
database credentials: it adds refusals and an audit row, not a privilege.

⚠ No router, scheduler job or dependency may import this module
(``tests/auth/test_superadmin_bootstrap_predicate.py`` fences it). An HTTP
promote path would let a stolen superadmin session mint a persistent backdoor
account; that design was rejected for TBD-443.

Refusals (each a distinct ``code``): the target is inactive (deactivated,
soft-deleted or merged), has an unverified email, has no MFA enrolled, or an
active superadmin already exists and the operator did not pass
``additional=True``. A target that already holds the flag is a no-op.

The grant also writes ``sessions_invalidated_at`` and revokes every live
API token the target owns: ``is_superadmin`` is read live from the row on
every request, so without them any session the target already had (a stolen
one included) would gain the platform flag at once. Tokens need their own
revoke because ``pat.py`` deliberately skips the session cutoff, so a former
superadmin's unrevoked PAT would otherwise come back to life on re-promotion.

The audit row records ids and counts only, never the email.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app._time import utcnow_naive
from app.models.api_token import ApiToken
from app.models.user import User
from app.security import session_cutoff
from app.services.api_token_service import cancel_staged_actions
from app.services.audit_service import add_audit_event_to_session, record_audit_event
from app.services.user_service import normalize_email

PROMOTED_EVENT_TYPE = "admin.superadmin.promoted"
REASON_MAX_LENGTH = 200

CODE_INACTIVE = "target_inactive"
CODE_EMAIL_UNVERIFIED = "target_email_unverified"
CODE_MFA_NOT_ENABLED = "target_mfa_not_enabled"
CODE_SUPERADMIN_EXISTS = "active_superadmin_exists"


class PromotionRefused(Exception):
    """A precondition failed; ``code`` is one of the ``CODE_*`` constants."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def count_active_superadmins(db: AsyncSession) -> int:
    """Superadmins who can sign in. An inactive one cannot (``pat.py`` and
    ``deps`` re-check ``is_active``), so counting it would hide the outage."""
    return await db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.is_superadmin.is_(True), User.is_active.is_(True))
    ) or 0


async def find_target(
    db: AsyncSession, *, email: Optional[str] = None, user_id: Optional[int] = None
) -> Optional[User]:
    if user_id is not None:
        return await db.get(User, user_id)
    return await db.scalar(select(User).where(User.email == normalize_email(email or "")))


def refusal_code(user: User, *, active_superadmins: int, additional: bool) -> Optional[str]:
    """The first failed precondition, or ``None``. Order is the order an
    operator fixes them in: account state before the install-wide check."""
    if not user.is_active:
        return CODE_INACTIVE
    if not user.email_verified:
        return CODE_EMAIL_UNVERIFIED
    if not user.mfa_enabled:
        return CODE_MFA_NOT_ENABLED
    if active_superadmins > 0 and not additional:
        return CODE_SUPERADMIN_EXISTS
    return None


async def promote_superadmin(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: int,
    operator: str,
    reason: str,
    additional: bool,
) -> dict[str, Any]:
    """Grant the flag to ``user_id`` and commit it with its audit row.

    Returns ``{"status": "promoted" | "already_superadmin", ...}``. Raises
    ``LookupError`` when the user does not exist and ``PromotionRefused`` on a
    failed precondition, after writing a ``failure`` audit row on its own
    session. The checks re-run under a row lock, so whatever the operator
    confirmed is re-validated at write time.
    """
    async with session_factory() as db:
        # Only the target row is locked, not the superadmin set. Two
        # concurrent first recoveries can both see a count of zero and both
        # grant; that is acceptable because this path only ever RAISES the
        # count, and each grant still passes every per-target check.
        user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            raise LookupError(user_id)
        before = await count_active_superadmins(db)
        detail: dict[str, Any] = {
            "target_user_id": user.id,
            "via": "operator_cli",
            "additional": additional,
            "prior_active_superadmin_count": before,
            "reason": reason,
        }
        if user.is_superadmin and user.is_active:
            return {"status": "already_superadmin", **detail}
        org_id = user.org_id
        # An inactive superadmin lands on CODE_INACTIVE: reactivation is TBD-377.
        code = refusal_code(user, active_superadmins=before, additional=additional)
        if code is not None:
            await db.rollback()
            await record_audit_event(
                session_factory,
                event_type=PROMOTED_EVENT_TYPE,
                actor_user_id=None,
                actor_email=operator,
                target_org_id=org_id,
                target_org_name=None,
                request_id=None,
                ip_address=None,
                outcome="failure",
                detail={**detail, "code": code},
            )
            raise PromotionRefused(code)

        user.is_superadmin = True
        user.sessions_invalidated_at = session_cutoff()
        live = (ApiToken.created_by_user_id == user.id, ApiToken.revoked_at.is_(None))
        await cancel_staged_actions(db, select(ApiToken.id).where(*live))
        revoked = await db.execute(
            update(ApiToken).where(*live).values(revoked_at=utcnow_naive())
        )
        detail["revoked_api_tokens"] = revoked.rowcount or 0
        add_audit_event_to_session(
            db,
            event_type=PROMOTED_EVENT_TYPE,
            actor_user_id=None,
            actor_email=operator,
            target_org_id=org_id,
            target_org_name=None,
            request_id=None,
            ip_address=None,
            outcome="success",
            detail=detail,
        )
        await db.commit()
        return {"status": "promoted", **detail}
