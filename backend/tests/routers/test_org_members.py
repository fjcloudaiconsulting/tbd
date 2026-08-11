"""Router-level tests for L3.8 — `/api/v1/orgs/...` invitation +
member endpoints. Service-layer behavior is pinned in
`tests/services/test_invitation_service.py`; this file pins the auth
gate, body validation, status codes, and serialized response shape.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.org_members import router as org_members_router
from app.security import create_invitation_token, hash_password
from app.services import invitation_service


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


def make_app(session_factory, current_user_factory):
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user(
        request: Request, db: AsyncSession = Depends(get_db)
    ) -> User:
        request.state.auth_method = "jwt"  # interactive-session guard (spec §7)
        # ⚠ TBD-364 (F-4b). The actor MUST be resolved on the REQUEST session,
        # not a private one. Production `get_current_user` loads it from the
        # request db (deps.py:52-55), so a later `db.rollback()` expires it and
        # any subsequent `current_user.email` read lazy-loads → MissingGreenlet
        # → 500 with zero audit rows.
        #
        # The previous harness returned a User loaded in its OWN session. That
        # instance is absent from the request session's identity map, so
        # rollback() provably could not expire it — and the
        # "read the actor after the rollback" mutant stayed GREEN across the
        # whole suite while being a 500 in production. Measured.
        #
        # FastAPI caches `get_db` per request, so this is the same session the
        # handler holds.
        detached = await current_user_factory(session_factory)
        return (
            await db.execute(select(User).where(User.id == detached.id))
        ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    # F-4a. Required so audit rows land in the test DB. WITHOUT this the real
    # engine is used, the insert violates the audit_events FK (the actor does
    # not exist there), record_audit_event swallows and returns None, and the
    # row-count assertions fail LOUDLY with 0 rows. It is a harness
    # precondition, not a vacuity risk — an earlier draft of the spec claimed
    # the reverse and a build round measured it false.
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.include_router(org_members_router)
    return app


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        owner = User(
            org_id=org.id, username="owner", email="owner@acme.io",
            password_hash=hash_password("pw-12345"),
            role=Role.OWNER, is_active=True, email_verified=True,
        )
        db.add(owner)
        await db.commit()
        return {"org_id": org.id, "owner_id": owner.id}


def _user_factory(role: Role, is_active: bool = True):
    async def factory(session_factory):
        async with session_factory() as db:
            from sqlalchemy import select
            user = (
                await db.execute(select(User).where(User.role == role).limit(1))
            ).scalar_one_or_none()
            if user is None:
                raise RuntimeError(f"No {role} seeded")
            return user
    return factory


# ── POST /invitations ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_invitations_owner_creates(session_factory):
    await _seed(session_factory)

    sent = []
    import app.routers.org_members as m
    async def fake_send(*args, **kwargs):
        sent.append((args, kwargs))
    # noqa — module-level binding patched per test
    m.send_invitation_email = fake_send

    app = make_app(session_factory, _user_factory(Role.OWNER))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/invitations",
            json={"email": "newbie@acme.io", "role": "member"},
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["email"] == "newbie@acme.io"
    assert body["role"] == "member"
    assert body["status"] == "pending"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_post_invitations_validates_role_via_pydantic(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _user_factory(Role.OWNER))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/invitations",
            json={"email": "x@acme.io", "role": "owner"},  # not allowed
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_post_invitations_member_role_403(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        m = User(
            org_id=seed["org_id"], username="reg", email="reg@acme.io",
            password_hash=hash_password("pw-12345"),
            role=Role.MEMBER, is_active=True, email_verified=True,
        )
        db.add(m)
        await db.commit()
    app = make_app(session_factory, _user_factory(Role.MEMBER))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/invitations",
            json={"email": "y@acme.io", "role": "member"},
        )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_post_invitations_duplicate_returns_409(session_factory):
    await _seed(session_factory)

    import app.routers.org_members as m
    async def fake_send(*args, **kwargs):
        return None
    m.send_invitation_email = fake_send

    app = make_app(session_factory, _user_factory(Role.OWNER))
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/orgs/invitations",
            json={"email": "dup@acme.io", "role": "member"},
        )
        assert first.status_code == 201
        dup = client.post(
            "/api/v1/orgs/invitations",
            json={"email": "dup@acme.io", "role": "member"},
        )
    assert dup.status_code == 409


# ── GET /invitations ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_invitations_lists_pending(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        await invitation_service.create_invitation(
            db, org_id=seed["org_id"], created_by=seed["owner_id"],
            email="p@acme.io", role=Role.MEMBER,
        )
        await db.commit()
    app = make_app(session_factory, _user_factory(Role.OWNER))
    with TestClient(app) as client:
        res = client.get("/api/v1/orgs/invitations")
    assert res.status_code == 200
    assert [i["email"] for i in res.json()["items"]] == ["p@acme.io"]


# ── DELETE /invitations/{id} ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_invitation_revokes(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=seed["org_id"], created_by=seed["owner_id"],
            email="rev@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        inv_id = inv.id
    app = make_app(session_factory, _user_factory(Role.OWNER))
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/orgs/invitations/{inv_id}")
    assert res.status_code == 204


# ── GET /invitations/preview ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_returns_metadata_for_pending(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=seed["org_id"], created_by=seed["owner_id"],
            email="pv@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
    # Public endpoint — no current_user
    app = make_app(session_factory, _user_factory(Role.OWNER))
    app.dependency_overrides.pop(get_current_user, None)
    with TestClient(app) as client:
        res = client.get(f"/api/v1/orgs/invitations/preview?token={token}")
    assert res.status_code == 200
    body = res.json()
    assert body["org_name"] == "Acme"
    assert body["email"] == "pv@acme.io"
    assert body["is_reactivation"] is False


@pytest.mark.asyncio
async def test_preview_returns_410_for_invalid_token(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _user_factory(Role.OWNER))
    app.dependency_overrides.pop(get_current_user, None)
    with TestClient(app) as client:
        res = client.get("/api/v1/orgs/invitations/preview?token=not-a-jwt")
    assert res.status_code == 410
    assert res.json()["detail"]["code"] == "invitation_unavailable"


# ── POST /invitations/accept ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_creates_user_and_returns_token(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=seed["org_id"], created_by=seed["owner_id"],
            email="acc@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
    app = make_app(session_factory, _user_factory(Role.OWNER))
    app.dependency_overrides.pop(get_current_user, None)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/invitations/accept",
            json={"token": token, "username": "acceptor", "password": "strong-pw-1234"},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "access_token" in body


@pytest.mark.asyncio
async def test_invite_accept_sets_root_path_refresh_cookie(session_factory):
    """Pins Finding 2 from PR #211 review: invite-accept must use Path=/
    on the refresh_token cookie so the browser sends it on regular page
    requests (needed for Next.js RSC to read via /auth/verify). Previously
    used the legacy Path=/api/v1/auth/refresh.
    """
    seed = await _seed(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=seed["org_id"], created_by=seed["owner_id"],
            email="cookieaccept@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
    app = make_app(session_factory, _user_factory(Role.OWNER))
    app.dependency_overrides.pop(get_current_user, None)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/invitations/accept",
            json={"token": token, "username": "cookieuser", "password": "strong-pw-1234"},
        )
    assert res.status_code == 200, res.text

    # Locate the refresh_token Set-Cookie value and assert Path=/
    raw = None
    for raw_value in (
        res.headers.get_list("set-cookie")
        if hasattr(res.headers, "get_list")
        else res.headers.raw
    ):
        if isinstance(raw_value, tuple):
            key, value = raw_value
            if key.decode().lower() != "set-cookie":
                continue
            value = value.decode()
        else:
            value = raw_value
        if value.split("=", 1)[0].strip().lower() == "refresh_token":
            raw = value
            break
    assert raw is not None, (
        f"Invite-accept must emit a refresh_token Set-Cookie. Got: {dict(res.headers)}"
    )
    assert "Path=/" in raw
    assert "Path=/api/v1/auth/refresh" not in raw


@pytest.mark.asyncio
async def test_accept_410_for_revoked(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        inv = await invitation_service.create_invitation(
            db, org_id=seed["org_id"], created_by=seed["owner_id"],
            email="revv@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
        await invitation_service.revoke_invitation(
            db, org_id=seed["org_id"], invitation_id=inv.id,
        )
        await db.commit()
    app = make_app(session_factory, _user_factory(Role.OWNER))
    app.dependency_overrides.pop(get_current_user, None)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/invitations/accept",
            json={"token": token, "username": "validname", "password": "strong-pw-1234"},
        )
    assert res.status_code == 410


@pytest.mark.asyncio
async def test_accept_409_for_username_collision(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        async_db = db
        # owner is already 'owner'; try to accept as 'owner'
        inv = await invitation_service.create_invitation(
            db, org_id=seed["org_id"], created_by=seed["owner_id"],
            email="dupun@acme.io", role=Role.MEMBER,
        )
        await db.commit()
        token = create_invitation_token(inv.id, inv.email)
    app = make_app(session_factory, _user_factory(Role.OWNER))
    app.dependency_overrides.pop(get_current_user, None)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/orgs/invitations/accept",
            json={"token": token, "username": "owner", "password": "strong-pw-1234"},
        )
    assert res.status_code == 409


# ── GET /members ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_members_visible_to_member(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        m = User(
            org_id=seed["org_id"], username="reg", email="reg@acme.io",
            password_hash=hash_password("pw-12345"),
            role=Role.MEMBER, is_active=True, email_verified=True,
        )
        db.add(m)
        await db.commit()
    app = make_app(session_factory, _user_factory(Role.MEMBER))
    with TestClient(app) as client:
        res = client.get("/api/v1/orgs/members")
    assert res.status_code == 200
    usernames = sorted(u["username"] for u in res.json()["items"])
    assert usernames == ["owner", "reg"]


# ── DELETE /members/{user_id} ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_member_owner_removes_member(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        m = User(
            org_id=seed["org_id"], username="vic", email="vic@acme.io",
            password_hash=hash_password("pw-12345"),
            role=Role.MEMBER, is_active=True, email_verified=True,
        )
        db.add(m)
        await db.commit()
        m_id = m.id
    app = make_app(session_factory, _user_factory(Role.OWNER))
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/orgs/members/{m_id}")
    assert res.status_code == 204


# ── ListEnvelope: sort + pagination contract (Tables PR2) ──────────────────


@pytest.mark.asyncio
async def test_members_list_envelope_sort_and_page(session_factory):
    """Members endpoint returns {items,total,limit,offset}, sorts by a
    whitelisted column, and pages."""
    seed = await _seed(session_factory)
    async with session_factory() as db:
        for name in ("zeb", "amy", "bob"):
            db.add(
                User(
                    org_id=seed["org_id"], username=name, email=f"{name}@acme.io",
                    password_hash=hash_password("pw-12345"),
                    role=Role.MEMBER, is_active=True, email_verified=True,
                )
            )
        await db.commit()
    app = make_app(session_factory, _user_factory(Role.OWNER))
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/orgs/members?sort_by=username&sort_dir=asc&limit=2&offset=0"
        )
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 4  # owner + amy + bob + zeb
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert [m["username"] for m in body["items"]] == ["amy", "bob"]


@pytest.mark.asyncio
async def test_members_list_unknown_sort_is_400(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _user_factory(Role.OWNER))
    with TestClient(app) as client:
        res = client.get("/api/v1/orgs/members?sort_by=password_hash")
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_invitations_list_envelope_sort_and_total(session_factory):
    seed = await _seed(session_factory)
    async with session_factory() as db:
        for email in ("c@acme.io", "a@acme.io", "b@acme.io"):
            await invitation_service.create_invitation(
                db, org_id=seed["org_id"], created_by=seed["owner_id"],
                email=email, role=Role.MEMBER,
            )
        await db.commit()
    app = make_app(session_factory, _user_factory(Role.OWNER))
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/orgs/invitations?sort_by=email&sort_dir=asc"
        )
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    assert [i["email"] for i in body["items"]] == ["a@acme.io", "b@acme.io", "c@acme.io"]


@pytest.mark.asyncio
async def test_invitations_list_unknown_sort_is_400(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _user_factory(Role.OWNER))
    with TestClient(app) as client:
        res = client.get("/api/v1/orgs/invitations?sort_by=org_id")
    assert res.status_code == 400


# ── TBD-364: org.member.remove.failed audit row ────────────────────────────
#
# Spec: specs/2026-08-11-tbd-364-remove-member-superadmin-guard.md


async def _audit_rows(factory, event_type="org.member.remove.failed"):
    """Rows for ONE event type. Never a bare table count — an unrelated
    call site writing its own row would silently break a global count."""
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(AuditEvent).where(AuditEvent.event_type == event_type)
                )
            )
            .scalars()
            .all()
        )


async def _add_member(factory, seed, **kw):
    async with factory() as db:
        u = User(
            org_id=seed["org_id"],
            password_hash=hash_password("pw-12345"),
            is_active=kw.pop("is_active", True),
            email_verified=True,
            **kw,
        )
        db.add(u)
        await db.commit()
        return u.id


@pytest.mark.asyncio
async def test_delete_member_superadmin_409_and_audit_row(session_factory):
    """F-4. The refusal is durable even though the business txn is abandoned.

    Kills: a service-only fix with no router wiring (0 rows);
    add_audit_event_to_session on this path (row discarded with the rollback);
    reading target.role off a select(User) ENTITY after the rollback (500).
    """
    seed = await _seed(session_factory)
    admin_id = await _add_member(
        session_factory, seed, username="orgadmin",
        email="orgadmin@acme.io", role=Role.ADMIN,
    )
    target_id = await _add_member(
        session_factory, seed, username="platsa",
        email="platsa@acme.io", role=Role.MEMBER, is_superadmin=True,
    )
    app = make_app(session_factory, _user_factory(Role.ADMIN))
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/orgs/members/{target_id}")

    assert res.status_code == 409
    # The user-visible half. `detail` must stay a plain STRING carrying the
    # explanatory sentence: frontend `extractErrorMessage` reads it straight
    # through (api.ts) and MembersSection renders it. Shipping `detail=e.code`
    # or a generic message would otherwise be green.
    detail = res.json()["detail"]
    assert isinstance(detail, str), f"detail must be a plain string, got {type(detail)}"
    assert "platform role" in detail

    rows = await _audit_rows(session_factory)
    assert len(rows) == 1, f"expected exactly 1 audit row, got {len(rows)}"
    row = rows[0]
    assert row.outcome == AuditOutcome.FAILURE
    assert row.actor_user_id == admin_id
    assert row.actor_email == "orgadmin@acme.io"
    assert row.target_org_id == seed["org_id"]
    assert row.target_org_name == "Acme"
    assert row.detail["reason"] == invitation_service.CODE_TARGET_IS_SUPERADMIN
    assert row.detail["target_user_id"] == target_id
    assert row.detail["target_role"] == "member"
    assert row.detail["target_is_active"] is True

    # The business txn was abandoned: the target must be untouched.
    async with session_factory() as db:
        target = (
            await db.execute(select(User).where(User.id == target_id))
        ).scalar_one()
        assert target.is_active is True
        assert target.sessions_invalidated_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["self_removal", "owner_removal_requires_owner", "target_is_platform_superadmin"],
)
async def test_every_router_reachable_refusal_writes_exactly_one_row(
    session_factory, scenario
):
    """F-5. EVERY refusal reachable through HTTP is audited, not just the
    superadmin one.

    Kills: `if e.code == CODE_TARGET_IS_SUPERADMIN` in the router. With that
    mutant, absence of a row becomes uninterpretable — an operator cannot
    distinguish "nobody tried" from "someone tried and hit another guard".

    ⚠ Only THREE of the four refusal branches are constructible here.
    `last_active_owner` needs an actor who is an active OWNER of the org, who
    is therefore counted alongside the target, so active_owners >= 2 always;
    and get_current_user rejects inactive users. It is fenced at the service
    layer instead (see test_remove_member_blocks_removing_last_owner).
    """
    seed = await _seed(session_factory)
    if scenario == "self_removal":
        actor_role, target_id = Role.OWNER, seed["owner_id"]
    elif scenario == "owner_removal_requires_owner":
        await _add_member(
            session_factory, seed, username="orgadmin",
            email="orgadmin@acme.io", role=Role.ADMIN,
        )
        actor_role, target_id = Role.ADMIN, seed["owner_id"]
    else:
        await _add_member(
            session_factory, seed, username="orgadmin",
            email="orgadmin@acme.io", role=Role.ADMIN,
        )
        target_id = await _add_member(
            session_factory, seed, username="platsa",
            email="platsa@acme.io", role=Role.MEMBER, is_superadmin=True,
        )
        actor_role = Role.ADMIN

    app = make_app(session_factory, _user_factory(actor_role))
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/orgs/members/{target_id}")

    assert res.status_code == 409
    rows = await _audit_rows(session_factory)
    assert len(rows) == 1, f"{scenario}: expected 1 row, got {len(rows)}"
    # Exact code equality — NEVER a message regex. Two distinct refusal
    # messages here both contain the word "owner", which is how the
    # pre-existing last-owner service test stayed vacuous for months.
    assert rows[0].detail["reason"] == scenario


@pytest.mark.asyncio
async def test_delete_ordinary_member_still_works_and_writes_no_failure_row(
    session_factory,
):
    """F-6. Control. Kills a blunt guard (`is not False`, a truthiness slip on
    a NULL column, a guard on the wrong subject) and any audit write leaking
    onto the success path under the failure event type."""
    seed = await _seed(session_factory)
    await _add_member(
        session_factory, seed, username="orgadmin",
        email="orgadmin@acme.io", role=Role.ADMIN,
    )
    target_id = await _add_member(
        session_factory, seed, username="vic",
        email="vic@acme.io", role=Role.MEMBER,
    )
    app = make_app(session_factory, _user_factory(Role.ADMIN))
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/orgs/members/{target_id}")

    assert res.status_code == 204
    async with session_factory() as db:
        target = (
            await db.execute(select(User).where(User.id == target_id))
        ).scalar_one()
        assert target.is_active is False
        assert target.sessions_invalidated_at is not None
    assert await _audit_rows(session_factory) == []


@pytest.mark.asyncio
async def test_refusal_audit_ip_comes_from_the_single_client_ip_helper(
    session_factory, monkeypatch
):
    """F-7. Kills `request.client.host`, which would record "testclient".

    tests/test_no_raw_request_client.py forbids the raw read at AST level, but
    its own docstring says it cannot catch a caller passing the WRONG value.
    This closes that gap for this call site.
    """
    monkeypatch.setenv("PFV_RUNTIME", "app_platform")
    seed = await _seed(session_factory)
    await _add_member(
        session_factory, seed, username="orgadmin",
        email="orgadmin@acme.io", role=Role.ADMIN,
    )
    target_id = await _add_member(
        session_factory, seed, username="platsa",
        email="platsa@acme.io", role=Role.MEMBER, is_superadmin=True,
    )
    app = make_app(session_factory, _user_factory(Role.ADMIN))
    with TestClient(app) as client:
        res = client.delete(
            f"/api/v1/orgs/members/{target_id}",
            headers={"do-connecting-ip": "203.0.113.77"},
        )

    assert res.status_code == 409
    rows = await _audit_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].ip_address == "203.0.113.77"
