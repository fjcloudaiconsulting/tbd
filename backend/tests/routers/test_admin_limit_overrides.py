"""TBD-585: admin limit-override PUT/DELETE, sweep over both tables, feature-state limits."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import StaticPool

from app._time import utcnow_naive
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.feature_override import OrgFeatureOverride
from app.models.limit_override import OrgLimitOverride
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.admin_orgs import router as admin_orgs_router
from app.schemas.feature_override import FeatureOverrideUpsert
from app.schemas.limit_override import LimitOverrideUpsert
from app.security import hash_password
from tests.factories import make_test_app

BIG = 2**53 - 1


@pytest.fixture(autouse=True)
def _fresh_limiter():
    # The 60/hour shared bucket lives in the limits DB and outlasts a test.
    limiter.reset()
    yield
    limiter.reset()


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
        admin_org = Organization(name="Admin Org", billing_cycle_day=1)
        target = Organization(name="Target Inc", billing_cycle_day=1)
        db.add_all([admin_org, target])
        await db.commit()
        sa = User(org_id=admin_org.id, username="root", email="root@platform.io",
                  password_hash=hash_password("pw-1234567"), role=Role.OWNER,
                  is_superadmin=True, is_active=True, email_verified=True)
        plain = User(org_id=target.id, username="t_owner", email="t_owner@target.io",
                     password_hash=hash_password("pw-1234567"), role=Role.OWNER,
                     is_superadmin=False, is_active=True, email_verified=True)
        db.add_all([sa, plain])
        await db.commit()
        return {"admin_user_id": sa.id, "target_id": target.id}


def _resolver(superadmin: bool):
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.is_superadmin.is_(superadmin)))
            ).scalar_one()
    return resolve


def _app(factory, superadmin=True):
    return make_test_app(
        factory, routers=admin_orgs_router, current_user=_resolver(superadmin),
        override_session_factory=True,
    )


def _url(org_id, meter):
    return f"/api/v1/admin/orgs/{org_id}/limit-overrides/{meter}"


async def _audit(factory, event_type):
    async with factory() as db:
        return (await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == event_type)
        )).scalars().all()


# ── PUT ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_sets_then_replaces(session_factory):
    seed = await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        r = c.put(_url(seed["target_id"], "mcp.calls"),
                  json={"period": "day", "limit_value": 5, "note": "trial"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["meter"] == "mcp.calls" and body["period"] == "day"
        assert body["limit_value"] == 5 and body["set_by_email"] == "root@platform.io"
        assert body["is_expired"] is False
        r = c.put(_url(seed["target_id"], "mcp.calls"),
                  json={"period": "month", "limit_value": None})
        assert r.status_code == 200 and r.json()["limit_value"] is None
    async with session_factory() as db:
        rows = (await db.execute(select(OrgLimitOverride))).scalars().all()
    assert len(rows) == 1 and rows[0].period == "month" and rows[0].limit_value is None


@pytest.mark.asyncio
async def test_put_unknown_meter_is_400_before_any_db_work(session_factory):
    seed = await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        # Org 999999 does not exist: a 400 (not 404) proves the meter is checked first.
        r = c.put(_url(999999, "nope.meter"), json={"period": "day", "limit_value": 1})
    assert r.status_code == 400
    assert seed  # seeded


@pytest.mark.asyncio
async def test_put_null_on_platform_meter_is_400(session_factory):
    seed = await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        r = c.put(_url(seed["target_id"], "platform_ai.cents"),
                  json={"period": "month", "limit_value": None})
    assert r.status_code == 400
    async with session_factory() as db:
        assert (await db.execute(select(OrgLimitOverride))).first() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"period": "day"},                                   # limit_value omitted != unlimited
    {"limit_value": 3},                                  # period omitted
    {"period": "day", "limit_value": BIG + 1},
    {"period": "day", "limit_value": -1},
    {"period": "day", "limit_value": True},              # StrictInt
    {"period": "day", "limit_value": "5"},
    {"period": "week", "limit_value": 5},
    {"period": "day", "limit_value": 5, "extra": 1},
])
async def test_put_body_validation_is_422(session_factory, body):
    seed = await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        r = c.put(_url(seed["target_id"], "mcp.calls"), json=body)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_accepts_max_limit(session_factory):
    seed = await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        r = c.put(_url(seed["target_id"], "mcp.calls"), json={"period": "day", "limit_value": BIG})
    assert r.status_code == 200 and r.json()["limit_value"] == BIG


@pytest.mark.asyncio
async def test_put_unknown_org_is_404(session_factory):
    await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        r = c.put(_url(999999, "mcp.calls"), json={"period": "day", "limit_value": 1})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_put_writes_audit_row(session_factory):
    seed = await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        c.put(_url(seed["target_id"], "mcp.calls"),
              json={"period": "day", "limit_value": 5, "note": "secret note"})
        c.put(_url(seed["target_id"], "mcp.calls"), json={"period": "month", "limit_value": 9})
    rows = await _audit(session_factory, "admin.limit_override.set")
    assert len(rows) == 2
    d = sorted(rows, key=lambda r: r.id)[1].detail
    assert d["meter"] == "mcp.calls" and d["old_limit_value"] == 5 and d["new_limit_value"] == 9
    assert d["old_period"] == "day" and d["new_period"] == "month"
    assert d["note_present"] is False
    assert "secret note" not in str(sorted(rows, key=lambda r: r.id)[0].detail)
    assert rows[0].target_org_id == seed["target_id"]


@pytest.mark.asyncio
async def test_put_aware_expires_at_is_stored_naive_utc(session_factory):
    seed = await _seed(session_factory)
    future = (utcnow_naive() + timedelta(days=3)).replace(tzinfo=timezone.utc)
    plus2 = future.astimezone(timezone(timedelta(hours=2)))
    with TestClient(_app(session_factory)) as c:
        r = c.put(_url(seed["target_id"], "mcp.calls"),
                  json={"period": "day", "limit_value": 1, "expires_at": plus2.isoformat()})
        assert r.status_code == 200, r.text
        r2 = c.put(f"/api/v1/admin/orgs/{seed['target_id']}/feature-overrides/plans",
                   json={"value": True, "expires_at": plus2.isoformat()})
        assert r2.status_code == 200, r2.text
    async with session_factory() as db:
        lim = (await db.execute(select(OrgLimitOverride))).scalar_one()
        feat = (await db.execute(select(OrgFeatureOverride))).scalar_one()
    expected = future.replace(tzinfo=None)
    assert lim.expires_at == expected and lim.expires_at.tzinfo is None
    assert feat.expires_at == expected and feat.expires_at.tzinfo is None


def test_upsert_schemas_normalise_aware_expires_at():
    aware = datetime(2030, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
    a = LimitOverrideUpsert(period="day", limit_value=1, expires_at=aware)
    b = FeatureOverrideUpsert(value=True, expires_at=aware)
    assert a.expires_at == b.expires_at == datetime(2030, 1, 1, 17, 0)
    naive = datetime(2030, 1, 1, 12, 0)
    assert LimitOverrideUpsert(period="day", limit_value=1, expires_at=naive).expires_at == naive
    with pytest.raises(PydanticValidationError):
        LimitOverrideUpsert(period="day")


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("limit-overrides/mcp.calls", {"period": "day", "limit_value": 1}),
    ("feature-overrides/plans", {"value": True}),
])
@pytest.mark.parametrize("exc", [StaleDataError("gone"), IntegrityError("s", {}, Exception("x"))])
async def test_put_concurrent_change_is_409(session_factory, path, body, exc):
    seed = await _seed(session_factory)

    async def boom(self):
        raise exc

    with TestClient(_app(session_factory)) as c:
        with patch.object(AsyncSession, "commit", boom):
            r = c.put(f"/api/v1/admin/orgs/{seed['target_id']}/{path}", json=body)
    assert r.status_code == 409


# ── DELETE ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_row_and_audits(session_factory):
    seed = await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        exp = (utcnow_naive() + timedelta(days=3)).replace(microsecond=0)
        c.put(_url(seed["target_id"], "mcp.calls"),
              json={"period": "day", "limit_value": 5, "expires_at": exp.isoformat()})
        assert c.delete(_url(seed["target_id"], "mcp.calls")).status_code == 204
        assert c.delete(_url(seed["target_id"], "mcp.calls")).status_code == 404
        assert c.delete(_url(seed["target_id"], "nope")).status_code == 400
        assert c.delete(_url(999999, "mcp.calls")).status_code == 404
    async with session_factory() as db:
        assert (await db.execute(select(OrgLimitOverride))).first() is None
    rows = await _audit(session_factory, "admin.limit_override.deleted")
    assert len(rows) == 1 and rows[0].detail["old_limit_value"] == 5
    assert rows[0].detail["old_expires_at"] == exp.isoformat()


# ── platform-guard ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_platform_meters_are_superadmin_only_even_with_orgs_manage(session_factory, monkeypatch):
    """Widening orgs.manage (L4.8) must not silently grant platform spend."""
    seed = await _seed(session_factory)
    monkeypatch.setattr("app.auth.permissions.has_permission", lambda u, p: True)
    with TestClient(_app(session_factory, superadmin=False)) as c:
        body = {"period": "month", "limit_value": 100}
        assert c.put(_url(seed["target_id"], "platform_ai.cents"), json=body).status_code == 403
        assert c.delete(_url(seed["target_id"], "platform_ai.cents")).status_code == 403
        assert c.put(_url(seed["target_id"], "mcp.calls"), json=body).status_code == 200
        assert c.delete(_url(seed["target_id"], "mcp.calls")).status_code == 204
    async with session_factory() as db:
        assert (await db.execute(select(OrgLimitOverride))).first() is None


@pytest.mark.asyncio
async def test_superadmin_may_set_platform_meter(session_factory):
    seed = await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        r = c.put(_url(seed["target_id"], "platform_ai.cents"),
                  json={"period": "month", "limit_value": 100})
    assert r.status_code == 200


# ── sweep ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_covers_both_tables(session_factory):
    seed = await _seed(session_factory)
    past, future = utcnow_naive() - timedelta(days=1), utcnow_naive() + timedelta(days=1)
    async with session_factory() as db:
        db.add_all([
            OrgFeatureOverride(org_id=seed["target_id"], feature_key="ai.budget", value=True,
                               set_by=seed["admin_user_id"], expires_at=past),
            OrgLimitOverride(org_id=seed["target_id"], meter="mcp.calls", period="day",
                             limit_value=1, set_by=seed["admin_user_id"], expires_at=past),
            OrgLimitOverride(org_id=seed["target_id"], meter="assistant.turns", period="day",
                             limit_value=1, set_by=seed["admin_user_id"], expires_at=past),
            OrgLimitOverride(org_id=seed["target_id"], meter="platform_ai.cents", period="day",
                             limit_value=1, set_by=seed["admin_user_id"], expires_at=future),
        ])
        await db.commit()
    with TestClient(_app(session_factory)) as c:
        r = c.post("/api/v1/admin/orgs/feature-overrides/sweep-expired")
    assert r.status_code == 200
    assert r.json() == {"deleted_count": 3, "feature_overrides_deleted": 1,
                        "limit_overrides_deleted": 2}
    async with session_factory() as db:
        left = (await db.execute(select(OrgLimitOverride.meter))).scalars().all()
    assert left == ["platform_ai.cents"]
    feat = await _audit(session_factory, "admin.feature_override.expired_swept")
    lim = await _audit(session_factory, "admin.limit_override.expired_swept")
    assert len(feat) == 1 and feat[0].detail["deleted_count"] == 1
    assert len(lim) == 1 and lim[0].detail["deleted_count"] == 2
    assert lim[0].detail["counts_by_meter"] == {"mcp.calls": 1, "assistant.turns": 1}


@pytest.mark.asyncio
async def test_sweep_limit_boundary_is_inclusive(session_factory):
    """expires_at == now is expired (matches the resolver's `expires_at > now` liveness)."""
    seed = await _seed(session_factory)
    with patch("app.routers.admin_orgs.utcnow_naive", return_value=datetime(2030, 1, 1)):
        async with session_factory() as db:
            db.add_all([
                OrgLimitOverride(org_id=seed["target_id"], meter="mcp.calls", period="day",
                                 limit_value=1, expires_at=datetime(2030, 1, 1)),
                OrgLimitOverride(org_id=seed["target_id"], meter="assistant.turns", period="day",
                                 limit_value=1, expires_at=datetime(2030, 1, 1, 0, 0, 0, 1)),
            ])
            await db.commit()
        with TestClient(_app(session_factory)) as c:
            r = c.post("/api/v1/admin/orgs/feature-overrides/sweep-expired")
    assert r.json()["limit_overrides_deleted"] == 1


# ── feature-state ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feature_state_has_limits_rows(session_factory):
    seed = await _seed(session_factory)
    past = utcnow_naive() - timedelta(days=1)
    async with session_factory() as db:
        db.add_all([
            OrgLimitOverride(org_id=seed["target_id"], meter="mcp.calls", period="day",
                             limit_value=7, set_by=seed["admin_user_id"]),
            OrgLimitOverride(org_id=seed["target_id"], meter="assistant.turns", period="day",
                             limit_value=2, set_by=seed["admin_user_id"], expires_at=past),
            OrgFeatureOverride(org_id=seed["target_id"], feature_key="ai.budget", value=True,
                               set_by=seed["admin_user_id"], expires_at=past),
        ])
        await db.commit()
    with TestClient(_app(session_factory)) as c:
        r = c.get(f"/api/v1/admin/orgs/{seed['target_id']}/feature-state")
    assert r.status_code == 200, r.text
    body = r.json()
    lim = {row["meter"]: row for row in body["limits"]}
    assert set(lim) == {"assistant.turns", "mcp.calls", "platform_ai.tokens", "platform_ai.cents"}
    assert lim["mcp.calls"]["source"] == "override"
    assert lim["mcp.calls"]["effective"] == {"period": "day", "limit": 7}
    assert lim["mcp.calls"]["plan"] == {"period": "month", "limit": None}
    assert lim["mcp.calls"]["module"] == "ai"
    assert lim["mcp.calls"]["override"]["limit_value"] == 7
    # expired: override shown but not effective
    assert lim["assistant.turns"]["source"] == "default"
    assert lim["assistant.turns"]["effective"] == lim["assistant.turns"]["plan"]
    assert lim["assistant.turns"]["override"]["is_expired"] is True
    assert lim["platform_ai.cents"]["source"] == "default"
    assert lim["platform_ai.cents"]["override"] is None
    feats = {row["key"]: row for row in body["features"]}
    assert feats["ai.budget"]["override"]["is_expired"] is True
    assert feats["ai.budget"]["effective"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("model,event_name", [
    (OrgLimitOverride, "admin.limit_override.sweep.lock_delete_mismatch"),
    (OrgFeatureOverride, "admin.feature_override.sweep.lock_delete_mismatch"),
])
async def test_sweep_mismatch_warning_is_named_after_the_table(model, event_name, monkeypatch):
    """The mismatch warning names its own table's event, not the feature one."""
    from types import SimpleNamespace

    from app.routers import admin_orgs

    row = SimpleNamespace(id=1, org_id=1, meter="m", feature_key="f", expires_at=None)
    results = iter([
        SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row])),
        SimpleNamespace(rowcount=0),
    ])

    class _Db:
        async def execute(self, _stmt):
            return next(results)

    seen: list[str] = []

    async def _warn(name, **_kw):
        seen.append(name)

    monkeypatch.setattr(admin_orgs.logger, "awarning", _warn)
    key = model.meter if model is OrgLimitOverride else model.feature_key
    await admin_orgs._sweep_expired(
        _Db(), model, key, "counts", utcnow_naive(), lambda r: {key.key: "x"})
    assert seen == [event_name]
