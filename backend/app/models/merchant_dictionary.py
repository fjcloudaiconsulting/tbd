from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MerchantDictionaryEntry(Base):
    """Cross-org PUBLIC merchant token -> category slug dictionary.

    ``vote_count`` is maintained by the service layer against
    ``MerchantDictionaryVote`` rows: one per org, added when an org confirms
    the server's own dictionary suggestion (BE-28). Counts stored before the
    per-org rows existed are kept as they were, so the column is that legacy
    baseline plus one per vote row. Nothing reads it yet.
    """

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


class MerchantDictionaryVote(Base):
    """One org's vote for one shared-dictionary entry (BE-28), server-internal.

    The unique constraint on ``(entry_id, org_id)`` is what makes a vote one
    per org: ``merchant_dictionary.vote_count`` is incremented only when this
    insert creates a new row. Never serialized in any response.
    """

    __tablename__ = "merchant_dictionary_votes"
    __table_args__ = (
        UniqueConstraint("entry_id", "org_id", name="uq_merchant_dictionary_votes_entry_org"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("merchant_dictionary.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
