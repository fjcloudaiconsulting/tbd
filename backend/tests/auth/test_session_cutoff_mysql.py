"""TBD-583: the whole-second session cutoff on REAL MySQL, in both directions.

Run with SESSION_CUTOFF_MYSQL_URL=mysql+aiomysql://... pointing at a
disposable database already at ``alembic upgrade head``. The users cutoff
columns are DATETIME(0) there and MySQL rounds a fraction on insert, which
SQLite (it keeps the microseconds) cannot show. The fixture deletes the rows
these tests write.

* (a) A token minted earlier in the second of a cutoff write must not pass it.
* (b) A token minted after the cutoff, once a sign-in has waited it out, must.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.api_token import ApiToken
from app.models.user import Organization, Role, User
from app.security import create_access_token, decode_token, session_cutoff, token_cutoff, wait_out_cutoff
from app.services import admin_org_members_service, api_token_service

URL = os.environ.get("SESSION_CUTOFF_MYSQL_URL")
if not URL:
    # CI sets REQUIRED=1, so a missing URL there is red, never a skip.
    if os.environ.get("SESSION_CUTOFF_MYSQL_REQUIRED") == "1":
        raise RuntimeError("SESSION_CUTOFF_MYSQL_URL is required here")
    pytestmark = pytest.mark.skipif(True, reason="SESSION_CUTOFF_MYSQL_URL not set")

_CREATED_ORGS: list[int] = []


@pytest_asyncio.fixture
async def factory():
    eng = create_async_engine(URL)
    _CREATED_ORGS.clear()
    try:
        yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    finally:
        async with eng.begin() as conn:
            for org_id in _CREATED_ORGS:
                uids = select(User.id).where(User.org_id == org_id)
                await conn.execute(delete(ApiToken).where(ApiToken.created_by_user_id.in_(uids)))
                await conn.execute(delete(User).where(User.org_id == org_id))
                await conn.execute(delete(Organization).where(Organization.id == org_id))
        await eng.dispose()


async def _org_with(f, *roles: Role) -> list[int]:
    tag = uuid.uuid4().hex[:8]
    async with f() as db:
        org = Organization(name=f"cutoff-{tag}", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        users = [
            User(org_id=org.id, username=f"c{tag}{i}", email=f"c{tag}{i}@example.com",
                 password_hash="x", role=role, is_active=True)
            for i, role in enumerate(roles)
        ]
        db.add_all(users)
        await db.commit()
    _CREATED_ORGS.append(org.id)
    return [u.id for u in users]


async def _at_fraction(fraction: float) -> None:
    """Sleep until the wall clock is ``fraction`` of the way into a second."""
    await asyncio.sleep((fraction - time.time() % 1) % 1 or 1)


async def _user(f, uid: int) -> User:
    async with f() as db:
        return await db.get(User, uid)


def _iat(token: str) -> datetime:
    return datetime.fromtimestamp(decode_token(token)["iat"], tz=timezone.utc)


@pytest.mark.asyncio
async def test_a_deactivation_refuses_that_seconds_tokens_after_a_reactivation(factory) -> None:
    """The deactivation lands at .2 of a second. Written raw, MySQL rounds it
    DOWN to that second, so a token minted earlier in it (``iat`` floored to
    the same second) passes ``iat < cutoff`` again once an admin reactivates
    the member."""
    owner_id, member_id = await _org_with(factory, Role.OWNER, Role.MEMBER)
    async with factory() as db:
        owner = await db.get(User, owner_id)
        org_id = owner.org_id
        await _at_fraction(0.2)
        token = create_access_token(member_id, org_id, Role.MEMBER.value)
        await admin_org_members_service.update_member(
            db, org_id=org_id, user_id=member_id, actor=owner, is_active=False,
        )
        await db.commit()
        await admin_org_members_service.update_member(
            db, org_id=org_id, user_id=member_id, actor=owner, is_active=True,
        )
        await db.commit()
    member = await _user(factory, member_id)
    assert member.is_active
    assert _iat(token) < token_cutoff(member)


@pytest.mark.asyncio
async def test_b_tokens_minted_after_a_waited_out_change_are_live(factory) -> None:
    """The change lands at .7 of a second; a raw cutoff would round UP past
    it. A sign-in waits the stored cutoff out and its JWT passes; an agent
    token minted in that same second was floored ONTO the cutoff and died on
    arrival (``created_at <= cutoff``) until the mint waited a second too."""
    (uid,) = await _org_with(factory, Role.MEMBER)
    await _at_fraction(0.7)
    async with factory() as db:
        user = await db.get(User, uid)
        cutoff = session_cutoff()
        user.password_changed_at = cutoff
        user.sessions_invalidated_at = cutoff
        await db.commit()
    user = await _user(factory, uid)
    await wait_out_cutoff(user)  # what login does
    assert not _iat(create_access_token(uid, user.org_id, Role.MEMBER.value)) < token_cutoff(user)
    async with factory() as db:
        user = await db.get(User, uid)
        _plain, row = await api_token_service.mint_agent(
            db, user=user, name="cli", scope="agent:read", expires_in_days=1,
            cutoff_seen=token_cutoff(user),
        )
        row_id = row.id
    async with factory() as db:
        row = await db.get(ApiToken, row_id)
        user = await db.get(User, uid)
    assert api_token_service.agent_token_status(row, token_cutoff(user)) == "active"
