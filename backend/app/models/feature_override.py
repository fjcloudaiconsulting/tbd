"""Per-org boolean feature override.

Single-current per (org, feature_key) — UNIQUE constraint at the DB
level. DELETE = revoke. Override row presence wins over plan features
(D5: row-presence resolver semantic).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OrgFeatureOverride(Base):
    __tablename__ = "org_feature_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    feature_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[bool] = mapped_column(Boolean, nullable=False)
    set_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    set_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("org_id", "feature_key", name="uq_org_feature"),)
