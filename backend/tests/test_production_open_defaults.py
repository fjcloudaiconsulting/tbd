"""INF-05: production logs every registration safeguard it runs without.

Captcha, its hostname/action pins and the CORS allowlist all default open, and production is only
documented to close them. A boot refusal would take production down (or stop sign-ups) over a flag
that is also the documented rollback lever, so the guard is a startup warning, not a validator.
"""

from __future__ import annotations

import ast
import inspect
import logging

from app.config import Settings, TURNSTILE_ALWAYS_PASS_SECRET, warn_open_production_defaults

_JWT = "jwt-secret-key-at-least-32-characters-long-000"
_PAT = "dedicated-pat-hmac-key-distinct-and-32plus-chars"
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


def test_the_shipped_defaults_warn_once_for_the_captcha_and_once_per_bad_origin(caplog):
    """Defaults as shipped: captcha off makes its pins moot, so one captcha warning, plus CORS."""
    caplog.set_level(logging.WARNING, logger="app.config")
    reasons = warn_open_production_defaults(
        Settings(_env_file=None, jwt_secret_key=_JWT, api_token_hmac_key=_PAT, app_env="production")
    )
    assert reasons == ["captcha_not_required", "cors_origin_insecure"]
    assert _warned(caplog) == [
        {"reason": "captcha_not_required", "origin": None},
        {"reason": "cors_origin_insecure", "origin": "http://localhost:3000"},
    ]


def test_the_always_pass_test_secret_and_missing_pins_each_warn(caplog):
    caplog.set_level(logging.WARNING, logger="app.config")
    reasons = warn_open_production_defaults(
        _settings(
            captcha_secret=TURNSTILE_ALWAYS_PASS_SECRET,
            captcha_expected_hostname="",
            captcha_expected_action="",
        )
    )
    assert reasons == [
        "captcha_test_secret",
        "captcha_hostname_unpinned",
        "captcha_action_unpinned",
    ]
    assert [w["reason"] for w in _warned(caplog)] == reasons


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
