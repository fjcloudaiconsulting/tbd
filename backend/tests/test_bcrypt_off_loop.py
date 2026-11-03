"""TBD-782: bcrypt never runs on the event loop.

A bcrypt hash or check takes tens to hundreds of milliseconds; called straight
from an ``async def`` it stalls every other request on the worker for that
time. Async callers go through ``asyncio.to_thread``, which passes the
function as an argument, so a direct ``Call`` of one inside an ``async def``
is the bug.
"""
from __future__ import annotations

import ast
import asyncio
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
BCRYPT_FUNCS = {"hash_password", "verify_password", "verify_password_or_dummy"}


def _direct_calls_in_async(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                if name in BCRYPT_FUNCS:
                    lines.append(node.lineno)
    return lines


def test_detector_flags_a_direct_call_and_passes_to_thread() -> None:
    wrong = ast.parse("async def f(p):\n    return hash_password(p)\n")
    right = ast.parse("async def f(p):\n    return await asyncio.to_thread(hash_password, p)\n")
    assert _direct_calls_in_async(wrong) == [2]
    assert _direct_calls_in_async(right) == []


def test_no_async_function_calls_bcrypt_directly() -> None:
    """FENCE. Wrong implementation: ``hash_password(...)`` or
    ``verify_password_or_dummy(...)`` called inside an ``async def``."""
    offenders = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [
            f"{path.relative_to(BACKEND_APP).as_posix()}:{n}"
            for n in _direct_calls_in_async(tree)
        ]
    assert offenders == []


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.asyncio
async def test_login_checks_the_password_off_the_event_loop(
    session_factory, monkeypatch
) -> None:
    await _seed_user(session_factory)
    seen: list[bool] = []
    real = bcrypt.checkpw

    def spy(password: bytes, hashed: bytes) -> bool:
        seen.append(_on_loop())
        return real(password, hashed)

    monkeypatch.setattr(security.bcrypt, "checkpw", spy)
    with TestClient(_make_app(session_factory)) as client:
        for login in ("alice", "nobody"):  # stored hash, then the dummy one
            r = client.post(
                "/api/v1/auth/login", json={"login": login, "password": "whatever-1"}
            )
            assert r.status_code == 401
    assert seen == [False, False]
