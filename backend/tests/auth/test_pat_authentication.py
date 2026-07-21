"""PAT authentication path — the security-critical core (spec §5/§6/§7).

These tests exercise the real ``get_current_user`` seam: a ``pat_``-prefixed
bearer branches into ``authenticate_pat`` (``app/auth/pat.py``) while the JWT
body stays untouched. The test app mounts routes behind the REAL
``require_permission`` superadmin gate so we prove PAT identity flows through
the existing authorization machinery (ARC-R3 seam), not a bypass.

Isolation: a self-contained in-memory SQLite engine + factory is injected via
``get_db`` / ``get_session_factory`` overrides — no shared stack touched.
"""
from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.auth.pat import require_interactive_session
from app.auth.permissions import require_permission
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.api_token import ApiToken
from app.models.user import Organization, Role, User
from app.security import create_access_token
from app.services.api_token_service import hash_api_token


UTC = timezone.utc


def _now() -> datetime:
    # Naive-UTC to match how the columns are stored (spec §4 / ARC-R7).
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def superadmin(factory) -> User:
    async with factory() as s:
        org = Organization(name="Acme", billing_cycle_day=1)
        s.add(org)
        await s.commit()
        u = User(
            org_id=org.id,
            username="root",
            email="root@example.com",
            password_hash="hashed",
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            last_active_at=datetime.now(UTC),
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        # Detach a plain snapshot the tests can read attributes off of.
        s.expunge(u)
        return u


def _make_client(factory) -> TestClient:
    """Build an app whose routes sit behind the REAL superadmin gate."""
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: factory

    @app.get("/orgs", dependencies=[Depends(require_permission("orgs.view"))])
    async def list_orgs(request: Request):
        return {"auth_method": getattr(request.state, "auth_method", None)}

    @app.post("/orgs", dependencies=[Depends(require_permission("orgs.manage"))])
    async def create_org():
        return {"ok": True}

    @app.put("/orgs/{oid}", dependencies=[Depends(require_permission("orgs.manage"))])
    async def update_org(oid: int):
        return {"ok": True}

    @app.patch("/orgs/{oid}", dependencies=[Depends(require_permission("orgs.manage"))])
    async def patch_org(oid: int):
        return {"ok": True}

    @app.delete("/orgs/{oid}", dependencies=[Depends(require_permission("orgs.manage"))])
    async def delete_org(oid: int):
        return {"ok": True}

    @app.get("/interactive-only")
    async def interactive_only(user: User = Depends(require_interactive_session)):
        return {"id": user.id}

    return TestClient(app)


async def _mint_row(
    factory,
    owner: User,
    *,
    scope: str = "write",
    plaintext: str | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    token_hash: str | None = None,
) -> tuple[str, int]:
    if plaintext is None:
        plaintext = "pat_" + secrets.token_urlsafe(32)
    if expires_at is None:
        expires_at = _now() + timedelta(days=30)
    async with factory() as s:
        row = ApiToken(
            token_hash=token_hash or hash_api_token(plaintext),
            token_prefix=plaintext[:14],
            name="test-token",
            scope=scope,
            created_by_user_id=owner.id,
            created_by_email=owner.email,
            expires_at=expires_at,
            revoked_at=revoked_at,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return plaintext, row.id


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Happy path ──────────────────────────────────────────────────────────────


async def test_valid_write_pat_authenticates(factory, superadmin):
    token, _ = await _mint_row(factory, superadmin, scope="write")
    with _make_client(factory) as client:
        r = client.get("/orgs", headers=_h(token))
    assert r.status_code == 200
    assert r.json()["auth_method"] == "pat"


async def test_write_pat_allows_mutation(factory, superadmin):
    token, _ = await _mint_row(factory, superadmin, scope="write")
    with _make_client(factory) as client:
        r = client.post("/orgs", headers=_h(token))
    assert r.status_code == 200


# ── Scope, fail-closed (spec §5) ────────────────────────────────────────────


async def test_read_pat_allows_get(factory, superadmin):
    token, _ = await _mint_row(factory, superadmin, scope="read")
    with _make_client(factory) as client:
        r = client.get("/orgs", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
async def test_read_pat_blocks_mutation(factory, superadmin, method):
    token, _ = await _mint_row(factory, superadmin, scope="read")
    with _make_client(factory) as client:
        path = "/orgs" if method == "post" else "/orgs/1"
        r = getattr(client, method)(path, headers=_h(token))
    assert r.status_code == 403
    assert r.json()["detail"] == "Token scope insufficient"


# ── Dead tokens → generic 401 (spec §6, no oracle) ──────────────────────────


async def test_expired_token_gives_generic_401(factory, superadmin):
    token, _ = await _mint_row(
        factory, superadmin, expires_at=_now() - timedelta(seconds=1)
    )
    with _make_client(factory) as client:
        r = client.get("/orgs", headers=_h(token))
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid or expired token"


async def test_revoked_token_gives_generic_401(factory, superadmin):
    token, _ = await _mint_row(factory, superadmin, revoked_at=_now())
    with _make_client(factory) as client:
        r = client.get("/orgs", headers=_h(token))
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid or expired token"


async def test_deactivated_owner_gives_generic_401(factory, superadmin):
    token, _ = await _mint_row(factory, superadmin)
    async with factory() as s:
        await s.execute(
            User.__table__.update().where(User.id == superadmin.id).values(is_active=False)
        )
        await s.commit()
    with _make_client(factory) as client:
        r = client.get("/orgs", headers=_h(token))
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid or expired token"


async def test_demoted_owner_gives_generic_401(factory, superadmin):
    """Instant kill-switch: losing is_superadmin kills the token immediately."""
    token, _ = await _mint_row(factory, superadmin)
    async with factory() as s:
        await s.execute(
            User.__table__.update()
            .where(User.id == superadmin.id)
            .values(is_superadmin=False)
        )
        await s.commit()
    with _make_client(factory) as client:
        r = client.get("/orgs", headers=_h(token))
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid or expired token"


async def test_deleted_owner_gives_generic_401(factory, superadmin):
    """SET-NULL owner (or hard-deleted) → token no longer authenticates."""
    token, _ = await _mint_row(factory, superadmin)
    async with factory() as s:
        # Simulate ON DELETE SET NULL: the owner FK becomes NULL.
        await s.execute(
            ApiToken.__table__.update()
            .where(ApiToken.created_by_user_id == superadmin.id)
            .values(created_by_user_id=None)
        )
        await s.commit()
    with _make_client(factory) as client:
        r = client.get("/orgs", headers=_h(token))
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid or expired token"


async def test_unknown_token_gives_generic_401(factory, superadmin):
    with _make_client(factory) as client:
        r = client.get("/orgs", headers={"Authorization": "Bearer pat_deadbeef"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid or expired token"


# ── JWT path unchanged (regression) ─────────────────────────────────────────


async def test_jwt_path_unchanged(factory, superadmin):
    jwt = create_access_token(superadmin.id, superadmin.org_id, superadmin.role.value)
    with _make_client(factory) as client:
        r = client.get("/orgs", headers=_h(jwt))
    assert r.status_code == 200
    assert r.json()["auth_method"] == "jwt"


# ── Pepper rotation: PREV-key token still verifies (spec §3) ─────────────────


async def test_prev_key_token_still_verifies(factory, superadmin, monkeypatch):
    from app.config import settings

    key_a = "a" * 40  # new primary
    key_b = "b" * 40  # previous rotation key
    plaintext = "pat_" + secrets.token_urlsafe(32)

    # Mint the stored hash UNDER the previous key (key_b) ...
    monkeypatch.setattr(settings, "api_token_hmac_key", key_b)
    monkeypatch.setattr(settings, "api_token_hmac_key_prev", None)
    hash_under_prev = hash_api_token(plaintext)

    # ... then rotate: primary=key_a, prev=key_b. token_hash_candidates must
    # include the prev hash so this token keeps validating.
    monkeypatch.setattr(settings, "api_token_hmac_key", key_a)
    monkeypatch.setattr(settings, "api_token_hmac_key_prev", key_b)

    await _mint_row(factory, superadmin, plaintext=plaintext, token_hash=hash_under_prev)
    with _make_client(factory) as client:
        r = client.get("/orgs", headers=_h(plaintext))
    assert r.status_code == 200


# ── Interactive-session guard (spec §7) ─────────────────────────────────────


async def test_interactive_guard_blocks_pat(factory, superadmin):
    token, _ = await _mint_row(factory, superadmin, scope="write")
    with _make_client(factory) as client:
        r = client.get("/interactive-only", headers=_h(token))
    assert r.status_code == 403


async def test_interactive_guard_allows_jwt(factory, superadmin):
    jwt = create_access_token(superadmin.id, superadmin.org_id, superadmin.role.value)
    with _make_client(factory) as client:
        r = client.get("/interactive-only", headers=_h(jwt))
    assert r.status_code == 200


# ── last_used stamp lands (throttled write, spec §8) ────────────────────────


async def test_last_used_stamp_written(factory, superadmin):
    token, tid = await _mint_row(factory, superadmin)
    with _make_client(factory) as client:
        assert client.get("/orgs", headers=_h(token)).status_code == 200
    async with factory() as s:
        row = await s.scalar(select(ApiToken).where(ApiToken.id == tid))
    assert row.last_used_at is not None
    assert row.last_used_ip is not None
