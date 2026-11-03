"""TBD-782: bcrypt never runs on the event loop.

A bcrypt hash or check takes tens to hundreds of milliseconds; called straight
from an ``async def`` it stalls every other request on the worker for that
time. Async callers go through ``security.run_bcrypt``, which runs the helper
on a dedicated pool so a login burst does not starve the default executor's
``asyncio.to_thread`` callers (the limits buckets) either.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app import security

from tests.auth.test_session_jti_sid import (  # noqa: F401  (fixtures + helpers)
    _make_app,
    _seed_user,
    reset_limiter,
    session_factory,
)

BACKEND_APP = Path(__file__).resolve().parents[1] / "app"
BCRYPT_FUNCS = {
    "hash_password", "verify_password", "verify_password_or_dummy", "hashpw", "checkpw",
}


def _name(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else getattr(node, "attr", None)


def _bad_calls_in_async(tree: ast.AST) -> list[int]:
    """Lines inside an ``async def`` that call a bcrypt function, or hand one
    to anything but ``run_bcrypt`` (``asyncio.to_thread``, ``run_in_executor``)."""
    lines: list[int] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if _name(node.func) in BCRYPT_FUNCS or (
                _name(node.func) != "run_bcrypt"
                and any(_name(a) in BCRYPT_FUNCS for a in node.args)
            ):
                lines.append(node.lineno)
    return lines


def test_detector_flags_direct_and_default_pool_calls() -> None:
    def bad(body: str) -> list[int]:
        return _bad_calls_in_async(ast.parse(f"async def f(p):\n    return {body}\n"))

    assert bad("hash_password(p)") == [2]
    assert bad("bcrypt.checkpw(p, h)") == [2]
    assert bad("await asyncio.to_thread(hash_password, p)") == [2]
    assert bad("await run_bcrypt(hash_password, p)") == []


def test_no_async_function_runs_bcrypt_outside_its_pool() -> None:
    """FENCE. Wrong implementation: a bcrypt helper called inside an
    ``async def``, or sent to the default executor instead of ``run_bcrypt``."""
    offenders = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [
            f"{path.relative_to(BACKEND_APP).as_posix()}:{n}"
            for n in _bad_calls_in_async(tree)
        ]
    assert offenders == []


@pytest.mark.asyncio
async def test_login_checks_the_password_on_the_bcrypt_pool(
    session_factory, monkeypatch
) -> None:
    """Not the event loop's thread, and not the default executor's."""
    await _seed_user(session_factory)
    seen: list[str] = []
    real = bcrypt.checkpw

    def spy(password: bytes, hashed: bytes) -> bool:
        seen.append(threading.current_thread().name)
        return real(password, hashed)

    monkeypatch.setattr(security.bcrypt, "checkpw", spy)
    with TestClient(_make_app(session_factory)) as client:
        for login in ("alice", "nobody"):  # stored hash, then the dummy one
            r = client.post(
                "/api/v1/auth/login", json={"login": login, "password": "whatever-1"}
            )
            assert r.status_code == 401
    assert len(seen) == 2
    assert all(name.startswith("bcrypt_") for name in seen), seen
