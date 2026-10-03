"""Serialize alembic migration runs on MySQL (INFRA-83).

Every path that applies migrations (scripts/migrate.py per revision, the dev
lifespan, a bare ``alembic upgrade``) goes through alembic/env.py, which calls
``acquire_migration_lock`` on its migration connection first. A second
migrator waits here, then finds the revision already applied (alembic then
does nothing) instead of running the same DDL twice. ``GET_LOCK`` belongs to
the connection, so the lock is released when env.py closes it, even if the
process dies.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Long enough for any migration we ship; a run stuck behind a dead holder
# fails loudly instead of hanging forever.
MIGRATION_LOCK_TIMEOUT_SECONDS = 600


class MigrationLockTimeout(RuntimeError):
    """Another migrator held the lock for the whole timeout (or GET_LOCK failed)."""


def acquire_migration_lock(connection: Connection) -> None:
    if connection.dialect.name != "mysql":
        return
    # User-level locks are server-wide; the database name keeps two
    # databases on one server from blocking each other.
    name = f"tbd_migrate.{connection.engine.url.database}"[:64]
    got = connection.execute(
        text("SELECT GET_LOCK(:name, :timeout)"),
        {"name": name, "timeout": MIGRATION_LOCK_TIMEOUT_SECONDS},
    ).scalar()
    if got != 1:
        raise MigrationLockTimeout(
            f"migration lock {name!r} not acquired within "
            f"{MIGRATION_LOCK_TIMEOUT_SECONDS}s (GET_LOCK returned {got!r})"
        )
    # End the SELECT's implicit transaction so alembic opens its own. The
    # lock is not transactional and survives the commit.
    connection.commit()
