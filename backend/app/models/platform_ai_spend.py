"""Global platform AI spend per UTC month (TBD-586). Written only by
``app.services.platform_reserve``."""
from __future__ import annotations

from datetime import date

from sqlalchemy import BigInteger, Date
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PlatformAISpend(Base):
    __tablename__ = "platform_ai_spend"

    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    cents: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
