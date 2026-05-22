"""Reports v2 model — owner-authored, optionally org-shared reports.

Substrate for ``specs/2026-05-22-reports-v2-flexible-canvas.md``. The
table stores the canvas layout (a JSON document) and the optional
canvas-wide filter block. Aggregation lives in the query AST endpoint
(``backend/app/routers/reports.py``); this model only holds the saved
report state.

Architect-locked decisions baked into the schema (spec sections 5 + 8):

- ``visibility`` is a closed enum (``private`` / ``org``). No public
  links in v1.
- ``owner_user_id`` is ``ON DELETE RESTRICT``. User-delete semantics
  (org-shared transfer to org owner, private hard-delete) are
  enforced at the service layer. The DB-level RESTRICT is a safety
  net for any code path that bypasses the service.
- ``organization_id`` is ``ON DELETE CASCADE`` so a future org-delete
  pathway (out of scope today) takes the rows with it.
- ``layout_json`` + ``canvas_filters_json`` are dialect-agnostic JSON
  columns. SQLite-under-pytest stores TEXT; MySQL stores JSON.
- ``schema_version`` starts at 1 and gives us a non-breaking upgrade
  path when the layout schema evolves.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReportVisibility(str, enum.Enum):
    PRIVATE = "private"
    ORG = "org"


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    organization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    visibility: Mapped[ReportVisibility] = mapped_column(
        Enum(
            ReportVisibility,
            name="report_visibility",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=ReportVisibility.PRIVATE,
        server_default=ReportVisibility.PRIVATE.value,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
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
        # "Shared by your org" + "Yours" reads.
        Index(
            "ix_reports_org_visibility",
            "organization_id",
            "visibility",
        ),
        Index("ix_reports_owner", "owner_user_id"),
    )
