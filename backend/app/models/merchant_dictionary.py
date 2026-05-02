from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MerchantDictionaryEntry(Base):
    __tablename__ = "merchant_dictionary"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    normalized_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    category_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    vote_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_seed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
