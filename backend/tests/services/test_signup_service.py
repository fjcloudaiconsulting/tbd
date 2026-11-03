"""TBD-654: the signup-start decision and its per-address mail caps.

The caps depend on the address only, never on the account behind it, so
they are claimed before the state lookup and hold on every path.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select, update

from app.models.session_state import UsedToken
from app.services import signup_service
from app.services.signup_service import (
    MAIL_CAP_10M_SCOPE,
    MAIL_CAP_24H_SCOPE,
    MAIL_CAP_DAILY,
    claim_mail_slot,
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
