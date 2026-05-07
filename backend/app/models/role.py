"""Platform-level roles and the permission keys they grant (L4.8).

Today only ``superadmin`` exists, and the runtime resolver in
``app/auth/permissions.py`` short-circuits on ``User.is_superadmin``
before consulting these tables. The tables exist so that:

- The role admin UI has a place to render rows (``GET /admin/roles``).
- Future non-superadmin roles (``support``, ``operator``, ``revenue``,
  ``analyst``) become a configuration row plus a per-user assignment
  row (L4.4 ships the user-role join), not a code change to a
  hard-coded ``ROLE_PERMISSIONS`` map.

Two design choices worth restating in code:

1. **``is_system_frozen`` is a UI/router concern, not a permission.**
   The seeded ``superadmin`` row is frozen so an admin can't
   accidentally rename / unsubscribe / delete it through the
   ``/admin/roles`` UI. The router and service both check this flag
   on every write, defense in depth.
2. **``role_permissions`` rows are not authoritative for superadmin.**
   They're seeded for parity with future roles and so the UI has a
   coherent display, but ``has_permission`` still grants everything to
   ``is_superadmin=True`` users without consulting this table. Removing
   a key from ``ALL_PERMISSIONS`` therefore never locks anyone out at
   runtime; it just leaves an orphan row in ``role_permissions`` until
   the next ``set_role_permissions`` call rewrites the set. The service
   read path filters role permissions through ``ALL_PERMISSIONS`` so
   removed keys don't surface in the UI.

The class is named ``PlatformRole`` (not ``Role``) because
``app.models.user.Role`` already exists as the per-org membership
enum (``OWNER`` / ``ADMIN`` / ``MEMBER``). Conflating the two would
break every router that imports ``Role`` from ``user``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class PlatformRole(Base):
    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_roles_slug"),
    )

    # BigInteger on MySQL (forward-compat with future audit-style
    # high-cardinality joins), Integer on SQLite so the in-memory test
    # path keeps autoincrement.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    is_system_frozen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.now(6),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.now(6),
        onupdate=func.now(6),
        nullable=False,
    )

    permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_key: Mapped[str] = mapped_column(
        String(80), primary_key=True
    )

    role: Mapped["PlatformRole"] = relationship(back_populates="permissions")
