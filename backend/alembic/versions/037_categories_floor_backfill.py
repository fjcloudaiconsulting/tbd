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
3. For any org below the floor: re-running
   ``org_bootstrap_service.seed_org_defaults`` (idempotent against
   ``slug``).
4. Asserting the floor holds after the seed; raising loudly if not.

The migration uses a sync session with the alembic bind because alembic
does not run async natively. ``seed_org_defaults`` is async, so we use
``asyncio.run`` per org.

Down-migration: no-op. The migration is data-only and the new state is
always closer to invariant-correct than the old state.
"""
from __future__ import annotations

import asyncio

import sqlalchemy as sa
from alembic import op


revision = "037_categories_floor_backfill"
down_revision = "036_settled_implies_settled_date"
branch_labels = None
depends_on = None


_FLOOR_QUERY = sa.text("""
    SELECT
        SUM(CASE WHEN c.parent_id IS NULL AND c.type = 'income'  THEN 1 ELSE 0 END) AS income_masters,
        SUM(CASE WHEN c.parent_id IS NULL AND c.type = 'expense' THEN 1 ELSE 0 END) AS expense_masters,
        SUM(CASE WHEN c.parent_id IS NOT NULL AND m.type = 'income'  THEN 1 ELSE 0 END) AS income_subs,
        SUM(CASE WHEN c.parent_id IS NOT NULL AND m.type = 'expense' THEN 1 ELSE 0 END) AS expense_subs
    FROM categories c
    LEFT JOIN categories m ON m.id = c.parent_id
    WHERE c.org_id = :org_id
""")


def _under_floor(row: sa.engine.row.Row) -> bool:
    if row is None:
        return True
    return any(
        (row._mapping.get(k) or 0) < 1
        for k in ("income_masters", "expense_masters", "income_subs", "expense_subs")
    )


async def _seed_org(org_id: int) -> None:
    """Async-call ``seed_org_defaults`` on its own session.

    The migration's bind is sync; we open an async session via the
    project's ``async_session`` factory so the existing
    seed-by-slug logic stays untouched. Each org is committed in its
    own transaction so a failure on one does not leave another half-seeded.
    """
    # Local imports because alembic loads the module at upgrade time
    # before the app's import graph is settled.
    from app.database import async_session
    from app.services.org_bootstrap_service import seed_org_defaults

    async with async_session() as session:
        async with session.begin():
            await seed_org_defaults(session, org_id=org_id)


async def _assert_floor(org_id: int) -> None:
    """Raise loudly if the org is still below the floor after the seed."""
    from app.database import async_session
    from app.services.category_service import assert_min_floor_for_org

    async with async_session() as session:
        await assert_min_floor_for_org(session, org_id=org_id)


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

        # Run the async seeder. asyncio.run cannot be called if there's
        # already a running loop, but alembic upgrade runs in a
        # synchronous context so we are safe here.
        asyncio.run(_seed_org(org_id))

        after = bind.execute(_FLOOR_QUERY, {"org_id": org_id}).first()
        if _under_floor(after):
            # The seed could not satisfy the floor — bail loudly.
            asyncio.run(_assert_floor(org_id))  # this raises ValidationError
            raise RuntimeError(
                f"037_categories_floor_backfill: org {org_id} still below "
                f"floor after seed: {dict(after._mapping) if after else None}"
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
