"""Add invitations table for L3.8 — org member invitations.

Revision ID: 026
Revises: 025
"""

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("email", sa.String(120), nullable=False),
        sa.Column(
            "role",
            sa.Enum("owner", "admin", "member", name="role"),
            nullable=False,
        ),
        # NULL when the invite is no longer the live pending one
        # (accepted, revoked, or lazily expired). MySQL allows multiple
        # NULLs in the unique index below, so historical rows don't
        # collide.
        sa.Column("open_email", sa.String(120), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("org_id", "open_email", name="uq_invitations_open"),
    )
    op.create_index(
        "ix_invitations_org_email",
        "invitations",
        ["org_id", "email"],
    )
    op.create_index(
        "ix_invitations_status",
        "invitations",
        ["org_id", "accepted_at", "revoked_at", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_invitations_status", table_name="invitations")
    op.drop_index("ix_invitations_org_email", table_name="invitations")
    op.drop_table("invitations")
