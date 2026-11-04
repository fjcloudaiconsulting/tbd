"""TBD-654 / INF-05 on REAL MySQL: ``_bootstrap_lock`` serialises first-user setup.

Two concurrent holders never overlap, and the lock is released when the body
raises (a refused setup must not wedge the next one). Run with
SIGNUP_MYSQL_URL=mysql+aiomysql://...; SIGNUP_MYSQL_REQUIRED=1 makes a missing
URL a failure instead of a skip. SQLite has no named locks, so there the lock
is a no-op (fenced in test_auth_register_setup_only.py).

The full two-request register race needs an empty ``users`` table, which the
shared CI database does not have, so this fences the lock the route holds.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.routers import auth as auth_module

URL = os.environ.get("SIGNUP_MYSQL_URL")
if not URL:
    if os.environ.get("SIGNUP_MYSQL_REQUIRED") == "1":
        raise RuntimeError("SIGNUP_MYSQL_REQUIRED=1 but SIGNUP_MYSQL_URL is not set")
    pytestmark = pytest.mark.skipif(True, reason="SIGNUP_MYSQL_URL not set")


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    eng = create_async_engine(URL)
    try:
        yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_two_holders_never_overlap(factory):
    events: list[str] = []

    async def leg(i: int) -> None:
        async with factory() as db:
            async with auth_module._bootstrap_lock(db):
                events.append(f"in{i}")
                await asyncio.sleep(0.3)
                events.append(f"out{i}")

    await asyncio.gather(leg(0), leg(1))
    first = events[0][-1]
    assert events[1] == f"out{first}", events


@pytest.mark.asyncio
async def test_the_lock_is_released_when_the_body_raises(factory):
    async with factory() as db:
        with pytest.raises(HTTPException):
            async with auth_module._bootstrap_lock(db):
                raise HTTPException(status_code=403)
    # A second holder gets the lock at once rather than waiting out the timeout.
    async with factory() as db:
        await asyncio.wait_for(_enter_and_leave(db), timeout=2)


async def _enter_and_leave(db: AsyncSession) -> None:
    async with auth_module._bootstrap_lock(db):
        pass
