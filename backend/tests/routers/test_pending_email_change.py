"""Two-phase email change: end-to-end fences (TBD-361).

`PUT /users/me` used to assign `users.email`, clear `email_verified` and set
`sessions_invalidated_at` in one request, then mail the verification link to
the NEW address. One typo therefore logged the user out and locked them out
permanently: every recovery path mails `user.email`, which was now the typo,
and `reset_password` never writes `email_verified`, so even a successful
password reset still 403s at login. On a solo org — the default, since every
registration mints its own — there is no way back at all.

⚠ WHAT THIS FILE EXISTS TO CATCH, above everything else.

Both design reviews independently predicted the same silent failure, by
different routes, and neither is caught by any hand-minted unit test:

  * F1 — the token gets minted against the WRONG address. `users.py` used to
    mint from the raw request body. If that survives, the confirmation mail
    lands in the OLD inbox, the promote guard waves it through on its
    current-address arm as an ordinary bootstrap verification, `pending_email`
    is never promoted, and the email change SILENTLY NEVER HAPPENS. Every
    test that builds its own token passes, because nothing else compares the
    address we MAILED against the address the user TYPED.

  * F12 — the completion audit row and both "changed" emails stay wired to
    the request, asserting a change that has not happened and may never.

`_seed_user`/`_make_app` mirror `test_users_password_set.py` deliberately:
that file already proved this wiring binds `get_current_user` to the same
session the handler mutates, without which the handler's writes go to a
detached instance and persist nothing.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import users as users_module
from app.routers.auth import router as auth_router
from app.routers.users import router as users_router
from app.security import hash_password

PASSWORD = "starting-password-1"
OLD_EMAIL = "alice@acme.io"


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
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def queued_verification(monkeypatch):
    """Capture what the handler queued on `background_tasks.add_task`.

    ⚠ Patch the name in `app.routers.users`, NOT in `app.services
    .email_service`: `users.py` imports it at module scope and `add_task`
    resolves that global at call time. The same shape as
    `test_rate_limit_sensitive_endpoints.py`.
    """
    sent: list[tuple[str, str]] = []

    async def _fake(email: str, token: str) -> None:
        sent.append((email, token))

    monkeypatch.setattr(users_module, "send_verification_email", _fake)
    return sent


async def _seed_user(session_factory, *, email: str = OLD_EMAIL) -> int:
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="alice",
            email=email,
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return user.id


def _make_app(session_factory, user_id: int) -> FastAPI:
    from fastapi import Depends as _Depends

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user(
        request: Request, db: AsyncSession = _Depends(get_db)
    ) -> User:
        request.state.auth_method = "jwt"
        user = await db.get(User, user_id)
        assert user is not None
        await db.refresh(user, ["organization"])
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.include_router(users_router)
    app.include_router(auth_router)
    return app


async def _reload(session_factory, user_id: int) -> User:
    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        return user


def _request_change(client: TestClient, email: str):
    return client.put(
        "/api/v1/users/me", json={"email": email, "current_password": PASSWORD}
    )


@pytest.mark.asyncio
async def test_link_is_mailed_to_the_new_address_and_promotes_it(
    session_factory, queued_verification
):
    """F1. THE fence. Mail the address the user typed, and promote THAT.

    ⚠ The input is deliberately MIXED CASE. With an all-lowercase fixture
    the raw body and the normalized stored value are byte-identical, and
    this fence would prove nothing about which one the token was minted
    from. Minting from `body.email` while storing a normalized
    `pending_email` produces a link that 400s FOREVER for the one user who
    typed capitals — the promote guard compares the claim to the stored
    value exactly, by design — and with no resend path, behind a 5/hour
    IP-keyed limit.

    Kills: minting from `body.email`; mailing `user.email`; promoting
    anything other than the address that was mailed.
    """
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)

    with TestClient(app) as client:
        res = _request_change(client, "New.Address@ACME.io")
        assert res.status_code == 200, res.text

        assert len(queued_verification) == 1
        recipient, token = queued_verification[0]

        # (a) the mail went to the address the user asked for, normalized.
        assert recipient == "new.address@acme.io"

        # (b) the token's claim is byte-identical to what we stored, which
        # is what the promote guard will compare against.
        user = await _reload(session_factory, user_id)
        claim = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )["email"]
        assert claim == user.pending_email == "new.address@acme.io"

        # (c) that exact token promotes.
        promoted = client.post("/api/v1/auth/verify-email", json={"token": token})
        assert promoted.status_code == 200, promoted.text

    user = await _reload(session_factory, user_id)
    assert user.email == "new.address@acme.io"
    assert user.pending_email is None
    assert user.email_verified is True


@pytest.mark.asyncio
async def test_request_leaves_identity_and_session_untouched(
    session_factory, queued_verification
):
    """F2. The request records a CLAIM and nothing else.

    Kills the request-time `sessions_invalidated_at` write returning — the
    defect itself. A session that dies here is a user locked out of an
    address they may have just mistyped.
    """
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        assert _request_change(client, "new@acme.io").status_code == 200

    user = await _reload(session_factory, user_id)
    assert user.email == OLD_EMAIL
    assert user.email_verified is True
    assert user.sessions_invalidated_at is None
    assert user.pending_email == "new@acme.io"


@pytest.mark.asyncio
async def test_promotion_invalidates_sessions(session_factory, queued_verification):
    """F3. Identity changed, so tokens minted under the old address die.

    The counterpart to F2: without it, F2 alone would reward deleting the
    cutoff entirely.

    ⚠ This fence is why the cutoff must NOT be floored to whole seconds.
    Every validator compares with a strict `<` and `create_access_token`
    already floors `iat`, so a cutoff floored to T.0 leaves a token minted
    at T.2 alive — `T < T` is False. Under the floored variant this
    assertion goes RED, and the tempting repair is to weaken it.
    """
    from app.security import token_cutoff

    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        assert _request_change(client, "new@acme.io").status_code == 200
        _recipient, token = queued_verification[0]

        # Stamp an `iat` the way `create_access_token` does -- floored to
        # the whole second -- immediately BEFORE promoting, so the mint and
        # the cutoff land in the same wall-clock second. That is the case a
        # floored cutoff gets wrong, and it is the overwhelmingly common
        # one in production too.
        iat = int(datetime.now(timezone.utc).timestamp())
        token_issued_at = datetime.fromtimestamp(iat, tz=timezone.utc)

        assert (
            client.post("/api/v1/auth/verify-email", json={"token": token})
        ).status_code == 200

    user = await _reload(session_factory, user_id)
    assert user.sessions_invalidated_at is not None
    # The exact comparison `deps.get_current_user` and
    # `_validate_refresh_cookie` perform.
    assert token_issued_at < token_cutoff(user), (
        "a token minted in the same second as the promotion must be "
        "rejected; flooring the cutoff to whole seconds leaves it alive"
    )


@pytest.mark.asyncio
async def test_bootstrap_verification_does_not_invalidate_sessions(session_factory):
    """F4. The bootstrap arm is untouched.

    ⚠ The AST allowlist structurally CANNOT see this: it is
    function-granular, so it certifies that `_promote_pending_email` writes
    the cutoff without seeing that the helper is only reached from the
    promoting branch. A version that fired the cutoff on EVERY verification
    would log out every first-time bootstrap and still pass the allowlist.

    Kills: hoisting the cutoff out of the promoting branch.
    """
    user_id = await _seed_user(session_factory)
    async with session_factory() as db:
        user = await db.get(User, user_id)
        user.email_verified = False
        await db.commit()

    from app.security import create_email_verification_token

    token = create_email_verification_token(user_id, OLD_EMAIL)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.post("/api/v1/auth/verify-email", json={"token": token})
    assert res.status_code == 200, res.text

    user = await _reload(session_factory, user_id)
    assert user.email_verified is True
    assert user.sessions_invalidated_at is None, (
        "a first-time verification is not an identity change"
    )
    assert user.email == OLD_EMAIL


@pytest.mark.asyncio
async def test_second_change_supersedes_the_first_token(
    session_factory, queued_verification
):
    """F7. Last write wins, and the superseded link goes inert.

    ⚠ The second claim MUST be a different address: re-claiming the same
    one leaves the first token legitimately valid, so the fence would pass
    for the wrong reason.

    Kills: a revocation list nobody maintains. The guard pins the claim to
    the column's LIVE value, so superseding is enough.
    """
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        assert _request_change(client, "first@acme.io").status_code == 200
        _r1, first_token = queued_verification[0]
        assert _request_change(client, "second@acme.io").status_code == 200

        res = client.post("/api/v1/auth/verify-email", json={"token": first_token})
        assert res.status_code == 400, res.text

    user = await _reload(session_factory, user_id)
    assert user.pending_email == "second@acme.io"
    assert user.email == OLD_EMAIL


@pytest.mark.asyncio
async def test_cancel_clears_the_claim_and_kills_the_link(
    session_factory, queued_verification
):
    """F8. Cancel is the escape hatch, and it is final for that link."""
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        assert _request_change(client, "new@acme.io").status_code == 200
        _r, token = queued_verification[0]

        assert client.delete("/api/v1/users/me/pending-email").status_code == 204
        # Idempotent: clicking Cancel twice is not an error.
        assert client.delete("/api/v1/users/me/pending-email").status_code == 204

        res = client.post("/api/v1/auth/verify-email", json={"token": token})
        assert res.status_code == 400, res.text

    user = await _reload(session_factory, user_id)
    # NULL, not "": an empty string still satisfies `is not None` in the
    # guard, and would serialize into the response as a live pending change,
    # rendering an empty row in the UI.
    assert user.pending_email is None
    assert user.email == OLD_EMAIL


@pytest.mark.asyncio
async def test_promotion_conflict_returns_409_and_clears_the_claim(
    session_factory, queued_verification
):
    """F9. Somebody else took the address during the 24h window.

    Exact-case collision on purpose, so it runs on the SQLite shards: the
    case-insensitive half is MySQL-only (`utf8mb4_0900_ai_ci`) and is
    recorded as accepted residual in the spec.

    Kills: a 500 on a link click, and leaving a claim that can only ever
    409 again.
    """
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        assert _request_change(client, "taken@acme.io").status_code == 200
        _r, token = queued_verification[0]

        # A different account claims it first, for real.
        async with session_factory() as db:
            org = Organization(name="Other", billing_cycle_day=1)
            db.add(org)
            await db.flush()
            db.add(
                User(
                    org_id=org.id,
                    username="bob",
                    email="taken@acme.io",
                    password_hash=hash_password(PASSWORD),
                    role=Role.OWNER,
                    is_active=True,
                    email_verified=True,
                )
            )
            await db.commit()

        res = client.post("/api/v1/auth/verify-email", json={"token": token})
        assert res.status_code == 409, res.text
        assert "in use" in res.json()["detail"]

    user = await _reload(session_factory, user_id)
    assert user.email == OLD_EMAIL
    assert user.pending_email is None, (
        "a claim that can only ever 409 must not be left in place"
    )


@pytest.mark.asyncio
async def test_case_only_change_is_a_no_op_and_clears_a_live_claim(
    session_factory, queued_verification
):
    """F10. Two things at once, both load-bearing.

    ⚠ The stored address is seeded RAW and MIXED CASE, because that is the
    population production actually holds: the old request path wrote
    `body.email` unnormalized. A fence seeded from an already-normalized row
    does not test it.

    Kills: (a) the missing `normalize_email` on the `email_changing`
    comparison, which makes a pure case change start a whole two-phase flow
    for the same address, re-demanding the password and mailing a pointless
    link; and (b) a bare no-op that leaves a live claim stranded — the
    natural undo gesture must actually undo.
    """
    user_id = await _seed_user(session_factory, email="Alice@ACME.io")
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        assert _request_change(client, "new@acme.io").status_code == 200
        assert (await _reload(session_factory, user_id)).pending_email == "new@acme.io"
        queued_verification.clear()

        # Same address, different case: not a change.
        res = _request_change(client, "ALICE@acme.IO")
        assert res.status_code == 200, res.text

    assert queued_verification == [], "a case-only change must mail nothing"
    user = await _reload(session_factory, user_id)
    assert user.email == "Alice@ACME.io", "the stored address must not be rewritten"
    assert user.pending_email is None, (
        "submitting your current address must abandon a live claim"
    )


@pytest.mark.asyncio
async def test_deactivated_user_cannot_promote(session_factory, queued_verification):
    """F11. A suspended account must not rotate its recovery address
    mid-investigation, which would also destabilise `actor_email` in the
    audit trail.

    Scoped to the promoting branch only: widening it to the bootstrap arm
    would change existing behaviour for a case with no defect.
    """
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        assert _request_change(client, "new@acme.io").status_code == 200
        _r, token = queued_verification[0]

        async with session_factory() as db:
            user = await db.get(User, user_id)
            user.is_active = False
            await db.commit()

        res = client.post("/api/v1/auth/verify-email", json={"token": token})
        assert res.status_code == 400, res.text

    user = await _reload(session_factory, user_id)
    assert user.email == OLD_EMAIL
    assert user.pending_email == "new@acme.io"


@pytest.mark.asyncio
async def test_auth_me_carries_pending_email(session_factory, queued_verification):
    """F13. The field must reach the builder the FRONTEND actually reads.

    There are two `_user_response` builders. `AuthProvider.fetchMe` calls
    `/api/v1/auth/me`, served by the one in `routers/auth.py`. Populating
    only the `users.py` builder leaves `pending_email` undefined on every
    page — the pending row never renders, the corrected copy has nothing to
    key off after a refresh — while every backend fence stays green.
    """
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        assert _request_change(client, "new@acme.io").status_code == 200

        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["pending_email"] == "new@acme.io"
        assert me.json()["email"] == OLD_EMAIL


# ── TBD-362 §6: cancelling a claim leaves an audit trail ────────────────────


@pytest.mark.asyncio
async def test_cancel_writes_an_audit_row(session_factory, queued_verification):
    """``DELETE /users/me/pending-email`` wrote NO audit row until TBD-362.

    ⚠ WHY THIS ROW IS RIGHT, and it is NOT the reason an earlier draft gave.
    That draft said the row is "the target's only defence" against an
    operator retrying a repoint after the victim cancels. Refuted: every
    target of the admin endpoint is unverified, so they 403 at login and
    cannot reach this route at all (it sits behind
    ``require_interactive_session``). That population cannot cancel, so the
    event the draft described cannot occur here.

    The row is still right, for the general case: for every OTHER caller of
    this endpoint a live session — INCLUDING A HIJACKED ONE — could void a
    pending claim with nothing whatsoever in ``/admin/audit``. Cancelling is
    the natural undo of the one gesture that moves an account's recovery
    channel, and the request-time ``user.email.change_requested`` row would
    otherwise stand forever as an unresolved half of a story.

    ⚠⚠ THE ABSENCE OF RE-AUTH ON THIS ROUTE IS DELIBERATE AND CORRECT, and
    must not be "fixed" alongside this. Its docstring argues it: requesting a
    change moves the recovery channel and demands proof of presence;
    cancelling one only restores the status quo and can move nothing.
    Demanding a password to undo a mistake is the exact shape that made the
    original defect unrecoverable. The gap was the missing audit row ONLY.
    """
    from app.models.audit_event import AuditEvent

    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        assert _request_change(client, "new@acme.io").status_code == 200
        assert client.delete("/api/v1/users/me/pending-email").status_code == 204

    async with session_factory() as db:
        rows = list(
            (
                await db.execute(
                    select(AuditEvent)
                    .where(AuditEvent.event_type == "user.email.change_cancelled")
                    .order_by(AuditEvent.id)
                )
            ).scalars().all()
        )

    assert len(rows) == 1, f"no cancel audit row: {rows!r}"
    row = rows[0]
    assert row.outcome.value == "success"
    assert row.actor_user_id == user_id
    assert row.actor_email == OLD_EMAIL
    assert row.target_org_id is not None, (
        "``/admin/audit``'s only org filter is ``target_org_id``; a NULL here "
        "makes the row invisible to every org-scoped audit query"
    )
    assert row.detail["cancelled_pending_email"] == "new@acme.io", (
        "the cancelled address must be recorded — it is destroyed by this "
        "write and nothing else preserves it"
    )


@pytest.mark.asyncio
async def test_cancel_with_nothing_pending_writes_no_audit_row(session_factory):
    """A no-op cancel is not a state transition and records nothing.

    Idempotency is a 204 either way; auditing the no-op would let anyone with
    a session spray rows into ``/admin/audit`` at ``10/hour`` with no
    corresponding change to reconstruct.
    """
    from app.models.audit_event import AuditEvent

    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        assert client.delete("/api/v1/users/me/pending-email").status_code == 204

    async with session_factory() as db:
        rows = list(
            (
                await db.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type == "user.email.change_cancelled"
                    )
                )
            ).scalars().all()
        )
    assert rows == []


@pytest.mark.asyncio
async def test_cancel_still_requires_no_password(session_factory, queued_verification):
    """Anti-regression pin on the DELIBERATE absence of re-auth.

    A reviewer reading "add an audit row to the cancel path" will be tempted
    to harden it at the same time. This fails the moment anyone does.
    """
    user_id = await _seed_user(session_factory)
    app = _make_app(session_factory, user_id)
    with TestClient(app) as client:
        assert _request_change(client, "new@acme.io").status_code == 200
        # No body, no password, no step-up token.
        res = client.delete("/api/v1/users/me/pending-email")
    assert res.status_code == 204, res.text
    user = await _reload(session_factory, user_id)
    assert user.pending_email is None
