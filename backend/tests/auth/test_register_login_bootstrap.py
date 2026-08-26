"""TBD-344 — the bootstrap account must be able to sign in.

`POST /api/v1/auth/register` created every user with `email_verified=False`
(the `User(...)` constructor omitted the field and the column is
`server_default="0"` with no Python default), while `POST /auth/login`
unconditionally 403s an unverified user. That broke both documented
register-then-login callers: the first-user `/setup` bootstrap
(`frontend/app/setup/page.tsx`, `README.md`, `CONTRIBUTING.md`) and
`backend/seed.py`. 100% of fresh installs, invisible to CI.

⚠ WHY THIS FILE EXISTS AT ALL, given `test_login_email_gate.py` already
covers the gate: every pre-existing fence for this column HAND-SETS
`email_verified=` on a directly-constructed `User(...)` — including that
file's own `_seed_user` — so no test in the repo ever observed what the
REGISTER handler actually stores. The fences here therefore drive the real
HTTP pair (`POST /register` then `POST /login`) and never hand-set
`email_verified` on an account they then register. The seeded users in F2,
F3 and F4 are pre-existing rows that establish the DB state; none of them
stands in for the account under test.

The fix mints verification at creation time, keyed on `is_first_user_setup`
(`user_count == 0`), and does NOT touch the login gate.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.captcha import CaptchaVerifyResult, REASON_OK
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.middleware.request_context import RequestContextMiddleware
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router
from app.security import hash_password


PASSWORD = "S3cret-Pass-1!"


# ── harness ──────────────────────────────────────────────────────────────────
#
# Copied from `tests/routers/test_auth_register_captcha.py`, which already
# solves everything a register-driving test needs: the `get_session_factory`
# override (without it the `auth.register.success` audit write opens a REAL
# engine and the request 500s — a failure that looks exactly like a fence
# going red for the right reason but is not), `RequestContextMiddleware`, the
# `Plan` row `subscription_service.create_trial` requires, and `limiter.reset()`.
# Login's `_issue_refresh_session` needs Redis; `tests/conftest.py`'s autouse
# `_autouse_fake_redis` supplies it.


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


@pytest.fixture(autouse=True)
def stub_send_verification_email(monkeypatch):
    """Capture verification-email sends without hitting the network.

    Same pattern as `tests/auth/test_login_email_gate.py`. F5 asserts on the
    captured list; every other test just needs the network call gone.
    """
    sent: list[tuple[str, str]] = []

    async def fake_send(email: str, token: str) -> None:
        sent.append((email, token))

    monkeypatch.setattr(auth_module, "send_verification_email", fake_send)
    return sent


@pytest.fixture(autouse=True)
def stub_captcha(monkeypatch):
    """Always-OK captcha verify, so a non-bootstrap register is reachable.

    The bootstrap path skips the gate on its own; the tests that register a
    SECOND user go through it and would otherwise need a real provider.
    """
    calls: list[Any] = []

    async def _ok(token, remote_ip):
        calls.append((token, remote_ip))
        return CaptchaVerifyResult(ok=True, reason=REASON_OK)

    monkeypatch.setattr(auth_module, "verify_captcha", _ok)
    monkeypatch.setattr(app_settings, "captcha_required", True)
    return calls


def _make_app(session_factory) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(RequestContextMiddleware)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(auth_router)
    return app


async def _seed_default_plan(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as db:
        existing = await db.scalar(select(Plan).where(Plan.slug == "free"))
        if existing is None:
            db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
            await db.commit()


async def _seed_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    is_superadmin: bool,
    email_verified: bool,
    username: str = "seed",
    email: str = "seed@example.com",
) -> int:
    """Seed a PRE-EXISTING row so `user_count > 0`.

    ⚠ This account is never the one under test — it exists only to move the
    DB out of the cold-start state. Since TBD-365 there is only ONE first-ness
    predicate, so `is_superadmin` no longer changes any grant outcome: seeding
    any user closes the bootstrap. It stays explicit because F3 needs the
    DIVERGENT state (`(1, 0)` — users present, none holding the flag), which
    is the state a reintroduced flag-count predicate would mis-answer.
    """
    async with factory() as db:
        org = Organization(name=f"{username} org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username=username,
            email=email,
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=is_superadmin,
            is_active=True,
            email_verified=email_verified,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _counts(factory) -> tuple[int, int]:
    """``(total users, superadmins)`` read straight from the DB.

    TBD-365: the controls in this file MUST be DB-level. The fix makes the
    divergent state (users exist, zero superadmins) behaviourally INVISIBLE at
    the HTTP layer — which is the point of the fix — so no assertion over the
    response body, the login result, or the audit row can prove the fixture
    built it any more. See the control notes on F3.
    """
    async with factory() as db:
        total = await db.scalar(select(func.count()).select_from(User))
        supers = await db.scalar(
            select(func.count()).select_from(User).where(User.is_superadmin.is_(True))
        )
    return int(total or 0), int(supers or 0)


async def _register_success_detail(factory) -> dict:
    """The `detail` dict off the single `auth.register.success` audit row.

    Read by KEY at the call sites, never by dict equality — the same posture
    `tests/routers/test_auth_register_captcha.py` takes with
    `detail["is_first_user"]` / `["granted_superadmin"]`, so that adding a
    field to this event stays a purely additive change.
    """
    async with factory() as db:
        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == "auth.register.success"
            )
        )
        rows = list(result.scalars())
    assert len(rows) == 1, f"expected exactly one success row, got {len(rows)}"
    return rows[0].detail


def _register(client: TestClient, *, username: str, email: str) -> Any:
    return client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": email,
            "password": PASSWORD,
            "captcha_token": "tok",
        },
    )


def _login(client: TestClient, *, login: str) -> Any:
    return client.post(
        "/api/v1/auth/login",
        json={"login": login, "password": PASSWORD},
    )


# ── F1 ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f1_bootstrap_register_then_login_succeeds(session_factory) -> None:
    """FENCE — the first account on a cold install can sign in.

    Wrong implementation killed: `main`, where the register handler omits
    `email_verified` from the `User(...)` constructor, the column defaults to
    0, and the login gate 403s the operator out of their own brand-new
    install. This is the defect, and it breaks 100% of fresh installs.

    The `email_verified is True` assertion on the 201 BODY additionally kills
    any implementation that special-cases the LOGIN side (a settings flag, an
    `app_env` branch, a superadmin exemption at the gate) instead of minting
    verification at creation time. Such a fix would make the login leg pass
    while the body still reported an unverified account.

    ⚠ The login failure must be specifically 403 `email_not_verified`. Without
    the `get_session_factory` override this test can go red with a 500 from the
    audit write and look like it is fencing the bug when it is fencing the
    harness. The status assertion below pins 403 explicitly for that reason.
    """
    await _seed_default_plan(session_factory)
    # No seeded user: `user_count == 0` is the bootstrap condition.

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = _register(client, username="founder", email="founder@example.com")
        assert res.status_code == 201, res.text

        login = _login(client, login="founder")

    # Asserted BEFORE the body check on purpose: this is the assertion that
    # reports the real 403 against unfixed code, which is how we know the
    # fence is red for the defect and not for a harness 500.
    assert login.status_code == 200, (
        "bootstrap login was refused; expected 200, got "
        f"{login.status_code} {login.text}"
    )
    assert login.json()["access_token"]

    body = res.json()
    assert body["email_verified"] is True, (
        "login succeeded but the account was CREATED unverified — the fix was "
        f"applied at the gate instead of at the mint. body={body}"
    )

    # The audit row must record that the grant happened. Positive side of the
    # pin; F3 carries the negative side, in the one state where a correct
    # predicate and a reintroduced flag count disagree. Asserted against the
    # account's ACTUAL stored flag rather
    # than a bare literal, so the row cannot drift from the thing it describes.
    # TBD-365: the cold install must still MINT AN OPERATOR. Without this
    # assertion, `is_superadmin=False` unconditional satisfies F3 here AND
    # every negative fence in `test_auth_google_callback_first_run.py`, and
    # ships a fresh install with no superadmin account at all — the same class
    # of defect as the one this file was created for, and equally invisible.
    assert body["is_superadmin"] is True, (
        "the first account on an empty install did not receive superadmin; "
        f"the install has no operator. body={body}"
    )
    total, supers = await _counts(session_factory)
    assert (total, supers) == (1, 1), (
        f"expected exactly one user holding superadmin, got {total} users / "
        f"{supers} superadmins"
    )

    detail = await _register_success_detail(session_factory)
    assert detail["email_verified_on_create"] is True
    assert detail["email_verified_on_create"] == body["email_verified"]
    # The three-way invariant, positive leg. ⚠ WEAK BY CONSTRUCTION: on a cold
    # install every first-ness value is True under every implementation under
    # consideration, so this cannot separate them. Its only kill is hardcoding
    # one slot False. What actually pins the BINDING is the structural fence
    # `test_audit_outcomes_read_the_row_not_the_local`, because no behavioural
    # test can distinguish a row that reads the row from one that restates the
    # local.
    assert (
        detail["is_first_user"]
        == detail["granted_superadmin"]
        == detail["email_verified_on_create"]
        is True
    )


# ── F2 ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f2_second_user_is_not_auto_verified(session_factory) -> None:
    """FENCE — the exemption is the BOOTSTRAP, not registration in general.

    Wrong implementation killed: `email_verified=True` unconditional in the
    `User(...)` constructor. That passes F1 perfectly and silently disables
    email verification for every public self-signup on the internet.

    A superadmin already exists here. Since TBD-365 there is one predicate,
    so this state and F3's divergent state now produce the SAME outcome — this
    test therefore cannot separate a correct predicate from a reintroduced
    flag count. F3 is the only one that can.
    """
    await _seed_default_plan(session_factory)
    await _seed_user(session_factory, is_superadmin=True, email_verified=True)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = _register(client, username="newuser", email="new@example.com")
        assert res.status_code == 201, res.text
        assert res.json()["email_verified"] is False, (
            "an ordinary public signup was auto-verified; email verification "
            "is effectively off"
        )

        login = _login(client, login="newuser")

    assert login.status_code == 403, login.text
    assert login.json()["detail"]["code"] == "email_not_verified"


# ── F3 (TBD-365: converted from the divergence fence) ────────────────────────


@pytest.mark.asyncio
async def test_f3_bootstrap_grant_keys_on_empty_table_not_flag_count(
    session_factory,
) -> None:
    """FENCE — with users present and ZERO superadmins, register grants nothing.

    Before TBD-365 the handler carried two first-ness predicates:

      * `is_first_user_setup` — `user_count == 0`           (bootstrap)
      * `is_first_user`       — `existing_superadmin == 0`  (superadmin grant)

    The second is strictly WEAKER: an empty table implies no superadmin, but
    not the reverse. So on any install whose superadmins had all been deleted
    it stayed true, and the next public self-signup from the open internet
    received superadmin AND a verified email AND a usable session.

    This is the ONLY state where the two predicates disagree. F1 (cold) and F2
    (warm, superadmin present) both pass under either predicate.

    Wrong implementations killed:
      * `is_superadmin=(existing_superadmin == 0)`  — the pre-TBD-365 code
      * `email_verified=(existing_superadmin == 0)` — the TBD-344 half of it
      * `user_count == 0 or existing_superadmin == 0` — the "defensive" merge

    ⚠ CONTROL NOTE — why the controls are DB-level and not on the response.
    This fence previously used `body["is_superadmin"] is True` as its control,
    which was valid only while the divergent state had an observable
    consequence. The fix REMOVES that consequence — that is the fix. Post-fix,
    `is_superadmin: False` is produced identically by this state and by F2's
    state, so a body assertion can no longer prove the fixture built anything.
    Worse, a 409 from a username collision, a 500 from a missing `Plan`, or any
    swallowed exception leaves "the new account is not a superadmin" trivially
    true with the handler never having run.

    The post-condition `(2, 0)` is the load-bearing assertion: it proves in one
    step that register EXECUTED (a second row exists) and that it minted no
    superadmin. No no-op fixture and no swallowed exception can produce it.

    ⚠ Injection leg is *implement the wrong predicate -> confirm RED -> restore*,
    never *revert to main* — see the note this fence inherited from its
    predecessor.
    """
    await _seed_default_plan(session_factory)
    await _seed_user(session_factory, is_superadmin=False, email_verified=True)

    # (a) PRE-CONDITION CONTROL — the fixture really built the divergent state.
    before = await _counts(session_factory)
    assert before == (1, 0), (
        "fixture did not produce the divergent state (users exist, zero "
        f"superadmins); the rest of this fence pins nothing. counts={before}"
    )

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = _register(client, username="newuser", email="new@example.com")
        # (b) LIVENESS — the handler ran to completion.
        assert res.status_code == 201, res.text
        body = res.json()

        # (c) POST-CONDITION CONTROL — the load-bearing one.
        after = await _counts(session_factory)
        assert after == (2, 0), (
            "register either did not run or minted a superadmin in a state "
            f"that is not the bootstrap. before={before} after={after}"
        )

        # (d) the flags under test, on the wire
        assert body["is_superadmin"] is False, (
            "a public self-signup received superadmin on an install that "
            "merely lacks one — the escalation TBD-365 removed"
        )
        assert body["email_verified"] is False, (
            "verification was granted on the superadmin-count predicate "
            "rather than the empty-table predicate"
        )

        # (e) and it is not merely response shaping
        login = _login(client, login="newuser")

    assert login.status_code == 403, login.text
    assert login.json()["detail"]["code"] == "email_not_verified"

    # (f) THE AUDIT ROW: one decision against two outcomes read off the row.
    #
    # This is the state where a payload that RESTATES the local predicate in
    # the outcome slots is indistinguishable from one that reads the row — so
    # each outcome is compared against the account's real stored value, never
    # against a literal. A rekey of either constructor keyword breaks the
    # equality and this fence reports it.
    detail = await _register_success_detail(session_factory)
    assert detail["is_first_user"] is False
    assert detail["granted_superadmin"] == body["is_superadmin"] is False, (
        "the audit row disagrees with the account it describes — "
        f"detail={detail}, body={body}"
    )
    assert detail["email_verified_on_create"] == body["email_verified"] is False
    assert (
        detail["is_first_user"]
        == detail["granted_superadmin"]
        == detail["email_verified_on_create"]
    ), f"three-way invariant violated on a correct row: {detail}"
    # No stray keys. Asserted as an exact key set rather than by naming one
    # forbidden string: a named-absence check only rules out the one spelling
    # somebody happened to think of, and the key it named never existed.
    assert set(detail) == {
        "method",
        "is_first_user",
        "granted_superadmin",
        "email_verified_on_create",
        "captcha_required",
    }, f"unexpected key set on auth.register.success: {sorted(detail)}"


# ── F4 ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f4_superadmin_gets_no_exemption_at_the_login_gate(
    session_factory,
) -> None:
    """FENCE — the LOGIN gate stays exactly as written.

    Wrong implementation killed: any "helpful" fix applied at the check rather
    than the mint — a superadmin exemption, an `app_env == "development"`
    branch, or a settings flag that skips the gate. Each would make F1 pass
    while leaving unverified accounts able to sign in.

    This user was created unverified by some other path (an older install, a
    direct DB write, an admin-created account); being a superadmin must not
    buy them past the gate.
    """
    await _seed_user(
        session_factory,
        is_superadmin=True,
        email_verified=False,
        username="root",
        email="root@example.com",
    )

    app = _make_app(session_factory)
    with TestClient(app) as client:
        login = _login(client, login="root")

    assert login.status_code == 403, login.text
    assert login.json()["detail"] == {
        "code": "email_not_verified",
        "message": "Please verify your email to sign in.",
    }


# ── F5 (guard, not a fence) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f5_guard_bootstrap_still_sends_exactly_one_verification_email(
    session_factory, stub_send_verification_email
) -> None:
    """GUARD — auto-verifying the bootstrap must NOT suppress its email.

    Not a fence: nothing about the fix pushes toward suppressing the mail, and
    a test cannot prove the copy is still appropriate. It guards the decision.

    The mail's copy is "Welcome. Confirm this email address so we know the
    account is yours, and so password resets and invitations reach you"
    (`email_service.send_verification_email`) — still true for an
    auto-verified user. ("Please verify your email to sign in" is the 403
    detail, not the email.) Sending also proves deliverability for the one
    account on the install that has no break-glass, and suppressing it would
    add a SECOND consumer of `is_first_user_setup` that can drift from the
    first.
    """
    await _seed_default_plan(session_factory)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        first = _register(client, username="founder", email="founder@example.com")
        assert first.status_code == 201, first.text
        assert len(stub_send_verification_email) == 1, stub_send_verification_email
        assert stub_send_verification_email[0][0] == "founder@example.com"

        second = _register(client, username="second", email="second@example.com")
        assert second.status_code == 201, second.text

    # Exactly one more — the ordinary path is unchanged too.
    assert len(stub_send_verification_email) == 2, stub_send_verification_email
    assert stub_send_verification_email[1][0] == "second@example.com"


# ── F6 (guard, not a fence) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f6_guard_ensure_verified_seed_helper(session_factory, monkeypatch):
    """GUARD — `seed.ensure_verified()` behaves as its call sites assume.

    ⚠ THIS DOES NOT FENCE THE REAL DEFECT. `backend/seed.py` is an httpx
    script driven against a live server; its only honest verification is a
    human running `./pfv seed` on a fresh stack and then running it a second
    time. This test pins the helper in isolation and nothing about the script's
    control flow.

    Three legs: unverified -> verified; called twice -> idempotent, no raise;
    unknown username -> no rows, no raise.
    """
    import seed as seed_module

    monkeypatch.setattr(seed_module, "async_session", session_factory)

    user_id = await _seed_user(
        session_factory,
        is_superadmin=False,
        email_verified=False,
        username="demo",
        email="demo@example.com",
    )

    async def _verified() -> bool:
        async with session_factory() as db:
            return await db.scalar(
                select(User.email_verified).where(User.id == user_id)
            )

    assert await _verified() is False

    await seed_module.ensure_verified("demo")
    assert await _verified() is True

    # Idempotent: a second call on an already-verified row is a no-op.
    await seed_module.ensure_verified("demo")
    assert await _verified() is True

    # Unknown username: zero rows matched, no exception.
    await seed_module.ensure_verified("nobody-with-this-name")
    assert await _verified() is True
