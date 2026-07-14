"""Validation for the optional MFA_RECOVERY_HMAC_KEY (recovery-code hashing
decouple from jwt_secret_key). See specs/mfa-recovery-hmac-key-decouple.md."""

import pytest
from pydantic import ValidationError

from app.config import Settings

_VALID_JWT = "jwt-secret-key-at-least-32-characters-long-000"
_VALID_DEDICATED = "dedicated-recovery-key-distinct-and-32plus-chars"


def _settings(**overrides) -> Settings:
    base = {"_env_file": None, "jwt_secret_key": _VALID_JWT}
    base.update(overrides)
    return Settings(**base)


def test_defaults_to_unset_noop() -> None:
    assert _settings().mfa_recovery_hmac_key == ""


def test_valid_distinct_key_accepted() -> None:
    s = _settings(mfa_recovery_hmac_key=_VALID_DEDICATED)
    assert s.mfa_recovery_hmac_key == _VALID_DEDICATED


def test_whitespace_only_normalized_to_empty_noop() -> None:
    # Whitespace-only must collapse to "" so downstream truthiness ("is it
    # set?") isn't fooled into using whitespace as an HMAC key.
    s = _settings(mfa_recovery_hmac_key="   \t  ")
    assert s.mfa_recovery_hmac_key == ""


def test_surrounding_whitespace_is_stripped() -> None:
    s = _settings(mfa_recovery_hmac_key=f"  {_VALID_DEDICATED}  ")
    assert s.mfa_recovery_hmac_key == _VALID_DEDICATED


def test_too_short_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        _settings(mfa_recovery_hmac_key="short-key")


def test_equal_to_jwt_secret_rejected() -> None:
    with pytest.raises(ValidationError, match="must differ from JWT_SECRET_KEY"):
        _settings(mfa_recovery_hmac_key=_VALID_JWT)


def test_equal_to_jwt_secret_after_strip_rejected() -> None:
    # Equality is checked against the stripped value.
    with pytest.raises(ValidationError, match="must differ from JWT_SECRET_KEY"):
        _settings(mfa_recovery_hmac_key=f"  {_VALID_JWT}  ")
