"""In-app feedback widget storage.

Privacy contract (spec captured 2026-05-08):

- `user_id` and `org_id` are NULLABLE. The router writes them ONLY
  when the submitter ticks the "Include my account info" box, which
  defaults OFF. Storage shape for an anonymous submission is therefore
  a row whose identity columns are NULL but whose `context` JSON has
  non-sensitive operational fields.

- `context` is non-nullable but the router strips query params from
  the URL and never adds account/transaction-shaped data. See
  `app.services.feedback_service.normalize_context`.

- ON DELETE SET NULL on both FKs so deleting a user or org leaves
  the feedback message intact for admin triage (mirrors `audit_events`).
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FeedbackCategory(str, enum.Enum):
    BUG = "bug"
    FEATURE = "feature"
    OTHER = "other"


class FeedbackEntry(Base):
    __tablename__ = "feedback_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identity-opt-in pair. Both nullable for the anonymous-shaped row.
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    org_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    message: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[FeedbackCategory] = mapped_column(
        Enum(
            FeedbackCategory,
            name="feedback_category",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
    )
    # See `feedback_service.normalize_context` for the exact shape and
    # the privacy-stripping rules. Non-nullable because operational
    # context is what makes anonymous submissions triageable.
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
        index=True,
    )
