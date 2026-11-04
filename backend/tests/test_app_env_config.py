"""INF-04: APP_ENV fails safe. Unset, it means production, and the process says so.

Production gets its checks (committed-secret refusals, the PAT key requirement, no Swagger, no
lifespan migrations) even when a manifest forgets APP_ENV. Dev compose, CI and conftest set it
explicitly; the fence at the bottom keeps every compose service and CI run that loads the backend
settings doing so.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config import Settings

BACKEND = Path(__file__).resolve().parents[1]
_JWT = "jwt-secret-key-at-least-32-characters-long-000"
_PAT = "dedicated-pat-hmac-key-distinct-and-32plus-chars"
# Production refuses the dev compose DSN (the field default), so give it a real one.
_DSN = "mysql+aiomysql://tbd_app:a-real-generated-password@db.internal:3306/tbd"


def _events(caplog) -> list[tuple[str, dict]]:
    return [
        (r.getMessage(), {k: getattr(r, k) for k in ("default", "app_env") if hasattr(r, k)})
        for r in caplog.records
        if r.getMessage().startswith("config.app_env_")
    ]


def test_an_unset_app_env_warns_and_means_production(monkeypatch, caplog):
    monkeypatch.delenv("APP_ENV", raising=False)
    caplog.set_level(logging.WARNING, logger="app.config")
    settings = Settings(
        _env_file=None, jwt_secret_key=_JWT, api_token_hmac_key=_PAT, database_url=_DSN
    )
    assert settings.app_env == "production"
    assert _events(caplog) == [("config.app_env_unset", {"default": "production"})]


def test_an_unset_app_env_gets_the_production_refusals(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("API_TOKEN_HMAC_KEY", raising=False)  # dev compose sets a shared one
    with pytest.raises(ValidationError, match="API_TOKEN_HMAC_KEY is required in production"):
        Settings(_env_file=None, jwt_secret_key=_JWT, database_url=_DSN)
    with pytest.raises(ValidationError, match="API_TOKEN_HMAC_KEY is a value committed"):
        Settings(
            _env_file=None,
            jwt_secret_key=_JWT,
            api_token_hmac_key="ci-migration-smoke-pat-key-distinct-02",
            database_url=_DSN,
        )


def test_a_chosen_app_env_is_silent_and_an_unrecognised_one_warns(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.config")
    for env in ("development", "test", "production"):
        monkeypatch.setenv("APP_ENV", env)
        Settings(
            _env_file=None, jwt_secret_key=_JWT, api_token_hmac_key="p" * 40, database_url=_DSN
        )
    assert _events(caplog) == []

    monkeypatch.setenv("APP_ENV", "prod")
    Settings(_env_file=None, jwt_secret_key=_JWT)
    assert _events(caplog) == [("config.app_env_unrecognised", {"app_env": "prod"})]


def test_the_warning_reaches_stderr_before_logging_is_configured():
    """At import, before logs.configure(): what uvicorn and the MCP process actually emit."""
    done = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=BACKEND,
        env={
            "PATH": "/usr/bin:/bin",
            "JWT_SECRET_KEY": _JWT,
            "API_TOKEN_HMAC_KEY": _PAT,
            "DATABASE_URL": _DSN,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "config.app_env_unset" in done.stderr


# ── fence: everything that loads the backend settings chooses APP_ENV ─────────


def _repo_root() -> Path:
    # The checkout on a host; /app in the backend container, where the repo-root files are mounted.
    for candidate in [BACKEND, *BACKEND.parents]:
        if (candidate / ".github" / "workflows" / "ci.yml").exists():
            return candidate
    raise RuntimeError("no .github/workflows/ci.yml above the backend")


REPO = _repo_root()


def _env_names(environment) -> set[str]:
    if isinstance(environment, dict):
        return set(environment)
    return {str(item).split("=", 1)[0] for item in environment or []}


def _loads_settings(service: dict) -> bool:
    build = service.get("build") or {}
    context = build.get("context", "") if isinstance(build, dict) else str(build)
    image = str(service.get("image", ""))
    return context.endswith("backend") or any(
        f"/{name}:" in image for name in ("backend", "migrations", "mcp")
    )


def test_every_compose_service_that_loads_the_settings_sets_app_env():
    """Merged as Compose does: docker-compose.prod.yml overlays docker-compose.yml."""
    loader = type("_L", (yaml.SafeLoader,), {})
    loader.add_constructor("!reset", lambda l, n: [])
    files = {name: yaml.load((REPO / name).read_text(), Loader=loader) for name in (
        "docker-compose.yml", "docker-compose.prod.yml", "compose.smoke.yaml")}
    dev, prod = files["docker-compose.yml"]["services"], files["docker-compose.prod.yml"]["services"]
    missing = []
    for label, services in (
        ("docker-compose.yml", dev),
        ("compose.smoke.yaml", files["compose.smoke.yaml"]["services"]),
    ):
        for name, svc in services.items():
            if _loads_settings(svc) and "APP_ENV" not in _env_names(svc.get("environment")):
                missing.append(f"{label}:{name}")
    for name, svc in prod.items():
        merged = {**dev.get(name, {}), **svc}
        if _loads_settings(merged) and "APP_ENV" not in _env_names(svc.get("environment")):
            missing.append(f"docker-compose.prod.yml:{name}")
    assert not missing, f"set APP_ENV explicitly (unset means production): {missing}"
    # The rehearsal migrates as production does; .env says development.
    assert "APP_ENV=production" in prod["migrate"]["environment"]


def test_every_ci_run_that_loads_the_settings_sets_app_env():
    """The image smokes run the images outside pytest; the migrations job boots uvicorn."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    runs = re.findall(r"docker run --rm\n((?:\s+-e .*\n)+)\s+\"tbd-(backend|mcp|migrations):", ci)
    assert len(runs) == 3
    assert all("-e APP_ENV=development" in flags for flags, _image in runs)
    jobs = yaml.safe_load(ci)["jobs"]
    booting = [n for n, j in jobs.items() if "uvicorn app.main:app" in str(j.get("steps"))]
    assert booting and all(jobs[n]["env"].get("APP_ENV") == "development" for n in booting)


def test_conftest_and_env_example_choose_development():
    assert 'os.environ.setdefault("APP_ENV", "development")' in (BACKEND / "tests" / "conftest.py").read_text()
    assert re.search(r"(?m)^APP_ENV=development$", (REPO / ".env.example").read_text())
