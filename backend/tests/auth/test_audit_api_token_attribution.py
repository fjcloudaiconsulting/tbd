"""``audit_events.api_token_id`` — the acting-credential fences (TBD-188).

Spec: ``specs/2026-08-07-audit-api-token-attribution.md``.

The column answers "everything token 42 did" in one hop, without joining
against a structlog stream that DO App Platform does not retain. It records
the API token **presented as the credential** for the request — the ACTOR,
never the subject.

Three traps this file is built to avoid; read them before editing:

**Trap 1 — the factory.** ``tests/factories/app.py`` stamps
``request.state.auth_method = "jwt"`` inside the ``get_current_user``
override it installs, so a test that passes ``current_user=`` can never
reach ``authenticate_pat``. Every app here is a bare ``FastAPI()`` with the
REAL ``get_current_user`` and only ``get_db`` / ``get_session_factory``
overridden — the ``test_pat_authentication.py::_make_client`` shape.

**Trap 2 — the hand-bound contextvar.** A test that calls
``bind_contextvars`` itself proves only that the *builder reads*; it stays
green against an implementation where ``authenticate_pat`` never *binds*.
Manual binds are confined to :func:`test_f5_builder_reads_bound_context`.
The request-driven fences additionally assert the context is empty before
the call, so they cannot be satisfied by leakage from the test process.

**Trap 3 — the false null.** A PAT aimed at an interactive-gated route gets
403 and writes NO audit row at all; ``row is None`` would then satisfy a
"NULL" assertion for entirely the wrong reason. Every NULL fence here
asserts the row EXISTS first.

**Trap 4 — every fixture id is 1.** The column holds a *token* id, and the
two nearest wrong values are the *user* id and the audit row's own PK. If
the seed leaves all three equal to ``1``, ``assert row.api_token_id ==
token_id`` compares ``1 == 1`` and a bind of ``row.created_by_user_id``
instead of ``row.id`` keeps the whole file green — while on prod MySQL that
value violates the ``api_tokens.id`` FK and ``record_audit_event`` swallows
the write error, losing every audit row silently. ``_seed_superadmin``
therefore burns an ``api_tokens`` id with a decoy row, and ``_mint_pat_row``
asserts the resulting ids actually differ. Both are load-bearing.

Acceptance route is ``POST /api/v1/tags``: absent from
``INTERACTIVE_ONLY_ROUTES``, gated only by ``Depends(get_current_user)``,
``POST`` so it exercises the ``scope == "write"`` branch, audits
unconditionally on success, and its ``detail`` carries no token id — so a
green cannot be satisfied by a pre-existing write. The NULL leg uses the
SAME route with a JWT bearer, making the credential the only varying input.
"""
from __future__ import annotations

import secrets as _secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.api_token import ApiToken
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.api_tokens import router as api_tokens_router
from app.routers.tags import router as tags_router
from app.security import create_access_token, hash_password
from app.services import audit_service, notification_service
from app.services.api_token_service import hash_api_token


UTC = timezone.utc
PASSWORD = "correct-horse-battery"
TAGS = "/api/v1/tags"
TOKENS = "/api/v1/system/api-tokens"


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield f
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _mock_email(monkeypatch):
    """The mint path notifies by email; stub it so no Mailgun call happens."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        notification_service, "send_notification_email", AsyncMock(return_value=None)
    )


async def _seed_superadmin(factory) -> int:
    async with factory() as s:
        org = Organization(name="Platform", billing_cycle_day=1)
        s.add(org)
        await s.flush()
        u = User(
            org_id=org.id,
            username="root",
            email="root@platform.io",
            first_name="Root",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
            password_set=True,
            mfa_enabled=False,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)

        # ⚠ ID-DISPLACEMENT DECOY — load-bearing, do NOT delete (trap 4).
        # This row exists only to consume ``api_tokens.id = 1`` so that every
        # token minted afterwards gets an id that is NOT the superadmin's user
        # id and NOT the first audit row's PK. Without it the fences below
        # compare 1 == 1 and stay green against a bind of the wrong column.
        # It is never authenticated with: nothing holds its plaintext, and
        # ``token_hash`` is a fixed non-secret literal precisely so it cannot
        # collide with a real minted token's hash.
        decoy = ApiToken(
            token_hash="decoy-" + "0" * 58,
            token_prefix="pat_decoy",
            name="id-displacement decoy",
            scope="read",
            created_by_user_id=u.id,
            created_by_email=u.email,
            expires_at=_naive_now() + timedelta(days=30),
        )
        s.add(decoy)
        await s.commit()
        return u.id


async def _mint_pat_row(
    factory,
    owner_id: int,
    *,
    scope: str = "write",
    revoked: bool = False,
    expired: bool = False,
) -> tuple[str, int]:
    """Insert an ``ApiToken`` directly and return ``(plaintext, row_id)``.

    Asserts the returned token id is distinguishable from the owner's user
    id (trap 4). This is the self-fence on ``_seed_superadmin``'s decoy: if
    someone deletes the decoy as "an unused fixture row", this fires here
    rather than leaving the whole file silently vacuous.
    """
    plaintext = "pat_" + _secrets.token_urlsafe(32)
    async with factory() as s:
        owner = await s.get(User, owner_id)
        row = ApiToken(
            token_hash=hash_api_token(plaintext),
            token_prefix=plaintext[:14],
            name="fence",
            scope=scope,
            created_by_user_id=owner.id,
            created_by_email=owner.email,
            expires_at=(
                _naive_now() - timedelta(days=1)
                if expired
                else _naive_now() + timedelta(days=30)
            ),
            revoked_at=_naive_now() if revoked else None,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        assert row.id != owner.id, (
            "the seeded token id equals the owner's user id — every "
            "api_token_id assertion below would compare a value to itself "
            "and stay green against a bind of the wrong column. Restore the "
            "id-displacement decoy in _seed_superadmin (trap 4)."
        )
        return plaintext, row.id


def _make_client(factory, *routers) -> TestClient:
    """REAL ``get_current_user`` seam — NOT ``make_test_app`` (trap 1)."""
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: factory
    for r in routers:
        app.include_router(r)
    return TestClient(app)


async def _jwt_for(factory, user_id: int) -> str:
    async with factory() as s:
        u = await s.get(User, user_id)
        return create_access_token(u.id, u.org_id, u.role.value)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _audit_rows(factory, event_type: str) -> list[AuditEvent]:
    async with factory() as s:
        result = await s.execute(
            select(AuditEvent).where(AuditEvent.event_type == event_type)
        )
        return list(result.scalars().all())


def _assert_test_context_is_clean() -> None:
    """Trap 2 guard: this test process must not have bound the value itself.

    If this ever fires, the fence below would be proving the builder reads a
    contextvar the TEST set — not one ``authenticate_pat`` set.
    """
    assert structlog.contextvars.get_contextvars().get("api_token_id") is None, (
        "the test process has api_token_id bound before the request — the "
        "assertions below would be vacuous (see trap 2 in the module docstring)"
    )


# ── F1 · population 1: PAT-authed HTTP, post-bind ────────────────────────────


async def test_f1_pat_authed_action_records_the_acting_token(factory):
    """A write-scope PAT creating a tag stamps its id on the audit row.

    Mutants that must redden this:
      * M1a — ``audit_service._acting_api_token_id`` returns ``None``.
      * M1b — delete the ``bind_contextvars(api_token_id=...)`` in ``pat.py``.
    """
    uid = await _seed_superadmin(factory)
    pat, token_id = await _mint_pat_row(factory, uid, scope="write")

    _assert_test_context_is_clean()

    with _make_client(factory, tags_router) as client:
        r = client.post(TAGS, json={"name": "groceries"}, headers=_h(pat))
    assert r.status_code == 201, r.text

    rows = await _audit_rows(factory, "tag.created")
    assert len(rows) == 1, "the acceptance route did not write its audit row"
    # Trap 4: the three ids in play must be pairwise distinct, or the
    # assertion below cannot tell a token id from the acting user's id or
    # from the audit row's own primary key.
    assert token_id != uid and token_id != rows[0].id, (
        f"ids are not distinguishable (token={token_id}, user={uid}, "
        f"audit pk={rows[0].id}) — see trap 4"
    )
    assert rows[0].api_token_id == token_id, (
        "a PAT-authed action was not attributed to the acting token "
        f"(got {rows[0].api_token_id!r}, want {token_id})"
    )
    # The token id is genuinely new information here — the tag.created detail
    # never carried it, so this green cannot come from a pre-existing write.
    assert "api_token_id" not in (rows[0].detail or {})


# ── F2 · population 2: interactive JWT on the SAME route ─────────────────────


async def test_f2_jwt_session_leaves_the_column_null(factory):
    """Same route, same app, JWT credential → row EXISTS and column is NULL.

    Mutant: ``_acting_api_token_id`` falls back to ``ctx.get("user_id")``
    ("inventing a value"). The ``actor_user_id`` assertion is what makes that
    fallback distinguishable from a genuine NULL.
    """
    uid = await _seed_superadmin(factory)
    jwt = await _jwt_for(factory, uid)

    _assert_test_context_is_clean()

    with _make_client(factory, tags_router) as client:
        r = client.post(TAGS, json={"name": "utilities"}, headers=_h(jwt))
    assert r.status_code == 201, r.text

    rows = await _audit_rows(factory, "tag.created")
    # Trap 3: prove the row EXISTS before reading NULL off it. A missing row
    # would satisfy "is None" for entirely the wrong reason.
    assert len(rows) == 1, (
        "no audit row was written — a NULL assertion here would be vacuous "
        "(trap 3)"
    )
    assert rows[0].actor_user_id == uid, "wrong actor — is this the right row?"
    assert rows[0].api_token_id is None, (
        "an interactive JWT session was attributed to an API token "
        f"({rows[0].api_token_id!r}) — the resolver is inventing a value"
    )


# ── F3 · population 3: THE DOOR — rejected pre-bind ──────────────────────────


@pytest.mark.parametrize(
    "kind,reason",
    [("revoked", "revoked"), ("expired", "expired")],
    ids=["revoked", "expired"],
)
async def test_f3_auth_rejected_row_carries_the_token_in_the_column(
    factory, kind, reason
):
    """A revoked/expired PAT's ``api_token.auth_rejected`` row is attributed.

    ⚠ THIS IS THE HALF-FIX DOOR. ``_record_auth_rejected`` fires BEFORE the
    composite ``bind_contextvars`` at the end of ``authenticate_pat``, so an
    implementation that binds ``api_token_id`` there leaves the one event
    type entirely *about* a token with ``api_token_id IS NULL`` — while
    ``detail`` still carries the id, so the row LOOKS attributed.

    The named mutant is therefore **POSITIONAL, not value-based**: move the
    bind back down into the composite bind. F1 stays green; this must go red.
    Mutating ``row.id`` → ``None`` instead reddens for the wrong reason and
    pins nothing.
    """
    uid = await _seed_superadmin(factory)
    pat, token_id = await _mint_pat_row(
        factory, uid, scope="write", revoked=(kind == "revoked"),
        expired=(kind == "expired"),
    )

    _assert_test_context_is_clean()

    with _make_client(factory, tags_router) as client:
        r = client.post(TAGS, json={"name": "nope"}, headers=_h(pat))
    assert r.status_code == 401, r.text

    rows = await _audit_rows(factory, "api_token.auth_rejected")
    assert len(rows) == 1, "the rejection did not write its audit row"
    assert token_id != uid and token_id != rows[0].id, (
        f"ids are not distinguishable (token={token_id}, user={uid}, "
        f"audit pk={rows[0].id}) — see trap 4"
    )
    assert rows[0].detail["reason"] == reason
    # detail keeps the id (pre-existing behaviour, deliberately unchanged) …
    assert rows[0].detail["api_token_id"] == token_id
    # … and the COLUMN must carry it too, or the query the column exists for
    # misses exactly the rows a leaked-token hunt starts from.
    assert rows[0].api_token_id == token_id, (
        "api_token.auth_rejected was written with a NULL api_token_id — the "
        "bind in authenticate_pat has moved below the rejection branches"
    )
    # And no tag was created, so nothing else could have supplied the id.
    assert await _audit_rows(factory, "tag.created") == []


# ── F4 · population 4: subject ≠ actor ───────────────────────────────────────


async def test_f4_token_created_by_a_session_keeps_the_column_null(factory):
    """Minting a token from a JWT session: subject in ``detail``, actor NULL.

    The wrong intuition — "the row mentions a token, so set the column" — is
    the single most likely thing a future implementer or reviewer will act
    on, and acting on it permanently merges actor and subject into one field.

    Mutant: ``bind_contextvars(api_token_id=row.id)`` before the
    ``record_audit_event`` in ``api_tokens.mint_token``.
    """
    uid = await _seed_superadmin(factory)
    jwt = await _jwt_for(factory, uid)

    _assert_test_context_is_clean()

    with _make_client(factory, api_tokens_router) as client:
        r = client.post(
            TOKENS,
            json={
                "name": "ci-runner",
                "scope": "read",
                "expires_in_days": 30,
                "current_password": PASSWORD,
            },
            headers=_h(jwt),
        )
    assert r.status_code == 201, r.text
    new_token_id = r.json()["id"]
    assert new_token_id != uid, (
        f"minted token id equals the actor's user id ({uid}) — the NULL "
        "assertion below could not tell them apart (trap 4)"
    )

    rows = await _audit_rows(factory, "api_token.created")
    assert len(rows) == 1, "mint wrote no audit row (trap 3)"
    assert rows[0].actor_user_id == uid
    assert rows[0].detail["api_token_id"] == new_token_id, "subject id lost"
    assert rows[0].api_token_id is None, (
        "api_token.created recorded its SUBJECT token as the ACTING "
        "credential — actor and subject must stay in separate fields"
    )


async def test_f4_token_revoked_by_a_session_keeps_the_column_null(factory):
    """Same contract on the revoke half of the pair."""
    uid = await _seed_superadmin(factory)
    _, victim_id = await _mint_pat_row(factory, uid, scope="read")
    jwt = await _jwt_for(factory, uid)

    _assert_test_context_is_clean()

    with _make_client(factory, api_tokens_router) as client:
        r = client.delete(f"{TOKENS}/{victim_id}", headers=_h(jwt))
    assert r.status_code == 200, r.text

    rows = await _audit_rows(factory, "api_token.revoked")
    assert len(rows) == 1, "revoke wrote no audit row (trap 3)"
    assert rows[0].actor_user_id == uid
    assert rows[0].detail["api_token_id"] == victim_id
    assert rows[0].api_token_id is None, (
        "api_token.revoked recorded its SUBJECT token as the ACTING "
        "credential — the actor here is a JWT session"
    )


# ── F5 · builder unit — the ONLY fence permitted to bind manually ────────────


def test_f5_builder_reads_bound_context():
    """``_build_audit_event`` resolves the column from the request context.

    This is the one place a manual ``bind_contextvars`` is legitimate: it
    isolates the *read* half of the mechanism. On its own it proves nothing
    about ``authenticate_pat`` ever *binding* — that is F1/F3's job (trap 2).
    """
    kwargs = dict(
        event_type="tag.created",
        actor_user_id=7,
        actor_email="a@b.io",
        target_org_id=1,
        target_org_name="Acme",
        request_id="rid",
        ip_address="1.2.3.4",
        outcome="success",
        detail=None,
    )

    structlog.contextvars.clear_contextvars()
    assert audit_service._build_audit_event(**kwargs).api_token_id is None

    structlog.contextvars.bind_contextvars(api_token_id=4242)
    try:
        assert audit_service._build_audit_event(**kwargs).api_token_id == 4242
    finally:
        structlog.contextvars.clear_contextvars()

    # And it goes back to NULL once the context is gone — the column is
    # request-scoped, not sticky process state.
    assert audit_service._build_audit_event(**kwargs).api_token_id is None
