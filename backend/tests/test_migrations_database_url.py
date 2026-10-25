"""TBD-655: migrations can run as their own database user.

``MIGRATIONS_DATABASE_URL`` is the DSN every migration path uses (alembic's
env.py, the ``scripts/migrate.py`` wrapper behind the migrations image, and
the dev lifespan, which shells out to alembic). Unset or blank, it falls back
to ``DATABASE_URL``, so a deployment that sets only ``DATABASE_URL`` keeps
working unchanged.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from scripts import migrate

from app.migration_url import migrations_database_url

BACKEND = Path(__file__).resolve().parents[1]

APP_URL = "mysql+aiomysql://tbd_app:app-pw@db.internal:3306/tbd"
MIGRATE_URL = "mysql+aiomysql://tbd_migrate:migrate-pw@db.internal:3306/tbd"


@pytest.fixture
def env(monkeypatch):
    def _set(*, database_url=None, migrations_url=None):
        for name, value in (
            ("DATABASE_URL", database_url),
            ("MIGRATIONS_DATABASE_URL", migrations_url),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

    return _set


def test_migrations_url_wins_when_set(env):
    env(database_url=APP_URL, migrations_url=MIGRATE_URL)
    assert migrations_database_url() == MIGRATE_URL


def test_falls_back_to_database_url_when_unset(env):
    env(database_url=APP_URL)
    assert migrations_database_url() == APP_URL


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_migrations_url_falls_back_to_database_url(env, blank):
    """An empty Secret key or ``KEY=`` line in .env counts as unset."""
    env(database_url=APP_URL, migrations_url=blank)
    assert migrations_database_url() == APP_URL


def test_none_when_neither_is_set(env):
    """Callers then read alembic.ini."""
    env()
    assert migrations_database_url() is None


def test_migrate_wrapper_uses_the_migrations_url(env):
    """The migrations image's wrapper reads alembic_version (and logs the
    redacted target) through the same URL alembic migrates with."""
    # A config whose file does not exist: an env URL must not need it.
    cfg = Config("/nonexistent/alembic.ini")
    env(database_url=APP_URL, migrations_url=MIGRATE_URL)
    assert migrate._resolve_database_url(cfg) == MIGRATE_URL
    env(database_url=APP_URL)
    assert migrate._resolve_database_url(cfg) == APP_URL


def _alembic_current(tmp_path, *, database_url, migrations_url=None):
    child_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("DATABASE_URL", "MIGRATIONS_DATABASE_URL")
    }
    child_env["DATABASE_URL"] = database_url
    if migrations_url is not None:
        child_env["MIGRATIONS_DATABASE_URL"] = migrations_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), "current"],
        cwd=BACKEND,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_alembic_env_connects_with_the_migrations_url(tmp_path):
    """End to end through alembic/env.py: with DATABASE_URL pointing nowhere,
    ``alembic current`` succeeds only if env.py connects with
    MIGRATIONS_DATABASE_URL."""
    unreachable = "mysql+aiomysql://nobody:nothing@127.0.0.1:1/none"
    reachable = f"sqlite+aiosqlite:///{tmp_path / 'migrate.db'}"

    ok = _alembic_current(tmp_path, database_url=unreachable, migrations_url=reachable)
    assert ok.returncode == 0, ok.stderr

    # Control: the same run without the override uses DATABASE_URL and fails.
    failed = _alembic_current(tmp_path, database_url=unreachable)
    assert failed.returncode != 0


def test_alembic_env_falls_back_to_database_url(tmp_path):
    reachable = f"sqlite+aiosqlite:///{tmp_path / 'fallback.db'}"
    ok = _alembic_current(tmp_path, database_url=reachable)
    assert ok.returncode == 0, ok.stderr
