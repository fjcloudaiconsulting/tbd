"""Two-phase email change: users.pending_email (TBD-361).

Revision ID: 080_pending_email
Revises: 079_audit_api_token_id
Create Date: 2026-08-15

One additive column on ``users``:

    pending_email  VARCHAR(120) NULL   -- an UNPROVEN claim on an address

Mirrors ``users.email``'s type exactly (``String(120)``), so the promotion
``user.email = user.pending_email`` can never truncate.

**Why this column exists.** ``PUT /users/me`` used to assign ``users.email``,
clear ``email_verified`` and set ``sessions_invalidated_at`` in a single
request, then mail the verification link to the NEW address. One typo
therefore logged the user out and locked them out permanently: every recovery
path mails ``user.email``, which is now the typo, and ``reset_password``
never writes ``email_verified``, so even a successful password reset still
403s at login. The change becomes a two-phase commit: the verified address
and the live session survive until the new address confirms.

**NO UNIQUE INDEX, and no index at all. This is deliberate.**

``pending_email`` is a claim nobody has proven yet. A unique constraint here
enforces the wrong invariant -- it does not prevent user A claiming an
address that equals user B's live ``users.email``, which is the collision
that actually matters -- and it creates an address-squatting denial
primitive: any authenticated account could claim an address, never prove it,
and block its legitimate owner from even requesting the change. Two users may
both claim an address; only one can prove it, and the loser is caught by the
uniqueness re-check against ``users.email`` at promote time. First to click
wins; proof beats claim order.

No plain index either: promotion resolves the row by ``sub`` from the token,
never by ``pending_email``, so nothing ever queries this column.

**No ``pending_email_requested_at`` companion.** ``audit_events.created_at``
already records when each change was requested, durably, for every request
including superseded ones -- a column on ``users`` would record only the
latest, which is strictly worse evidence. The token's own ``exp`` is the
expiry clock; a second, independently-driftable timestamp would desynchronise
the day that TTL changes. Nothing in the write path, the promote path, or the
cancel endpoint branches on when the claim was made.

**No backfill.** NULL is the correct and only meaningful value for every
pre-existing row: nobody has a claim in flight, because the flow did not
exist. VARCHAR NULL is addable on a non-empty table in one statement.

**No native ENUM, no collation choice.** Plain VARCHAR, so SQLite CI mirrors
MySQL prod faithfully here (``reference_abn_tab_import``). Values are
normalised by ``user_service.normalize_email`` at every write site, which is
what keeps the case-insensitive ``utf8mb4_0900_ai_ci`` unique index on
``users.email`` and the case-sensitive Python comparisons agreeing
(``reference_mysql_collation_for_uniqueness``, TBD-322).
"""
import sqlalchemy as sa
from alembic import op

revision = "080_pending_email"
down_revision = "079_audit_api_token_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("pending_email", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "pending_email")
