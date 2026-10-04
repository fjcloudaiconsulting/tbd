"""REDIS_URL is required in production (TBD-438).

Redis is the auth **session store**. Every token-issue path in
``routers/auth.py`` fails closed without it, so a production instance booted
with ``REDIS_URL`` unset cannot log anybody in — it comes up healthy-looking
and refuses every login.

Why a boot refusal is earned here
---------------------------------
``config.py`` already states the criterion, in the note explaining why
``founder_count_exclude_usernames`` deliberately does NOT get one:

    "the blast radius of a boot refusal is the whole application down.
    ``api_token_hmac_key`` earns its prod-required validator because losing it
    breaks PAT authentication -- a security primitive."

Redis is the session store for interactive auth, which is the same class of
primitive. This validator applies the existing rule rather than inventing a
new one, and mirrors ``_validate_api_token_hmac_key`` in shape.

⚠ THE COUPLED CHANGE THIS FILE EXISTS TO PROTECT.
``backend/scripts/migrate.py`` imports ``app.logging`` (line 42), which does
``from app.config import settings`` (``app/logging.py:7``), which constructs
``Settings()`` at module import (``config.py:601``). The production migrate
init container runs with ``APP_ENV=production``, so it needs REDIS_URL bound
or every rollout fails before any backend replica starts. That binding lives
in fjcloudaiconsulting/aws-infra (``clusters/platform/tbd-prod/backend.yaml``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

_VALID_JWT = "jwt-secret-key-at-least-32-characters-long-000"
_VALID_PAT_KEY = "dedicated-pat-hmac-key-distinct-and-32plus-chars"
_REDIS = "redis://10.42.0.5:6379/0"


def _settings(**overrides) -> Settings:
    """Construct Settings off the real defaults, never the developer's .env.

    ``_env_file=None`` matters: without it a populated local ``.env`` supplies
    ``redis_url`` and the required-in-production test passes vacuously on the
    author's machine while failing in CI (or vice versa).
    """
    base = {"_env_file": None, "jwt_secret_key": _VALID_JWT}
    base.update(overrides)
    return Settings(**base)


def _prod(**overrides) -> Settings:
    """A production Settings with every OTHER prod-required knob satisfied.

    ``api_token_hmac_key`` is required in production too (``config.py:508``).
    Supplying it here is what makes a raised ValidationError unambiguously
    about REDIS_URL — otherwise this file would go green on the wrong error.
    """
    base = {"app_env": "production", "api_token_hmac_key": _VALID_PAT_KEY}
    base.update(overrides)
    return _settings(**base)


# ── The ruling ──────────────────────────────────────────────────────────────


def test_production_refuses_to_construct_without_redis_url():
    """F1: the fence. Kills 'no validator at all'."""
    with pytest.raises(ValidationError, match="REDIS_URL"):
        _prod(redis_url="")


def test_production_accepts_a_configured_redis_url():
    """F2: positive control. Kills a validator that raises unconditionally.

    Without this, ``raise ValueError("REDIS_URL is required in production")``
    with no guard at all would satisfy the test above and break every deploy.
    """
    assert _prod(redis_url=_REDIS).redis_url == _REDIS


def test_whitespace_only_redis_url_is_refused_in_production():
    """F3: kills a truthiness check that a space satisfies.

    ``redis_url = " "`` is truthy, so a bare ``if not self.redis_url`` passes
    it through and the app boots with an unusable connection string.
    """
    with pytest.raises(ValidationError, match="REDIS_URL"):
        _prod(redis_url="   ")


@pytest.mark.parametrize("env", ["development", "test", "staging"])
def test_non_production_still_constructs_without_redis_url(env):
    """F4: kills a validator that forgot to scope itself to production.

    Local development runs without Redis by design. A validator missing its
    ``app_env`` branch would break every developer's stack and the whole test
    suite, so this is also the blast-radius control on the change itself.
    """
    assert _settings(app_env=env, redis_url="").redis_url == ""
