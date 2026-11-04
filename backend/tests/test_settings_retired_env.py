"""TBD-661 -- a retired setting left in a deployment's environment must not stop boot.

``FOUNDER_COUNT_EXCLUDE_USERNAMES`` belonged to the removed public founder-count
endpoint. Deployed environments may still set it until it is cleaned up, so
``Settings`` must keep ignoring variables it does not declare.
"""

from __future__ import annotations

from app.config import Settings

_RETIRED = "FOUNDER_COUNT_EXCLUDE_USERNAMES"


def test_retired_variable_in_the_environment_is_ignored(monkeypatch):
    monkeypatch.setenv(_RETIRED, "someone")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not hasattr(s, _RETIRED.lower())


def test_retired_variable_in_the_env_file_is_ignored(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{_RETIRED}=someone\n", encoding="utf-8")
    s = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert not hasattr(s, _RETIRED.lower())
