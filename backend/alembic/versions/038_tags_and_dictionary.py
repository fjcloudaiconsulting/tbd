"""Tags + cross-org tag dictionary (PR-Tags-A).

Revision ID: 038_tags_and_dictionary
Revises: 037_categories_floor_backfill
Create Date: 2026-05-09

Implements the schema half of the Tags + Auto-learning discovery spec
(``specs/2026-05-09-tags-discovery.md``) following the 2026-05-09
amendment that introduces a real ``tag_dictionary_contributors`` table
as the single source of truth for cross-org contribution tracking.

Four tables:

1. ``tags`` - per-org user-defined tags. ``UNIQUE(org_id, name_normalized)``
   so two orgs may each have their own ``insurance`` tag without collision.
   ``name_normalized`` is filled at write time from
   ``app.services.tag_service.normalize_tag_name`` (lowercased, trimmed,
   internal whitespace collapsed). We do NOT use a generated column here
   (unlike the org-rename precedent in PR #158): SQLite's ``Computed`` form
   doesn't behave the same as MySQL, and the test path runs on SQLite. The
   service layer is the single normalization site, the unique constraint
   pins uniqueness, and a CHECK or trigger isn't needed because all writes
   go through the service.

2. ``transaction_tags`` - many-to-many join.
   ``ON DELETE CASCADE`` on both FKs: transaction delete cleans up the
   join row; tag delete detaches from all transactions.
   PK is ``(transaction_id, tag_id)`` so the cover for the FK on
   ``transaction_id`` is the leading column. An explicit
   ``ix_transaction_tags_tag`` covers the FK on ``tag_id``.

3. ``tag_dictionary`` - cross-org public-shape table. Read-side surface
   for the suggestion endpoint. Columns: ``(name_normalized,
   contributor_org_count, usage_count, is_seed, ...)``. NO FK to orgs.

4. ``tag_dictionary_contributors`` - cross-org PRIVATE table.
   ``(dictionary_tag_id FK tag_dictionary, contributor_org_id FK
   organizations)`` with ``UNIQUE(dictionary_tag_id, contributor_org_id)``.
   This is the source of truth for the k-anonymity floor. NEVER exposed
   via any read endpoint. Both FKs cascade on parent delete.

   Privacy invariant: ``tag_dictionary.contributor_org_count == COUNT(DISTINCT
   contributor_org_id) FROM tag_dictionary_contributors WHERE
   dictionary_tag_id = tag_dictionary.id``. Maintained inside the same
   transaction as contributor row mutations by the service layer.

Seed data: 8 generic EU-targeted tags inserted into ``tag_dictionary``
with ``is_seed=TRUE``. Seed entries skip the k-anonymity gate (they appear
in suggestions for every user immediately) per the spec.

Seed list (final for v1):
    gym, insurance, vacation, work-travel, gift, electronics, subscription, donation

These are deliberately short, generic, and cross-locale. Personal or
specific tags (``vacation-divorce-trip``, ``kids-school-2026``) are
intentionally absent: they would only enter the dictionary via the
contribution path, which has its own length/hyphen-group guard.

**Migration ordering.** ``down_revision`` is
``037_categories_floor_backfill`` (the Categories C0 backend migration
that landed on main ahead of this one). The Tags B team should pick up
this revision in their fixtures.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "038_tags_and_dictionary"
down_revision = "037_categories_floor_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Per-org tags table.
    # ON DELETE CASCADE on org_id matches the convention used for
    # transaction_tags to tags. When an org is deleted via
    # admin_orgs_service.delete_org_cascade, the wipe path explicitly
    # deletes tags before the org row, but the CASCADE here is defense
    # in depth (and keeps the FK consistent with the rest of the schema
    # where org-scoped data cascades on org delete).
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id", sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("name_normalized", sa.String(32), nullable=False),
        sa.Column(
            "created_by_user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint(
            "org_id", "name_normalized",
            name="uq_tags_org_name_normalized",
        ),
    )
    op.create_index("ix_tags_org_id", "tags", ["org_id"])

    # 2. transaction_tags join. PK covers the transaction-side FK; explicit
    # cover for the tag-side FK to satisfy InnoDB's FK-must-be-covered rule
    # (cf. reference_mysql_fk_index_cover.md).
    op.create_table(
        "transaction_tags",
        sa.Column(
            "transaction_id", sa.Integer(),
            sa.ForeignKey("transactions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id", sa.Integer(),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index(
        "ix_transaction_tags_tag", "transaction_tags", ["tag_id"]
    )

    # 3. Cross-org PUBLIC-shape dictionary.
    tag_dictionary = op.create_table(
        "tag_dictionary",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name_normalized", sa.String(32), nullable=False, unique=True),
        sa.Column(
            "contributor_org_count", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "usage_count", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "is_seed", sa.Boolean(),
            nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            "first_seen_at", sa.DateTime(),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(),
            server_default=sa.func.now(), nullable=False,
        ),
    )

    # 4. Cross-org PRIVATE contributors table. The unique constraint
    # is the dedupe mechanism; the count on tag_dictionary is denormalized
    # and maintained by the service layer in the same transaction.
    op.create_table(
        "tag_dictionary_contributors",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "dictionary_tag_id", sa.Integer(),
            sa.ForeignKey("tag_dictionary.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contributor_org_id", sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contributed_at", sa.DateTime(),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint(
            "dictionary_tag_id", "contributor_org_id",
            name="uq_tag_dictionary_contributors_tag_org",
        ),
    )
    # Explicit cover for the contributor_org_id FK; the unique constraint
    # already covers dictionary_tag_id as the leading column.
    op.create_index(
        "ix_tag_dictionary_contributors_org",
        "tag_dictionary_contributors",
        ["contributor_org_id"],
    )

    # Seed dictionary: 8 generic EU-targeted tags, is_seed=TRUE.
    seed_tags = [
        "gym",
        "insurance",
        "vacation",
        "work-travel",
        "gift",
        "electronics",
        "subscription",
        "donation",
    ]
    op.bulk_insert(
        tag_dictionary,
        [
            {
                "name_normalized": name,
                "contributor_org_count": 0,
                "usage_count": 0,
                "is_seed": True,
            }
            for name in seed_tags
        ],
    )


def downgrade() -> None:
    # Drop tables in FK-safe order so InnoDB does not reject any drop
    # because a child still references the parent (errno 1553 / 1217):
    #   tag_dictionary_contributors -> tag_dictionary  (FK)
    #   transaction_tags            -> tags             (FK)
    # Children first, then parents.
    #
    # Explicit ``op.drop_index`` calls were intentionally removed from
    # this downgrade. MySQL InnoDB also rejects dropping an index that
    # still covers an FK with errno 1553, and these indexes
    # (``ix_tag_dictionary_contributors_org``, ``ix_transaction_tags_tag``,
    # ``ix_tags_org_id``) are only relevant while their parent table
    # exists. Dropping the table drops its indexes automatically, and we
    # do not need the indexes after the tables themselves are gone.
    # Cross-reference: ``reference_mysql_fk_index_cover.md``.
    op.drop_table("tag_dictionary_contributors")
    op.drop_table("transaction_tags")
    op.drop_table("tag_dictionary")
    op.drop_table("tags")
