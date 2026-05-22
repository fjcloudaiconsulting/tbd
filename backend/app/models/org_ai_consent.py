"""Per-org AI consents (PR1 follow-up).

Append-only — every consent grant or revocation writes a new row.
``revoked_at NOT NULL`` means consent was withdrawn. The latest row by
``consented_at`` is the current state, never an UPDATE.

Spec §5: native dispatch refuses unless the latest row has
``revoked_at IS NULL`` AND ``consent_version == settings.ai_native_current_consent_version``.
PR1 stores the rows; PR4 wires the refusal logic.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OrgAIConsent(Base):
    __tablename__ = "org_ai_consents"
    __table_args__ = (
        Index("ix_org_active", "org_id", "revoked_at"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id", name="fk_consent_org", ondelete="CASCADE"
        ),
        nullable=False,
    )
    allow_training: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    allow_rag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    allow_telemetry: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    consent_version: Mapped[str] = mapped_column(String(40), nullable=False)
    consented_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey(
            "users.id", name="fk_consent_user", ondelete="SET NULL"
        ),
        nullable=True,
    )
    consented_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
