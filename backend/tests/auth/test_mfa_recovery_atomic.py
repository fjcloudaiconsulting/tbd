"""BE-21: a recovery code is spent with a compare-and-swap on the stored list,
so two requests that read the same list cannot both turn one code into a
session."""
from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import second_factor
from app.auth.second_factor import consume_recovery_code
from app.models import Base
from app.models.user import User
from app.routers import auth as auth_module
from app.security import create_mfa_challenge_token
from app.services import mfa_service
from app.services.mfa_service import generate_recovery_codes, hash_recovery_code

from tests.auth.test_session_jti_sid import (  # noqa: F401  (fixtures + helpers)
    _make_app,
    _seed_user,
    reset_limiter,
)


@pytest_asyncio.fixture
async def file_db(tmp_path) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str]]:
    """A file-backed database, so a second connection can commit a competing
    write while a request is in flight."""
    path = str(tmp_path / "app.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory, path
    finally:
        await engine.dispose()


def _stored_codes(path: str, user_id: int) -> str | None:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT recovery_codes FROM users WHERE id = ?", (user_id,)).fetchone()[0]


@pytest.mark.asyncio
async def test_a_concurrent_spend_of_the_same_code_loses(file_db, monkeypatch) -> None:
    factory, path = file_db
    codes = generate_recovery_codes()
    seed = await _seed_user(factory, mfa_enabled=True, recovery_codes_plaintext=codes)
    user_id = seed["user_id"]
    winner_list = ",".join(hash_recovery_code(c) for c in codes[1:])

    real_verify = mfa_service.verify_recovery_code

    def verify_then_lose_the_race(code, hashed_codes):
        # Another request spent the same code and committed between this
        # request's read of the row and its write.
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE users SET recovery_codes = ? WHERE id = ?", (winner_list, user_id))
        return real_verify(code, hashed_codes)

    monkeypatch.setattr(second_factor, "verify_recovery_code", verify_then_lose_the_race)
    # The pre-fix handler called the router's own import.
    monkeypatch.setattr(auth_module, "verify_recovery_code", verify_then_lose_the_race, raising=False)

    with TestClient(_make_app(factory)) as client:
        res = client.post(
            "/api/v1/auth/mfa/recovery",
            json={"mfa_token": create_mfa_challenge_token(user_id), "code": codes[0]},
        )
    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Invalid recovery code"
    # The winner's write stands; nothing was written back over it.
    assert _stored_codes(path, user_id) == winner_list


@pytest.mark.asyncio
async def test_consume_with_a_stale_row_fails_and_keeps_the_winner(file_db) -> None:
    factory, path = file_db
    codes = generate_recovery_codes()
    seed = await _seed_user(factory, mfa_enabled=True, recovery_codes_plaintext=codes)

    async with factory() as stale_db, factory() as fresh_db:
        stale = await stale_db.scalar(select(User).where(User.id == seed["user_id"]))
        await stale_db.commit()  # the read is over; the object keeps its values
        fresh = await fresh_db.scalar(select(User).where(User.id == seed["user_id"]))
        assert await consume_recovery_code(fresh_db, fresh, codes[0]) is True
        await fresh_db.commit()

        assert await consume_recovery_code(stale_db, stale, codes[0]) is False

    remaining = _stored_codes(path, seed["user_id"]).split(",")
    assert len(remaining) == len(codes) - 1
    assert hash_recovery_code(codes[0]) not in remaining


@pytest.mark.asyncio
async def test_spending_codes_one_by_one_empties_the_list(file_db) -> None:
    factory, path = file_db
    codes = generate_recovery_codes(count=2)
    seed = await _seed_user(factory, mfa_enabled=True, recovery_codes_plaintext=codes)

    async with factory() as db:
        user = await db.scalar(select(User).where(User.id == seed["user_id"]))
        assert await consume_recovery_code(db, user, "not-a-code") is False
        assert await consume_recovery_code(db, user, codes[1]) is True
        assert await consume_recovery_code(db, user, codes[1]) is False  # spent
        assert await consume_recovery_code(db, user, codes[0]) is True
        await db.commit()
    assert _stored_codes(path, seed["user_id"]) is None
