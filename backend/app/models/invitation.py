from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.user import Organization, Role, User


class Invitation(Base):
    """Pending org-membership invitations issued by OWNER/ADMIN.

    `open_email` carries the normalized email iff the row is the live
    pending invite for `(org_id, email)`. Accept / revoke / lazy expiry
    cleanup all set `open_email = NULL`. The unique key
    `(org_id, open_email)` enforces "one open invite per (org, email)"
    at the DB level — MySQL allows multiple NULLs in a unique index, so
    historical rows don't collide with new pendings.
    """

    __tablename__ = "invitations"
    __table_args__ = (
        UniqueConstraint("org_id", "open_email", name="uq_invitations_open"),
        Index("ix_invitations_org_email", "org_id", "email"),
        Index(
            "ix_invitations_status",
            "org_id", "accepted_at", "revoked_at", "expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[Role] = mapped_column(
        Enum(Role, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    open_email: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_by: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    organization: Mapped["Organization"] = relationship()
    inviter: Mapped["User"] = relationship(foreign_keys=[created_by])
