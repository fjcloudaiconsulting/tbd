"""INF-03 follow-up: production refuses a database DSN that uses a password committed here.

The dev compose stack and CI's service container publish their MySQL passwords in this repository.
A production DSN that still carries one is open to anyone who can reach the database, so every
process that loads the settings refuses to boot on it, naming the variable and never the value.
The fence half scans the committed sources so a new dev password cannot land without its refusal.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import _PUBLIC_DB_PASSWORDS, Settings

BACKEND = Path(__file__).resolve().parents[1]
REPO = next(
    c for c in [BACKEND, *BACKEND.parents] if (c / ".github" / "workflows" / "ci.yml").exists()
)
_JWT = "jwt-secret-key-at-least-32-characters-long-000"
_PAT = "dedicated-pat-hmac-key-distinct-and-32plus-chars"
_REAL = "mysql+aiomysql://tbd_app:a-real-generated-password@db.internal:3306/tbd"


def _settings(**overrides) -> Settings:
    return Settings(
        **{"_env_file": None, "jwt_secret_key": _JWT, "api_token_hmac_key": _PAT, **overrides}
    )


@pytest.mark.parametrize("password", sorted(_PUBLIC_DB_PASSWORDS))
def test_production_refuses_a_committed_password_in_database_url(monkeypatch, password):
    monkeypatch.delenv("MIGRATIONS_DATABASE_URL", raising=False)
    dsn = f"mysql+aiomysql://pfv2:{password}@db.internal:3306/pfv2"
    with pytest.raises(ValidationError) as caught:
        _settings(app_env="production", database_url=dsn)
    (error,) = caught.value.errors()
    assert "DATABASE_URL uses a database password committed" in error["msg"]
    assert password not in error["msg"]
    # Dev and test keep booting on it.
    for env in ("development", "test"):
        assert _settings(app_env=env, database_url=dsn).database_url == dsn


def test_production_refuses_it_in_migrations_database_url_and_percent_encoded(monkeypatch):
    monkeypatch.setenv(
        "MIGRATIONS_DATABASE_URL", "mysql+aiomysql://root:root%5Fsecret@db.internal:3306/tbd"
    )
    with pytest.raises(ValidationError, match="MIGRATIONS_DATABASE_URL uses a database password"):
        _settings(app_env="production", database_url=_REAL)


def test_production_refuses_the_shipped_default(monkeypatch):
    """Unset DATABASE_URL falls back to the dev compose DSN, which production refuses."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MIGRATIONS_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="DATABASE_URL uses a database password"):
        _settings(app_env="production")


@pytest.mark.parametrize("key", ["password", "passwd"])
def test_production_refuses_it_as_a_query_argument(monkeypatch, key):
    monkeypatch.delenv("MIGRATIONS_DATABASE_URL", raising=False)
    dsn = f"mysql+aiomysql://pfv2@db.internal:3306/pfv2?{key}=pfv2_secret"
    with pytest.raises(ValidationError, match="DATABASE_URL uses a database password"):
        _settings(app_env="production", database_url=dsn)


def test_production_boots_on_a_real_password(monkeypatch):
    monkeypatch.delenv("MIGRATIONS_DATABASE_URL", raising=False)
    assert _settings(app_env="production", database_url=_REAL).database_url == _REAL


def test_the_release_smoke_commits_no_database_password():
    """The smoke runs production, so it generates its password per run into the smoke-db volume."""
    text = (REPO / "compose.smoke.yaml").read_text()
    assert not re.findall(r"mysql(?:\+\w+)?://[^:/\s]+:([^@\s$]+)@", text)
    assert not re.findall(r"MYSQL_(?:ROOT_)?PASSWORD[=:]\s*([^\s$\"'{}]+)", text)
    assert "DATABASE_URL=mysql+aiomysql://tbd:$$(cat /smoke-db/password)@mysql:3306/tbd" in text


def test_every_committed_dev_and_ci_database_password_is_refused():
    """The fence: passwords in .env.example, the compose files and ci.yml, by DSN or MYSQL_*."""
    sources = [".env.example", "docker-compose.yml", "docker-compose.prod.yml", ".github/workflows/ci.yml"]
    found: set[str] = set()
    for name in sources:
        text = (REPO / name).read_text()
        found |= set(re.findall(r"mysql(?:\+\w+)?://[^:/\s]+:([^@\s]+)@", text))
        found |= set(re.findall(r"MYSQL_(?:ROOT_)?PASSWORD[=:]\s*([^\s$\"'{}]+)", text))
        found |= set(re.findall(r"mysqladmin ping[^\n]*?-p(\S+)", text))
    assert found, "the scan found no committed password"
    assert found <= _PUBLIC_DB_PASSWORDS, sorted(found - _PUBLIC_DB_PASSWORDS)


def test_the_boot_refusal_names_the_variable_and_never_the_value():
    done = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=BACKEND,
        env={
            "PATH": "/usr/bin:/bin",
            "APP_ENV": "production",
            "JWT_SECRET_KEY": _JWT,
            "API_TOKEN_HMAC_KEY": _PAT,
            "DATABASE_URL": "mysql+aiomysql://pfv2:pfv2_secret@db.internal:3306/pfv2",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode != 0
    assert "DATABASE_URL uses a database password committed" in done.stderr
    assert "pfv2_secret" not in done.stderr + done.stdout
