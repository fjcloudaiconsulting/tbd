"""Per-call usage ledger row for AI dispatch (PR2 of AI tier train).

Every ``call_llm`` invocation writes exactly one row here on
completion (success or failure). The ledger is the source of truth
for cap aggregation, soft-cap warnings, and the superadmin
``/admin/ai/usage`` debug surface.

Indexes:

- ``(org_id, dispatched_at)`` — feeds the rolling-window cap check
  for the org-wide cap.
- ``(org_id, feature_key, dispatched_at)`` — feeds the per-feature
  cap check.

The ``credential_id`` is the composite-FK reference used by the
routing tables; it stays nullable so a credential delete doesn't
cascade-kill historical ledger rows.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AIUsageLedger(Base):
    __tablename__ = "ai_usage_ledger"
    __table_args__ = (
        # Composite FK to (org_id, id) on org_ai_credentials matches
        # the routing tables — keeps cross-org references structurally
        # impossible at the DB layer. Nullable so credential deletes
        # don't cascade-kill historical rows.
        ForeignKeyConstraint(
            ["org_id", "credential_id"],
            ["org_ai_credentials.org_id", "org_ai_credentials.id"],
            name="fk_ai_usage_ledger_cred",
            ondelete="SET NULL",
        ),
        Index("ix_ai_usage_org_dispatched", "org_id", "dispatched_at"),
        Index(
            "ix_ai_usage_org_feature_dispatched",
            "org_id",
            "feature_key",
            "dispatched_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_ai_usage_ledger_org",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    # Nullable on the column itself; the composite FK above carries
    # the ON DELETE SET NULL behavior so a credential delete leaves
    # the historical row intact.
    credential_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    feature_key: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    est_cost_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    dispatched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0"
    )
    error_class: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True
    )
