import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RuleSource(str, enum.Enum):
    USER_EDIT = "user_edit"
    USER_PICK = "user_pick"
    DICTIONARY_PROMOTION = "dictionary_promotion"


class CategoryRule(Base):
    __tablename__ = "category_rules"
    __table_args__ = (
        UniqueConstraint("org_id", "normalized_token", name="uq_category_rules_org_token"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    normalized_token: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_description_seen: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[int] = mapped_column(Integer, ForeignKey("categories.id"), nullable=False, index=True)
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source: Mapped[RuleSource] = mapped_column(
        Enum(RuleSource, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
