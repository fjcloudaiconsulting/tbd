"""Migration 028 backfill verification.

Asserts that plans.features after upgrade contains canonical alias-key
booleans for every seeded plan, and that the stored value is a JSON
object (not a JSON string scalar).

Skipped unless PFV_RUN_MYSQL_TESTS=1 because this exercises real MySQL
(JSON_TYPE, post-upgrade dataset). The default DATABASE_URL is MySQL
even in environments where no MySQL service is running, so the explicit
opt-in flag is the only reliable signal.
"""
import os

from sqlalchemy import select, text
import pytest

from app.database import get_db
from app.models.subscription import Plan

pytestmark = pytest.mark.skipif(
    os.environ.get("PFV_RUN_MYSQL_TESTS") != "1",
    reason="MySQL-only migration test; set PFV_RUN_MYSQL_TESTS=1 to run.",
)


@pytest.mark.asyncio
async def test_seeded_plans_have_canonical_features():
    async for db in get_db():
        plans = (await db.execute(select(Plan).order_by(Plan.slug))).scalars().all()
        assert len(plans) >= 2, "expected Free + Pro seed plans"

        for plan in plans:
            assert isinstance(plan.features, dict), (
                f"{plan.slug}: features stored as {type(plan.features).__name__}, "
                "expected dict — backfill likely json.dumps'd a string scalar"
            )
            assert set(plan.features.keys()) == {
                "ai.budget", "ai.forecast", "ai.smart_plan", "ai.autocategorize"
            }, f"{plan.slug}: unexpected keys {plan.features.keys()}"

            # ai.autocategorize is always False post-backfill (LAI.1 not yet shipped).
            assert plan.features["ai.autocategorize"] is False, (
                f"{plan.slug}: ai.autocategorize should be False post-backfill"
            )
        break


@pytest.mark.asyncio
async def test_features_stored_as_json_object_not_string():
    """MySQL-only: JSON_TYPE must report OBJECT, not STRING."""
    async for db in get_db():
        result = await db.execute(text(
            "SELECT id, JSON_TYPE(features) AS json_type FROM plans"
        ))
        for row in result.all():
            assert row.json_type == "OBJECT", (
                f"plan id={row.id}: features stored as {row.json_type}, "
                "expected OBJECT — backfill regression"
            )
        break
