"""TBD-654 on REAL MySQL: two concurrent ``/signup/complete`` calls with one
link make exactly one account and one clean 4xx.

Run with SIGNUP_MYSQL_URL=mysql+aiomysql://... pointing at a disposable
database already at ``alembic upgrade head``; SIGNUP_MYSQL_REQUIRED=1 makes a
missing URL a failure instead of a skip. The rows flush before the link's
``jti`` is claimed, so the loser waits on the winner's unique-key lock and
then meets the account the winner committed. SQLite has no row locks, so it
cannot show this race.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import get_db
from app.deps import get_session_factory
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import router as auth_router
from app.security import create_signup_token, hash_password
from app.services.admin_orgs_service import delete_org_cascade

URL = os.environ.get("SIGNUP_MYSQL_URL")
if not URL:
    if os.environ.get("SIGNUP_MYSQL_REQUIRED") == "1":
        raise RuntimeError("SIGNUP_MYSQL_REQUIRED=1 but SIGNUP_MYSQL_URL is not set")
    pytestmark = pytest.mark.skipif(True, reason="SIGNUP_MYSQL_URL not set")


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    eng = create_async_engine(URL)
    f = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    tag = secrets.token_hex(4)
    org_ids: list[int] = []
    # A user must exist, or both legs answer setup_required.
    async with f() as db:
        org = Organization(name=f"signup-race-seed-{tag}", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        db.add(User(org_id=org.id, username=f"race-seed-{tag}", email=f"race-seed-{tag}@example.com",
                    password_hash=hash_password("seed-password-1"), role=Role.OWNER,
                    is_active=True, email_verified=True))
        await db.commit()
        org_ids.append(org.id)
    f.tag = tag  # type: ignore[attr-defined]
    try:
        yield f
    finally:
        async with f() as db:
            org_ids += list((await db.execute(
                select(User.org_id).where(User.email == f"race-{tag}@example.com")
            )).scalars())
            for org_id in org_ids:
                await delete_org_cascade(db, org_id=org_id)
            await db.commit()
        await eng.dispose()


def _make_app(f) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with f() as session:
            yield session

    async def override_session_factory():
        return f

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(auth_router)
    return app


@pytest.mark.asyncio
async def test_concurrent_completes_with_one_link_make_one_account(factory):
    limiter.reset()
    tag = factory.tag
    email = f"race-{tag}@example.com"
    token = create_signup_token(email)
    transport = httpx.ASGITransport(app=_make_app(factory))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        results = await asyncio.gather(*(
            client.post("/api/v1/auth/signup/complete", json={
                "token": token, "username": f"race-{tag}-{i}", "password": "race-password-1",
            })
            for i in range(2)
        ))
    codes = sorted(r.status_code for r in results)
    assert codes[0] == 200 and 400 <= codes[1] < 500, [r.text for r in results]
    loser = next(r for r in results if r.status_code != 200)
    assert loser.json()["detail"]["code"] in ("account_exists", "invalid_signup_token")
    async with factory() as db:
        assert await db.scalar(
            select(func.count()).select_from(User).where(User.email == email)
        ) == 1
        # The loser's org was rolled back with its user.
        assert await db.scalar(
            select(func.count()).select_from(Organization).where(
                Organization.name.like(f"race-{tag}-%")
            )
        ) == 1
