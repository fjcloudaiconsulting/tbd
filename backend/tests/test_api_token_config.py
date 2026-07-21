"""Validation for the required-in-production API_TOKEN_HMAC_KEY (superadmin
Personal Access Token hashing pepper, decoupled from jwt_secret_key). See
specs/ for the PAT feature this settings knob backs.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings

_VALID_JWT = "jwt-secret-key-at-least-32-characters-long-000"
_VALID_PAT_KEY = "dedicated-pat-hmac-key-distinct-and-32plus-chars"
_VALID_PAT_KEY_PREV = "previous-rotation-pat-hmac-key-distinct-32plus"


def _settings(**overrides) -> Settings:
    base = {"_env_file": None, "jwt_secret_key": _VALID_JWT}
    base.update(overrides)
    return Settings(**base)


def test_prod_requires_api_token_hmac_key():
    with pytest.raises(ValidationError, match="API_TOKEN_HMAC_KEY"):
        _settings(app_env="production", api_token_hmac_key=None)


def test_prod_accepts_valid_key():
    s = _settings(app_env="production", api_token_hmac_key=_VALID_PAT_KEY)
    assert s.api_token_hmac_key == _VALID_PAT_KEY


def test_key_must_differ_from_jwt_secret():
    with pytest.raises(ValidationError, match="must differ"):
        _settings(app_env="production", api_token_hmac_key=_VALID_JWT)


def test_key_min_length():
    with pytest.raises(ValidationError):
        _settings(app_env="production", api_token_hmac_key="short")


def test_dev_allows_unset_key():
    s = _settings(app_env="development", api_token_hmac_key=None)
    assert s.api_token_hmac_key is None


def test_default_expiry_days():
    s = _settings()
    assert s.api_token_default_expiry_days == 30
    assert s.api_token_max_expiry_days == 90


# ── api_token_hmac_key_prev (verify-only rotation key) ──────────────────────
#
# Unlike the primary key, PREV has no prod-required branch (it's an optional
# rotation aid) — only validate-when-present, mirroring the same two checks
# (min length, differs from jwt_secret_key) applied to the primary key.


def test_prev_key_unset_is_fine():
    s = _settings(app_env="production", api_token_hmac_key=_VALID_PAT_KEY)
    assert s.api_token_hmac_key_prev is None


def test_prev_key_accepts_valid_distinct_key():
    s = _settings(
        app_env="production",
        api_token_hmac_key=_VALID_PAT_KEY,
        api_token_hmac_key_prev=_VALID_PAT_KEY_PREV,
    )
    assert s.api_token_hmac_key_prev == _VALID_PAT_KEY_PREV


def test_prev_key_min_length():
    with pytest.raises(ValidationError, match="API_TOKEN_HMAC_KEY_PREV"):
        _settings(
            app_env="production",
            api_token_hmac_key=_VALID_PAT_KEY,
            api_token_hmac_key_prev="short",
        )


def test_prev_key_must_differ_from_jwt_secret():
    with pytest.raises(ValidationError, match="must differ"):
        _settings(
            app_env="production",
            api_token_hmac_key=_VALID_PAT_KEY,
            api_token_hmac_key_prev=_VALID_JWT,
        )
