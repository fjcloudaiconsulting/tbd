"""Tags + cross-org dictionary models (PR-Tags-A).

Four tables, two privacy tiers:

- ``Tag`` (per-org) and ``TransactionTag`` (per-org join) are scoped
  by ``org_id`` like everything else in the multi-tenant schema.
- ``TagDictionary`` is the cross-org public-shape table read by the
  suggestion endpoint. ``TagDictionaryContributor`` is the cross-org
  PRIVATE table that tracks "which org contributed which dictionary
  tag". The contributors table is server-internal: never read by an
  endpoint, never serialized in any response.

The denormalized ``TagDictionary.contributor_org_count`` is the
k-anonymity number the suggestion query filters on. The invariant
(``count == COUNT(DISTINCT contributor_org_id)``) is maintained by
``app.services.tag_service`` inside the same transaction as
contributor row inserts/deletes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Tag(Base):
    """Per-org user-defined tag.

    Uniqueness lives on ``(org_id, name_normalized)`` so two orgs can
    each have their own ``insurance`` tag without colliding. The display
    column ``name`` keeps the user's casing for chip rendering;
    ``name_normalized`` is lowercased + trimmed + collapsed by the
    service layer at write time.
    """

    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "name_normalized",
            name="uq_tags_org_name_normalized",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TransactionTag(Base):
    """Many-to-many join between transactions and tags.

    PK is composite ``(transaction_id, tag_id)`` so a tag can only be
    attached to a given transaction once. Both FKs cascade on parent
    delete (transaction delete cleans up join rows; tag delete detaches
    from every transaction).
    """

    __tablename__ = "transaction_tags"

    transaction_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("transactions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class TagDictionary(Base):
    """Cross-org PUBLIC-shape tag dictionary.

    Read by the suggestion endpoint. ``contributor_org_count`` is the
    k-anonymity number; the invariant is maintained against
    ``TagDictionaryContributor`` rows by the service layer. ``is_seed=True``
    rows bypass the k-anonymity floor so seeded tags appear in
    suggestions immediately.

    No FK to ``organizations`` exists on this table. A row is just
    ``(string, two counts, seed flag, timestamps)``.
    """

    __tablename__ = "tag_dictionary"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name_normalized: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True
    )
    contributor_org_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    usage_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    is_seed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    contributors: Mapped[list["TagDictionaryContributor"]] = relationship(
        back_populates="dictionary_tag",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class TagDictionaryContributor(Base):
    """Cross-org PRIVATE contributors table, server-internal only.

    Source of truth for the k-anonymity floor. Each row says "this org
    has contributed this dictionary tag at least once." Unique constraint
    on ``(dictionary_tag_id, contributor_org_id)`` enforces one row per
    org per tag, the dedupe mechanism for the count.

    **This table is never read by any API endpoint, never serialized in
    any response, never surfaced to admins or users.** The suggestion
    path queries ``tag_dictionary`` only.
    """

    __tablename__ = "tag_dictionary_contributors"
    __table_args__ = (
        UniqueConstraint(
            "dictionary_tag_id", "contributor_org_id",
            name="uq_tag_dictionary_contributors_tag_org",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dictionary_tag_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tag_dictionary.id", ondelete="CASCADE"),
        nullable=False,
    )
    contributor_org_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contributed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    dictionary_tag: Mapped[TagDictionary] = relationship(back_populates="contributors")
