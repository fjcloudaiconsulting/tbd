"""Pin-contract regression tests for dependencies whose unpinned drift
has previously broken the application at runtime."""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _parse_requirements() -> dict[str, str]:
    """Return ``{name_lower: version}`` for every ``==`` / ``~=`` pin in the
    ``app`` dependency group of ``backend/pyproject.toml``; strips extras
    (``pydantic[email]`` -> ``pydantic``)."""
    pins: dict[str, str] = {}
    for line in tomllib.loads(PYPROJECT.read_text())["dependency-groups"]["app"]:
        for sep in ("==", "~="):
            if sep in line:
                name, _, ver = line.partition(sep)
                pins[name.split("[", 1)[0].strip().lower()] = ver.strip()
                break
    return pins


def test_pymysql_is_pinned() -> None:
    """PyMySQL is a transitive dependency of aiomysql. SQLAlchemy's
    mysql dialect decides whether to invoke ``ping()`` with or without
    an argument by inspecting ``PyMySQL.connect.__doc__`` at engine
    init. Drift to a PyMySQL whose docstring no longer matches that
    detection breaks every DB pool checkout, because the async adapter's
    ``ping(self, reconnect)`` still requires the positional arg.
    Pinning PyMySQL is therefore part of the contract — not optional."""
    assert "pymysql" in _parse_requirements(), (
        "PyMySQL must be pinned in pyproject.toml. aiomysql does "
        "not pin it, so a fresh `uv lock` will resolve whatever "
        "PyMySQL is current on PyPI — and that has caused production "
        "outages when SQLAlchemy's dialect detection silently flipped."
    )
