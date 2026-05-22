"""Per-org AI routing tables (PR1 follow-up).

Split tables for the same reason the spec mandates (§4): MySQL treats
NULL as distinct in unique indexes, so a single-table-with-nullable-
feature_name shape can't structurally enforce "one default per org".

Both tables reference ``org_ai_credentials.(org_id, id)`` via a
composite foreign key so a cross-org credential reference fails at
the DB layer (T14 in the spec's threat model).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OrgAIDefaultRouting(Base):
    __tablename__ = "org_ai_default_routing"
    __table_args__ = (
        PrimaryKeyConstraint("org_id", name="pk_org_ai_default_routing"),
        ForeignKeyConstraint(
            ["org_id", "credential_id"],
            ["org_ai_credentials.org_id", "org_ai_credentials.id"],
            name="fk_default_routing_cred",
            ondelete="CASCADE",
        ),
        Index("ix_default_cred", "org_id", "credential_id"),
    )

    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_default_routing_org",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    credential_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OrgAIFeatureRouting(Base):
    __tablename__ = "org_ai_feature_routing"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org_id", "feature_name", name="pk_org_ai_feature_routing"
        ),
        ForeignKeyConstraint(
            ["org_id", "credential_id"],
            ["org_ai_credentials.org_id", "org_ai_credentials.id"],
            name="fk_feature_routing_cred",
            ondelete="CASCADE",
        ),
        Index("ix_feature_cred", "org_id", "credential_id"),
    )

    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_feature_routing_org",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    feature_name: Mapped[str] = mapped_column(String(120), nullable=False)
    credential_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# Closed set of routable feature names. Adding a new feature => bump
# this list and ship UI for it; the service-layer write path refuses
# anything outside this set so the table doesn't accumulate junk keys.
ROUTABLE_FEATURE_NAMES: frozenset[str] = frozenset(
    {
        "categorize_transactions",
        "smart_forecast",
        "smart_budget",
        "smart_plan",
        "chat",
    }
)
