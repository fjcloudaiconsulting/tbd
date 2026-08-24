"""F6 — an admin-initiated claim dies the moment the row becomes verified
(TBD-362).

Spec: ``specs/2026-08-23-tbd-362-admin-email-recovery.md`` §3, fence F6.

THE DOOR THIS CLOSES. The endpoint's ``user_already_verified`` guard reads
``email_verified`` at **trigger** time. The claim redeems up to 24 hours
later, and there are **four** arms by which an unverified target becomes
verified in between:

  1. the registration link  -- ``routers/auth.py``'s bootstrap arm
  2. Google sign-in on the existing row -- ``routers/auth.py::google_callback``
  3. invitation accept -- ``services/invitation_service.py::accept_invitation``
  4. admin merge -- ``services/user_merge_service.py::merge_users``

The bootstrap arm deliberately does **not** clear ``pending_email`` (it is
"none of that path's business"), so the operator's link stays armed and would
otherwise promote onto a now-verified, now-loginable account. Without this
check the guard's whole safety argument — "the accepted population owns no
data" — is defeated by waiting.

⚠ ``_promote_pending_email`` IS A FIFTH SITE THAT VERIFIES AN EXISTING ROW.
It is excluded from the four arms above only because it sets
``pending_email = None`` in the same transaction (and
``_abandon_pending_email`` clears it on the ``IntegrityError`` path), so no
claim can survive it. **A refactor that stops clearing there reopens the arm
silently.** That is written here rather than in a comment on the code because
this file is where a reader looking for the arm inventory will land.

⚠ THE PROVENANCE RIDES IN THE TOKEN, NOT IN A COLUMN. A
``users.pending_email_admin_initiated`` column was considered and rejected: it
needs a migration and must be cleared at four existing sites, and a flag
missed at one of them fails **closed** on a legitimate claim — a new failure
mode with no counterpart here. The token cannot be tampered with without
breaking the HS signature.

⚠ A TOKEN MINTED BEFORE THIS SHIPS carries no ``admin_initiated`` claim and
correctly fails **open** into the user-initiated path. That is the right
direction: no admin-initiated token existed before this shipped. Pinned by
:func:`test_user_initiated_claim_still_promotes_onto_a_verified_row`.

⚠ THE ABORT IS SILENT, AND THAT IS AN ACCEPTED DECISION, NOT AN OVERSIGHT.
``_abandon_pending_email`` clears the claim and ``verify_email`` returns its
generic refusal; no row names the admin claim that just died. Accepted for
v1 — the operator sees the claim gone on next load, and that helper has
exactly two other callers, both of which would inherit any writer added
there.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.invitation import Invitation
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.admin_users import router as admin_users_router
from app.routers.auth import router as auth_router
from app.security import (
    create_email_verification_token,
    create_invitation_token,
    hash_password,
)
from tests.factories import make_test_app


UTC = timezone.utc
TYPO_EMAIL = "dana@exmaple.com"
GOOD_EMAIL = "dana@example.com"
PASSWORD = "S3cret-Pass!"


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    made = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield made
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def sent_mail(monkeypatch) -> list[tuple[str, str]]:
    from app.routers import admin_users as admin_users_module

    captured: list[tuple[str, str]] = []

    async def fake_send(to: str, token: str) -> bool:
        captured.append((to, token))
        return True

    monkeypatch.setattr(admin_users_module, "send_verification_email", fake_send)
    return captured


async def _seed(factory) -> tuple[int, int, User]:
    """Target (unverified, active) + acting superadmin. Returns ids + actor."""
    async with factory() as db:
        org = Organization(name="Dana Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        target = User(
            org_id=org.id,
            username="dana",
            email=TYPO_EMAIL,
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=False,
        )
        actor = User(
            org_id=org.id,
            username="root",
            email="root@platform.example",
            password_hash=hash_password("irrelevant"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        db.add_all([target, actor])
        await db.commit()
        await db.refresh(actor)
        db.expunge(actor)
        return target.id, org.id, actor


def _app(factory, actor: User) -> FastAPI:
    app = make_test_app(
        factory,
        routers=[admin_users_router, auth_router],
        current_user=actor,
        override_session_factory=True,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    return app


async def _read(factory, user_id: int) -> tuple:
    async with factory() as db:
        return (
            await db.execute(
                select(User.email, User.email_verified, User.pending_email).where(
                    User.id == user_id
                )
            )
        ).one()


# ── the four verification arms ──────────────────────────────────────────────


async def _verify_via_bootstrap(factory, client, target_id, org_id, monkeypatch) -> None:
    """Arm 1 — the registration link. ``verify_email``'s bootstrap branch.

    ⚠ This is the arm the design calls out by name, because it verifies the
    row and DELIBERATELY leaves ``pending_email`` intact.
    """
    async with factory() as db:
        current = await db.scalar(select(User.email).where(User.id == target_id))
    token = create_email_verification_token(target_id, current)
    res = client.post("/api/v1/auth/verify-email", json={"token": token})
    assert res.status_code == 200, res.text


async def _verify_via_google(factory, client, target_id, org_id, monkeypatch) -> None:
    """Arm 2 — Google sign-in on the EXISTING row."""
    from app.routers import auth as auth_module

    async with factory() as db:
        current = await db.scalar(select(User.email).where(User.id == target_id))

    class _FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, Any]):
            self.status_code = status_code
            self._json = payload

        def json(self) -> dict[str, Any]:
            return self._json

    class _FakeClient:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, url: str, **k: Any) -> _FakeResponse:
            return _FakeResponse(200, {"access_token": "fake-token"})

        async def get(self, url: str, **k: Any) -> _FakeResponse:
            return _FakeResponse(
                200,
                {
                    "email": current,
                    "verified_email": True,
                    "given_name": "Dana",
                    "family_name": "Doe",
                    "picture": None,
                },
            )

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(auth_module, "_validate_google_config", lambda: None)
    client.cookies.set("oauth_state", "test-state")
    try:
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "test-state"},
            follow_redirects=False,
        )
        assert res.status_code in (200, 302), res.text
    finally:
        client.cookies.delete("oauth_state")


async def _verify_via_invitation(factory, client, target_id, org_id, monkeypatch) -> None:
    """Arm 3 — ``invitation_service.accept_invitation``'s reactivation branch.

    That branch fires only for an INACTIVE same-org row, so the target is
    deactivated first. Realistic: a support flow deactivates the stranded
    account and re-invites it.
    """
    from app.services import invitation_service

    async with factory() as db:
        target = await db.get(User, target_id)
        target.is_active = False
        current = target.email
        inviter = await db.scalar(
            select(User).where(User.is_superadmin.is_(True))
        )
        inv = Invitation(
            org_id=org_id,
            email=current,
            role=Role.MEMBER,
            open_email=current,
            created_by=inviter.id,
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=7),
        )
        db.add(inv)
        await db.commit()
        token = create_invitation_token(inv.id, current)

    async with factory() as db:
        await invitation_service.accept_invitation(
            db, token=token, username="dana", password=PASSWORD
        )
        await db.commit()

    # Reactivate so the redeem below is not refused by the unrelated
    # ``is_active`` guard in ``_promote_pending_email`` — that guard is a
    # DIFFERENT refusal and would mask the one under test.
    async with factory() as db:
        target = await db.get(User, target_id)
        target.is_active = True
        await db.commit()


async def _verify_via_merge(factory, client, target_id, org_id, monkeypatch) -> None:
    """Arm 4 — ``user_merge_service.merge_users`` carries the verified bit
    over from the source row.

    ⚠ The source must be SAME-ORG: ``merge_users`` refuses a cross-org merge
    before it ever reaches the ``email_verified`` carry-over.
    """
    from app.services import user_merge_service

    async with factory() as db:
        source = User(
            org_id=org_id,
            username="dana-sso",
            email="dana.sso@example.com",
            password_hash=hash_password("irrelevant"),
            role=Role.MEMBER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add(source)
        await db.commit()
        source_id = source.id

    async with factory() as db:
        await user_merge_service.merge_users(
            db, source_user_id=source_id, target_user_id=target_id
        )
        await db.commit()


_ARMS = [
    ("bootstrap_link", _verify_via_bootstrap),
    ("google_sign_in", _verify_via_google),
    ("invitation_accept", _verify_via_invitation),
    ("admin_merge", _verify_via_merge),
]


@pytest.mark.parametrize("arm", [a for _n, a in _ARMS], ids=[n for n, _a in _ARMS])
@pytest.mark.asyncio
async def test_f6_admin_token_is_refused_after_the_row_becomes_verified(
    factory, sent_mail, monkeypatch, arm
):
    """Repoint an unverified target, let it become verified by ``arm``, then
    redeem the operator's link. It must be REFUSED.

    Kills the trigger-time-only check.
    """
    target_id, org_id, actor = await _seed(factory)

    with TestClient(_app(factory, actor)) as client:
        res = client.post(
            f"/api/v1/admin/users/{target_id}/email-change",
            json={
                "new_email": GOOD_EMAIL,
                "new_email_confirm": GOOD_EMAIL,
                "reason": "typo at signup, confirmed by phone",
            },
        )
        assert res.status_code == 200, res.text
        _recipient, admin_token = sent_mail[0]

        await arm(factory, client, target_id, org_id, monkeypatch)

        email, verified, pending = await _read(factory, target_id)
        assert verified is True, f"the arm did not verify the row: {arm.__name__}"
        assert pending == GOOD_EMAIL, (
            "the arm cleared pending_email, so this parametrisation proves "
            "nothing for that arm — see the FIFTH-SITE note in the module "
            "docstring"
        )

        clicked = client.post(
            "/api/v1/auth/verify-email", json={"token": admin_token}
        )

    assert clicked.status_code == 400, (
        "an admin-initiated token promoted onto a now-VERIFIED row. The "
        "trigger-time guard is not enough: the claim redeems up to 24h later. "
        f"body={clicked.text}"
    )
    email_after, verified_after, pending_after = await _read(factory, target_id)
    assert email_after == email, "users.email moved on a refused promotion"
    assert verified_after is True
    assert pending_after is None, (
        "the refused claim must be ABANDONED, or it sits armed until it "
        "expires and the operator cannot tell it died"
    )


@pytest.mark.asyncio
async def test_user_initiated_claim_still_promotes_onto_a_verified_row(factory):
    """The polarity control, and the backward-compatibility pin in one.

    A verified user changing their own address via ``PUT /users/me`` produces
    a claim on a row where ``email_verified`` is ALREADY True. Gating the new
    refusal on anything other than the ``admin_initiated`` claim breaks that
    flow entirely — and a token minted before this shipped carries no claim,
    so it must take exactly this path.
    """
    target_id, _org_id, _actor = await _seed(factory)
    async with factory() as db:
        u = await db.get(User, target_id)
        u.email_verified = True
        u.pending_email = GOOD_EMAIL
        await db.commit()

    token = create_email_verification_token(target_id, GOOD_EMAIL)
    app = make_test_app(factory, routers=[auth_router], override_session_factory=True)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    with TestClient(app) as client:
        res = client.post("/api/v1/auth/verify-email", json={"token": token})

    assert res.status_code == 200, res.text
    assert res.json().get("email_changed") is True
    email, verified, pending = await _read(factory, target_id)
    assert email == GOOD_EMAIL
    assert verified is True
    assert pending is None


@pytest.mark.asyncio
async def test_admin_claim_promotes_normally_while_the_row_stays_unverified(
    factory, sent_mail
):
    """The other polarity control. Without it, "refuse admin-initiated
    tokens" unconditionally is a green implementation — and a dead endpoint.
    """
    target_id, _org_id, actor = await _seed(factory)
    with TestClient(_app(factory, actor)) as client:
        assert client.post(
            f"/api/v1/admin/users/{target_id}/email-change",
            json={
                "new_email": GOOD_EMAIL,
                "new_email_confirm": GOOD_EMAIL,
                "reason": "typo at signup, confirmed by phone",
            },
        ).status_code == 200
        _recipient, admin_token = sent_mail[0]
        clicked = client.post(
            "/api/v1/auth/verify-email", json={"token": admin_token}
        )
    assert clicked.status_code == 200, clicked.text
    email, verified, pending = await _read(factory, target_id)
    assert (email, verified, pending) == (GOOD_EMAIL, True, None)
