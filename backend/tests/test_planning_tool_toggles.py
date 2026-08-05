"""TBD-197 PR 1 — org-level Budgets toggle (the "planning tools" opt-out).

Fence ids match specs/2026-08-04-planning-tool-toggles.md §8 "PR 1 — backend".

The rule these fences exist to enforce (spec §8): **every fence names the row
it writes.** No test here says "budgets off" — each one writes an explicit
``OrgSetting(org, "orgpref.budgets", "off")`` / ``OrgSetting(org,
"feature.budgets", "on")`` / ``SystemSetting("feature.budgets", "off")`` row,
or pins the env floor, and says which.

The two storage slots are NOT interchangeable:

  ``feature.<name>``   platform/operator intent — superadmin only, both polarities
  ``orgpref.<name>``   tenant intent — org admins, **off-only**, never "on"

``resolve_feature()`` is the masked, tenant-facing answer (platform chain AND
NOT org opt-out). ``_resolve_platform_feature()`` is the raw three-level chain
and is operator-facing only. F2a is the fence that proves the mask lives inside
the resolver rather than at a call site.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_current_user_optional, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.settings import OrgSetting
from app.models.system_setting import SystemSetting
from app.models.user import Organization, Role, User
from app.routers.auth import router as auth_router
from app.routers.budgets import router as budgets_router
from app.routers.settings import router as settings_router
from app.security import hash_password
from app.services.feature_gate import (
    Feature,
    _resolve_platform_feature,
    feature_setting_key,
    org_preference_key,
    resolve_feature,
)


# ── fixtures ─────────────────────────────────────────────────────────────────


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
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(factory: async_sessionmaker[AsyncSession]) -> dict:
    """One org with an ADMIN and a MEMBER."""
    async with factory() as db:
        org = Organization(name="Toggle Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        admin = User(
            org_id=org.id,
            username="toggle-admin",
            email="admin@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        member = User(
            org_id=org.id,
            username="toggle-member",
            email="member@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add_all([admin, member])
        await db.commit()
        return {"org_id": org.id, "admin_id": admin.id, "member_id": member.id}


async def _get_user(factory, user_id: int) -> User:
    async with factory() as db:
        return (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one()


def _make_app(factory, routers, user_id: int | None) -> FastAPI:
    app = FastAPI()

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    async def override_user() -> User:
        return await _get_user(factory, user_id)

    def override_factory():
        return factory

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_session_factory] = override_factory
    if user_id is not None:
        app.dependency_overrides[get_current_user] = override_user
        app.dependency_overrides[get_current_user_optional] = override_user
    for r in routers:
        app.include_router(r)
    return app


async def _write_org_row(factory, org_id: int, key: str, value: str) -> None:
    async with factory() as db:
        db.add(OrgSetting(org_id=org_id, key=key, value=value))
        await db.commit()


async def _write_system_row(factory, key: str, value: str) -> None:
    async with factory() as db:
        db.add(SystemSetting(key=key, value=value))
        await db.commit()


async def _org_rows(factory, org_id: int) -> dict[str, str]:
    async with factory() as db:
        rows = (
            await db.execute(
                select(OrgSetting.key, OrgSetting.value).where(
                    OrgSetting.org_id == org_id
                )
            )
        ).all()
    return {k: v for k, v in rows}


# ── F1 — both new features default ON with nothing written anywhere ──────────


@pytest.mark.asyncio
async def test_f1_defaults_on_with_no_rows_anywhere(session_factory):
    """F1. No OrgSetting, no SystemSetting, env floor untouched → both True.

    Mutant killed: an env floor of ``False`` for either key
    (``config.feature_budgets`` / ``config.feature_forecast``).
    """
    ids = await _seed(session_factory)
    async with session_factory() as db:
        assert await resolve_feature(Feature.BUDGETS, ids["org_id"], db) is True
        assert await resolve_feature(Feature.FORECAST, ids["org_id"], db) is True


# ── F1b — a missing _ENV_FLOOR entry must be loud, not silently False ────────


@pytest.mark.asyncio
async def test_f1b_missing_env_floor_entry_raises(session_factory, monkeypatch):
    """F1b. ``_ENV_FLOOR[feature]()`` is ``[]`` indexing on purpose.

    A future enum member added without an ``_ENV_FLOOR`` entry must blow up
    (KeyError → 500) rather than resolve to a silent ``False`` that would
    close a surface with no diagnostic. Mutant killed: swapping the indexing
    for ``.get(feature, lambda: False)``.
    """
    ids = await _seed(session_factory)
    from app.services import feature_gate

    floor_without_budgets = {
        f: fn for f, fn in feature_gate._ENV_FLOOR.items() if f is not Feature.BUDGETS
    }
    monkeypatch.setattr(feature_gate, "_ENV_FLOOR", floor_without_budgets)

    async with session_factory() as db:
        with pytest.raises(KeyError):
            await resolve_feature(Feature.BUDGETS, ids["org_id"], db)


# ── F2a — THE fence: the mask lives inside resolve_feature ───────────────────


@pytest.mark.asyncio
async def test_f2a_orgpref_off_closes_the_budgets_router(session_factory):
    """F2a. Writes ``OrgSetting(org, "orgpref.budgets", "off")``.

    Observes ``GET /api/v1/budgets`` → **404**.

    Mutant killed: the mask applied only in ``auth.py``'s ``/auth/status``
    handler (v2's exploit B1) — nav hides, the page shows a notice, and every
    backend route stays wide open.
    """
    ids = await _seed(session_factory)
    await _write_org_row(
        session_factory, ids["org_id"], org_preference_key(Feature.BUDGETS), "off"
    )
    app = _make_app(session_factory, [budgets_router], ids["admin_id"])
    with TestClient(app) as client:
        res = client.get("/api/v1/budgets")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_f2a_control_no_orgpref_row_keeps_the_router_open(session_factory):
    """F2a control. Same request with **no** ``orgpref.budgets`` row → 200.

    Without this, a gate that 404s unconditionally would pass F2a.
    """
    ids = await _seed(session_factory)
    app = _make_app(session_factory, [budgets_router], ids["admin_id"])
    with TestClient(app) as client:
        res = client.get("/api/v1/budgets")
    assert res.status_code == 200
    assert res.json() == []


# ── F2b — the same row is visible on /auth/status ────────────────────────────


@pytest.mark.asyncio
async def test_f2b_orgpref_off_reported_by_auth_status(session_factory):
    """F2b. Writes ``OrgSetting(org, "orgpref.budgets", "off")``.

    Observes ``/auth/status`` → ``features.budgets is False``.
    Mutant killed: the mask deleted from the resolver.
    """
    ids = await _seed(session_factory)
    await _write_org_row(
        session_factory, ids["org_id"], org_preference_key(Feature.BUDGETS), "off"
    )
    app = _make_app(session_factory, [auth_router], ids["admin_id"])
    with TestClient(app) as client:
        res = client.get("/api/v1/auth/status")
    assert res.status_code == 200
    assert res.json()["features"]["budgets"] is False


# ── F2c / F2d — the operator slot still closes the same surfaces ─────────────


@pytest.mark.asyncio
async def test_f2c_global_system_setting_off_closes_the_budgets_router(session_factory):
    """F2c. Writes ``SystemSetting("feature.budgets", "off")`` (no org rows).

    Observes ``GET /api/v1/budgets`` → 404. Models the existing global-level
    contract in ``test_feature_gate.py`` for the new key.
    """
    ids = await _seed(session_factory)
    await _write_system_row(
        session_factory, feature_setting_key(Feature.BUDGETS), "off"
    )
    app = _make_app(session_factory, [budgets_router], ids["admin_id"])
    with TestClient(app) as client:
        res = client.get("/api/v1/budgets")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_f2d_global_system_setting_off_reported_by_auth_status(session_factory):
    """F2d. Writes ``SystemSetting("feature.budgets", "off")``.

    Observes ``/auth/status`` → ``features.budgets is False``.
    """
    ids = await _seed(session_factory)
    await _write_system_row(
        session_factory, feature_setting_key(Feature.BUDGETS), "off"
    )
    app = _make_app(session_factory, [auth_router], ids["admin_id"])
    with TestClient(app) as client:
        res = client.get("/api/v1/auth/status")
    assert res.json()["features"]["budgets"] is False


# ── F2e — an org "enable" must never destroy a superadmin grant ──────────────


@pytest.mark.asyncio
async def test_f2e_enable_preserves_the_superadmin_grant_row(session_factory):
    """F2e. Writes ``SystemSetting("feature.budgets","off")`` **and**
    ``OrgSetting(org, "feature.budgets", "on")`` (the superadmin grant), then
    the org admin sends ``PUT /api/v1/settings/features/budgets {enabled:true}``.

    Expects: effective **True**, and the ``feature.budgets`` grant row still
    present, byte-identical.

    Mutant killed: v2's design, where "enable" meant "delete the per-org
    feature row" — which flipped this org True → False, unrecoverably.
    """
    ids = await _seed(session_factory)
    await _write_system_row(
        session_factory, feature_setting_key(Feature.BUDGETS), "off"
    )
    await _write_org_row(
        session_factory, ids["org_id"], feature_setting_key(Feature.BUDGETS), "on"
    )

    app = _make_app(session_factory, [settings_router], ids["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/settings/features/budgets", json={"enabled": True}
        )
    assert res.status_code == 200
    assert res.json() == {"feature": "budgets", "enabled": True}

    rows = await _org_rows(session_factory, ids["org_id"])
    assert rows.get(feature_setting_key(Feature.BUDGETS)) == "on", (
        "the superadmin grant row must survive an org-level enable"
    )
    async with session_factory() as db:
        assert await resolve_feature(Feature.BUDGETS, ids["org_id"], db) is True


# ── F2f — the org opt-out masks even a superadmin grant ──────────────────────


@pytest.mark.asyncio
async def test_f2f_orgpref_off_masks_the_superadmin_grant(session_factory):
    """F2f. Writes ``OrgSetting(org, "feature.budgets", "on")`` **and**
    ``OrgSetting(org, "orgpref.budgets", "off")``.

    Expects ``resolve_feature`` → **False** while ``_resolve_platform_feature``
    still reports **True** (the two answers are deliberately different: one is
    tenant-facing, one operator-facing).

    Mutant killed: the mask deleted from the resolver.
    """
    ids = await _seed(session_factory)
    await _write_org_row(
        session_factory, ids["org_id"], feature_setting_key(Feature.BUDGETS), "on"
    )
    await _write_org_row(
        session_factory, ids["org_id"], org_preference_key(Feature.BUDGETS), "off"
    )
    async with session_factory() as db:
        assert await resolve_feature(Feature.BUDGETS, ids["org_id"], db) is False
        assert (
            await _resolve_platform_feature(Feature.BUDGETS, ids["org_id"], db)
        ) is True


# ── F3 — the endpoint writes orgpref.* and ONLY orgpref.* ────────────────────


@pytest.mark.asyncio
async def test_f3_put_writes_then_deletes_only_the_orgpref_row(session_factory):
    """F3. ``PUT {enabled:false}`` then ``PUT {enabled:true}``.

    Expects: ``orgpref.budgets`` written ("off") then deleted, and
    ``feature.budgets`` **never written by this endpoint** at any point.

    Mutant killed: any write to the ``feature.`` namespace from the tenant
    endpoint — that namespace is the operator's, and a tenant writer there is
    a privilege escalation (it can carry "on").
    """
    ids = await _seed(session_factory)
    app = _make_app(session_factory, [settings_router], ids["admin_id"])

    with TestClient(app) as client:
        off = client.put(
            "/api/v1/settings/features/budgets", json={"enabled": False}
        )
        assert off.status_code == 200
        assert off.json() == {"feature": "budgets", "enabled": False}
        rows_after_off = await _org_rows(session_factory, ids["org_id"])

        on = client.put(
            "/api/v1/settings/features/budgets", json={"enabled": True}
        )
        assert on.status_code == 200
        assert on.json() == {"feature": "budgets", "enabled": True}
        rows_after_on = await _org_rows(session_factory, ids["org_id"])

    assert rows_after_off == {org_preference_key(Feature.BUDGETS): "off"}
    assert rows_after_on == {}
    assert feature_setting_key(Feature.BUDGETS) not in rows_after_off
    assert feature_setting_key(Feature.BUDGETS) not in rows_after_on


@pytest.mark.asyncio
async def test_f3b_double_disable_is_idempotent(session_factory):
    """F3b. Two consecutive ``{enabled:false}`` calls → still exactly one row.

    ``org_settings`` carries ``uq_org_settings_org_key``; an upsert without the
    read-then-insert path (or without the IntegrityError retry copied from
    ``settings.upsert_setting``) 500s here instead of returning 200.
    """
    ids = await _seed(session_factory)
    app = _make_app(session_factory, [settings_router], ids["admin_id"])
    with TestClient(app) as client:
        first = client.put(
            "/api/v1/settings/features/budgets", json={"enabled": False}
        )
        second = client.put(
            "/api/v1/settings/features/budgets", json={"enabled": False}
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert await _org_rows(session_factory, ids["org_id"]) == {
        org_preference_key(Feature.BUDGETS): "off"
    }


# ── F4 — the allow-list is a Literal, not a str ──────────────────────────────


@pytest.mark.asyncio
async def test_f4_reports_is_not_a_planning_tool(session_factory):
    """F4. ``PUT /api/v1/settings/features/reports`` → **422**.

    The path param is a ``Literal["forecast","budgets"]``. Mutant killed:
    typing it ``str`` and looking the feature up dynamically, which would hand
    org admins an opt-out for ``reports`` / ``plans`` / ``custom_dashboard``.
    """
    ids = await _seed(session_factory)
    app = _make_app(session_factory, [settings_router], ids["admin_id"])
    with TestClient(app) as client:
        for slug in ("reports", "plans", "custom_dashboard"):
            res = client.put(
                f"/api/v1/settings/features/{slug}", json={"enabled": False}
            )
            assert res.status_code == 422, slug
    assert await _org_rows(session_factory, ids["org_id"]) == {}


# ── F4b — the generic settings writer must not reach orgpref.* either ────────


@pytest.mark.asyncio
async def test_f4b_generic_put_cannot_write_the_orgpref_namespace(session_factory):
    """F4b. ``PUT /api/v1/settings`` with key ``orgpref.budgets`` → **403**.

    Mutant killed: ``RESERVED_SETTINGS_PREFIX`` left as the bare string
    ``"feature."`` instead of a tuple including ``"orgpref."``. Without the
    tuple, the generic writer becomes a second, unaudited writer of the mask.
    """
    ids = await _seed(session_factory)
    app = _make_app(session_factory, [settings_router], ids["admin_id"])
    with TestClient(app) as client:
        put = client.put(
            "/api/v1/settings",
            json={"key": org_preference_key(Feature.BUDGETS), "value": "off"},
        )
        assert put.status_code == 403
        # ...and the 403 body must not claim this was the "feature." namespace.
        assert "orgpref." in put.json()["detail"]

        delete = client.delete(
            f"/api/v1/settings/{org_preference_key(Feature.BUDGETS)}"
        )
        assert delete.status_code == 403
        assert "orgpref." in delete.json()["detail"]

    assert await _org_rows(session_factory, ids["org_id"]) == {}


@pytest.mark.asyncio
async def test_f4b_control_ordinary_key_still_writable(session_factory):
    """F4b control. A non-reserved key still goes through the generic writer."""
    ids = await _seed(session_factory)
    app = _make_app(session_factory, [settings_router], ids["admin_id"])
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/settings", json={"key": "session_lifetime_days", "value": "30"}
        )
    assert res.status_code == 200


# ── F5 — the endpoint is admin-gated ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_f5_member_cannot_toggle(session_factory):
    """F5. A MEMBER calling the PUT → **403**, no row written.

    Mutant killed: ``_require_admin`` omitted.
    """
    ids = await _seed(session_factory)
    app = _make_app(session_factory, [settings_router], ids["member_id"])
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/settings/features/budgets", json={"enabled": False}
        )
    assert res.status_code == 403
    assert await _org_rows(session_factory, ids["org_id"]) == {}


# ── G1 — /auth/status carries all five keys ──────────────────────────────────


@pytest.mark.asyncio
async def test_g1_auth_status_returns_all_five_feature_keys(
    session_factory, monkeypatch
):
    """G1. ``/auth/status`` reports the three legacy keys **and** the two new
    ones, each correctly resolved.

    Env floors are pinned explicitly so this reads as a resolution assertion
    rather than a defaults assertion (F1 owns the defaults).
    """
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)
    monkeypatch.setattr(app_settings, "feature_plans", False)
    monkeypatch.setattr(app_settings, "feature_custom_dashboard", True)
    monkeypatch.setattr(app_settings, "feature_forecast", True)
    monkeypatch.setattr(app_settings, "feature_budgets", True)

    ids = await _seed(session_factory)
    # One org opt-out row so the payload is not uniformly True.
    await _write_org_row(
        session_factory, ids["org_id"], org_preference_key(Feature.FORECAST), "off"
    )
    app = _make_app(session_factory, [auth_router], ids["admin_id"])
    with TestClient(app) as client:
        body = client.get("/api/v1/auth/status").json()

    assert body["features"] == {
        "reports": True,
        "plans": False,
        "custom_dashboard": True,
        "forecast": False,
        "budgets": True,
    }


@pytest.mark.asyncio
async def test_g1b_unauthenticated_status_skips_the_org_mask(
    session_factory, monkeypatch
):
    """G1b. With ``org_id`` None the orgpref lookup is skipped entirely — an
    anonymous caller cannot be masked by any org's preference row."""
    monkeypatch.setattr(app_settings, "feature_forecast", True)
    monkeypatch.setattr(app_settings, "feature_budgets", True)
    ids = await _seed(session_factory)
    await _write_org_row(
        session_factory, ids["org_id"], org_preference_key(Feature.BUDGETS), "off"
    )
    app = _make_app(session_factory, [auth_router], None)
    with TestClient(app) as client:
        body = client.get("/api/v1/auth/status").json()
    assert body["features"]["budgets"] is True
    assert body["features"]["forecast"] is True


# ── G3 — both PUT branches are audited ───────────────────────────────────────


@pytest.mark.asyncio
async def test_g3_both_branches_write_one_audit_row_each(session_factory):
    """G3. ``{enabled:false}`` and ``{enabled:true}`` each write exactly one
    ``org.config.feature.set`` audit row carrying the feature and new value."""
    ids = await _seed(session_factory)
    app = _make_app(session_factory, [settings_router], ids["admin_id"])
    with TestClient(app) as client:
        client.put("/api/v1/settings/features/budgets", json={"enabled": False})
        client.put("/api/v1/settings/features/budgets", json={"enabled": True})

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "org.config.feature.set"
                )
            )
        ).scalars().all()

    assert len(rows) == 2
    assert [r.detail["new"] for r in rows] == [False, True]
    assert {r.detail["feature"] for r in rows} == {"budgets"}
    assert {r.target_org_id for r in rows} == {ids["org_id"]}
