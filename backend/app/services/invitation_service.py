"""Org-membership invitation lifecycle (L3.8).

Service-layer rules — keep these out of the router so they're testable in
isolation:

- Email is normalized to ``strip().lower()`` at every boundary.
- Lazy expiry cleanup: at create time, any pending row for the same
  ``(org_id, email)`` whose ``expires_at`` has passed gets ``open_email``
  nulled, freeing the unique slot. Saves a cron.
- Reactivation: if an existing soft-deleted user (``is_active=False``)
  exists in the same org, the invite is allowed and accept reactivates
  the user instead of creating a new one — preserves history.
- Member-removal guards: same-org only; cannot remove self; ADMIN cannot
  remove OWNER; cannot remove the last OWNER. Soft-delete +
  ``sessions_invalidated_at = now`` so old tokens die immediately.
"""

from __future__ import annotations

import datetime
from app._time import utcnow_naive

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import re

from app.models.invitation import Invitation
from app.models.user import Organization, Role, User
from app.schemas.auth import (
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    USERNAME_PATTERN,
)
from app.security import decode_token, hash_password
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.list_query import resolve_order_by
from app.services.user_service import normalize_email as _normalize_email_shared


# Closed sort whitelists for the org Members + Pending-invitations tables
# (settings/organization). Keys are the public sort tokens the frontend
# sends; values are the columns to order by. Limited to UI-exposed columns.
_INVITATION_SORT_COLUMNS = {
    "email": Invitation.email,
    "role": Invitation.role,
    "created_at": Invitation.created_at,
    "expires_at": Invitation.expires_at,
}
_MEMBER_SORT_COLUMNS = {
    "username": User.username,
    "email": User.email,
    "role": User.role,
}


class InvitationUnavailable(Exception):
    """Raised when a preview/accept token is invalid, revoked, accepted,
    or expired. Routers translate this to 410 Gone with a single error
    code so the client can't distinguish between cases (no leaks)."""


INVITATION_TTL = datetime.timedelta(days=7)


def _normalize_email(value: str) -> str:
    # Thin wrapper preserving the historical name used throughout this
    # module. The canonical implementation lives in user_service so
    # every user-creation site shares the same rule.
    return _normalize_email_shared(value)


async def _clear_expired_open_invites(
    db: AsyncSession, *, org_id: int, email: str
) -> None:
    """Null `open_email` on expired pending rows for this (org, email)
    so the unique slot is free for a fresh invite."""
    now = utcnow_naive()
    await db.execute(
        update(Invitation)
        .where(
            Invitation.org_id == org_id,
            Invitation.email == email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at <= now,
            Invitation.open_email.isnot(None),
        )
        .values(open_email=None)
    )


async def create_invitation(
    db: AsyncSession,
    *,
    org_id: int,
    created_by: int,
    email: str,
    role: Role,
) -> Invitation:
    """Create a pending invitation. Caller must have already verified the
    requesting user has OWNER or ADMIN role."""
    norm = _normalize_email(email)

    # Reject if a user with that email exists ACTIVE in this org. Inactive
    # same-org user → reactivation, allowed. Active user in another org
    # → reject (multi-org out of scope).
    existing = (
        await db.execute(select(User).where(User.email == norm))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.org_id == org_id and existing.is_active:
            raise ConflictError("This person is already a member of the org")
        if existing.org_id != org_id:
            raise ConflictError("Email already registered to another organization")
        # else: same-org soft-deleted → fall through to reactivation invite

    # Lazy-clear expired pending rows so they don't block a fresh invite.
    await _clear_expired_open_invites(db, org_id=org_id, email=norm)

    # Active pending invite already in place?
    pending = (
        await db.execute(
            select(Invitation).where(
                Invitation.org_id == org_id,
                Invitation.open_email == norm,
            )
        )
    ).scalar_one_or_none()
    if pending is not None:
        raise ConflictError("This email is already invited")

    now = utcnow_naive()
    inv = Invitation(
        org_id=org_id,
        email=norm,
        open_email=norm,
        role=role,
        created_by=created_by,
        expires_at=now + INVITATION_TTL,
    )
    db.add(inv)
    try:
        await db.flush()
    except IntegrityError as e:
        # Lost the race — another request committed an invite for this
        # (org, email) between our pre-check and flush. The
        # `UNIQUE(org_id, open_email)` constraint is the source of
        # truth; surface as the same 409 the pre-check would have.
        await db.rollback()
        raise ConflictError("This email is already invited") from e
    # Hydrate server-default fields (created_at) so the caller can
    # serialize the row before commit without triggering a lazy load
    # under prod's async engine.
    await db.refresh(inv)
    return inv


def _pending_invitation_filters(org_id: int):
    """The pending-invitation WHERE clauses, shared by the page query and
    the COUNT so ``total`` can never drift from ``items`` (org-scoping
    contract in ``app.services.list_query``)."""
    now = utcnow_naive()
    return (
        Invitation.org_id == org_id,
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
        Invitation.expires_at > now,
    )


async def list_pending_invitations(
    db: AsyncSession,
    *,
    org_id: int,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[Invitation]:
    """Pending = not accepted, not revoked, not expired. Lazy expiry —
    rows past `expires_at` are filtered out here even if `open_email` is
    still set.

    ``sort_by`` is resolved against the closed ``_INVITATION_SORT_COLUMNS``
    whitelist (unknown key raises ``ValidationError`` → router 400);
    default is ``created_at`` desc with ``id`` desc as a stable
    tiebreaker. ``limit``/``offset`` page the result when supplied.
    """
    order_by = resolve_order_by(
        sort_by,
        sort_dir,
        allowed=_INVITATION_SORT_COLUMNS,
        default_key="created_at",
        default_dir="desc",
        tiebreaker=Invitation.id.desc(),
    )
    stmt = (
        select(Invitation)
        .where(*_pending_invitation_filters(org_id))
        .order_by(*order_by)
    )
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def count_pending_invitations(db: AsyncSession, *, org_id: int) -> int:
    """COUNT over the same filters as ``list_pending_invitations``."""
    return (
        await db.scalar(
            select(func.count())
            .select_from(Invitation)
            .where(*_pending_invitation_filters(org_id))
        )
    ) or 0


async def revoke_invitation(
    db: AsyncSession, *, org_id: int, invitation_id: int
) -> Invitation:
    inv = (
        await db.execute(
            select(Invitation).where(
                Invitation.id == invitation_id, Invitation.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if inv is None:
        raise NotFoundError("Invitation")
    if inv.accepted_at is not None or inv.revoked_at is not None:
        # Already terminal — keep idempotent and return as-is.
        return inv
    inv.revoked_at = utcnow_naive()
    inv.open_email = None
    await db.flush()
    return inv


async def _resolve_pending(
    db: AsyncSession, *, token: str
) -> Invitation:
    """Decode the invitation token and load the live pending row, or
    raise :class:`InvitationUnavailable` (one error code, no info leak).
    """
    payload = decode_token(token)
    if payload is None or payload.get("type") != "invitation":
        raise InvitationUnavailable()
    try:
        invitation_id = int(payload["sub"])
        token_email = _normalize_email(str(payload["email"]))
    except (KeyError, ValueError, TypeError):
        raise InvitationUnavailable()

    inv = (
        await db.execute(select(Invitation).where(Invitation.id == invitation_id))
    ).scalar_one_or_none()
    if inv is None:
        raise InvitationUnavailable()
    if inv.email != token_email:
        # Token reused against a row whose email was changed — refuse.
        raise InvitationUnavailable()
    now = utcnow_naive()
    if inv.accepted_at is not None or inv.revoked_at is not None or inv.expires_at <= now:
        raise InvitationUnavailable()
    return inv


async def preview_invitation(db: AsyncSession, *, token: str) -> dict:
    """Return public-safe metadata for the accept-invite page. Raises
    :class:`InvitationUnavailable` for any non-pending state."""
    inv = await _resolve_pending(db, token=token)
    org = (
        await db.execute(
            select(Organization).where(Organization.id == inv.org_id)
        )
    ).scalar_one()
    existing = (
        await db.execute(select(User).where(User.email == inv.email))
    ).scalar_one_or_none()
    is_reactivation = (
        existing is not None
        and existing.org_id == inv.org_id
        and not existing.is_active
    )
    return {
        "org_name": org.name,
        "email": inv.email,
        "role": inv.role.value,
        "is_reactivation": is_reactivation,
        "existing_username": existing.username if is_reactivation else None,
    }


async def accept_invitation(
    db: AsyncSession,
    *,
    token: str,
    username: str,
    password: str,
) -> User:
    """Accept the invitation. Either creates a new user (and marks the
    invitation accepted) or reactivates an existing soft-deleted same-org
    user. Raises :class:`InvitationUnavailable` for any non-pending
    state, :class:`ConflictError` for username collisions on new users.
    Caller must commit the session.
    """
    inv = (
        await db.execute(
            select(Invitation)
            .where(Invitation.id == _decode_id_or_raise(token))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if inv is None:
        raise InvitationUnavailable()
    # Re-run the full pending guard inside the lock — protects against a
    # racing accept that flipped the state between decode and lock.
    payload = decode_token(token)
    if (
        payload is None
        or payload.get("type") != "invitation"
        or _normalize_email(str(payload.get("email", ""))) != inv.email
    ):
        raise InvitationUnavailable()
    now = utcnow_naive()
    if inv.accepted_at is not None or inv.revoked_at is not None or inv.expires_at <= now:
        raise InvitationUnavailable()

    existing = (
        await db.execute(select(User).where(User.email == inv.email))
    ).scalar_one_or_none()

    if existing is not None and existing.org_id == inv.org_id and not existing.is_active:
        # Reactivation: keep the row, refresh credentials, kill old
        # sessions atomically with marking the invite accepted.
        existing.is_active = True
        existing.role = inv.role
        existing.password_hash = hash_password(password)
        existing.password_changed_at = now
        existing.sessions_invalidated_at = now
        existing.email_verified = True
        inv.accepted_at = now
        inv.open_email = None
        await db.flush()
        return existing

    if existing is not None:
        # Email belongs to a foreign org or active same-org member.
        # Safety net — _create_invitation already guards this, but a
        # racing membership change could land here.
        raise InvitationUnavailable()

    # New user — enforce the strict username constraints from
    # RegisterRequest. Reactivation skips this check (existing legacy
    # usernames keep working; the user can't change it via this flow).
    if (
        len(username) < USERNAME_MIN_LENGTH
        or len(username) > USERNAME_MAX_LENGTH
        or not re.fullmatch(USERNAME_PATTERN, username)
    ):
        raise ValidationError(
            f"Username must be {USERNAME_MIN_LENGTH}-{USERNAME_MAX_LENGTH} chars "
            "and contain only letters, digits, dot, underscore, hyphen.",
        )

    # Username uniqueness is a DB constraint — let the flush raise
    # IntegrityError, then surface as ConflictError.
    user = User(
        org_id=inv.org_id,
        username=username,
        email=inv.email,
        password_hash=hash_password(password),
        role=inv.role,
        is_superadmin=False,
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    try:
        await db.flush()
    except Exception as e:  # SQLAlchemy IntegrityError or similar
        await db.rollback()
        raise ConflictError("That username is already taken") from e

    inv.accepted_at = now
    inv.open_email = None
    db.add(inv)
    await db.flush()
    return user


def _decode_id_or_raise(token: str) -> int:
    """Quick decode — full validation happens inside the lock."""
    payload = decode_token(token)
    if payload is None or payload.get("type") != "invitation":
        raise InvitationUnavailable()
    try:
        return int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        raise InvitationUnavailable()


# ── members ────────────────────────────────────────────────────────────────


async def list_members(
    db: AsyncSession,
    *,
    org_id: int,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[User]:
    """Active users in this org, deterministic order for the UI.

    ``sort_by`` is resolved against the closed ``_MEMBER_SORT_COLUMNS``
    whitelist (unknown key raises ``ValidationError`` → router 400);
    default is ``username`` asc with ``id`` asc as a stable tiebreaker.
    ``limit``/``offset`` page the result when supplied.
    """
    order_by = resolve_order_by(
        sort_by,
        sort_dir,
        allowed=_MEMBER_SORT_COLUMNS,
        default_key="username",
        default_dir="asc",
        tiebreaker=User.id.asc(),
    )
    stmt = (
        select(User)
        .where(User.org_id == org_id, User.is_active.is_(True))
        .order_by(*order_by)
    )
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def count_members(db: AsyncSession, *, org_id: int) -> int:
    """COUNT over the same filters as ``list_members``."""
    return (
        await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.org_id == org_id, User.is_active.is_(True))
        )
    ) or 0


async def remove_member(
    db: AsyncSession,
    *,
    org_id: int,
    current_user: User,
    target_user_id: int,
) -> User:
    """Soft-delete + session invalidation. Caller must commit."""
    if target_user_id == current_user.id:
        raise ConflictError("You cannot remove yourself")

    target = (
        await db.execute(
            select(User).where(User.id == target_user_id, User.org_id == org_id)
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundError("Member")
    if not target.is_active:
        # Already removed — keep idempotent.
        return target

    # ADMIN cannot remove OWNER. Only OWNER can remove OWNER.
    if target.role == Role.OWNER and current_user.role != Role.OWNER:
        raise ConflictError("Only an owner can remove another owner")

    # Cannot remove the last active OWNER, even if requester is OWNER.
    if target.role == Role.OWNER:
        active_owners = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.org_id == org_id,
                User.role == Role.OWNER,
                User.is_active.is_(True),
            )
        )
        if (active_owners or 0) <= 1:
            raise ConflictError("Cannot remove the last owner of the organization")

    target.is_active = False
    target.sessions_invalidated_at = utcnow_naive()
    await db.flush()
    return target
