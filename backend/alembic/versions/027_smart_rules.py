"""smart rules + merchant dictionary

Revision ID: 027
Revises: 026
Create Date: 2026-05-02
"""
from alembic import op
import sqlalchemy as sa


revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "category_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("normalized_token", sa.String(64), nullable=False),
        sa.Column("raw_description_seen", sa.String(255), nullable=False),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id"), nullable=False),
        sa.Column("match_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "source",
            sa.Enum("user_edit", "user_pick", "dictionary_promotion", name="rulesource"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "normalized_token", name="uq_category_rules_org_token"),
    )
    op.create_index("ix_category_rules_org_id", "category_rules", ["org_id"])
    op.create_index("ix_category_rules_category_id", "category_rules", ["category_id"])

    merchant_dict = op.create_table(
        "merchant_dictionary",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("normalized_token", sa.String(64), nullable=False, unique=True),
        sa.Column("category_slug", sa.String(64), nullable=False),
        sa.Column("vote_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_seed", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    seed = [
        # Groceries
        ("LIDL", "groceries"), ("PINGO DOCE", "groceries"), ("CONTINENTE", "groceries"),
        ("AUCHAN", "groceries"), ("CARREFOUR", "groceries"), ("MERCADONA", "groceries"),
        ("ALDI", "groceries"), ("TESCO", "groceries"), ("SAINSBURYS", "groceries"),
        ("ALBERT HEIJN", "groceries"), ("EDEKA", "groceries"), ("REWE", "groceries"),
        # Transportation
        ("BOLT", "public_transit"), ("FREE NOW", "public_transit"), ("UBER", "public_transit"),
        ("MB WAY", "public_transit"), ("VIA VERDE", "parking_tolls"), ("RENFE", "public_transit"),
        ("DEUTSCHE BAHN", "public_transit"), ("NS", "public_transit"), ("SNCF", "public_transit"),
        # Streaming -> "internet" is the closest seeded slug for now
        ("SPOTIFY", "internet"), ("NETFLIX", "internet"), ("DISNEY", "internet"),
        ("HBO", "internet"), ("PRIME VIDEO", "internet"),
        # Telecoms
        ("VODAFONE", "phone"), ("NOS", "phone"), ("MEO", "phone"),
        ("ORANGE", "phone"), ("T MOBILE", "phone"), ("TELEKOM", "phone"),
        # Utilities
        ("EDP", "electricity"), ("ENDESA", "electricity"), ("IBERDROLA", "electricity"),
        ("EON", "electricity"), ("BRITISH GAS", "gas_utility"),
        # Restaurants & food delivery
        ("UBER EATS", "fast_food"), ("DELIVEROO", "fast_food"), ("GLOVO", "fast_food"),
        ("JUSTEAT", "fast_food"), ("WOLT", "fast_food"),
        # General
        ("AMZN MKTP", "groceries"), ("AMAZON", "groceries"),
        ("IKEA", "home_repairs"), ("APPLE", "internet"), ("GOOGLE", "internet"),
    ]
    op.bulk_insert(
        merchant_dict,
        [
            {"normalized_token": tok, "category_slug": slug, "is_seed": True, "vote_count": 0}
            for tok, slug in seed
        ],
    )


def downgrade() -> None:
    op.drop_table("merchant_dictionary")
    op.drop_table("category_rules")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="rulesource").drop(bind, checkfirst=True)
