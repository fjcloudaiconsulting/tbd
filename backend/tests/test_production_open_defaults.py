"""INF-05: the registration captcha fails closed in production.

Unset, CAPTCHA_REQUIRED is on in production, and the API refuses to boot while a required captcha
would let any token through (an empty or Cloudflare test secret, an unpinned hostname or action).
An explicit CAPTCHA_REQUIRED=false is the documented rollback lever during a provider outage, so it
boots and is logged, like a CORS allowlist with a plain-http or wildcard origin.
"""

from __future__ import annotations

import ast
import inspect
import logging

import pytest

from app import config
from app.config import (
    TURNSTILE_TEST_SECRETS,
    Settings,
    refuse_open_production_captcha,
    warn_open_production_defaults,
)

_JWT = "jwt-secret-key-at-least-32-characters-long-000"
_PAT = "dedicated-pat-hmac-key-distinct-and-32plus-chars"
# Production refuses the dev compose DSN (the field default), so give it a real one.
_DSN = "mysql+aiomysql://tbd_app:a-real-generated-password@db.internal:3306/tbd"
_CLOSED = {
    "captcha_required": True,
    "captcha_secret": "real-siteverify-secret-from-the-environment",
    "captcha_expected_hostname": "app.example.test",
    "captcha_expected_action": "register",
    "backend_cors_origins": "https://app.example.test,https://example.test",
}


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "jwt_secret_key": _JWT,
        "api_token_hmac_key": _PAT,
        "app_env": "production",
        "database_url": _DSN,
        **_CLOSED,
    }
    base.update(overrides)
    return Settings(**base)


def _warned(caplog) -> list[dict]:
    return [
        {"reason": r.reason, "origin": getattr(r, "origin", None)}
        for r in caplog.records
        if r.getMessage() == "config.production_open_default"
    ]


def test_a_closed_production_config_and_dev_and_test_stay_silent(caplog):
    caplog.set_level(logging.WARNING, logger="app.config")
    assert warn_open_production_defaults(_settings()) == []
    for env in ("development", "test"):
        assert warn_open_production_defaults(
            Settings(_env_file=None, jwt_secret_key=_JWT, app_env=env)
        ) == []
    assert _warned(caplog) == []


# config.OpenProductionCaptcha, read at raise time: other tests reload app.config, which rebinds
# the class the function raises.


def _shipped(monkeypatch, **overrides) -> Settings:
    # The dev backend container sets BACKEND_CORS_ORIGINS and the CAPTCHA_* values from .env.
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    return Settings(
        _env_file=None, jwt_secret_key=_JWT, api_token_hmac_key=_PAT, database_url=_DSN, **overrides
    )


def test_the_captcha_is_required_by_default_in_production_only(monkeypatch):
    assert _shipped(monkeypatch, app_env="production").captcha_required is True
    for env in ("development", "test"):
        assert _shipped(monkeypatch, app_env=env).captcha_required is False
    monkeypatch.setenv("CAPTCHA_REQUIRED", "false")
    assert Settings(
        _env_file=None, jwt_secret_key=_JWT, api_token_hmac_key=_PAT, database_url=_DSN,
        app_env="production",
    ).captcha_required is False


def test_the_shipped_defaults_refuse_to_boot_naming_every_open_captcha_setting(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.config")
    with pytest.raises(config.OpenProductionCaptcha) as caught:
        refuse_open_production_captcha(_shipped(monkeypatch, app_env="production"))
    assert str(caught.value).startswith(
        "CAPTCHA_SECRET, CAPTCHA_EXPECTED_HOSTNAME, CAPTCHA_EXPECTED_ACTION must hold real values"
    )
    (record,) = [r for r in caplog.records if r.getMessage() == "config.production_captcha_open"]
    assert record.fields == ["CAPTCHA_SECRET", "CAPTCHA_EXPECTED_HOSTNAME", "CAPTCHA_EXPECTED_ACTION"]


@pytest.mark.parametrize("secret", ["", "   ", *sorted(TURNSTILE_TEST_SECRETS)])
def test_an_empty_or_test_secret_refuses_and_names_the_variable_only(secret):
    with pytest.raises(config.OpenProductionCaptcha, match=r"^CAPTCHA_SECRET must hold") as caught:
        refuse_open_production_captcha(_settings(captcha_secret=secret))
    assert secret.strip() == "" or secret not in str(caught.value)


@pytest.mark.parametrize("field", ["captcha_expected_hostname", "captcha_expected_action"])
def test_an_unset_pin_refuses(field):
    with pytest.raises(config.OpenProductionCaptcha, match=rf"^{field.upper()} must hold"):
        refuse_open_production_captcha(_settings(**{field: ""}))


def test_a_closed_config_the_rollback_lever_and_dev_boot(monkeypatch):
    refuse_open_production_captcha(_settings())
    # CAPTCHA_REQUIRED=false is the rollback lever: no refusal, the warning below instead.
    refuse_open_production_captcha(_settings(captcha_required=False, captcha_secret=""))
    for env in ("development", "test"):
        refuse_open_production_captcha(_shipped(monkeypatch, app_env=env, captcha_required=True))


def test_the_rollback_lever_and_bad_origins_warn(caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="app.config")
    reasons = warn_open_production_defaults(
        _shipped(monkeypatch, app_env="production", captcha_required=False)
    )
    assert reasons == ["captcha_not_required", "cors_origin_insecure"]
    assert _warned(caplog) == [
        {"reason": "captcha_not_required", "origin": None},
        {"reason": "cors_origin_insecure", "origin": "http://localhost:3000"},
    ]


def test_wildcard_and_plain_http_cors_origins_warn_with_the_origin(caplog):
    caplog.set_level(logging.WARNING, logger="app.config")
    reasons = warn_open_production_defaults(
        _settings(backend_cors_origins="https://app.example.test, http://localhost:3000,*")
    )
    assert reasons == ["cors_origin_insecure", "cors_origin_insecure"]
    assert _warned(caplog) == [
        {"reason": "cors_origin_insecure", "origin": "http://localhost:3000"},
        {"reason": "cors_origin_insecure", "origin": "*"},
    ]


def test_the_api_lifespan_runs_the_check():
    """app.main's lifespan runs migrations and a scheduler, so assert its source (as F21 does)."""
    from app import main

    tree = ast.parse(inspect.getsource(main.lifespan).lstrip())
    calls = [ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert "warn_open_production_defaults(app_settings)" in calls
    assert calls.index("refuse_open_production_captcha(app_settings)") < calls.index(
        "warn_open_production_defaults(app_settings)"
    )


def test_the_release_smoke_turns_the_captcha_off_explicitly():
    """It boots the published backend in production with no Turnstile secret, so without the
    rollback lever the refusal above would fail every release smoke."""
    import yaml
    from pathlib import Path

    root = next(
        c for c in Path(__file__).resolve().parents if (c / "compose.smoke.yaml").exists()
    )
    smoke = yaml.safe_load((root / "compose.smoke.yaml").read_text())
    env = smoke["services"]["backend"]["environment"]
    assert env["APP_ENV"] == "production" and env["CAPTCHA_REQUIRED"] == "false"
