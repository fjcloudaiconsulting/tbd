"""INF-04: a process that never chose APP_ENV says so.

Unset, APP_ENV is ``development``: Swagger on, lifespan migrations, and none of the production
secret checks. The backend image happens to fail its boot then (the migration branch guard finds
no .git), but the MCP process has no such guard. Flipping the default needs the production
manifests (cloud-infra) confirmed first, so the settings only warn, at import, in every process.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from app.config import Settings

BACKEND = Path(__file__).resolve().parents[1]
_JWT = "jwt-secret-key-at-least-32-characters-long-000"


def _events(caplog) -> list[tuple[str, dict]]:
    return [
        (r.getMessage(), {k: getattr(r, k) for k in ("default", "app_env") if hasattr(r, k)})
        for r in caplog.records
        if r.getMessage().startswith("config.app_env_")
    ]


def test_an_unset_app_env_warns_and_still_means_development(monkeypatch, caplog):
    monkeypatch.delenv("APP_ENV", raising=False)
    caplog.set_level(logging.WARNING, logger="app.config")
    settings = Settings(_env_file=None, jwt_secret_key=_JWT)
    assert settings.app_env == "development"
    assert _events(caplog) == [("config.app_env_unset", {"default": "development"})]


def test_a_chosen_app_env_is_silent_and_an_unrecognised_one_warns(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.config")
    for env in ("development", "test", "production"):
        monkeypatch.setenv("APP_ENV", env)
        Settings(_env_file=None, jwt_secret_key=_JWT, api_token_hmac_key="p" * 40)
    assert _events(caplog) == []

    monkeypatch.setenv("APP_ENV", "prod")
    Settings(_env_file=None, jwt_secret_key=_JWT)
    assert _events(caplog) == [("config.app_env_unrecognised", {"app_env": "prod"})]


def test_the_warning_reaches_stderr_before_logging_is_configured():
    """At import, before logs.configure(): what uvicorn and the MCP process actually emit."""
    done = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=BACKEND,
        env={"PATH": "/usr/bin:/bin", "JWT_SECRET_KEY": _JWT},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "config.app_env_unset" in done.stderr
