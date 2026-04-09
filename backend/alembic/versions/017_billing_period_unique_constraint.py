"""Add unique constraint on billing_periods(org_id, start_date)

Revision ID: 017
Revises: 016
"""

from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove any existing duplicates first (keep the one with lowest id)
    op.execute("""
        DELETE bp1 FROM billing_periods bp1
        INNER JOIN billing_periods bp2
        ON bp1.org_id = bp2.org_id
           AND bp1.start_date = bp2.start_date
           AND bp1.id > bp2.id
    """)
    op.create_unique_constraint("uq_billing_period_org_start", "billing_periods", ["org_id", "start_date"])


def downgrade() -> None:
    op.drop_constraint("uq_billing_period_org_start", "billing_periods", type_="unique")
