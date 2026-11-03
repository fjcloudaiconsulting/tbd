"""TBD-654: the signup-start decision and its per-address mail caps.

The caps depend on the address only, never on the account behind it, so
they are claimed before the state lookup and hold on every path.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.session_state import UsedToken
from app.models.user import Organization, Role, User
from app.security import decode_signup_token, decode_token, hash_password
from app.services import email_service, signup_service
from app.services.signup_service import (
    MAIL_CAP_10M_SCOPE,
    MAIL_CAP_24H_SCOPE,
    MAIL_CAP_DAILY,
    claim_mail_slot,
    plan_start,
)

_U = UsedToken.__table__


def _expire(engine, scope: str) -> None:
    """Move every claim in ``scope`` into the past, as if its window ended."""
    with engine.begin() as c:
        c.execute(update(_U).where(_U.c.scope == scope).values(expires_at=datetime(2000, 1, 1)))


def _count(engine, scope: str) -> int:
    with engine.begin() as c:
        return c.scalar(select(func.count()).select_from(_U).where(_U.c.scope == scope))


@pytest.mark.asyncio
async def test_first_claim_allowed_second_within_10m_refused():
    assert await claim_mail_slot("a@b.co") is True
    assert await claim_mail_slot("a@b.co") is False
    assert await claim_mail_slot("other@b.co") is True


@pytest.mark.asyncio
async def test_caps_allow_after_window(_state_engine):
    assert await claim_mail_slot("a@b.co") is True
    _expire(_state_engine, MAIL_CAP_10M_SCOPE)
    assert await claim_mail_slot("a@b.co") is True


@pytest.mark.asyncio
async def test_daily_cap_holds_at_five(_state_engine):
    results = []
    for _ in range(MAIL_CAP_DAILY + 1):
        results.append(await claim_mail_slot("a@b.co"))
        _expire(_state_engine, MAIL_CAP_10M_SCOPE)
    assert results == [True] * 5 + [False]
    # A day later the slots free up again.
    _expire(_state_engine, MAIL_CAP_24H_SCOPE)
    assert await claim_mail_slot("a@b.co") is True


@pytest.mark.asyncio
async def test_refusal_consumes_no_daily_slot(_state_engine):
    assert await claim_mail_slot("a@b.co") is True
    assert await claim_mail_slot("a@b.co") is False
    assert _count(_state_engine, MAIL_CAP_24H_SCOPE) == 1


@pytest.mark.asyncio
async def test_cap_key_is_normalised():
    assert await claim_mail_slot(" A@B.co ") is True
    assert await claim_mail_slot("a@b.co") is False


def test_scopes_fit_column():
    assert MAIL_CAP_DAILY == 5
    for scope in (MAIL_CAP_10M_SCOPE, MAIL_CAP_24H_SCOPE):
        assert len(scope) <= 32


@pytest.mark.asyncio
async def test_claims_are_keyed_by_hmac_never_the_address(monkeypatch):
    """The claimed token is keyed by the HMAC, never the address."""
    seen: list[str] = []

    async def _claim(scope, token, ttl):
        seen.append(token)
        return True

    monkeypatch.setattr(signup_service.state_db, "claim_token", _claim)
    await claim_mail_slot("secret-person@b.co")
    assert seen and all("secret-person" not in t for t in seen)


# ── plan_start: the path per account state ──────────────────────────────────


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        org = Organization(name="Seed Org", billing_cycle_day=1)
        session.add(org)
        await session.flush()
        for name, active, verified in (
            ("existing", True, True),
            ("legacy", True, False),
            ("inactive", False, True),
        ):
            session.add(User(
                org_id=org.id, username=name, email=f"{name}@example.com",
                password_hash=hash_password("seed-password-1"), role=Role.OWNER,
                is_active=active, email_verified=verified,
            ))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("email", "path", "sender"),
    [
        ("new@example.com", "new", email_service.send_signup_link_email),
        ("existing@example.com", "existing", email_service.send_existing_account_signup_email),
        ("legacy@example.com", "legacy_unverified", email_service.send_legacy_verification_email),
        ("inactive@example.com", "inactive", None),
    ],
)
async def test_plan_start_paths(db, email, path, sender):
    got_path, send = await plan_start(db, email)
    assert got_path == path
    if sender is None:
        assert send is None
        return
    assert send.func is sender
    assert send.args[0] == email


@pytest.mark.asyncio
async def test_plan_start_new_mints_signup_token_for_the_address(db):
    _, send = await plan_start(db, "new@example.com")
    claims = decode_signup_token(send.args[1])
    assert claims is not None and claims.email == "new@example.com"


@pytest.mark.asyncio
async def test_plan_start_legacy_mints_verification_for_the_account(db):
    _, send = await plan_start(db, "legacy@example.com")
    payload = decode_token(send.args[1])
    user_id = await db.scalar(select(User.id).where(User.email == "legacy@example.com"))
    assert payload["type"] == "email_verify"
    assert payload["sub"] == str(user_id)
    assert payload["email"] == "legacy@example.com"


@pytest.mark.asyncio
async def test_plan_start_claims_caps_before_state_lookup(monkeypatch):
    class _NoReads:
        async def execute(self, *a, **k):
            raise AssertionError("state was read before the caps")

        scalar = execute

    async def _refuse(email):
        return False

    monkeypatch.setattr(signup_service, "claim_mail_slot", _refuse)
    assert await plan_start(_NoReads(), "existing@example.com") == ("capped", None)


@pytest.mark.asyncio
async def test_start_normalises_email_for_state_and_caps(db):
    """Review focus 1: a padded mixed-case address hits the same account and
    the same cap key as its canonical form."""
    path, send = await plan_start(db, " Existing@Example.COM ")
    assert path == "existing"
    assert send.args[0] == "existing@example.com"
    assert await plan_start(db, "existing@example.com") == ("capped", None)
