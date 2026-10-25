"""The database URL migrations run with (TBD-655).

Migrations need DDL rights the running app should not hold, so they can use
their own database user: ``MIGRATIONS_DATABASE_URL``. Unset or blank, it
falls back to ``DATABASE_URL``, so a deployment that sets only
``DATABASE_URL`` behaves as before.

Every migration path resolves the URL here: ``alembic/env.py`` (which the dev
lifespan reaches by running ``alembic upgrade head`` in a subprocess that
inherits the environment) and ``scripts/migrate.py``, the migrations image's
entrypoint.
"""
from __future__ import annotations

import os
from typing import Optional

MIGRATIONS_DATABASE_URL_ENV = "MIGRATIONS_DATABASE_URL"
DATABASE_URL_ENV = "DATABASE_URL"


def migrations_database_url(default: Optional[str] = None) -> Optional[str]:
    """``MIGRATIONS_DATABASE_URL``, else ``DATABASE_URL``, else ``default``.

    A blank value counts as unset.
    """
    for name in (MIGRATIONS_DATABASE_URL_ENV, DATABASE_URL_ENV):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default
