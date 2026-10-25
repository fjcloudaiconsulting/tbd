"""Idempotent, chunked import confirm: import_batches columns (TBD-737).

Revision ID: 095_import_confirm_idempotency
Revises: 094_tag_contributor_cleanup
Create Date: 2026-10-25

Two additive columns and one unique index on ``import_batches``:

    idempotency_key  VARCHAR(64) NULL  -- the client's key for one confirm
    confirm_state    JSON NULL         -- resumable progress of that confirm
    UNIQUE (idempotency_key, org_id)

The confirm now commits in chunks and records its progress on the batch it
creates, so a repeated confirm with the same key resumes or replays instead
of importing twice.

Live-safe: nullable columns with no default are ``ALGORITHM=INSTANT`` on
MySQL 8; the unique index is built in place on a small table. No backfill:
existing batches keep NULL in both columns (NULLs never collide in a unique
index), which means "confirmed before keys existed".

The key leads the index on purpose: an index led by ``org_id`` could serve the
``org_id`` foreign key, and MySQL would then silently drop the FK's implicit
index, after which ``downgrade`` could not drop this one.
"""
import sqlalchemy as sa
from alembic import op

revision = "095_import_confirm_idempotency"
down_revision = "094_tag_contributor_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "import_batches",
        sa.Column("idempotency_key", sa.String(64), nullable=True),
    )
    op.add_column(
        "import_batches",
        sa.Column("confirm_state", sa.JSON(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_import_batches_org_idempotency_key",
        "import_batches",
        ["idempotency_key", "org_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_import_batches_org_idempotency_key", "import_batches", type_="unique"
    )
    op.drop_column("import_batches", "confirm_state")
    op.drop_column("import_batches", "idempotency_key")
