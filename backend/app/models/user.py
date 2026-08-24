import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


# Single source of truth for the avatar_url length ceiling. The DB column,
# the ProfileUpdate schema, and the Google SSO guard all import this so a
# future bump only happens in one place (plus an Alembic ALTER migration).
AVATAR_URL_MAX_LENGTH = 2048


class Role(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    billing_cycle_day: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Track E: when True, org admins can call POST /accounts/{id}/adjust-balance
    # to set an account balance directly (every adjustment still generates a
    # real, audited transaction so the trail stays intact). OFF by default —
    # this is a deliberate escape hatch from the "balance is derived from
    # transactions" invariant.
    allow_manual_balance_adjustment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0", default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    users: Mapped[list["User"]] = relationship(back_populates="organization")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id"), nullable=False
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(
        String(AVATAR_URL_MAX_LENGTH), nullable=True
    )
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    # TBD-361. An UNPROVEN claim on an address: what the user typed into the
    # email field, held here until they click the link we mail to it. The
    # live, verified ``email`` above and the user's session are untouched
    # until then, which is what stops a typo destroying the account.
    #
    # ⚠ CLEARED FOR EXACTLY SIX REASONS, and nothing else. These are REASONS,
    # not functions: 4, 5 and 6 all route through
    # ``auth._abandon_pending_email``, and reason 2 now has two endpoints
    # behind it.
    #   1. promotion            (``auth.verify_email``, on the promoting branch)
    #   2. explicit cancel      (``DELETE /users/me/pending-email``, and since
    #                            TBD-362 also
    #                            ``DELETE /admin/users/{id}/pending-email``)
    #   3. overwrite            (a later ``PUT /users/me``, last write wins --
    #                            and since TBD-362 also a later
    #                            ``POST /admin/users/{id}/email-change``)
    #   4. promote-time conflict abort (another row took the address first)
    #   5. promote-time IntegrityError backstop (lost the race between the
    #                            uniqueness SELECT and the commit; MySQL only)
    #   6. promote-time PROVENANCE abort (TBD-362): an ADMIN-INITIATED token
    #                            met a row that has since become verified, so
    #                            the claim can never legitimately promote and
    #                            is dropped rather than left armed for the
    #                            rest of its 24h TTL
    #
    # NOT cleared by token expiry, by ``reset_password``, or by deactivation.
    # A stale claim is inert: every verification-token mint site reads
    # ``email``, so nothing can resurrect it, and the guard in
    # ``verify_email`` compares the token's claim against THIS column's
    # current value -- so a superseded claim's link is refused with no
    # revocation list.
    #
    # Deliberately NOT unique and not indexed; see 080_pending_email.
    pending_email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[Role] = mapped_column(
        Enum(Role, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=Role.OWNER,
    )
    is_superadmin: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    # Founding-members program (2026-06-22). True for every user created
    # during the founder window; server_default "1" grandfathers all
    # existing rows (the pre-launch testers are the most-founding members).
    # Soft cap (1000 is a marketing number) — no gating at registration.
    is_founder: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="1", default=True
    )
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    sessions_invalidated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # False for users created via Google SSO who have not yet set a real
    # password (the SSO flow stores a random `secrets.token_urlsafe(32)`
    # hash they cannot use). Once a user calls POST /me/password the flag
    # flips True permanently and the standard "current password required"
    # check kicks in. Default True so every existing row stays on the
    # normal change-password path.
    password_set: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    # Single-use, 5-minute step-up token issued by the SSO step-up
    # callback (POST /api/v1/auth/sso-stepup/initiate → callback). The
    # email-change endpoint accepts it as an alternative to the current
    # password when `password_set` is False so SSO users can still
    # rotate their email without ever having a password to type. Token
    # is consumed (set to None) on first use; expiry is hard.
    stepup_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stepup_token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    totp_secret: Mapped[str | None] = mapped_column(String(256), nullable=True)
    recovery_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # L3.3 first-run wizard: NULL means the user has not finished the
    # onboarding flow yet; a timestamp means "completed at <ts>". The
    # frontend redirects to ``/onboarding`` while this is NULL. The
    # ``POST /api/v1/users/me/onboarding/complete`` endpoint sets it.
    # Existing rows are backfilled with ``created_at`` by migration 041.
    onboarded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    # Founding-members program: last authenticated activity, stamped
    # (throttled) by get_current_user via an independent session. Tracked
    # now; the "lose founder status after 30 days idle" rule ships with
    # payments. NULL until first stamped.
    last_active_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    organization: Mapped["Organization"] = relationship(back_populates="users")
