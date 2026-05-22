"""Per-org AI spend cap tables (PR1 follow-up).

Same split-table pattern as routing. Enforcement / ledger writes ride
in PR2; PR1 only ships CRUD + the storage substrate so caps can be
configured ahead of dispatch.

Cents (INT) over USD DECIMAL on purpose — see migration docstring.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OrgAIDefaultCaps(Base):
    __tablename__ = "org_ai_default_caps"
    __table_args__ = (
        PrimaryKeyConstraint("org_id", name="pk_org_ai_default_caps"),
    )

    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_default_caps_org",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    soft_cap_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hard_cap_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    period: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="monthly"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OrgAIFeatureCaps(Base):
    __tablename__ = "org_ai_feature_caps"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org_id", "feature_key", name="pk_org_ai_feature_caps"
        ),
    )

    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_feature_caps_org",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    feature_key: Mapped[str] = mapped_column(String(120), nullable=False)
    soft_cap_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hard_cap_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    period: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="monthly"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
