"""INF-05: validation for the optional SETUP_TOKEN bootstrap secret."""

import pytest
from pydantic import ValidationError

from app.config import Settings

_VALID_JWT = "jwt-secret-key-at-least-32-characters-long-000"
_VALID_TOKEN = "a" * 32


def _settings(**overrides) -> Settings:
    base = {"_env_file": None, "jwt_secret_key": _VALID_JWT}
    base.update(overrides)
    return Settings(**base)


def test_defaults_to_unset() -> None:
    assert _settings().setup_token == ""


def test_thirty_two_characters_accepted() -> None:
    assert _settings(setup_token=_VALID_TOKEN).setup_token == _VALID_TOKEN


def test_too_short_rejected() -> None:
    with pytest.raises(ValidationError, match="SETUP_TOKEN must be at least 32 characters"):
        _settings(setup_token="a" * 31)


@pytest.mark.parametrize("token", [f" {_VALID_TOKEN}", f"{_VALID_TOKEN}\n", f"\t{_VALID_TOKEN} "])
def test_surrounding_whitespace_rejected(token: str) -> None:
    with pytest.raises(ValidationError, match="must not start or end with whitespace"):
        _settings(setup_token=token)
