"""Platform AI model catalog (TBD-631): one row per OpenRouter model, written only by
``app.services.scheduler.jobs.platform_ai_catalog``. Prices are integer micro-USD per
1,000,000 tokens. ``enabled`` is the operator allowlist; the job only ever writes False."""
from __future__ import annotations

from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.session_state import ascii_id


class PlatformAIModel(Base):
    __tablename__ = "platform_ai_models"

    openrouter_id: Mapped[str] = mapped_column(ascii_id(128), primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    context_length: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    max_completion_tokens: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    supports_structured_outputs: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    headline_prompt_micros_per_mtok: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    headline_completion_micros_per_mtok: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    zdr_prompt_micros_per_mtok: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    zdr_completion_micros_per_mtok: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    expires_at: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
