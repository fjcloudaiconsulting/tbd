"""Per-org usage meter counter (TBD-585).

One row per (org, meter, period kind, period start). ``period`` is part of the
key so a plan edited from monthly to daily starts a fresh day row instead of
colliding with the month row that shares the same start date (the 1st).
Written only by ``app.services.usage_service`` through an atomic conditional
UPDATE; never refunded.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import BigInteger, Date, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

USAGE_PERIOD = Enum("day", "month", name="usage_period")


class UsageCounter(Base):
    __tablename__ = "usage_counters"

    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    meter: Mapped[str] = mapped_column(String(40), primary_key=True)
    period: Mapped[str] = mapped_column(USAGE_PERIOD, primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
