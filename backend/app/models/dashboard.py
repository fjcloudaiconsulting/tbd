"""DashboardLayout model — persists a user's customizable dashboard state.

One layout per user (``owner_user_id`` UNIQUE). Stores the widget grid
layout (``layout_json``) and the canvas-wide filter block
(``canvas_filters_json``) as dialect-agnostic JSON columns (SQLite stores
TEXT under pytest; MySQL stores JSON in production).

Architecture decisions baked into the schema:

- ``owner_user_id`` is ``ON DELETE RESTRICT`` — matches ``reports``. User
  deletion semantics (transfer / hard-delete) are handled at the service
  layer; the DB-level RESTRICT is the safety net.
- ``org_id`` is ``ON DELETE CASCADE`` — a future org-delete pathway takes
  the row with it automatically, consistent with every other multi-tenant
  table in the repo.
- ``schema_version`` starts at 1 and provides a non-breaking upgrade path
  when the layout schema evolves (same convention as ``reports``).
- The UNIQUE constraint on ``owner_user_id`` enforces the one-layout-per-
  user invariant at the DB level (v1 design).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DashboardLayout(Base):
    __tablename__ = "dashboard_layouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_dashboard_layouts_org",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    layout_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    canvas_filters_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("owner_user_id", name="uq_dashboard_layouts_owner"),
        Index("ix_dashboard_layouts_owner", "owner_user_id"),
        Index("ix_dashboard_layouts_org", "org_id"),
    )
