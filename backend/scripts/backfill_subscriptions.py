"""Ops CLI: backfill trial subscriptions for orgs missing one.

K8S-2 (L0.6): the subscription backfill no longer runs on every
backend boot. Migration 043_backfill_subscriptions performs the one-
shot fill on deploy. This script is the recovery path for any later
"my org has no subscription row" incident — it shares the same
idempotent SQL the migration uses, so running it twice is safe.

Usage from inside the backend container::

    docker compose exec backend python -m scripts.backfill_subscriptions

Exits 0 with a JSON summary on stdout; non-zero on hard failure.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys

import structlog

from app.database import engine
from app.logging import setup_logging


setup_logging()
logger = structlog.stdlib.get_logger()


def _load_migration_module():
    """Import the 043 backfill migration as a regular Python module.

    Alembic version files live under ``backend/alembic/versions/`` and
    do not form a package (no ``__init__.py``), so importing by
    filename via ``importlib.util`` is the canonical way to reuse the
    SQL in this script without duplicating it.
    """
    migration_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "043_backfill_subscriptions.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_043_backfill_subscriptions", migration_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"could not load migration module at {migration_path}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run() -> dict:
    module = _load_migration_module()
    try:
        async with engine.connect() as conn:
            # Drive the sync function inside the async connection via
            # the standard SQLAlchemy adapter, mirroring how alembic's
            # env.py bridges async -> sync for op.get_bind().
            summary = await conn.run_sync(module.backfill_missing_subscriptions)
            await conn.commit()
        return summary
    finally:
        # Dispose so aiomysql tears its connection down inside the
        # event loop; otherwise the GC ``Connection.__del__`` fires
        # after the loop closes and warns "Event loop is closed".
        await engine.dispose()


def main() -> int:
    try:
        summary = asyncio.run(_run())
    except Exception as exc:
        logger.error("backfill_subscriptions.failed", error=str(exc))
        return 1
    print(json.dumps(summary, default=str))
    logger.info("backfill_subscriptions.complete", **summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
