"""Rate-limit counters: rate limits move to MySQL (INFRA-121)."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import CHAR, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RateLimit(Base):
    __tablename__ = "rate_limits"

    # sha256 hex of the limiter key
    key: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    hits: Mapped[int] = mapped_column(Integer, nullable=False)
    # sa.Double, never Float: MySQL FLOAT is 4 bytes
    expires_at: Mapped[float] = mapped_column(sa.Double, nullable=False)

    __table_args__ = (sa.Index("ix_rate_limits_expires_at", "expires_at"),)
