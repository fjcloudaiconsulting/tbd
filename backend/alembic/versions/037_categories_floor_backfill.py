"""Backfill the 1+1+1+1 category floor for every org.

Revision ID: 037_categories_floor_backfill
Revises: 036_settled_implies_settled_date
Create Date: 2026-05-09

C0 Invariant 1 (`categories-c0-invariants` spec) requires every org to
satisfy the floor:

  - count(masters where type='income' AND parent_id IS NULL) >= 1
  - count(subs where master.type='income') >= 1
  - count(masters where type='expense') >= 1
  - count(subs where master.type='expense') >= 1

Pre-launch every org goes through ``seed_org_defaults`` on register, but
nothing has prevented a user from deleting their way down to zero. From
this revision forward the service-layer guards in
``backend/app/services/category_service.py`` enforce the floor on every
delete.

This migration ensures the invariant holds for every existing org at the
moment the C0 code ships, by:

1. SELECTing every org_id from ``organizations``.
2. Computing the four floor counts per org via raw SQL.
3. For any org below the floor: inserting the missing system categories
   from ``SYSTEM_CATEGORIES`` using raw SQL via the Alembic-supplied
   connection. Inserts are idempotent against the existing
   ``(org_id, slug, is_system)`` rows -- existing seeded slugs are
   skipped, only missing rows are added.
4. Asserting the floor holds afterwards via the same raw-SQL count
   query; raising loudly if not.

Implementation note: this migration runs inside Alembic's async migration
environment (env.py runs ``asyncio.run(run_migrations_online())``) and
the connection passed to ``upgrade()`` via ``op.get_bind()`` is a sync
connection adapted from ``connection.run_sync(do_run_migrations)``.
Calling ``asyncio.run(...)`` from inside this hook fails with
``RuntimeError: asyncio.run() cannot be called from a running event
loop``. So we drop the asyncio path entirely and use plain
``op.get_bind()`` SQL instead, mirroring the data-mutation pattern in
migrations 030, 035, 036.

Down-migration: no-op. The migration is data-only and the new state is
always closer to invariant-correct than the old state.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "037_categories_floor_backfill"
down_revision = "036_settled_implies_settled_date"
branch_labels = None
depends_on = None


# Mirror of ``backend/app/models/category.SYSTEM_CATEGORIES`` (kept inline
# so the migration stays self-contained -- importing the live module risks
# drift if a future migration alters the table shape, and Alembic best-
# practice is to make data migrations independent of app code).
SYSTEM_CATEGORIES: list[dict] = [
    {
        "slug": "income", "name": "Income", "type": "income",
        "description": "All sources of earnings and revenue",
        "children": [
            {"slug": "paycheck", "name": "Paycheck/Salary", "description": "Regular employment income"},
            {"slug": "side_hustles", "name": "Side Hustles", "description": "Freelance, gig work, or side business income"},
            {"slug": "bonuses", "name": "Bonuses", "description": "Performance bonuses, commissions, tips"},
            {"slug": "interest_dividends", "name": "Interest/Dividends", "description": "Bank interest, stock dividends, investment returns"},
            {"slug": "tax_refunds", "name": "Tax Refunds", "description": "Government tax refunds"},
        ],
    },
    {
        "slug": "housing", "name": "Housing", "type": "expense",
        "description": "Home-related expenses",
        "children": [
            {"slug": "rent_mortgage", "name": "Rent/Mortgage", "description": "Monthly rent or mortgage payment"},
            {"slug": "property_taxes", "name": "Property Taxes", "description": "Annual or semi-annual property taxes"},
            {"slug": "home_insurance", "name": "Home Insurance", "description": "Homeowner's or renter's insurance"},
            {"slug": "hoa_fees", "name": "HOA Fees", "description": "Homeowners association fees"},
            {"slug": "home_repairs", "name": "Repairs/Maintenance", "description": "Home repairs, renovations, upkeep"},
        ],
    },
    {
        "slug": "utilities", "name": "Utilities", "type": "expense",
        "description": "Monthly utility bills",
        "children": [
            {"slug": "electricity", "name": "Electricity", "description": "Electric power bill"},
            {"slug": "water", "name": "Water", "description": "Water and sewer bill"},
            {"slug": "gas_utility", "name": "Gas", "description": "Natural gas or heating bill"},
            {"slug": "internet", "name": "Internet", "description": "Internet service provider"},
            {"slug": "phone", "name": "Phone", "description": "Mobile or landline phone plan"},
            {"slug": "trash", "name": "Trash/Recycling", "description": "Waste collection and recycling"},
        ],
    },
    {
        "slug": "food_dining", "name": "Food & Dining", "type": "expense",
        "description": "Groceries and eating out",
        "children": [
            {"slug": "groceries", "name": "Groceries", "description": "Supermarket and grocery store purchases"},
            {"slug": "restaurants", "name": "Restaurants", "description": "Dine-in meals"},
            {"slug": "coffee_shops", "name": "Coffee Shops", "description": "Coffee, tea, and cafe visits"},
            {"slug": "fast_food", "name": "Fast Food/Takeout", "description": "Quick-service restaurants and delivery"},
        ],
    },
    {
        "slug": "transportation", "name": "Transportation", "type": "expense",
        "description": "Getting around expenses",
        "children": [
            {"slug": "fuel", "name": "Fuel/Gas", "description": "Gasoline, diesel, or EV charging"},
            {"slug": "car_payments", "name": "Car Payments", "description": "Auto loan or lease payments"},
            {"slug": "auto_insurance", "name": "Auto Insurance", "description": "Vehicle insurance premiums"},
            {"slug": "public_transit", "name": "Public Transit", "description": "Bus, train, subway fares"},
            {"slug": "car_maintenance", "name": "Maintenance/Repairs", "description": "Oil changes, tire rotations, repairs"},
            {"slug": "parking_tolls", "name": "Parking/Tolls", "description": "Parking fees and road tolls"},
        ],
    },
    {
        "slug": "health", "name": "Health & Wellness", "type": "expense",
        "description": "Medical and wellness expenses",
        "children": [
            {"slug": "health_insurance", "name": "Health Insurance", "description": "Medical insurance premiums"},
            {"slug": "doctor_visits", "name": "Doctor Visits/Copays", "description": "Medical appointments and copays"},
            {"slug": "pharmacy", "name": "Pharmacy/Meds", "description": "Prescription and over-the-counter medications"},
            {"slug": "gym", "name": "Gym Membership", "description": "Fitness center or gym fees"},
            {"slug": "dental_vision", "name": "Dental/Vision", "description": "Dental checkups and eye care"},
        ],
    },
    {
        "slug": "personal_care", "name": "Personal Care", "type": "expense",
        "description": "Personal grooming and clothing",
        "children": [
            {"slug": "haircuts", "name": "Haircuts", "description": "Barber or salon visits"},
            {"slug": "toiletries", "name": "Toiletries", "description": "Soap, shampoo, hygiene products"},
            {"slug": "clothing", "name": "Clothing", "description": "Clothes, outerwear, accessories"},
            {"slug": "shoes", "name": "Shoes", "description": "Footwear purchases"},
            {"slug": "laundry", "name": "Laundry/Dry Cleaning", "description": "Laundry services or dry cleaning"},
        ],
    },
    {
        "slug": "lifestyle", "name": "Lifestyle & Fun", "type": "expense",
        "description": "Entertainment and leisure",
        "children": [
            {"slug": "streaming", "name": "Streaming Services", "description": "Netflix, Spotify, and similar subscriptions"},
            {"slug": "entertainment", "name": "Movies/Concerts", "description": "Cinema, live shows, events"},
            {"slug": "hobbies", "name": "Hobbies", "description": "Sports, crafts, gaming, and other hobbies"},
            {"slug": "travel", "name": "Travel/Vacation", "description": "Flights, hotels, vacation expenses"},
            {"slug": "books_media", "name": "Books/Media", "description": "Books, magazines, digital media"},
        ],
    },
    {
        "slug": "financial_goals", "name": "Financial Goals", "type": "expense",
        "description": "Savings and investment contributions",
        "children": [
            {"slug": "emergency_fund", "name": "Emergency Fund", "description": "Contributions to emergency savings"},
            {"slug": "retirement", "name": "Retirement (401k/IRA)", "description": "Retirement account contributions"},
            {"slug": "general_savings", "name": "General Savings", "description": "General-purpose savings deposits"},
            {"slug": "investments", "name": "Brokerage Investments", "description": "Stock, bond, or fund purchases"},
        ],
    },
    {
        "slug": "debt", "name": "Debt Repayment", "type": "expense",
        "description": "Paying down outstanding debt",
        "children": [
            {"slug": "credit_card_debt", "name": "Credit Cards", "description": "Credit card balance payments"},
            {"slug": "student_loans", "name": "Student Loans", "description": "Education loan payments"},
            {"slug": "personal_loans", "name": "Personal Loans", "description": "Personal or payday loan payments"},
        ],
    },
    {
        "slug": "giving", "name": "Giving & Gifts", "type": "expense",
        "description": "Charitable giving and gifts",
        "children": [
            {"slug": "donations", "name": "Charitable Donations", "description": "Donations to nonprofits and causes"},
            {"slug": "gifts", "name": "Birthday/Holiday Gifts", "description": "Gifts for friends and family"},
        ],
    },
    {
        "slug": "miscellaneous", "name": "Miscellaneous", "type": "expense",
        "description": "Uncategorized and one-off expenses",
        "children": [
            {"slug": "bank_fees", "name": "Bank Fees", "description": "ATM fees, overdraft charges, service fees"},
            {"slug": "taxes_other", "name": "Taxes (Non-Property)", "description": "Income tax, sales tax, other tax payments"},
            {"slug": "uncategorized", "name": "Uncategorized/Unexpected", "description": "One-off or hard-to-classify expenses"},
        ],
    },
]


_FLOOR_QUERY = sa.text(
    """
    SELECT
        SUM(CASE WHEN c.parent_id IS NULL AND c.type = 'income'  THEN 1 ELSE 0 END) AS income_masters,
        SUM(CASE WHEN c.parent_id IS NULL AND c.type = 'expense' THEN 1 ELSE 0 END) AS expense_masters,
        SUM(CASE WHEN c.parent_id IS NOT NULL AND m.type = 'income'  THEN 1 ELSE 0 END) AS income_subs,
        SUM(CASE WHEN c.parent_id IS NOT NULL AND m.type = 'expense' THEN 1 ELSE 0 END) AS expense_subs
    FROM categories c
    LEFT JOIN categories m ON m.id = c.parent_id
    WHERE c.org_id = :org_id
    """
)


_EXISTING_SEED_SLUGS_QUERY = sa.text(
    """
    SELECT slug
      FROM categories
     WHERE org_id = :org_id
       AND is_system = TRUE
       AND slug IS NOT NULL
    """
)


_INSERT_MASTER = sa.text(
    """
    INSERT INTO categories (org_id, parent_id, name, slug, description, type, is_system)
    VALUES (:org_id, NULL, :name, :slug, :description, :type, TRUE)
    """
)


_INSERT_CHILD = sa.text(
    """
    INSERT INTO categories (org_id, parent_id, name, slug, description, type, is_system)
    VALUES (:org_id, :parent_id, :name, :slug, :description, :type, TRUE)
    """
)


_LOOKUP_MASTER_ID = sa.text(
    """
    SELECT id
      FROM categories
     WHERE org_id = :org_id
       AND slug = :slug
       AND is_system = TRUE
    """
)


_INSERT_TRANSFER = sa.text(
    """
    INSERT INTO categories (org_id, parent_id, name, slug, description, type, is_system)
    VALUES (:org_id, NULL, 'Transfer', 'transfer',
            'Internal transfers between accounts', 'both', TRUE)
    """
)


def _under_floor(row) -> bool:
    if row is None:
        return True
    return any(
        (row._mapping.get(k) or 0) < 1
        for k in ("income_masters", "expense_masters", "income_subs", "expense_subs")
    )


def _seed_org_via_alembic_bind(bind, org_id: int) -> None:
    """Insert any missing system categories for ``org_id`` using raw SQL.

    Idempotent: skips slugs that already exist for the org with
    ``is_system = TRUE``. Mirrors ``seed_org_defaults`` from
    ``app/services/org_bootstrap_service.py`` but uses the Alembic-
    supplied sync connection (``op.get_bind()``) so the migration does
    not need to open an async session inside the alembic event loop.
    """
    existing_slugs = {
        row[0]
        for row in bind.execute(
            _EXISTING_SEED_SLUGS_QUERY, {"org_id": org_id}
        ).all()
    }

    for master_def in SYSTEM_CATEGORIES:
        if master_def["slug"] not in existing_slugs:
            bind.execute(
                _INSERT_MASTER,
                {
                    "org_id": org_id,
                    "name": master_def["name"],
                    "slug": master_def["slug"],
                    "description": master_def["description"],
                    "type": master_def["type"],
                },
            )

        # Look up the master id (whether we just inserted it or it
        # already existed). Children need parent_id.
        master_row = bind.execute(
            _LOOKUP_MASTER_ID,
            {"org_id": org_id, "slug": master_def["slug"]},
        ).first()
        if master_row is None:
            # Should not happen unless a non-system row owns the slug;
            # skip gracefully.
            continue
        master_id = master_row[0]

        for child_def in master_def.get("children", []):
            if child_def["slug"] in existing_slugs:
                continue
            bind.execute(
                _INSERT_CHILD,
                {
                    "org_id": org_id,
                    "parent_id": master_id,
                    "name": child_def["name"],
                    "slug": child_def["slug"],
                    "description": child_def["description"],
                    "type": master_def["type"],
                },
            )

    # Transfer system category (BOTH; no children).
    if "transfer" not in existing_slugs:
        bind.execute(_INSERT_TRANSFER, {"org_id": org_id})


def upgrade() -> None:
    bind = op.get_bind()

    org_ids = [
        row[0]
        for row in bind.execute(sa.text("SELECT id FROM organizations")).all()
    ]

    summary: list[dict] = []
    for org_id in org_ids:
        before = bind.execute(_FLOOR_QUERY, {"org_id": org_id}).first()
        if not _under_floor(before):
            summary.append({"org_id": org_id, "action": "skip"})
            continue

        # Seed the missing categories using raw SQL on the Alembic bind.
        # This avoids ``asyncio.run`` (which would fail inside Alembic's
        # async migration event loop) and keeps the migration self-
        # contained.
        _seed_org_via_alembic_bind(bind, org_id)

        after = bind.execute(_FLOOR_QUERY, {"org_id": org_id}).first()
        if _under_floor(after):
            # The seed could not satisfy the floor, e.g. because a non-
            # system master is squatting the income/expense slug with the
            # wrong type. Bail loudly so deploy fails fast rather than
            # producing a malformed org.
            raise RuntimeError(
                f"037_categories_floor_backfill: org {org_id} still below "
                f"floor after seed: "
                f"{dict(after._mapping) if after else None}"
            )
        summary.append({
            "org_id": org_id,
            "action": "seeded",
            "before": dict(before._mapping) if before else {},
            "after": dict(after._mapping) if after else {},
        })

    # Emit a structured summary so the migrate-job log captures the
    # action taken across orgs.
    print(f"migrate.category.backfill.summary {summary}")  # noqa: T201


def downgrade() -> None:
    """No-op. The up-migration is data-only and idempotent; reverting it
    would re-introduce an invariant violation rather than restore prior
    state. Pre-launch we have no production data to fear, so a hard
    no-op is correct."""
    pass
