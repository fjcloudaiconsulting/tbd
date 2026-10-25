"""org_ai_credentials.provider from ENUM to VARCHAR(32) (TBD-584).

Revision ID: 095_ai_provider_varchar
Revises: 094_tag_contributor_cleanup
Create Date: 2026-10-25

Bring-your-own local models (``ollama``) and the dead ``native`` stub left
the app's provider registry. The column becomes VARCHAR(32), validated by the
app against that registry, so adding a vendor needs no DDL and removing one
cannot crash a reader that does not know the value.

Non-destructive: every stored value is kept as is, and no row is deleted.
Rows that still say ``ollama`` stay inert (listed and deletable, never
dispatched) until the operator rules on them. ``platform_provider`` is left
alone. ``encrypted_api_key`` stays nullable (migration 062): platform rows
are keyless too.

Rolling deploy: the previous release reads the column through a SQLAlchemy
ENUM type, which accepts any string it already knows; this release only
writes ``openai``, ``anthropic`` or ``openai_compatible``, all known to it.

MySQL rebuilds the table for the type change (ALGORITHM=COPY). The table
holds one row per provider connection per org, so the copy is short.

``downgrade`` restores the ENUM exactly as 057 left it. It refuses, without
changing anything, when a row holds a value outside that ENUM: MySQL would
otherwise truncate it or fail half way.

Migrations never import ``app``: the value literals are below.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "095_ai_provider_varchar"
down_revision: Union[str, None] = "094_tag_contributor_cleanup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The ENUM as of 057_ai_provider_enum_native.
_ENUM_VALUES = ("openai", "anthropic", "ollama", "openai_compatible", "native")
_ENUM = sa.Enum(*_ENUM_VALUES, name="ai_provider")
_VARCHAR = sa.String(length=32)


def upgrade() -> None:
    op.alter_column(
        "org_ai_credentials",
        "provider",
        existing_type=_ENUM,
        type_=_VARCHAR,
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    outside = bind.execute(
        sa.text(
            "SELECT DISTINCT provider FROM org_ai_credentials "
            "WHERE provider NOT IN :values"
        ).bindparams(sa.bindparam("values", expanding=True)),
        {"values": list(_ENUM_VALUES)},
    ).scalars().all()
    if outside:
        raise RuntimeError(
            "org_ai_credentials.provider holds values the old ENUM cannot "
            f"store: {sorted(outside)!r}. Delete or migrate those rows first."
        )
    op.alter_column(
        "org_ai_credentials",
        "provider",
        existing_type=_VARCHAR,
        type_=_ENUM,
        existing_nullable=False,
    )
