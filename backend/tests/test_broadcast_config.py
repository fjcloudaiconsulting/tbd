"""BROADCAST_MAX_ATTEMPTS lower bound (TBD-637).

The drain's claim UPDATE requires ``attempts < max_attempts``, so a value of 0
would claim nothing and leave every broadcast stuck in "sending".
"""

import pytest
from pydantic import ValidationError

from app.config import Settings

_VALID_JWT = "jwt-secret-key-at-least-32-characters-long-000"


def _settings(**overrides) -> Settings:
    base = {"_env_file": None, "jwt_secret_key": _VALID_JWT}
    base.update(overrides)
    return Settings(**base)


@pytest.mark.parametrize("value", [0, -1])
def test_broadcast_max_attempts_below_one_is_refused(value: int) -> None:
    """FENCE. Wrong implementation: no bound (a 0 stalls every send)."""
    with pytest.raises(ValidationError, match="BROADCAST_MAX_ATTEMPTS"):
        _settings(broadcast_max_attempts=value)


def test_broadcast_max_attempts_of_one_is_accepted() -> None:
    """GUARD. Wrong implementation: an off-by-one bound refusing 1."""
    assert _settings(broadcast_max_attempts=1).broadcast_max_attempts == 1
