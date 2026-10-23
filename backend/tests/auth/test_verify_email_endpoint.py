"""Behavioural fences for POST /api/v1/auth/verify-email (TBD-366).

Before this file, grepping `verify-email|verify_email|email_verify` across
the whole of `backend/tests/` returned exactly two things, NEITHER of which
tested behaviour: a reachability row in the public-route allowlist, and URL
string assertions in the email-template tests. The endpoint had **zero**
behavioural coverage.

It is on the closed 26-pair public allowlist -- unauthenticated by design,
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

⚠ A SECOND GAP, FOUND BY REVIEW AND NOW CLOSED. `payload is None or ...` has
TWO arms and the first draft fenced only the second. Deleting the
`payload is None` arm left all nine tests GREEN while turning garbage,
EXPIRED and wrong-key tokens into a 500 on a public endpoint -- the same
class V7 fences, on a strictly more reachable path. V9/V10/V11 close it, and
V10 additionally kills decoding with `options={"verify_exp": False}`.

WHAT INDIVIDUAL TESTS HERE DO AND DO NOT BUY, measured rather than assumed:

* V6 kills nothing V5 does not; its kill set is a strict subset, because
  V6's address pair differs in more places than V5's. Keep it for the
  polarity intent it records, but do not count it as independent coverage.
  V5 is the uniquely valuable one -- it alone catches plus-tag
  normalisation, a plausible real mistake.
* V7b kills nothing under any single-point mutant: both its tokens are
  refused by two guards at once, so two must fail together for it to fire,
  and V4 already flags that. It is documentation, and labelled as such. Note
  it would NOT start fencing anything if `create_access_token` gained an
  `email` claim -- the type check refuses `type: "access"` regardless.
* Dropping the `not token_email` arm alone is an EQUIVALENT MUTANT and
  survives correctly: `None != <str>` and `"" != <str>` both refuse, so that
  arm is purely defensive. Mutation tooling will show it surviving. Do not
  chase it with a test; there is no reachable input that distinguishes it.
* `int(payload["sub"])` raises on a non-integer or missing `sub`, but only
  the signing key can mint such a token, so it is a hardening item rather
  than a live hole. Deliberately not fenced here.
* The `10/minute` limit is unfenced, and the autouse `reset_limiter` is
  load-bearing: the file sits exactly at the budget. No assertion here can
  be satisfied by a 429 -- every assert is `== 400` or `== 200` -- so a
  rate-limited request produces a red test, never a false green.
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


GENERIC = "Invalid or expired verification token"


def _post(app, token: str, password: str | None = None):
    body = {"token": token}
    if password is not None:
        body["password"] = password
    with TestClient(app) as client:
        return client.post("/api/v1/auth/verify-email", json=body)


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

    resp = _post(make_app(session_factory), token, PASSWORD)

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

    resp = _post(make_app(session_factory), invitation, PASSWORD)

    assert resp.status_code == 400
    assert resp.json()["detail"] == GENERIC
    assert resp.json()["detail"] == GENERIC
    assert resp.json()["detail"] == GENERIC
    assert resp.json()["detail"] == GENERIC
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

    resp = _post(make_app(session_factory), access_shaped, PASSWORD)

    assert resp.status_code == 400
    assert resp.json()["detail"] == GENERIC
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
        resp = _post(app, token, PASSWORD)
        assert resp.status_code == 400
        assert resp.json()["detail"] == GENERIC
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

    resp = _post(make_app(session_factory), legacy, PASSWORD)

    assert resp.status_code == 400
    assert resp.json()["detail"] == GENERIC
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

    resp = _post(make_app(session_factory), token, PASSWORD)

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

    resp = _post(make_app(session_factory), token, PASSWORD)

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

    resp = _post(make_app(session_factory), token, PASSWORD)

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


# ── The `payload is None` arm ────────────────────────────────────────────────
#
# ⚠ ADDED AFTER REVIEW. `decode_token` returns None for anything PyJWT
# rejects, and `if payload is None or payload.get("type") != ...` has TWO
# arms. The first draft of this file fenced only the second. Deleting the
# `payload is None` arm left all nine tests GREEN while turning three
# attacker-reachable token shapes into an AttributeError -- a 500 on a public,
# unauthenticated endpoint.
#
# That is the same class V7 exists to fence, on a strictly MORE reachable
# path: V7 needs a validly-signed token naming a deleted user, these need
# `{"token": "x"}` and no credential at all.
#
# The expiry case matters most. A verification link is valid for 24 hours,
# and TTL enforcement is one of this endpoint's security properties. Nothing
# asserted an expired link is refused. `decode_token`'s `except
# jwt.PyJWTError` is what enforces it, and it was one deleted `or` clause
# away from being a 500 instead. This also kills a future refactor that
# decodes with `options={"verify_exp": False}`.


@pytest.mark.asyncio
async def test_garbage_token_is_rejected(session_factory):
    """V9. A malformed token is a 400, not a 500.

    Requires no credential whatsoever, so it is the cheapest possible probe
    against this endpoint.
    """
    user_id = await _seed_user(session_factory, email_verified=False)

    resp = _post(make_app(session_factory), "not.a.jwt.at.all", PASSWORD)

    assert resp.status_code == 400
    assert resp.json()["detail"] == GENERIC
    assert await _is_verified(session_factory, user_id) is False


@pytest.mark.asyncio
async def test_expired_verification_token_is_rejected(session_factory):
    """V10. AN EXPIRED LINK MUST NOT VERIFY. The 24-hour TTL is a security
    property of this endpoint and nothing asserted it.

    Kills both dropping the `payload is None` arm (500) and any refactor
    that decodes with expiry verification disabled (200, silently making the
    link eternal).
    """
    user_id = await _seed_user(session_factory, email_verified=False)
    expired = jwt.encode(
        {
            "sub": str(user_id),
            "email": EMAIL,
            "type": "email_verify",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    resp = _post(make_app(session_factory), expired, PASSWORD)

    assert resp.status_code == 400
    assert resp.json()["detail"] == GENERIC
    assert await _is_verified(session_factory, user_id) is False


@pytest.mark.asyncio
async def test_token_signed_with_the_wrong_key_is_rejected(session_factory):
    """V11. Signature verification. A token that is correct in every claim
    but signed with a key we do not hold must be refused.

    Kills dropping the `payload is None` arm, and kills any decode that
    passes `options={"verify_signature": False}` -- under which a forged
    token would verify any account its `sub` names.
    """
    user_id = await _seed_user(session_factory, email_verified=False)
    forged = jwt.encode(
        {
            "sub": str(user_id),
            "email": EMAIL,
            "type": "email_verify",
            "exp": datetime.now(timezone.utc) + timedelta(hours=24),
        },
        settings.jwt_secret_key + "-not-our-key",
        algorithm=settings.jwt_algorithm,
    )

    resp = _post(make_app(session_factory), forged, PASSWORD)

    assert resp.status_code == 400
    assert resp.json()["detail"] == GENERIC
    assert await _is_verified(session_factory, user_id) is False


# ── TBD-657: verifying an UNVERIFIED account needs its password ──────────────
#
# Attack: an attacker registers with the victim's address and password P (row
# unverified). The victim clicks the unsolicited mail; the bootstrap arm sets
# `email_verified` and keeps P, so the attacker logs in. The check is on ROW
# STATE (`not user.email_verified`), after the token sub/email binding and
# before the `promoting` branch, so it covers admin-initiated promotion too.
# Nothing is written on a refusal, so a pending claim survives it.


def _code(resp) -> str:
    return resp.json()["detail"]["code"]


async def _row(factory, user_id: int):
    async with factory() as db:
        return await db.scalar(select(User).where(User.id == user_id))


async def _set_pending(factory, user_id: int, pending: str) -> None:
    async with factory() as db:
        user = await db.scalar(select(User).where(User.id == user_id))
        user.pending_email = pending
        await db.commit()


def _login(app, password: str):
    with TestClient(app) as client:
        return client.post(
            "/api/v1/auth/login", json={"login": "alice", "password": password}
        )


@pytest.mark.asyncio
async def test_f1_bootstrap_without_password_is_refused(session_factory):
    """F1. Kills: status quo; `body.password and not verify_password(...)`
    (an optional password that is simply skippable)."""
    user_id = await _seed_user(session_factory, email_verified=False)
    app = make_app(session_factory)

    resp = _post(app, create_email_verification_token(user_id, EMAIL))

    assert resp.status_code == 400
    assert _code(resp) == "password_required"
    assert (await _row(session_factory, user_id)).email_verified is False
    assert _login(app, PASSWORD).status_code == 403


@pytest.mark.asyncio
async def test_f2_bootstrap_with_wrong_password_is_refused(session_factory):
    """F2. Kills: a presence-only check."""
    user_id = await _seed_user(session_factory, email_verified=False)

    resp = _post(
        make_app(session_factory),
        create_email_verification_token(user_id, EMAIL),
        "not-the-password",
    )

    assert resp.status_code == 400
    assert _code(resp) == "password_incorrect"
    assert (await _row(session_factory, user_id)).email_verified is False


@pytest.mark.asyncio
async def test_f3_bootstrap_with_correct_password_verifies_and_logs_in(session_factory):
    """F3. Kills: always-refuse; comparing the wrong column."""
    user_id = await _seed_user(session_factory, email_verified=False)
    app = make_app(session_factory)

    resp = _post(app, create_email_verification_token(user_id, EMAIL), PASSWORD)

    assert resp.status_code == 200
    assert (await _row(session_factory, user_id)).email_verified is True
    assert _login(app, PASSWORD).status_code == 200


@pytest.mark.asyncio
async def test_f4_admin_promotion_on_unverified_row_needs_password(session_factory):
    """F4. Kills: the check living only inside `if not promoting`, or after /
    inside `_promote_pending_email`."""
    user_id = await _seed_user(session_factory, email_verified=False)
    await _set_pending(session_factory, user_id, "new@example.com")
    token = create_email_verification_token(
        user_id, "new@example.com", admin_initiated=True
    )

    resp = _post(make_app(session_factory), token)

    assert resp.status_code == 400
    assert _code(resp) == "password_required"
    row = await _row(session_factory, user_id)
    assert row.pending_email == "new@example.com"
    assert row.email == EMAIL
    assert row.email_verified is False


@pytest.mark.asyncio
async def test_f4b_admin_promotion_with_taken_target_still_needs_password(
    session_factory,
):
    """F4b. Kills: the check placed inside `_promote_pending_email` after its
    `taken` check (that would 409 and abandon the claim first)."""
    user_id = await _seed_user(session_factory, email_verified=False)
    async with session_factory() as db:
        org = Organization(name="o2", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        db.add(
            User(
                org_id=org.id, username="bob", email="taken@example.com",
                password_hash=hash_password("x-irrelevant-1"), role=Role.OWNER,
                is_superadmin=False, is_active=True, email_verified=True,
            )
        )
        await db.commit()
    await _set_pending(session_factory, user_id, "taken@example.com")
    token = create_email_verification_token(
        user_id, "taken@example.com", admin_initiated=True
    )

    resp = _post(make_app(session_factory), token)

    assert resp.status_code == 400
    assert _code(resp) == "password_required"
    assert (await _row(session_factory, user_id)).pending_email == "taken@example.com"


@pytest.mark.asyncio
async def test_f5_self_service_promotion_on_verified_row_needs_no_password(
    session_factory,
):
    """F5. TBD-361 intact. Kills: an unconditional password requirement."""
    user_id = await _seed_user(session_factory, email_verified=True)
    await _set_pending(session_factory, user_id, "new@example.com")

    resp = _post(
        make_app(session_factory),
        create_email_verification_token(user_id, "new@example.com"),
    )

    assert resp.status_code == 200
    row = await _row(session_factory, user_id)
    assert row.email == "new@example.com"
    assert row.pending_email is None


@pytest.mark.asyncio
@pytest.mark.parametrize("password", [None, "wrong-password-1"])
async def test_f6_bad_token_never_reaches_the_password_check(
    session_factory, password
):
    """F6 (R2). A stale / foreign token answers the generic 400 whether the
    password is absent or wrong. Kills: the password check placed before the
    token binding, which would answer password_required / password_incorrect
    and be a password oracle for a stale-token holder."""
    user_id = await _seed_user(session_factory, email_verified=False)
    app = make_app(session_factory)
    stale = create_email_verification_token(user_id, "old@example.com")
    foreign = create_email_verification_token(user_id + 1000, EMAIL)

    for token in (stale, foreign):
        resp = _post(app, token, password)
        assert resp.status_code == 400
        assert resp.json()["detail"] == GENERIC
    assert (await _row(session_factory, user_id)).email_verified is False


@pytest.mark.asyncio
async def test_f7_already_verified_reclick_without_password_is_200(session_factory):
    """F7. Kills: locking out double clicks."""
    user_id = await _seed_user(session_factory, email_verified=True)

    resp = _post(
        make_app(session_factory), create_email_verification_token(user_id, EMAIL)
    )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_f8_register_squat_then_victim_recovers_end_to_end(
    session_factory, monkeypatch
):
    """F8. Real POST /register as the attacker with the victim's address and
    P. The victim's click without a password changes nothing and P cannot log
    in; after forgot/reset to Q the same link verifies with Q only. Kills:
    binding a password fingerprint into the token (victim's link dead)."""
    from app.models.subscription import Plan
    from app.routers import auth as auth_module
    from app.security import create_password_reset_token

    async with session_factory() as db:
        db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
        await db_first_user(db)
    sent: list[tuple[str, str]] = []

    async def capture(to, token):
        sent.append((to, token))

    monkeypatch.setattr(auth_module, "send_verification_email", capture)
    app = make_app(session_factory)
    victim = "victim@example.com"
    with TestClient(app) as client:
        reg = client.post(
            "/api/v1/auth/register",
            json={"username": "alice", "email": victim, "password": "attacker-P-1"},
        )
        assert reg.status_code in (200, 201), reg.text
        token = sent[-1][1]

        click = client.post("/api/v1/auth/verify-email", json={"token": token})
        assert click.status_code == 400 and _code(click) == "password_required"
        login = client.post(
            "/api/v1/auth/login", json={"login": "alice", "password": "attacker-P-1"}
        )
        assert login.status_code == 403

        async with session_factory() as db:
            uid = await db.scalar(select(User.id).where(User.email == victim))
        reset = client.post(
            "/api/v1/auth/reset-password",
            json={"token": create_password_reset_token(uid), "new_password": "victim-Q-123"},
        )
        assert reset.status_code == 200, reset.text

        old = client.post(
            "/api/v1/auth/verify-email", json={"token": token, "password": "attacker-P-1"}
        )
        assert old.status_code == 400 and _code(old) == "password_incorrect"
        ok = client.post(
            "/api/v1/auth/verify-email", json={"token": token, "password": "victim-Q-123"}
        )
        assert ok.status_code == 200, ok.text
        assert (
            client.post(
                "/api/v1/auth/login", json={"login": "alice", "password": "attacker-P-1"}
            ).status_code
            == 401
        )


async def db_first_user(db) -> None:
    """Someone else owns the table first, so the attacker is not the
    first-user bootstrap (which is pre-verified)."""
    org = Organization(name="first", billing_cycle_day=1)
    db.add(org)
    await db.commit()
    db.add(
        User(
            org_id=org.id, username="first", email="first@example.com",
            password_hash=hash_password("first-pass-12"), role=Role.OWNER,
            is_superadmin=True, is_active=True, email_verified=True,
        )
    )
    await db.commit()
