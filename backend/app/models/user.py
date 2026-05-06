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
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[Role] = mapped_column(
        Enum(Role, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=Role.OWNER,
    )
    is_superadmin: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    organization: Mapped["Organization"] = relationship(back_populates="users")
