"""Withdraw stale shared tag dictionary contributions and recount (BE-18).

Revision ID: 092_tag_contributor_cleanup
Revises: 091_micro_usd_metering
Create Date: 2026-10-24

Until BE-18 a contribution to the shared tag dictionary outlived its tag:
deleting or renaming the tag left the contributor row, turning
``share_tag_data`` off left all of them, and ``usage_count`` grew on every
create. The app now withdraws a contribution on delete, rename and opt-out,
and counts an org once per tag. This brings existing rows to that state:

1. delete every contributor row whose org has no tag of that name, or does
   not share tag data (only the stored value ``"true"`` shares, as in
   ``tag_service.share_tag_data_value_enabled``);
2. set ``contributor_org_count`` and ``usage_count`` of each dictionary row
   that differs to its number of contributor rows.

DML only, on two small tables, so no DDL and no long lock: the deletes go by
primary key in batches and the recount touches only rows that change. Run
twice, it deletes nothing and writes nothing. The org-settings lookup uses the
same SQL equality as the app's, so a key spelling the collation folds is read
the same way; the value is compared in Python, exactly.

``downgrade`` is a no-op: the deleted rows described contributions that no
longer exist, and the old ``usage_count`` values counted repeated creates.

Migrations never import ``app``: the key and value literals are below.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import column, table


revision: str = "092_tag_contributor_cleanup"
down_revision: Union[str, None] = "091_micro_usd_metering"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SHARE_TAG_DATA_KEY = "share_tag_data"
SHARING_VALUE = "true"
BATCH = 500

_contributors = table(
    "tag_dictionary_contributors",
    column("id"), column("dictionary_tag_id"), column("contributor_org_id"),
)
_dictionary = table(
    "tag_dictionary",
    column("id"), column("name_normalized"), column("contributor_org_count"),
    column("usage_count"),
)
_tags = table("tags", column("org_id"), column("name_normalized"))
_settings = table("org_settings", column("org_id"), column("key"), column("value"))


def stale_contributor_ids(conn) -> list[int]:
    """Contributor rows that the app would not keep today."""
    sharing = {
        org_id
        for org_id, value in conn.execute(
            sa.select(_settings.c.org_id, _settings.c.value)
            .where(_settings.c.key == SHARE_TAG_DATA_KEY)
        )
        if value == SHARING_VALUE
    }
    has_tag = (
        sa.select(sa.literal(1))
        .select_from(_tags)
        .where(
            _tags.c.org_id == _contributors.c.contributor_org_id,
            _tags.c.name_normalized == _dictionary.c.name_normalized,
        )
        .exists()
    )
    rows = conn.execute(
        sa.select(_contributors.c.id, _contributors.c.contributor_org_id, has_tag)
        .select_from(
            _contributors.join(
                _dictionary, _dictionary.c.id == _contributors.c.dictionary_tag_id
            )
        )
        .order_by(_contributors.c.id)
    ).all()
    return [row_id for row_id, org_id, tagged in rows if not tagged or org_id not in sharing]


def recount(conn) -> int:
    """Set both counts of every dictionary row that differs; returns rows written."""
    n = (
        sa.select(sa.func.count())
        .select_from(_contributors)
        .where(_contributors.c.dictionary_tag_id == _dictionary.c.id)
        .scalar_subquery()
    )
    result = conn.execute(
        _dictionary.update()
        .where(sa.or_(_dictionary.c.contributor_org_count != n, _dictionary.c.usage_count != n))
        .values(contributor_org_count=n, usage_count=n)
    )
    return result.rowcount or 0


def cleanup(conn) -> tuple[int, int]:
    """Returns ``(contributor rows deleted, dictionary rows recounted)``."""
    stale = stale_contributor_ids(conn)
    for start in range(0, len(stale), BATCH):
        conn.execute(
            _contributors.delete().where(_contributors.c.id.in_(stale[start:start + BATCH]))
        )
    return len(stale), recount(conn)


def upgrade() -> None:
    cleanup(op.get_bind())


def downgrade() -> None:
    pass
