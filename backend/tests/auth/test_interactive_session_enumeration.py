"""Enumeration guard for the interactive-session-only surface (spec §7 / §14).

Categories A (token management), B (account-takeover) and C (Tier-0
destructive) are a **deny-list**: every route in them must carry
``Depends(require_interactive_session)`` so a leaked PAT can never escalate
into permanent account ownership or irreversible platform damage (spec §2/§7).

The single source of truth for that surface is :data:`INTERACTIVE_ONLY_ROUTES`
below. A newly-added destructive/account-altering endpoint MUST be added both
to its router's ``dependencies=[...]`` AND to this constant — the parametrized
test then proves, for every entry:

* a valid **write PAT** → ``403`` (the guard fires), and
* a superadmin **JWT session** → **not** ``403`` (the guard admits interactive
  sessions; 200/302/400/404/409/422 are all fine — we are proving the guard
  does not block a session, not exercising each endpoint's happy path).

The app mounts the REAL routers behind the REAL ``get_current_user`` seam (no
``get_current_user`` override), so identity provenance
(``request.state.auth_method``) is set exactly as in production: a ``pat_``
bearer stamps ``"pat"`` and a JWT stamps ``"jwt"``. Only ``get_db`` /
``get_session_factory`` are pointed at an isolated in-memory SQLite engine.
"""
from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.api_token import ApiToken
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.security import create_access_token
from app.services.api_token_service import hash_api_token
from tests.factories import make_test_app

# Routers whose interactive-only routes we mount and exercise.
from app.routers.admin_broadcasts import router as admin_broadcasts_router
from app.routers.admin_features import router as admin_features_router
from app.routers.admin_orgs import router as admin_orgs_router
from app.routers.admin_roles import router as admin_roles_router
from app.routers.admin_users import router as admin_users_router
from app.routers.api_tokens import router as api_tokens_router
from app.routers.auth import router as auth_router
from app.routers.org_data import router as org_data_router
from app.routers.users import router as users_router


UTC = timezone.utc


# ── The carve-out inventory (spec §7 A/B/C) ─────────────────────────────────
#
# (method, path) → the interactive-only surface. Keep this list in lockstep
# with the ``Depends(require_interactive_session)`` dependencies on the real
# routes. Path params use "1"; the guard fires before any existence check, so
# the concrete id is irrelevant to the assertion.
INTERACTIVE_ONLY_ROUTES: list[tuple[str, str]] = [
    # ── A. Token management (spec §7A — token-mints-successor guard, Task 4) ──
    ("POST", "/api/v1/system/api-tokens"),           # mint_token
    ("GET", "/api/v1/system/api-tokens"),            # list_tokens
    ("DELETE", "/api/v1/system/api-tokens/1"),       # revoke_token
    ("POST", "/api/v1/system/api-tokens/revoke-all"),  # revoke_all_tokens
    # ── B. Account-takeover surface (spec §7B) ──────────────────────────────
    ("PUT", "/api/v1/users/me"),                     # update_profile (email change)
    ("POST", "/api/v1/users/me/password"),           # change_password
    ("POST", "/api/v1/auth/mfa/setup"),              # mfa_setup (writes totp secret)
    ("POST", "/api/v1/auth/mfa/enable"),             # mfa_enable
    ("POST", "/api/v1/auth/mfa/disable"),            # mfa_disable
    ("POST", "/api/v1/auth/mfa/recovery-codes"),     # mfa_regenerate_codes
    ("POST", "/api/v1/admin/users/merge"),           # merge_users (destructive)
    ("DELETE", "/api/v1/admin/users/1"),             # delete_user
    ("PATCH", "/api/v1/admin/orgs/1/members/1"),     # update_org_member (role grant)
    # ── C. Tier-0 destructive ops (spec §7C) ────────────────────────────────
    ("POST", "/api/v1/orgs/data/reset"),             # reset_org_data
    ("DELETE", "/api/v1/admin/orgs/1"),              # delete_org (org-data wipe)
    ("POST", "/api/v1/admin/roles"),                 # create_role
    ("PATCH", "/api/v1/admin/roles/1"),              # update_role
    ("DELETE", "/api/v1/admin/roles/1"),             # delete_role
    ("PUT", "/api/v1/admin/features/reports"),       # set_global_feature (/system/features)
    ("PUT", "/api/v1/admin/orgs/1/features/reports"),  # set_org_feature
    ("PUT", "/api/v1/admin/orgs/1/feature-overrides/reports"),   # set_feature_override
    ("DELETE", "/api/v1/admin/orgs/1/feature-overrides/reports"),  # revoke_feature_override
    ("POST", "/api/v1/admin/orgs/feature-overrides/sweep-expired"),  # sweep (override sweep)
    ("POST", "/api/v1/admin/broadcasts/1/send"),     # send_broadcast
    ("POST", "/api/v1/admin/broadcasts/1/resume"),   # resume_broadcast
    ("POST", "/api/v1/admin/broadcasts/1/dry-run"),  # dry_run_broadcast (sends real email)
]


def _now_naive() -> datetime:
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
        org = Organization(name="Platform", billing_cycle_day=1)
        s.add(org)
        await s.commit()
        u = User(
            org_id=org.id,
            username="root",
            email="root@example.com",
            password_hash="hashed",
            role=Role.OWNER,          # OWNER so require_org_owner (org reset) passes
            is_superadmin=True,       # short-circuits every require_permission gate
            is_active=True,
            last_active_at=datetime.now(UTC),
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        s.expunge(u)
        return u


@pytest.fixture
def app(factory):
    """Real routers behind the real ``get_current_user`` — no auth override."""
    application = make_test_app(
        factory,
        routers=[
            api_tokens_router,
            users_router,
            auth_router,
            admin_users_router,
            admin_roles_router,
            admin_orgs_router,
            admin_features_router,
            admin_broadcasts_router,
            org_data_router,
        ],
        override_session_factory=True,
    )
    # A handful of these routes carry ``@limiter.limit`` (users.py); SlowAPI
    # reads ``request.app.state.limiter``. Point it at the real limiter and
    # reset its in-memory buckets so single calls never trip a 429.
    application.state.limiter = limiter
    limiter.reset()
    try:
        yield application
    finally:
        limiter.reset()


async def _mint_write_pat(factory, owner: User) -> str:
    plaintext = "pat_" + secrets.token_urlsafe(32)
    async with factory() as s:
        s.add(
            ApiToken(
                token_hash=hash_api_token(plaintext),
                token_prefix=plaintext[:14],
                name="enum-test",
                scope="write",
                created_by_user_id=owner.id,
                created_by_email=owner.email,
                expires_at=_now_naive() + timedelta(days=30),
            )
        )
        await s.commit()
    return plaintext


def _call(client, method: str, path: str, token: str):
    # json={} keeps a content-type on the request; the interactive-session
    # dependency fires before body validation, so a body is never required.
    return client.request(
        method, path, headers={"Authorization": f"Bearer {token}"}, json={}
    )


@pytest.mark.parametrize(
    "method,path",
    INTERACTIVE_ONLY_ROUTES,
    ids=[f"{m}_{p}" for m, p in INTERACTIVE_ONLY_ROUTES],
)
async def test_write_pat_is_forbidden(app, factory, superadmin, method, path):
    """A valid write PAT must be rejected with 403 on every carve-out route."""
    from fastapi.testclient import TestClient

    pat = await _mint_write_pat(factory, superadmin)
    with TestClient(app) as client:
        r = _call(client, method, path, pat)
    assert r.status_code == 403, (
        f"{method} {path} accepted a PAT (status {r.status_code}) — "
        "missing Depends(require_interactive_session)?"
    )
    assert r.json()["detail"] == "This action requires an interactive session"


@pytest.mark.parametrize(
    "method,path",
    INTERACTIVE_ONLY_ROUTES,
    ids=[f"{m}_{p}" for m, p in INTERACTIVE_ONLY_ROUTES],
)
async def test_superadmin_session_not_forbidden(app, superadmin, method, path):
    """A superadmin JWT session must NOT be blocked by the guard. Any status
    other than 403 is acceptable — we prove the guard admits sessions, not the
    endpoint's happy path (missing bodies / ids legitimately yield 400/404/422).
    """
    from fastapi.testclient import TestClient

    jwt = create_access_token(superadmin.id, superadmin.org_id, superadmin.role.value)
    with TestClient(app) as client:
        r = _call(client, method, path, jwt)
    assert r.status_code != 403, (
        f"{method} {path} 403'd an interactive superadmin session "
        f"(body: {r.text[:200]})"
    )
