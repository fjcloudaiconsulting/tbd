"""Fences for the operator email-recovery endpoints (TBD-362).

Spec: ``specs/2026-08-23-tbd-362-admin-email-recovery.md``.

Two endpoints ship here:

* ``POST   /api/v1/admin/users/{user_id}/email-change``
* ``DELETE /api/v1/admin/users/{user_id}/pending-email``

The ruling the whole design rests on: **the operator writes exactly one
column, ``users.pending_email``.** The user proves control by clicking; the
existing ``verify_email`` -> ``_promote_pending_email`` path does the rest.
The ``email_verified`` writer set gains ZERO members (pinned separately, by
``tests/auth/test_email_verified_writer_set.py``).

This file carries F1, F2, F3, F5, F7, F8, F9 and F10. F4 (authz) lives in
``tests/auth/test_admin_email_change_authz.py`` because its legs need the
real ``get_current_user`` seam, F6 (token provenance) in
``tests/auth/test_admin_email_change_provenance.py``, F11 in
``tests/auth/test_email_verified_writer_set.py``.

⚠ WHY F2 IS THE HIGHEST-VALUE FENCE IN THIS FILE.
``specs/2026-05-22-l4-4-admin-slices.md:325-348`` already designed this
endpoint, three months before TBD-361, and its design is BROKEN: it mints an
``email_verify`` token with the new address baked in and never writes
``pending_email``. Implemented literally, ``verify_email`` computes
``promoting = (user.pending_email is not None and token_email ==
user.pending_email)`` -> ``False``, then ``token_email != user.email`` ->
**400, on every click, forever**. That implementation returns 200, dispatches
mail, and passes any "200 returned, mail dispatched" assertion. F2 reads the
token off the dispatch and redeems it end to end, which is the only shape
that can tell the two apart.

⚠ THE TOKEN IS READ OFF THE DISPATCH, NEVER CONSTRUCTED. A self-constructed
token fences the test's own arithmetic instead of the handler's, and would
stay green against a handler that mints from ``body.new_email`` (raw,
possibly mixed-case) rather than from the normalized stored value.

⚠ ``send_verification_email`` is patched in ``app.routers.admin_users``'s
namespace, not in ``app.services.email_service``. The router binds the name
at import (``from ... import send_verification_email``), so patching the
service module leaves the router's reference untouched and the fence would
capture nothing.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

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
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.admin_users import router as admin_users_router
from app.routers.auth import router as auth_router
from app.security import hash_password
from tests.factories import make_test_app


TARGET_PASSWORD = "S3cret-Pass!"
TYPO_EMAIL = "alice@exmaple.com"
GOOD_EMAIL = "alice@example.com"
REASON = "typo at signup, confirmed by phone"


# ── infrastructure ──────────────────────────────────────────────────────────


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
    """The POST carries ``10/hour`` and the DELETE the same. Several tests
    here issue more than one call; without a per-test reset the later ones
    bleed into the budget and 429. No assertion in this file can be
    satisfied by a 429 (every one pins an exact status), so a leak produces
    a red test, never a false green.
    """
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def sent_mail(monkeypatch) -> list[tuple[str, str]]:
    """Capture ``send_verification_email`` dispatches from the ROUTER's
    namespace. See the module docstring for why the service module is the
    wrong patch point.
    """
    from app.routers import admin_users as admin_users_module

    captured: list[tuple[str, str]] = []

    async def fake_send(to: str, token: str) -> bool:
        captured.append((to, token))
        return True

    monkeypatch.setattr(admin_users_module, "send_verification_email", fake_send)
    return captured


async def _seed(
    factory,
    *,
    email: str = TYPO_EMAIL,
    email_verified: bool = False,
    is_active: bool = True,
    is_superadmin: bool = False,
    pending_email: str | None = None,
    password_set: bool = True,
    username: str = "alice",
    org_name: str = "Alice Org",
) -> tuple[int, int]:
    """Seed one org + one target user. Returns ``(user_id, org_id)``."""
    async with factory() as db:
        org = Organization(name=org_name, billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username=username,
            email=email,
            password_hash=hash_password(TARGET_PASSWORD),
            role=Role.OWNER,
            is_superadmin=is_superadmin,
            is_active=is_active,
            email_verified=email_verified,
            pending_email=pending_email,
            password_set=password_set,
        )
        db.add(user)
        await db.commit()
        return user.id, org.id


async def _seed_actor(factory, org_id: int) -> User:
    """The acting superadmin. Detached so it can be returned from the
    ``get_current_user`` override without binding a request session.
    """
    async with factory() as db:
        actor = User(
            org_id=org_id,
            username="root",
            email="root@platform.example",
            password_hash=hash_password("irrelevant"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        db.add(actor)
        await db.commit()
        await db.refresh(actor)
        db.expunge(actor)
        return actor


def _app(factory, actor: User, *, with_auth_router: bool = False) -> FastAPI:
    routers = [admin_users_router]
    if with_auth_router:
        routers.append(auth_router)
    app = make_test_app(
        factory,
        routers=routers,
        current_user=actor,
        override_session_factory=True,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    return app


def _post(client, user_id: int, *, new_email: str, confirm: str | None = None,
          reason: str = REASON):
    return client.post(
        f"/api/v1/admin/users/{user_id}/email-change",
        json={
            "new_email": new_email,
            "new_email_confirm": new_email if confirm is None else confirm,
            "reason": reason,
        },
    )


async def _row(factory, user_id: int) -> dict:
    """Re-read the target as SCALARS on a FRESH session.

    ⚠ Never snapshot the ORM instance the request touched: SQLAlchemy's
    identity map returns the same object, so a "before" snapshot compares an
    object to itself and passes tautologically.
    """
    async with factory() as db:
        res = await db.execute(
            select(
                User.email,
                User.email_verified,
                User.sessions_invalidated_at,
                User.pending_email,
            ).where(User.id == user_id)
        )
        email, verified, cutoff, pending = res.one()
        return {
            "email": email,
            "email_verified": verified,
            "sessions_invalidated_at": cutoff,
            "pending_email": pending,
        }


async def _audit(factory, event_type: str | None = None) -> list[AuditEvent]:
    async with factory() as db:
        stmt = select(AuditEvent).order_by(AuditEvent.id)
        if event_type is not None:
            stmt = stmt.where(AuditEvent.event_type == event_type)
        return list((await db.execute(stmt)).scalars().all())


# ── F1 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f1_endpoint_writes_pending_email_only(factory, sent_mail):
    """F1 — after 200, exactly ONE column moved.

    All four assertions, not three: a handler that gets three right is the
    half-fix, and the ticket's literal ask ("and/or mark it verified") is
    killed by the ``email_verified`` leg specifically.

    ``sessions_invalidated_at`` is seeded non-NULL so "unchanged" is a real
    comparison rather than NULL-against-NULL, which a handler that cleared
    the column would also satisfy.
    """
    user_id, org_id = await _seed(factory)
    async with factory() as db:
        u = await db.get(User, user_id)
        u.sessions_invalidated_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        await db.commit()
    before = await _row(factory, user_id)
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor)) as client:
        res = _post(client, user_id, new_email=GOOD_EMAIL)

    assert res.status_code == 200, res.text
    after = await _row(factory, user_id)
    assert after["email_verified"] is False, "email_verified must NOT be written"
    assert after["email"] == TYPO_EMAIL, "users.email must still hold the typo"
    assert after["sessions_invalidated_at"] == before["sessions_invalidated_at"], (
        "the session cutoff must NOT move — nothing about identity changed"
    )
    assert after["pending_email"] == GOOD_EMAIL, "the claim must be recorded"


# ── F2 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f2_minted_token_actually_promotes_end_to_end(factory, sent_mail):
    """F2 — the link the operator mails must actually work.

    Kills the stale L4.4 implementation, which returns 200, dispatches mail,
    and produces a link that 400s forever.
    """
    user_id, org_id = await _seed(factory)
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor, with_auth_router=True)) as client:
        res = _post(client, user_id, new_email=GOOD_EMAIL)
        assert res.status_code == 200, res.text

        assert len(sent_mail) == 1, f"expected one dispatch, got {sent_mail!r}"
        recipient, token = sent_mail[0]
        assert recipient == GOOD_EMAIL

        clicked = client.post("/api/v1/auth/verify-email", json={"token": token})

    assert clicked.status_code == 200, (
        "the operator's link 400'd. This is the L4.4 dead end: a token minted "
        "without writing pending_email can never satisfy verify_email's "
        f"promoting branch. body={clicked.text}"
    )
    assert clicked.json().get("email_changed") is True, (
        "the click must take the PROMOTING branch, not the bootstrap arm"
    )

    after = await _row(factory, user_id)
    assert after["email"] == GOOD_EMAIL
    assert after["email_verified"] is True
    assert after["pending_email"] is None


@pytest.mark.asyncio
async def test_f2b_mixed_case_input_still_promotes(factory, sent_mail):
    """F2b — mint from the NORMALIZED STORED value, never from ``body``.

    ``_promote_pending_email`` compares the token's ``email`` claim to the
    stored column byte-exactly. A handler minting from ``body.new_email``
    hands any operator who types mixed case a link that 400s forever — the
    hazard ``users.py:222-226`` documents. That handler passes F2 (which
    types lowercase) and fails only here.
    """
    user_id, org_id = await _seed(factory)
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor, with_auth_router=True)) as client:
        res = _post(client, user_id, new_email="Alice@Example.COM",
                    confirm="Alice@Example.COM")
        assert res.status_code == 200, res.text
        recipient, token = sent_mail[0]
        assert recipient == GOOD_EMAIL, "the dispatch must go to the normalized form"
        clicked = client.post("/api/v1/auth/verify-email", json={"token": token})

    assert clicked.status_code == 200, (
        "token minted from raw body input — the promote-time comparison is "
        f"byte-exact, so this link is dead. body={clicked.text}"
    )
    after = await _row(factory, user_id)
    assert after["email"] == GOOD_EMAIL


# ── F3 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f3_login_still_403s_before_the_click(factory, sent_mail):
    """F3 — both halves, and the FIRST is the load-bearing one.

    "200 after the click" alone is satisfied by the wrong design (an
    operator-asserted verification). "403 before the click" alone is
    satisfied by an endpoint that does nothing. Together they pin the
    ruling: the operator moves the challenge, the USER clears the gate.

    ⚠ The target is seeded ``password_set=True`` with a known password, or
    the second half is structurally unreachable for an SSO-shaped row.
    """
    user_id, org_id = await _seed(factory, password_set=True)
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor, with_auth_router=True)) as client:
        assert _post(client, user_id, new_email=GOOD_EMAIL).status_code == 200

        blocked = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": TARGET_PASSWORD},
        )
        assert blocked.status_code == 403, (
            "the account must stay locked out until the USER clicks — an "
            f"exemption was sneaked into the login gate. body={blocked.text}"
        )
        assert blocked.json()["detail"]["code"] == "email_not_verified"

        _, token = sent_mail[0]
        assert client.post(
            "/api/v1/auth/verify-email", json={"token": token}
        ).status_code == 200

        allowed = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": TARGET_PASSWORD},
        )
    assert allowed.status_code == 200, allowed.text
    assert "access_token" in allowed.json()


# ── F5 ──────────────────────────────────────────────────────────────────────


_REFUSAL_CASES = [
    # (id, seed kwargs, new_email, expected code)
    (
        "verified_target",
        {"email_verified": True},
        GOOD_EMAIL,
        "user_already_verified",
    ),
    (
        "superadmin_target",
        {"is_superadmin": True},
        GOOD_EMAIL,
        "target_is_superadmin",
    ),
    (
        "inactive_target",
        {"is_active": False},
        GOOD_EMAIL,
        "user_inactive",
    ),
    (
        # ⚠ MIXED-CASE STORED EMAIL, deliberately. ``_promote_pending_email``'s
        # own comment records that mixed-case ``users.email`` rows genuinely
        # exist in production. Against a BYTE comparison this leg passes green
        # on the SQLite shards while production (utf8mb4_0900_ai_ci) returns a
        # misleading ``email_already_in_use`` from the advisory SELECT — and
        # on SQLite it would arm the self-referential promotion the design
        # refutes by execution. Seeding lowercase here makes the leg vacuous.
        "unchanged_email_mixed_case",
        {"email": "Alice@Example.com"},
        GOOD_EMAIL,
        "email_unchanged",
    ),
]


@pytest.mark.parametrize(
    "seed_kwargs,new_email,expected_code",
    [(c[1], c[2], c[3]) for c in _REFUSAL_CASES],
    ids=[c[0] for c in _REFUSAL_CASES],
)
@pytest.mark.asyncio
async def test_f5_refuses_and_writes_nothing(
    factory, sent_mail, seed_kwargs, new_email, expected_code
):
    """F5 — 409, AND ``pending_email`` not written, AND no mail dispatched.

    A handler that commits and then raises leaves the claim live: the
    operator sees an error and a stranger holds a working promotion link.
    Status alone cannot see that.
    """
    user_id, org_id = await _seed(factory, **seed_kwargs)
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor)) as client:
        res = _post(client, user_id, new_email=new_email)

    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == expected_code, res.text
    after = await _row(factory, user_id)
    assert after["pending_email"] is None, "a refused request wrote the claim anyway"
    assert after["email_verified"] is seed_kwargs.get("email_verified", False)
    assert sent_mail == [], "a refused request dispatched mail"


@pytest.mark.asyncio
async def test_f5_refuses_email_already_in_use(factory, sent_mail):
    """F5 — the address belongs to ANOTHER row.

    ⚠⚠ MEASURED, NOT ASSUMED: deleting ``User.id != user_id`` from the
    advisory SELECT leaves this file **GREEN** on the SQLite shards. That is
    an equivalent mutant HERE and a live defect on MySQL, and the reason is
    worth writing down rather than rediscovering.

    Two things shadow it on SQLite. First, ``email_unchanged`` is checked
    BEFORE this SELECT, so for a lowercase-stored row the target can never
    reach it. Second — and this is the half no shard can see —
    ``users.email`` is ``utf8mb4_0900_ai_ci``, which is accent-INsensitive as
    well as case-insensitive, while ``normalize_email`` is only
    ``strip().lower()``. So ``jose@x.com`` against a stored ``josé@x.com``
    passes ``email_unchanged`` (the two differ after ``.lower()``) and then
    matches the target's OWN row in this SELECT on MySQL — returning
    ``email_already_in_use`` for the account's own address. SQLite compares
    binary and matches nothing.

    The id guard is therefore load-bearing on production and structurally
    unfenceable on the shards, which is exactly the class CLAUDE.md warns
    about ("everything else in CI runs on in-process aiosqlite, so a
    MySQL-only defect is invisible to the shards"). Keep the guard; do not
    read a green run here as proof it is unnecessary.
    """
    user_id, org_id = await _seed(factory)
    await _seed(
        factory, email=GOOD_EMAIL, username="bob", org_name="Bob Org",
        email_verified=True,
    )
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor)) as client:
        res = _post(client, user_id, new_email=GOOD_EMAIL)

    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "email_already_in_use", res.text
    assert (await _row(factory, user_id))["pending_email"] is None
    assert sent_mail == []


@pytest.mark.asyncio
async def test_f5_missing_target_is_404(factory, sent_mail):
    user_id, org_id = await _seed(factory)
    actor = await _seed_actor(factory, org_id)
    with TestClient(_app(factory, actor)) as client:
        res = _post(client, user_id + 9999, new_email=GOOD_EMAIL)
    assert res.status_code == 404, res.text
    assert res.json()["detail"]["code"] == "user_not_found", res.text
    assert sent_mail == []


# ── F7 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f7_confirmation_mismatch_refused_after_normalization(
    factory, sent_mail
):
    """F7 — the double entry compares NORMALIZED values.

    Kills BOTH wrong implementations in one test:

    * a byte-equality check rejects a legitimate case difference, which
      trains operators to paste both fields and defeats the confirmation
      entirely (leg one, which must be 200);
    * no comparison at all accepts a genuine typo in the correction (leg
      two, which must be 400).
    """
    user_id, org_id = await _seed(factory)
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor)) as client:
        same = _post(client, user_id, new_email="Alice@x.com", confirm="alice@x.com")
        assert same.status_code == 200, (
            "a pure case difference is the SAME address; refusing it defeats "
            f"the confirmation. body={same.text}"
        )

    # Reset for the second leg so a live claim does not change the shape.
    user_id2, org_id2 = await _seed(
        factory, username="carol", org_name="Carol Org", email="carol@exmaple.com"
    )
    with TestClient(_app(factory, actor)) as client:
        differs = _post(
            client, user_id2, new_email="alice@x.com", confirm="aliceee@x.com"
        )
    assert differs.status_code == 400, differs.text
    assert differs.json()["detail"]["code"] == "emails_do_not_match", differs.text
    assert (await _row(factory, user_id2))["pending_email"] is None


# ── F8 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f8_overwrite_records_the_claim_it_destroyed(factory, sent_mail):
    """F8 — this write IS the "overwrite by a later request" clearer.

    Without ``previous_pending_email`` in the audit detail a destroyed claim
    leaves no trace anywhere: the column is gone and no other row names it.
    """
    user_id, org_id = await _seed(factory, pending_email="wrong@example.com")
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor)) as client:
        res = _post(client, user_id, new_email=GOOD_EMAIL)
    assert res.status_code == 200, res.text

    rows = await _audit(factory, "admin.user.email_change.triggered")
    assert len(rows) == 1, rows
    assert rows[0].detail["previous_pending_email"] == "wrong@example.com"
    assert rows[0].detail["target_pending_email"] == GOOD_EMAIL


@pytest.mark.asyncio
async def test_f8b_success_row_carries_the_forensic_snapshot(factory, sent_mail):
    """The success row's contract, spec §5.

    ``target_email_old`` matters because promotion OVERWRITES ``users.email``
    and nothing else preserves the typo that caused the incident.

    ⚠ ``target_org_id`` is read off the TARGET and must be set:
    ``/admin/audit``'s only org filter is ``target_org_id``, so a row built
    by copying ``merge_users`` (which passes ``None``) is invisible to every
    org-scoped audit query.

    ⚠ ``actor_email`` is the SUPERADMIN's, diverging from the self-initiated
    convention: ``audit_events`` has no ``target_user_id`` column, so on an
    admin-triggered row the reader's question is "which operator".
    """
    user_id, org_id = await _seed(factory)
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor)) as client:
        assert _post(client, user_id, new_email=GOOD_EMAIL).status_code == 200

    rows = await _audit(factory, "admin.user.email_change.triggered")
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome.value == "success"
    assert row.actor_email == "root@platform.example"
    assert row.target_org_id == org_id
    assert row.detail["target_user_id"] == user_id
    assert row.detail["target_email_old"] == TYPO_EMAIL
    assert row.detail["target_pending_email"] == GOOD_EMAIL
    assert row.detail["previous_pending_email"] is None
    assert row.detail["reason"] == REASON
    assert row.detail["kind"] == "admin_initiated"


# ── F9 ──────────────────────────────────────────────────────────────────────


_FAILURE_CASES = [
    ("user_not_found", {}, GOOD_EMAIL, None, 404),
    ("emails_do_not_match", {}, GOOD_EMAIL, "nope@example.com", 400),
    ("user_already_verified", {"email_verified": True}, GOOD_EMAIL, None, 409),
    ("target_is_superadmin", {"is_superadmin": True}, GOOD_EMAIL, None, 409),
    ("user_inactive", {"is_active": False}, GOOD_EMAIL, None, 409),
    ("email_unchanged", {"email": "Alice@Example.com"}, GOOD_EMAIL, None, 409),
]


@pytest.mark.parametrize(
    "expected_code,seed_kwargs,new_email,confirm,expected_status",
    _FAILURE_CASES,
    ids=[c[0] for c in _FAILURE_CASES],
)
@pytest.mark.asyncio
async def test_f9_audit_on_every_failure_path(
    factory, sent_mail, expected_code, seed_kwargs, new_email, confirm,
    expected_status,
):
    """F9 — every refusal writes a row, on the independent session.

    ⚠ ``outcome == "failure"`` is asserted EXPLICITLY. A write that forgot
    the argument and defaulted to success would pass a row-exists check while
    making every refusal invisible to an outcome-filtered audit query.

    ⚠ ``404`` IS audited here, diverging from ``delete_user``: probing this
    path carries an attacker-supplied DESTINATION address, where the delete
    path carries no payload at all.

    The pre-refusal snapshot is asserted because a row saying only "409"
    cannot distinguish "this account was already locked out" from "an
    operator just attacked an active superadmin".
    """
    user_id, org_id = await _seed(factory, **seed_kwargs)
    actor = await _seed_actor(factory, org_id)
    target_id = user_id + 9999 if expected_code == "user_not_found" else user_id

    with TestClient(_app(factory, actor)) as client:
        res = _post(client, target_id, new_email=new_email, confirm=confirm)

    assert res.status_code == expected_status, res.text
    assert res.json()["detail"]["code"] == expected_code, res.text

    rows = await _audit(factory, "admin.user.email_change.failed")
    assert len(rows) == 1, f"no failure audit row for {expected_code}: {rows!r}"
    row = rows[0]
    assert row.outcome.value == "failure", "default-outcome write"
    assert row.actor_email == "root@platform.example"
    assert row.detail["code"] == expected_code
    assert row.detail["target_user_id"] == target_id
    assert row.detail["attempted_email"] == new_email
    assert row.detail["reason"] == REASON
    if expected_code != "user_not_found":
        assert row.target_org_id == org_id
        assert row.detail["target_email_verified"] is seed_kwargs.get(
            "email_verified", False
        )
        assert row.detail["target_is_active"] is seed_kwargs.get("is_active", True)
        assert row.detail["target_is_superadmin"] is seed_kwargs.get(
            "is_superadmin", False
        )


@pytest.mark.asyncio
async def test_f9_email_already_in_use_is_audited(factory, sent_mail):
    user_id, org_id = await _seed(factory)
    await _seed(
        factory, email=GOOD_EMAIL, username="bob", org_name="Bob Org",
        email_verified=True,
    )
    actor = await _seed_actor(factory, org_id)
    with TestClient(_app(factory, actor)) as client:
        res = _post(client, user_id, new_email=GOOD_EMAIL)
    assert res.status_code == 409
    rows = await _audit(factory, "admin.user.email_change.failed")
    assert len(rows) == 1
    assert rows[0].outcome.value == "failure"
    assert rows[0].detail["code"] == "email_already_in_use"


# ── F10 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f10_mail_goes_only_to_the_new_address_at_request_time(
    factory, monkeypatch, sent_mail
):
    """F10 — exactly one verification dispatch, to the NEW address, and no
    credential-bearing sender is touched at all.

    ⚠ NO CREDENTIAL IS EVER MAILED TO ``pending_email``. The verification
    link is a proof-of-control challenge and grants nothing unless the
    recipient controls the mailbox. Mailing a password reset there instead
    would be remote account takeover.

    ⚠ PAIRED with the promotion leg below, or the fence is satisfiable by
    deleting the downstream notification instead of by getting this right.
    """
    from app.routers import admin_users as admin_users_module
    from app.services import email_service, notification_service

    forbidden: list[str] = []

    async def _forbid(name):
        async def _f(*a, **k):
            forbidden.append(name)
            return True
        return _f

    for name in (
        "send_password_reset_email",
        "send_password_changed_email",
        "send_account_deleted_email",
    ):
        if hasattr(email_service, name):
            async def _tripwire(*a, _n=name, **k):
                forbidden.append(_n)
                return True
            monkeypatch.setattr(email_service, name, _tripwire)

    security_emails: list[str] = []

    async def fake_security_email(db, *, user_id, email, event_type, title,
                                  body, link_url=None):
        security_emails.append(email)

    monkeypatch.setattr(
        notification_service, "send_security_email_best_effort", fake_security_email
    )
    monkeypatch.setattr(
        admin_users_module.notification_service,
        "send_security_email_best_effort",
        fake_security_email,
        raising=False,
    )

    user_id, org_id = await _seed(factory)
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor, with_auth_router=True)) as client:
        assert _post(client, user_id, new_email=GOOD_EMAIL).status_code == 200

        assert len(sent_mail) == 1, sent_mail
        assert sent_mail[0][0] == GOOD_EMAIL
        assert forbidden == [], f"a credential sender was invoked: {forbidden}"
        # The old-address alert is a SECURITY notice, not a credential, and
        # fires even though that address is unverified: "typo'd" and
        # "attacker-chosen" are indistinguishable to the system, and where
        # the address is live this is the only out-of-band signal the target
        # gets.
        assert security_emails == [TYPO_EMAIL], security_emails

        # ── the paired half: promotion still mails BOTH addresses ──────────
        security_emails.clear()
        _, token = sent_mail[0]
        assert client.post(
            "/api/v1/auth/verify-email", json={"token": token}
        ).status_code == 200

    assert sorted(security_emails) == sorted([TYPO_EMAIL, GOOD_EMAIL]), (
        "promotion must still notify the OLD address (the inbox a hijack "
        "victim still controls) and confirm to the NEW one; this fence is "
        f"otherwise satisfiable by deleting that dispatch. got={security_emails}"
    )


# ── DELETE /pending-email (spec §2) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_cancel_clears_the_claim_and_audits(factory):
    """Spec §2. The operator's escape from their OWN mistyped correction.

    ⚠ Returns **200 {"cleared": bool}** where the user-side sibling returns
    204. The divergence is deliberate: the operator needs to know whether
    anything was actually cleared. Do not "harmonise" it.
    """
    user_id, org_id = await _seed(factory, pending_email="attacker@evil.example")
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor)) as client:
        res = client.delete(f"/api/v1/admin/users/{user_id}/pending-email")

    assert res.status_code == 200, res.text
    assert res.json() == {"cleared": True}
    after = await _row(factory, user_id)
    assert after["pending_email"] is None
    assert after["email"] == TYPO_EMAIL
    assert after["email_verified"] is False

    rows = await _audit(factory, "admin.user.email_change.cancelled")
    assert len(rows) == 1
    assert rows[0].outcome.value == "success"
    assert rows[0].target_org_id == org_id
    assert rows[0].detail["previous_pending_email"] == "attacker@evil.example"


@pytest.mark.asyncio
async def test_admin_cancel_is_idempotent(factory):
    user_id, org_id = await _seed(factory, pending_email=None)
    actor = await _seed_actor(factory, org_id)
    with TestClient(_app(factory, actor)) as client:
        res = client.delete(f"/api/v1/admin/users/{user_id}/pending-email")
    assert res.status_code == 200, res.text
    assert res.json() == {"cleared": False}


@pytest.mark.asyncio
async def test_admin_cancel_kills_the_live_link(factory, sent_mail):
    """The whole justification for the endpoint, end to end.

    Without it the remedies for a mistyped CORRECTION are: wait out the 24h
    window with a live takeover link in a stranger's inbox; overwrite with a
    third address, which revokes the bad link only by minting another one at
    an address the operator by hypothesis does not have; or direct SQL.

    ⚠ "Just overwrite the claim with the target's own ``users.email``, which
    is inert" is WRONG and was refuted by execution: that write makes
    ``promoting`` evaluate True for any live register-minted bootstrap token,
    manufacturing a false completion record.
    """
    user_id, org_id = await _seed(factory)
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor, with_auth_router=True)) as client:
        assert _post(client, user_id, new_email="stranger@evil.example").status_code == 200
        _, token = sent_mail[0]
        assert client.delete(
            f"/api/v1/admin/users/{user_id}/pending-email"
        ).json() == {"cleared": True}
        clicked = client.post("/api/v1/auth/verify-email", json={"token": token})

    assert clicked.status_code == 400, (
        f"a cancelled claim's link still promoted. body={clicked.text}"
    )
    after = await _row(factory, user_id)
    assert after["email"] == TYPO_EMAIL
    assert after["email_verified"] is False


@pytest.mark.asyncio
async def test_admin_cancel_404s_for_a_missing_target(factory):
    """⚠ The structured ``code`` is asserted, not just the status. A bare
    ``== 404`` is ALSO satisfied by the route never being mounted, which is a
    vacuous pass — the same trap F4 calls out for "not 200".
    """
    user_id, org_id = await _seed(factory)
    actor = await _seed_actor(factory, org_id)
    with TestClient(_app(factory, actor)) as client:
        res = client.delete(f"/api/v1/admin/users/{user_id + 9999}/pending-email")
    assert res.status_code == 404, res.text
    assert res.json()["detail"]["code"] == "user_not_found", res.text


# ── the admin payload carries pending_email (spec "UI", change 3) ───────────


@pytest.mark.asyncio
async def test_admin_user_payload_exposes_pending_email(factory):
    """The serializer is the SHARED list-and-detail payload, so this also
    exposes pending claims on ``GET /admin/users``. Intended, and asserted on
    both surfaces so the breadth is recorded rather than discovered.
    """
    user_id, org_id = await _seed(factory, pending_email=GOOD_EMAIL)
    actor = await _seed_actor(factory, org_id)

    with TestClient(_app(factory, actor)) as client:
        detail = client.get(f"/api/v1/admin/users/{user_id}")
        listing = client.get("/api/v1/admin/users")

    assert detail.status_code == 200, detail.text
    assert detail.json()["pending_email"] == GOOD_EMAIL
    assert listing.status_code == 200, listing.text
    row = next(i for i in listing.json()["items"] if i["id"] == user_id)
    assert row["pending_email"] == GOOD_EMAIL
