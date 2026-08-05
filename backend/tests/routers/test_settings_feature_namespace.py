"""Security regression: feature.* namespace must be blocked from the generic
settings writer (PUT /api/v1/settings and DELETE /api/v1/settings/{key}).

The feature gate resolves per-org OrgSetting rows at the HIGHEST priority,
so an unconstrained PUT lets a non-superadmin OWNER bypass a globally-disabled
feature.  See admin_features.py — that is the ONLY legitimate writer of the
feature.* namespace.

Tests:
  1. PUT feature.reports → 403, no row created
  2. DELETE feature.plans → 403
  3. End-to-end bypass closed: rejected PUT leaves resolve_feature returning False
  4. Positive control: non-feature key PUT still works (no over-block)

TBD-322 — the guard must be CASE-INSENSITIVE.  ``str.startswith`` is
case-sensitive, but ``org_settings.key`` is ``String(100)`` with no explicit
collation and production MySQL runs ``utf8mb4_0900_ai_ci``, which is not.  So
``PUT {"key": "Feature.reports"}`` walked past the guard and its
``WHERE key = 'Feature.reports'`` then matched the existing lowercase
``feature.reports`` row on MySQL — updating a superadmin-owned feature override
with no audit trail.  Every backend test runs SQLite, where ``=`` on TEXT is
case-sensitive, which is why CI never saw it.

The fences below are deliberately DIALECT-INDEPENDENT: they assert the Python
guard's 403, not the database's comparison semantics, so they hold on SQLite
CI and on MySQL alike.  The column-collation pin is TBD-322 part 2.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.settings import OrgSetting
from app.models.user import Organization, Role, User
from app.routers.settings import router as settings_router
from app.security import hash_password
from app.services.feature_gate import Feature, resolve_feature


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session_factory():
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
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="TestOrg", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        owner = User(
            org_id=org.id,
            username="owner",
            email="owner@test.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
        )
        db.add(owner)
        await db.commit()
        return {"org_id": org.id, "owner_id": owner.id}


async def _get_user(factory, user_id: int) -> User:
    async with factory() as db:
        return (await db.execute(select(User).where(User.id == user_id))).scalar_one()


def _make_app(session_factory, resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await resolver(session_factory)

    def override_get_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.include_router(settings_router)
    return app


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_feature_key_returns_403(session_factory):
    """A non-superadmin OWNER PUT to feature.reports must return 403."""
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.put(
        "/api/v1/settings",
        json={"key": "feature.reports", "value": "on"},
    )
    assert resp.status_code == 403
    assert "feature." in resp.json()["detail"].lower() or "platform" in resp.json()["detail"].lower()

    # Confirm no row was persisted
    async with session_factory() as db:
        row = (
            await db.execute(
                select(OrgSetting).where(
                    OrgSetting.org_id == ids["org_id"],
                    OrgSetting.key == "feature.reports",
                )
            )
        ).scalar_one_or_none()
    assert row is None, "No OrgSetting row must be created for a rejected feature.* PUT"


@pytest.mark.asyncio
async def test_delete_feature_key_returns_403(session_factory):
    """A non-superadmin OWNER DELETE on feature.plans must return 403."""
    ids = await _seed(session_factory)

    # Pre-seed a row directly (simulating a row a superadmin wrote) so we
    # confirm it's the namespace guard—not a 404—that fires.
    async with session_factory() as db:
        db.add(OrgSetting(org_id=ids["org_id"], key="feature.plans", value="off"))
        await db.commit()

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.delete("/api/v1/settings/feature.plans")
    assert resp.status_code == 403

    # Row must still exist (delete was blocked)
    async with session_factory() as db:
        row = (
            await db.execute(
                select(OrgSetting).where(
                    OrgSetting.org_id == ids["org_id"],
                    OrgSetting.key == "feature.plans",
                )
            )
        ).scalar_one_or_none()
    assert row is not None, "Blocked DELETE must leave the superadmin row intact"


@pytest.mark.asyncio
async def test_bypass_closed_end_to_end(session_factory):
    """After a rejected PUT feature.reports, resolve_feature still returns False
    when global is absent and env-floor is False (monkeypatched).

    This is the end-to-end close of the bypass: even if the request somehow
    slipped through, there must be no OrgSetting row to elevate the feature.
    """
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    # Attempt bypass (will be blocked by the guard we're adding)
    resp = client.put(
        "/api/v1/settings",
        json={"key": "feature.reports", "value": "on"},
    )
    assert resp.status_code == 403

    # resolve_feature with env-floor=False, no global row, no org row → False
    async with session_factory() as db:
        with patch("app.services.feature_gate.app_settings") as mock_cfg:
            mock_cfg.feature_reports_v2 = False
            mock_cfg.feature_plans = False
            result = await resolve_feature(Feature.REPORTS, ids["org_id"], db)
    assert result is False, "Bypass must be closed: feature must stay off"


@pytest.mark.asyncio
async def test_put_non_feature_key_still_works(session_factory):
    """Positive control: PUT with a non-feature.* key must still succeed (no over-block)."""
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.put(
        "/api/v1/settings",
        json={"key": "session_lifetime_days", "value": "30"},
    )
    assert resp.status_code == 200
    assert resp.json()["key"] == "session_lifetime_days"
    assert resp.json()["value"] == "30"


# ── TBD-322: case-variant bypass of the reserved namespace ───────────────────

#: Every shape an attacker can spell ``feature.`` in.  The lowercase form is
#: NOT here — it is the control in ``test_put_exact_lowercase_feature_key_
#: still_returns_403`` below, which proves the fix did not simply move the
#: guard off the original string.
CASE_VARIANT_FEATURE_KEYS = [
    "Feature.reports",       # leading capital
    "FEATURE.REPORTS",       # all caps
    "FeAtUrE.reports",       # mixed case
    "feature.REPORTS",       # suffix-only capitals (prefix already lowercase)
]


@pytest.mark.parametrize("key", CASE_VARIANT_FEATURE_KEYS)
@pytest.mark.asyncio
async def test_put_case_variant_feature_key_returns_403(session_factory, key):
    """PUT with a case-variant of ``feature.`` must be blocked exactly like the
    lowercase form, and must persist NO row.

    On production MySQL (``utf8mb4_0900_ai_ci``) the upsert's
    ``WHERE key = 'Feature.reports'`` matches the existing lowercase
    ``feature.reports`` row, so slipping past the guard rewrites a
    superadmin-managed feature override in place.
    """
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.put("/api/v1/settings", json={"key": key, "value": "on"})
    assert resp.status_code == 403, (
        f"{key!r} bypassed the reserved-namespace guard "
        f"(got {resp.status_code})"
    )

    # Nothing in the reserved namespace may have been written, under ANY
    # spelling. Read every row for the org rather than querying by key: on
    # SQLite an equality filter is itself case-sensitive, so a key-scoped
    # query would be blind to exactly the row this test exists to catch.
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(OrgSetting).where(OrgSetting.org_id == ids["org_id"])
            )
        ).scalars().all()
    assert not [
        r for r in rows if r.key.casefold().startswith("feature.")
    ], f"A rejected PUT of {key!r} still persisted a reserved-namespace row"


@pytest.mark.parametrize("key", CASE_VARIANT_FEATURE_KEYS)
@pytest.mark.asyncio
async def test_delete_case_variant_feature_key_returns_403(session_factory, key):
    """DELETE with a case-variant key must 403, and leave the row intact.

    The PUT and DELETE guards were added at the same time but are two separate
    call sites; a fence on only one leaves the other free to regress.
    """
    ids = await _seed(session_factory)

    # Pre-seed the canonical lowercase row a superadmin would have written.
    async with session_factory() as db:
        db.add(OrgSetting(org_id=ids["org_id"], key="feature.reports", value="off"))
        await db.commit()

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.delete(f"/api/v1/settings/{key}")
    assert resp.status_code == 403, (
        f"DELETE {key!r} bypassed the reserved-namespace guard "
        f"(got {resp.status_code})"
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                select(OrgSetting).where(
                    OrgSetting.org_id == ids["org_id"],
                    OrgSetting.key == "feature.reports",
                )
            )
        ).scalar_one_or_none()
    assert row is not None, "Blocked DELETE must leave the superadmin row intact"


@pytest.mark.asyncio
async def test_put_exact_lowercase_feature_key_still_returns_403(session_factory):
    """Control: the ORIGINAL lowercase guard must keep firing.

    Without this, a fix that casefolds the *constant* instead of the *input*
    (or otherwise shifts the comparison) could pass the variant tests above
    while quietly opening the exact key the guard was written for.
    """
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.put(
        "/api/v1/settings", json={"key": "feature.reports", "value": "on"}
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("key", ["mysetting", "Feature_flags", "featureless.mode"])
@pytest.mark.asyncio
async def test_put_unrelated_key_still_writes(session_factory, key):
    """Control: keys outside the reserved namespace must still write with 200.

    Without this, an implementation that 403s everything — or one that
    casefolds so aggressively it swallows near-miss keys like
    ``featureless.mode`` — would pass every test above.
    """
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.put("/api/v1/settings", json={"key": key, "value": "yes"})
    assert resp.status_code == 200, f"{key!r} was over-blocked"
    assert resp.json()["key"] == key
    assert resp.json()["value"] == "yes"

    async with session_factory() as db:
        row = (
            await db.execute(
                select(OrgSetting).where(
                    OrgSetting.org_id == ids["org_id"],
                    OrgSetting.key == key,
                )
            )
        ).scalar_one_or_none()
    assert row is not None and row.value == "yes"


@pytest.mark.asyncio
async def test_delete_unrelated_key_still_deletes(session_factory):
    """Control for the DELETE site: a non-reserved key still deletes (204)."""
    ids = await _seed(session_factory)

    async with session_factory() as db:
        db.add(OrgSetting(org_id=ids["org_id"], key="mysetting", value="1"))
        await db.commit()

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner_id"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.delete("/api/v1/settings/mysetting")
    assert resp.status_code == 204

    async with session_factory() as db:
        row = (
            await db.execute(
                select(OrgSetting).where(
                    OrgSetting.org_id == ids["org_id"],
                    OrgSetting.key == "mysetting",
                )
            )
        ).scalar_one_or_none()
    assert row is None
