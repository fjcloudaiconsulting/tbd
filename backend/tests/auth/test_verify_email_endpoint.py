"""Behavioural fences for POST /api/v1/auth/verify-email (TBD-366).

Before this file, grepping `verify-email|verify_email|email_verify` across
the whole of `backend/tests/` returned exactly two things, NEITHER of which
tested behaviour: a reachability row in the public-route allowlist, and URL
string assertions in the email-template tests. The endpoint had **zero**
behavioural coverage.

It is on the closed 25-pair public allowlist -- unauthenticated by design,
with the token as its only credential -- so its guards are the whole of its
security:

* the token-TYPE discriminator (`payload["type"] != "email_verify"`), which
  stops a token minted for another purpose being redeemed here;
* the **S-P2-1 email-binding guard** (`not token_email or token_email !=
  user.email`), whose entire purpose is refusing a token issued for an
  address the account has since changed away from, and refusing a
  pre-migration token that carries no `email` claim at all.

⚠ WHY THESE FENCES DO NOT ASSERT ON THE RESPONSE BODY.

All four rejection paths raise the SAME status and the SAME detail string
("Invalid or expired verification token"), deliberately -- the endpoint is
public and must not disclose which check failed. So a test that asserts
`400` and that detail CANNOT distinguish which guard fired, and would be
satisfied by the wrong one. That is the exact failure the ticket calls out.

Each fence therefore does two things instead:

1. Builds a request that would be ACCEPTED if the guard under test were
   removed, and that passes every other guard. So the only thing standing
   between the request and a 200 is the one guard named.
2. Asserts the STATE consequence -- `email_verified` is still False -- not
   just the status code. A 400 from the wrong guard would still leave the
   flag False, which is why point 1 carries the weight: the injection proof
   for each fence is that deleting its guard turns the request into a 200
   with `email_verified` True.

Recorded injections, each run against this file and then restored:

* delete the `type` check                         -> V2, V3 RED
* `not token_email or ...` -> `token_email and ...` -> V4 RED
* delete the `token_email != user.email` arm      -> V5, V6 RED
* `scalar_one_or_none()` -> `scalar_one()`        -> V7 RED
* refuse unconditionally                          -> V1, V8 RED

⚠ THE FIRST DRAFT OF V2/V3 WAS VACUOUS AND THE INJECTION GATE CAUGHT IT.
They used a password-reset and an access token, neither of which carries an
`email` claim -- so deleting the type check left both refused anyway by the
binding guard's `not token_email` arm, and the type-check mutant ran GREEN
against the two tests written to kill it. Sibling-rule suppression: a guard
downstream of the one under test masks its removal. The fix was to pick
token shapes that pass every downstream guard (an invitation token, which
is the one other real token type carrying an `email` claim, and an
access-shaped token signed with a matching claim). V7b keeps the original
pair as an honestly-labelled reachability control.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import router as auth_router
from app.security import (
    create_access_token,
    create_email_verification_token,
    create_invitation_token,
    create_password_reset_token,
    hash_password,
)

PASSWORD = "S3cret-Pass!"
EMAIL = "alice@example.com"


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def reset_limiter():
    """`/verify-email` carries `10/minute`. Several tests here hit it, and
    without a reset the later ones bleed into that budget and 429."""
    limiter.reset()
    yield
    limiter.reset()


def make_app(session_factory) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(auth_router)
    return app


async def _seed_user(
    factory, *, email_verified: bool = False, email: str = EMAIL
) -> int:
    async with factory() as db:
        org = Organization(name="org", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username="alice",
            email=email,
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=email_verified,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _is_verified(factory, user_id: int) -> bool:
    async with factory() as db:
        user = await db.scalar(select(User).where(User.id == user_id))
        return user.email_verified


async def _set_email(factory, user_id: int, email: str) -> None:
    async with factory() as db:
        user = await db.scalar(select(User).where(User.id == user_id))
        user.email = email
        await db.commit()


def _post(app, token: str):
    with TestClient(app) as client:
        return client.post("/api/v1/auth/verify-email", json={"token": token})


@pytest.mark.asyncio
async def test_valid_token_verifies_the_account(session_factory):
    """V1. HAPPY PATH. Nothing asserted that redeeming a valid token
    actually sets `email_verified = True`.

    Kills an endpoint that returns 200 without writing the flag -- which
    would leave the user permanently unable to log in
    (`/auth/login` 403s unverified accounts unconditionally) while the UI
    told them verification succeeded.
    """
    user_id = await _seed_user(session_factory, email_verified=False)
    token = create_email_verification_token(user_id, EMAIL)

    resp = _post(make_app(session_factory), token)

    assert resp.status_code == 200
    assert resp.json() == {"detail": "Email verified"}
    assert await _is_verified(session_factory, user_id) is True


@pytest.mark.asyncio
async def test_invitation_token_is_rejected_by_the_type_check(session_factory):
    """V2. TYPE DISCRIMINATOR, genuinely isolated.

    ⚠ THE FIRST DRAFT OF THIS FENCE WAS VACUOUS, and the injection gate is
    what caught it. It used a password-reset token, which carries NO `email`
    claim -- so deleting the type check left the request refused anyway by
    the binding guard's `not token_email` arm, and the test stayed green
    against the very mutant it was written to kill. It passed for the wrong
    reason. That is this repo's sibling-rule suppression class.

    An INVITATION token is the right instrument: it is minted by a real
    function, is correctly signed, and is the one other token type that
    carries an `email` claim. Passing the user's own id as the invitation id
    (a collision that happens naturally) makes `sub` and `email` both match,
    so every downstream guard passes and the ONLY thing refusing this token
    is `payload["type"] != "email_verify"`.

    Kills: dropping the type check. Proven RED against that mutant -- the
    request 200s and sets `email_verified` when the check is removed.
    """
    user_id = await _seed_user(session_factory, email_verified=False)
    invitation = create_invitation_token(user_id, EMAIL)

    resp = _post(make_app(session_factory), invitation)

    assert resp.status_code == 400
    assert await _is_verified(session_factory, user_id) is False


@pytest.mark.asyncio
async def test_access_shaped_token_is_rejected_by_the_type_check(session_factory):
    """V3. The same isolation for the ACCESS shape, fenced separately.

    An access token is the credential an attacker is most likely to already
    hold. `create_access_token` emits no `email` claim, so a token straight
    from it is masked by the binding guard exactly as the reset token was
    (see V7b). This token is therefore signed with the real key and given
    `type: "access"` plus a matching `email`, so that the type check is the
    only thing standing in the way.

    Kills a type check that special-cased one token shape: V2 would pass
    and this would fail.
    """
    user_id = await _seed_user(session_factory, email_verified=False)
    access_shaped = jwt.encode(
        {
            "sub": str(user_id),
            "email": EMAIL,
            "type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    resp = _post(make_app(session_factory), access_shaped)

    assert resp.status_code == 400
    assert await _is_verified(session_factory, user_id) is False


@pytest.mark.asyncio
async def test_real_password_reset_and_access_tokens_are_also_refused(session_factory):
    """V7b. DEFENCE IN DEPTH, labelled honestly.

    Both of these are refused today, but NOT by the type check: neither
    carries an `email` claim, so the binding guard's `not token_email` arm
    takes them first. This test therefore documents that the two guards
    overlap for the token shapes that actually exist in this system -- it is
    a reachability control, and it is explicitly NOT the fence for the type
    discriminator. V2 and V3 are.

    Recording that distinction is the point. Reading this as a type-check
    fence is exactly the mistake the first draft made.
    """
    user_id = await _seed_user(session_factory, email_verified=False)
    app = make_app(session_factory)

    for token in (
        create_password_reset_token(user_id),
        create_access_token(user_id, org_id=1, role=Role.OWNER.value),
    ):
        resp = _post(app, token)
        assert resp.status_code == 400
        assert await _is_verified(session_factory, user_id) is False


@pytest.mark.asyncio
async def test_token_without_an_email_claim_is_rejected(session_factory):
    """V4. THE PRE-MIGRATION TOKEN SHAPE. S-P2-1's `not token_email` arm.

    This token is signed with the real key, carries the right `sub` and the
    right `type`, and differs from a valid one ONLY in having no `email`
    claim -- the shape every token issued before the binding migration has.
    It must NOT be treated as a pass.

    Kills: writing the guard as `token_email and token_email != user.email`
    (or simply `token_email != user.email`, where a missing claim is None
    and compares unequal only by luck of the current schema). Under the
    forgiving form this request 200s and verifies the account.
    """
    user_id = await _seed_user(session_factory, email_verified=False)
    legacy = jwt.encode(
        {
            "sub": str(user_id),
            "type": "email_verify",
            "exp": datetime.now(timezone.utc) + timedelta(hours=24),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    resp = _post(make_app(session_factory), legacy)

    assert resp.status_code == 400
    assert await _is_verified(session_factory, user_id) is False


@pytest.mark.asyncio
async def test_token_for_a_since_changed_address_is_rejected(session_factory):
    """V5. THE ACTUAL S-P2-1 SCENARIO, and the reason the guard exists.

    The user is issued a verification token for their current address, then
    changes their email, then clicks the older link. Without the binding
    the link would verify the account against an address the user no longer
    holds -- proving control of a mailbox that is no longer the account's.

    Kills: dropping `token_email != user.email`. Every other guard passes
    here (real signature, right `sub`, right `type`, `email` claim
    present), so this request 200s the moment that comparison goes.
    """
    user_id = await _seed_user(session_factory, email_verified=False)
    token = create_email_verification_token(user_id, EMAIL)
    await _set_email(session_factory, user_id, "alice+new@example.com")

    resp = _post(make_app(session_factory), token)

    assert resp.status_code == 400
    assert await _is_verified(session_factory, user_id) is False


@pytest.mark.asyncio
async def test_token_bound_to_a_different_address_is_rejected(session_factory):
    """V6. The polarity twin of V5: the token's claim, not the account's
    address, is the odd one out.

    Kills a guard written as a one-sided check against a constant, or one
    that compares the claim to itself. V5 moves the ACCOUNT, this moves the
    TOKEN, and a correct implementation refuses both.
    """
    user_id = await _seed_user(session_factory, email_verified=False)
    token = create_email_verification_token(user_id, "someone-else@example.com")

    resp = _post(make_app(session_factory), token)

    assert resp.status_code == 400
    assert await _is_verified(session_factory, user_id) is False


@pytest.mark.asyncio
async def test_token_for_a_nonexistent_user_is_rejected(session_factory):
    """V7. The user-lookup arm. A correctly signed token whose `sub` names
    no row must 400 rather than raise.

    Kills replacing `scalar_one_or_none()` with `scalar_one()`, which turns
    a deleted account's stale link into a 500 -- and a 500 on a public
    endpoint is an availability and disclosure problem, not a 400.
    """
    await _seed_user(session_factory, email_verified=False)
    token = create_email_verification_token(999999, EMAIL)

    resp = _post(make_app(session_factory), token)

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_redeeming_twice_is_a_harmless_200(session_factory):
    """V8. CONTROL. The endpoint is idempotent by design: an already-verified
    account redeeming a still-valid token is a harmless 200, not an error.

    This is the fence that stops V2-V7 being "fixed" by making the endpoint
    refuse everything. Without it, a guard that rejects unconditionally
    passes every other test in this file.
    """
    user_id = await _seed_user(session_factory, email_verified=True)
    token = create_email_verification_token(user_id, EMAIL)

    resp = _post(make_app(session_factory), token)

    assert resp.status_code == 200
    assert await _is_verified(session_factory, user_id) is True
