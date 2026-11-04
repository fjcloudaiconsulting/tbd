"""Delete the Ollama credentials; org_ai_credentials.provider to VARCHAR(32) (TBD-584).

Revision ID: 096_ai_provider_varchar
Revises: 095_import_confirm_idempotency
Create Date: 2026-10-25

Bring-your-own local models (``ollama``) and the dead ``native`` stub left
the app's provider registry. The column becomes VARCHAR(32), validated by the
app against that registry, so adding a vendor needs no DDL and removing one
cannot crash a reader that does not know the value.

Every ``ollama`` credential row is deleted first, as the app's own credential
delete does: the org's routing rows that point at it go too, and its
``ai_usage_ledger`` rows stay with ``credential_id`` NULL. The encrypted key
and bearer token live only in the deleted row. An org whose AI ran on Ollama
ends up exactly like an org with no AI provider configured. DML by primary
key in batches, before the type change, so no long lock and fewer rows to
copy. Run twice, it deletes nothing. ``platform_provider`` is left alone.
``encrypted_api_key`` stays nullable (migration 062): platform rows are
keyless too.

Rolling deploy: the previous release reads the column through a SQLAlchemy
ENUM type, which accepts any string it already knows; this release only
writes ``openai``, ``anthropic`` or ``openai_compatible``, all known to it.
A pod of the previous release can still store an ``ollama`` row after this
runs; the app keeps such a row inert (listed and deletable, never dispatched).

MySQL rebuilds the table for the type change (ALGORITHM=COPY). The table
holds one row per provider connection per org, so the copy is short.

``downgrade`` restores the ENUM exactly as 057 left it. It cannot restore
the deleted credentials or their routing rows; nothing keeps a copy. It
refuses, without changing anything, when a row holds a value outside that
ENUM: MySQL would otherwise truncate it or fail half way.

Migrations never import ``app``: the value literals are below.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import column, table


revision: str = "096_ai_provider_varchar"
down_revision: Union[str, None] = "095_import_confirm_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The ENUM as of 057_ai_provider_enum_native.
_ENUM_VALUES = ("openai", "anthropic", "ollama", "openai_compatible", "native")
_ENUM = sa.Enum(*_ENUM_VALUES, name="ai_provider")
_VARCHAR = sa.String(length=32)

RETIRED_PROVIDER = "ollama"
BATCH = 500

_credentials = table("org_ai_credentials", column("id"), column("provider"))
_default_routing = table("org_ai_default_routing", column("credential_id"))
_feature_routing = table("org_ai_feature_routing", column("credential_id"))
_ledger = table("ai_usage_ledger", column("id"), column("credential_id"))


def retired_credential_ids(conn) -> list[int]:
    return list(
        conn.execute(
            sa.select(_credentials.c.id)
            .where(_credentials.c.provider == RETIRED_PROVIDER)
            .order_by(_credentials.c.id)
        ).scalars()
    )


def _detach_ledger(conn, credential_ids: list[int]) -> None:
    # What ON DELETE SET NULL would do, in batches: the ledger can be large.
    while True:
        ledger_ids = list(
            conn.execute(
                sa.select(_ledger.c.id)
                .where(_ledger.c.credential_id.in_(credential_ids))
                .order_by(_ledger.c.id)
                .limit(BATCH)
            ).scalars()
        )
        if not ledger_ids:
            return
        conn.execute(
            _ledger.update()
            .where(_ledger.c.id.in_(ledger_ids))
            .values(credential_id=None)
        )


def delete_retired_credentials(conn) -> int:
    """Delete every ``ollama`` credential and its dependents; returns rows deleted."""
    ids = retired_credential_ids(conn)
    for start in range(0, len(ids), BATCH):
        chunk = ids[start:start + BATCH]
        _detach_ledger(conn, chunk)
        # The routing FKs cascade on MySQL; deleting them here keeps the
        # statement explicit and does not depend on the cascade.
        for routing in (_default_routing, _feature_routing):
            conn.execute(routing.delete().where(routing.c.credential_id.in_(chunk)))
        conn.execute(_credentials.delete().where(_credentials.c.id.in_(chunk)))
    return len(ids)


def upgrade() -> None:
    delete_retired_credentials(op.get_bind())
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
