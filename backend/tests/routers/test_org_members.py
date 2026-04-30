"""Router-level tests for L3.8 — `/api/v1/orgs/...` invitation +
member endpoints. Service-layer behavior is pinned in
`tests/services/test_invitation_service.py`; this file pins the auth
gate, body validation, status codes, and serialized response shape.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Base
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

    async def override_current_user() -> User:
        return await current_user_factory(session_factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
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
    assert [i["email"] for i in res.json()] == ["p@acme.io"]


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
    usernames = sorted(u["username"] for u in res.json())
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
