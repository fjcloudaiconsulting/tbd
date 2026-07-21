"""Rate-limit regression tests for the sensitive-endpoint sweep.

Pins ``@limiter.limit(...)`` decorators added in the hardening pass on:

- POST   /api/v1/users/me/password         (5/hour)
- PUT    /api/v1/users/me                  (5/hour)
- PATCH  /api/v1/orgs/{org_id}/rename      (10/hour)
- POST   /api/v1/accounts/{id}/adjust-balance (20/hour)

Each test makes ``limit`` requests that the handler accepts (so the
per-IP counter actually increments) and then asserts the next call
returns 429. Body content uses values that pass Pydantic validation
and dependency-injection auth, so the wrapper increments on each call.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.user import Role, User
from app.rate_limit import limiter
from app.routers.accounts import router as accounts_router
from app.routers.orgs import router as orgs_router
from app.routers.users import router as users_router
from app.security import hash_password


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Mirror production constraints relevant to the endpoints under
        # test so dependency-injection paths run without dialect skew.
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_organizations_name_normalized "
                "ON organizations (LOWER(name))"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_categories_org_slug_system "
                "ON categories (org_id, slug, is_system)"
            )
        )

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def reset_limiter():
    """Each test starts and ends with an empty rate-limiter state. The
    SlowAPI ``Limiter`` instance is a module-level singleton; without an
    explicit reset the per-IP counter from one test bleeds into the next
    and a perfectly good test fails with a stale 429."""
    limiter.reset()
    yield
    limiter.reset()


def _make_app(
    routers: list,
    *,
    current_user_resolver=None,
    session_factory=None,
) -> FastAPI:
    app = FastAPI()
    # SlowAPI requires both these to be wired for ``@limiter.limit`` to
    # surface 429s through the handler chain in TestClient.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    if session_factory is not None:
        async def override_session_factory():
            return session_factory

        app.dependency_overrides[get_session_factory] = override_session_factory

    if current_user_resolver is not None:
        # The test rebinds get_current_user to a fresh User from the
        # SAME session the handler will use (per-request dep cache).
        async def override_current_user(
            request: Request,
            db: AsyncSession = Depends(get_db),
        ) -> User:
            request.state.auth_method = "jwt"  # interactive-session guard (spec §7)
            return await current_user_resolver(db)

        app.dependency_overrides[get_current_user] = override_current_user

    for r in routers:
        app.include_router(r)
    return app


# ── Seed helpers ───────────────────────────────────────────────────────────


async def _seed_org_with_owner(
    factory,
    *,
    allow_manual_balance_adjustment: bool = False,
) -> dict:
    async with factory() as db:
        org = Organization(
            name="Acme",
            billing_cycle_day=1,
            allow_manual_balance_adjustment=allow_manual_balance_adjustment,
        )
        db.add(org)
        await db.flush()

        owner = User(
            org_id=org.id,
            username="owner",
            email="owner@acme.io",
            password_hash=hash_password("starting-password"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
            password_set=True,
        )
        db.add(owner)
        await db.flush()

        if allow_manual_balance_adjustment:
            at = AccountType(
                org_id=org.id, name="Checking", slug="checking", is_system=True
            )
            db.add(at)
            await db.flush()
            acct = Account(
                org_id=org.id,
                account_type_id=at.id,
                name="Primary",
                balance=Decimal("100.00"),
                currency="EUR",
                is_active=True,
            )
            db.add(acct)

            cat = Category(
                org_id=org.id,
                name="Other",
                slug="other",
                description="Misc",
                type=CategoryType.BOTH,
            )
            db.add(cat)
            await db.flush()
            account_id = acct.id
        else:
            account_id = None

        await db.commit()
        return {
            "org_id": org.id,
            "owner_id": owner.id,
            "account_id": account_id,
        }


def _resolver_for_owner(owner_id: int):
    async def resolve(db: AsyncSession) -> User:
        user = await db.get(User, owner_id)
        assert user is not None
        await db.refresh(user, ["organization"])
        return user

    return resolve


# ── /users/me/password — 5/hour ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_change_password_rate_limited(session_factory):
    """Sixth password change within the hour returns 429."""
    seed = await _seed_org_with_owner(session_factory)
    app = _make_app(
        [users_router],
        current_user_resolver=_resolver_for_owner(seed["owner_id"]),
        session_factory=session_factory,
    )

    with TestClient(app) as client:
        # Five legitimate password changes succeed (each rotates
        # current_password to the next value so the 400-on-mismatch
        # branch doesn't short-circuit before counting). 204 = no body.
        old, new = "starting-password", "new-password-001"
        for i in range(5):
            res = client.post(
                "/api/v1/users/me/password",
                json={"current_password": old, "new_password": new},
            )
            assert res.status_code == 204, res.text
            old, new = new, f"new-password-{i + 2:03d}"

        # Sixth call within the same hour for the same IP → 429.
        throttled = client.post(
            "/api/v1/users/me/password",
            json={"current_password": old, "new_password": new},
        )

    assert throttled.status_code == 429


# ── /users/me — 5/hour ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_profile_rate_limited(session_factory, monkeypatch):
    """Sixth profile update within the hour returns 429.

    Stubs ``send_verification_email`` so the handler does not depend on
    SMTP, but uses non-email-changing PUTs (first_name only) to avoid
    re-issuing verification tokens on every call.
    """
    seed = await _seed_org_with_owner(session_factory)

    # Background-task email send: stub to a no-op so the handler stays
    # purely synchronous from the test's perspective. (Not strictly
    # needed since we only mutate first_name, but defensive.)
    from app.routers import users as users_module

    async def _fake_send(email: str, token: str) -> None:
        return None

    monkeypatch.setattr(users_module, "send_verification_email", _fake_send)

    app = _make_app(
        [users_router],
        current_user_resolver=_resolver_for_owner(seed["owner_id"]),
        session_factory=session_factory,
    )

    with TestClient(app) as client:
        for i in range(5):
            res = client.put(
                "/api/v1/users/me",
                json={"first_name": f"Name{i}"},
            )
            assert res.status_code == 200, res.text

        throttled = client.put(
            "/api/v1/users/me",
            json={"first_name": "Throttled"},
        )

    assert throttled.status_code == 429


# ── /orgs/{org_id}/rename — 10/hour ────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_org_rate_limited(session_factory):
    """Eleventh rename within the hour returns 429."""
    seed = await _seed_org_with_owner(session_factory)
    app = _make_app(
        [orgs_router],
        current_user_resolver=_resolver_for_owner(seed["owner_id"]),
        session_factory=session_factory,
    )

    with TestClient(app) as client:
        for i in range(10):
            res = client.patch(
                f"/api/v1/orgs/{seed['org_id']}/rename",
                json={"name": f"Acme {i:02d}"},
            )
            assert res.status_code == 200, res.text

        throttled = client.patch(
            f"/api/v1/orgs/{seed['org_id']}/rename",
            json={"name": "Acme 99"},
        )

    assert throttled.status_code == 429


# ── /accounts/{id}/adjust-balance — 20/hour ────────────────────────────────


@pytest.mark.asyncio
async def test_adjust_balance_rate_limited(session_factory):
    """Twenty-first balance adjustment within the hour returns 429."""
    seed = await _seed_org_with_owner(
        session_factory, allow_manual_balance_adjustment=True
    )
    assert seed["account_id"] is not None

    app = _make_app(
        [accounts_router],
        current_user_resolver=_resolver_for_owner(seed["owner_id"]),
        session_factory=session_factory,
    )

    with TestClient(app) as client:
        # Each adjustment moves the balance by 1 so deltas stay non-zero
        # and 409 ConflictError ("zero delta") is not raised.
        for i in range(20):
            target = Decimal("100.00") + Decimal(i + 1)
            res = client.post(
                f"/api/v1/accounts/{seed['account_id']}/adjust-balance",
                json={"target_balance": str(target), "reason": f"adj {i}"},
            )
            assert res.status_code == 200, res.text

        target = Decimal("100.00") + Decimal(99)
        throttled = client.post(
            f"/api/v1/accounts/{seed['account_id']}/adjust-balance",
            json={"target_balance": str(target), "reason": "throttled"},
        )

    assert throttled.status_code == 429
