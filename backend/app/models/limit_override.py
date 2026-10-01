"""Per-org usage-limit override (TBD-585), sibling of ``OrgFeatureOverride``.

Single-current per (org, meter). Row presence wins over the plan's limit and
replaces the meter's ``{period, limit}`` wholesale; ``limit_value`` NULL means
unlimited (refused for ``platform_ai.*`` meters). Expiry is
``expires_at <= now`` (naive UTC).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.usage_counter import USAGE_PERIOD


class OrgLimitOverride(Base):
    __tablename__ = "org_limit_overrides"
    __table_args__ = (
        UniqueConstraint("org_id", "meter", name="uq_org_limit_meter"),
        Index("ix_olo_expires_at", "expires_at"),
        Index("ix_olo_set_by", "set_by"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    meter: Mapped[str] = mapped_column(String(40), nullable=False)
    period: Mapped[str] = mapped_column(USAGE_PERIOD, nullable=False)
    limit_value: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    set_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    set_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
