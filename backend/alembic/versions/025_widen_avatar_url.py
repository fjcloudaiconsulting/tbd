"""widen users.avatar_url to fit long CDN URLs

Revision ID: 025
Revises: 024
Create Date: 2026-04-24 08:30:00.000000

Google profile pictures routinely exceed 500 characters (900+ chars is
common for the lh3.googleusercontent.com URL shape). The previous
VARCHAR(500) caused Google SSO to 500 when persisting the avatar_url
on both new-user create and existing-user profile backfill. 2048 is
the practical URL length limit across mainstream browsers and CDNs.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '025'
down_revision: Union[str, None] = '024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "avatar_url",
        existing_type=sa.String(500),
        type_=sa.String(2048),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Downgrade is lossy — any row storing a URL > 500 chars would be
    # truncated by MySQL on the ALTER. That is the whole reason we widened
    # the column in the first place, so the downgrade path accepts the
    # truncation risk on rollback.
    op.alter_column(
        "users",
        "avatar_url",
        existing_type=sa.String(2048),
        type_=sa.String(500),
        existing_nullable=True,
    )
