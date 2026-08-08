"""Record the acting API token on audit events (TBD-188).

Revision ID: 079_audit_api_token_id
Revises: 078_recurring_occurrence_count
Create Date: 2026-08-07

One additive column on ``audit_events``:

    api_token_id BIGINT NULL
      FK -> api_tokens(id) ON DELETE SET NULL

Semantics (see ``app/models/audit_event.py`` design note 3): the API token
**presented as the credential** for the request that produced the row. On
``outcome="success"`` rows it additionally validated; on
``api_token.auth_rejected`` rows it was presented and rejected. It is the
ACTOR, never the subject.

⚠ ``BigInteger`` is load-bearing, not stylistic. ``api_tokens.id`` is
``BigInteger().with_variant(Integer, "sqlite")`` → ``BIGINT`` on MySQL, and
MySQL rejects an FK whose referencing column type differs from the referenced
key. A plain ``sa.Integer()`` here is green on SQLite CI and fails at
``ALTER TABLE`` on prod MySQL — the ``reference_abn_tab_import`` landmine in a
new dress. ``with_variant`` keeps the SQLite test path on the same INTEGER
affinity it already has.

⚠ **Index before FK, on purpose.** MySQL requires an index covering an FK's
referencing column and will silently auto-create one under an unpredictable
name if none exists. Creating ``ix_audit_events_api_token_id`` first lets the
constraint adopt it, so the index name in the DB matches the one the ORM
model declares (``index=True``).

⚠ **``downgrade()`` order is this migration's one landmine and is invisible on
SQLite:** drop FK → drop index → drop column. MySQL InnoDB raises errno 1553
when asked to drop an index that still covers a foreign key
(``reference_mysql_fk_index_cover``).

``ON DELETE SET NULL`` matches both existing ``audit_events`` FKs for the
reason documented at ``audit_event.py``: audit history must outlive the rows it
describes. Tokens are soft-revoked (``revoked_at``) and never hard-deleted, so
the branch never actually fires — which is exactly why it is free.

**No backfill, and this is a "must not", not a "cannot".** Three event types
already carry ``detail.api_token_id``, but the field is semantically
overloaded: on ``api_token.auth_rejected`` it is the *acting* token, while on
``api_token.created`` / ``api_token.revoked`` it is the *subject* of a
JWT-session action (both routes are ``require_interactive_session``). A naive
``UPDATE ... SET api_token_id = detail->>'$.api_token_id'`` would therefore
write subjects into an actor column and permanently corrupt the semantic —
which is the whole reason this is a separate column rather than a
``detail`` key.

**Cutover:** every ``audit_events`` row created before this migration ran has
``api_token_id IS NULL`` regardless of how it was authenticated. An operator
reading a NULL on a pre-cutover row learns nothing; on a post-cutover row a
NULL is informative and means "not a PAT-authenticated request".
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "079_audit_api_token_id"
down_revision: Union[str, None] = "078_recurring_occurrence_count"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column(
            "api_token_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
    )
    # Index FIRST so the FK adopts it (see module docstring).
    op.create_index(
        "ix_audit_events_api_token_id", "audit_events", ["api_token_id"]
    )
    op.create_foreign_key(
        "fk_audit_events_api_token_id",
        "audit_events",
        "api_tokens",
        ["api_token_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Order is load-bearing on MySQL: FK → index → column. Dropping the index
    # while the FK still covers it raises InnoDB errno 1553.
    op.drop_constraint(
        "fk_audit_events_api_token_id", "audit_events", type_="foreignkey"
    )
    op.drop_index("ix_audit_events_api_token_id", table_name="audit_events")
    op.drop_column("audit_events", "api_token_id")
