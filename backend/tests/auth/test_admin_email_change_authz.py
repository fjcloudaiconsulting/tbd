"""F4 — nobody but a platform operator on an interactive session can repoint
an account's recovery address (TBD-362).

Spec: ``specs/2026-08-23-tbd-362-admin-email-recovery.md`` §1, fence F4.

The endpoint's whole security is its gate. Without one it is the platform's
first cross-tenant account-takeover primitive: repoint -> click at the new
inbox -> ``_promote_pending_email`` -> ``POST /auth/forgot-password`` at the
new address -> reset token issued -> attacker login. That chain was confirmed
end to end against a real stack during design.

⚠⚠ WHY THIS FILE DOES NOT USE ``make_test_app(..., current_user=...)``.

``tests/factories/app.py`` stamps ``request.state.auth_method = "jwt"``
UNCONDITIONALLY in both branches of its ``get_current_user`` override. A PAT
request therefore cannot be constructed under that override at all, and
``test_pat_cannot_call_it`` would pass green forever no matter what the route
declares. This file overrides ONLY ``get_db`` / ``get_session_factory`` and
leaves the real ``app.deps.get_current_user`` in the graph, so identity
provenance is resolved exactly as in production: a ``pat_`` bearer stamps
``"pat"``, a JWT stamps ``"jwt"``. Same construction as
``tests/auth/test_interactive_session_enumeration.py``.

The org-admin leg does not share that hazard — ``require_permission`` depends
on ``get_current_user`` (``permissions.py:114-127``), so the gate fires under
an override too — but it is built the same way here so both legs rest on one
construction.

⚠ ASSERT **403 EXACTLY**, never "not 200". A ``404`` also satisfies "not 200"
and means the router was never mounted, which is a vacuous pass. The route's
presence on the production app is pinned separately by
:func:`test_both_routes_are_mounted_on_the_real_app`.

⚠ THE ROW MUST BE BYTE-UNCHANGED AND NO AUDIT ROW MAY BE WRITTEN. A handler
that mutated first and authorized second would still return 403.
"""
from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.api_token import ApiToken
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.admin_users import router as admin_users_router
from app.security import create_access_token, hash_password
from app.services.api_token_service import hash_api_token
from tests.factories import make_test_app


UTC = timezone.utc

POST_PATH = "/api/v1/admin/users/{uid}/email-change"
DELETE_PATH = "/api/v1/admin/users/{uid}/pending-email"

BODY = {
    "new_email": "attacker@evil.example",
    "new_email_confirm": "attacker@evil.example",
    "reason": "lateral movement attempt",
}


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
def app(factory):
    """Real routers behind the REAL ``get_current_user`` — no auth override."""
    application = make_test_app(
        factory, routers=[admin_users_router], override_session_factory=True
    )
    application.state.limiter = limiter
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    return application


async def _seed_target(factory) -> tuple[int, int]:
    async with factory() as db:
        org = Organization(name="Victim Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        u = User(
            org_id=org.id,
            username="victim",
            email="victim@example.com",
            password_hash=hash_password("irrelevant"),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=False,
        )
        db.add(u)
        await db.commit()
        return u.id, org.id


async def _seed_caller(
    factory, *, role: Role, same_org_as: int | None, is_superadmin: bool = False,
    username: str = "caller",
) -> User:
    async with factory() as db:
        if same_org_as is None:
            org = Organization(name="Other Org", billing_cycle_day=1)
            db.add(org)
            await db.flush()
            org_id = org.id
        else:
            org_id = same_org_as
        u = User(
            org_id=org_id,
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password("irrelevant"),
            role=role,
            is_superadmin=is_superadmin,
            is_active=True,
            email_verified=True,
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        db.expunge(u)
        return u


async def _snapshot(factory, user_id: int) -> tuple:
    async with factory() as db:
        return (
            await db.execute(
                select(
                    User.email,
                    User.email_verified,
                    User.pending_email,
                    User.sessions_invalidated_at,
                ).where(User.id == user_id)
            )
        ).one()


async def _audit_count(factory) -> int:
    async with factory() as db:
        return len(list((await db.execute(select(AuditEvent))).scalars().all()))


_ORG_ROLE_MATRIX = [
    ("owner_same_org", Role.OWNER, True),
    ("admin_same_org", Role.ADMIN, True),
    ("member_same_org", Role.MEMBER, True),
    ("owner_other_org", Role.OWNER, False),
    ("admin_other_org", Role.ADMIN, False),
    ("member_other_org", Role.MEMBER, False),
]


@pytest.mark.parametrize(
    "role,same_org",
    [(r, s) for _i, r, s in _ORG_ROLE_MATRIX],
    ids=[i for i, _r, _s in _ORG_ROLE_MATRIX],
)
@pytest.mark.parametrize("method", ["POST", "DELETE"])
@pytest.mark.asyncio
async def test_f4_org_admin_cannot_call_it(app, factory, role, same_org, method):
    """Every org role, in and out of the target's org, all
    ``is_superadmin=False`` — 403 exactly, row untouched, no audit row.

    ``ROLE_PERMISSIONS`` is ``{}`` (``permissions.py:79``), so the
    ``is_superadmin`` short-circuit is the ONLY grant for
    ``users.reset_credentials``. This leg pins that: an org OWNER is the most
    privileged non-platform identity there is and still gets nothing.
    """
    target_id, org_id = await _seed_target(factory)
    caller = await _seed_caller(
        factory, role=role, same_org_as=org_id if same_org else None
    )
    before = await _snapshot(factory, target_id)
    jwt = create_access_token(caller.id, caller.org_id, caller.role.value)

    path = (POST_PATH if method == "POST" else DELETE_PATH).format(uid=target_id)
    with TestClient(app) as client:
        res = client.request(
            method, path, headers={"Authorization": f"Bearer {jwt}"}, json=BODY
        )

    assert res.status_code == 403, (
        f"{method} {path} returned {res.status_code} for {role.value} "
        f"(same_org={same_org}). 403 EXACTLY: a 404 would mean the route was "
        f"never mounted, which proves nothing. body={res.text}"
    )
    assert await _snapshot(factory, target_id) == before, "the row was mutated"
    assert await _audit_count(factory) == 0, "an unauthorized call wrote an audit row"


@pytest.mark.parametrize("method", ["POST", "DELETE"])
@pytest.mark.asyncio
async def test_f4_pat_cannot_call_it(app, factory, method):
    """A valid superadmin-owned WRITE PAT is refused by
    ``require_interactive_session``, matching ``merge_users`` and
    ``delete_user``.

    This is the leg that is structurally vacuous under
    ``make_test_app(current_user=...)``; see the module docstring.
    """
    target_id, org_id = await _seed_target(factory)
    owner = await _seed_caller(
        factory, role=Role.OWNER, same_org_as=None, is_superadmin=True,
        username="root",
    )
    plaintext = "pat_" + secrets.token_urlsafe(32)
    async with factory() as db:
        db.add(
            ApiToken(
                token_hash=hash_api_token(plaintext),
                token_prefix=plaintext[:14],
                name="tbd-362-authz",
                scope="write",
                created_by_user_id=owner.id,
                created_by_email=owner.email,
                expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30),
            )
        )
        await db.commit()

    before = await _snapshot(factory, target_id)
    path = (POST_PATH if method == "POST" else DELETE_PATH).format(uid=target_id)
    with TestClient(app) as client:
        res = client.request(
            method, path, headers={"Authorization": f"Bearer {plaintext}"}, json=BODY
        )

    assert res.status_code == 403, res.text
    assert res.json()["detail"] == "This action requires an interactive session", (
        "403 came from the PERMISSION gate, not the interactive-session gate — "
        "the PAT owner is a superadmin, so the permission check passes and only "
        f"require_interactive_session can refuse it. body={res.text}"
    )
    assert await _snapshot(factory, target_id) == before
    assert await _audit_count(factory) == 0


@pytest.mark.parametrize("method", ["POST", "DELETE"])
@pytest.mark.asyncio
async def test_f4_anonymous_cannot_call_it(app, factory, method):
    """No bearer at all — 403 from ``HTTPBearer``. The control that proves
    the two legs above are not passing for want of a mounted route.
    """
    target_id, _org_id = await _seed_target(factory)
    path = (POST_PATH if method == "POST" else DELETE_PATH).format(uid=target_id)
    with TestClient(app) as client:
        res = client.request(method, path, json=BODY)
    assert res.status_code in (401, 403), res.text
    assert await _audit_count(factory) == 0


@pytest.mark.parametrize("method", ["POST", "DELETE"])
@pytest.mark.asyncio
async def test_f4_superadmin_session_is_admitted(app, factory, method):
    """The positive control. Without it every assertion above is satisfied by
    a route that refuses EVERYONE, which would be a green fence over a
    permanently broken endpoint.
    """
    target_id, _org_id = await _seed_target(factory)
    root = await _seed_caller(
        factory, role=Role.OWNER, same_org_as=None, is_superadmin=True,
        username="root",
    )
    jwt = create_access_token(root.id, root.org_id, root.role.value)
    path = (POST_PATH if method == "POST" else DELETE_PATH).format(uid=target_id)
    with TestClient(app) as client:
        res = client.request(
            method, path, headers={"Authorization": f"Bearer {jwt}"}, json=BODY
        )
    assert res.status_code == 200, res.text


def test_both_routes_are_mounted_on_the_real_app():
    """The routes exist on ``app.main:app``, not merely on a test app.

    Import only — never enter the lifespan (``_run_migrations`` and the
    scheduler live there), and never mutate ``dependency_overrides``:
    ``test_public_route_allowlist.py::test_p7_real_app_is_under_inspection``
    asserts the real app carries none.
    """
    from app.main import app as real_app

    pairs = {
        (method, getattr(route, "path", None))
        for route in real_app.routes
        for method in (getattr(route, "methods", None) or ())
    }
    assert ("POST", "/api/v1/admin/users/{user_id}/email-change") in pairs
    assert ("DELETE", "/api/v1/admin/users/{user_id}/pending-email") in pairs
