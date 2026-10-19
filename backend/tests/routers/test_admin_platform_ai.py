"""TBD-586: the superadmin platform AI settings (GET/PUT /api/v1/admin/platform-ai).
The allowlist is ``platform_ai_models.enabled``."""
from __future__ import annotations

import datetime
import json

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Update, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app._time import utcnow_naive
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.platform_ai_model import PlatformAIModel
from app.models.system_setting import SystemSetting
from app.models.user import Organization, Role, User
from app.routers import admin_features

URL = "/api/v1/admin/platform-ai"
SECRET = "sk-env-SECRET-"
GOOD = {"enabled": True, "global_monthly_cents": 5000, "models": ["a/m", "a/m"]}


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(app_settings, "ai_native_enabled", True)
    monkeypatch.setattr(app_settings, "platform_ai_openrouter_api_key", SECRET)


@pytest_asyncio.fixture
async def users(sf):
    async with sf() as s:
        org = Organization(name="Platform", billing_cycle_day=1)
        s.add(org)
        await s.flush()
        root = User(org_id=org.id, username="root", email="root@x.io", password_hash="h",
                    role=Role.OWNER, is_superadmin=True, is_active=True)
        owner = User(org_id=org.id, username="own", email="own@x.io", password_hash="h",
                     role=Role.OWNER, is_active=True)
        s.add_all([root, owner])
        await s.commit()
        s.add(model_row("a/m"))
        await s.commit()
        return {"root": root.id, "owner": owner.id}


def model_row(mid, *, enabled=False, zp=100_000, zc=400_000, ctx=200_000, mct=8192, seen=None, expires=None):
    return PlatformAIModel(
        openrouter_id=mid, name=mid, context_length=ctx, max_completion_tokens=mct,
        supports_structured_outputs=True, zdr_prompt_micros_per_mtok=zp, zdr_completion_micros_per_mtok=zc,
        expires_at=expires, last_seen_at=seen or utcnow_naive(), enabled=enabled)


async def add(sf, *rows):
    async with sf() as s:
        s.add_all(rows)
        await s.commit()


async def flags(sf):
    async with sf() as s:
        return dict((await s.execute(select(PlatformAIModel.openrouter_id, PlatformAIModel.enabled))).all())


def client(sf, user_id, auth="jwt"):
    app = FastAPI()

    async def _db():
        async with sf() as s:
            yield s

    async def _user(request: Request):
        request.state.auth_method = auth
        async with sf() as s:
            return await s.get(User, user_id)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_session_factory] = lambda: sf
    app.include_router(admin_features.router)
    return TestClient(app, raise_server_exceptions=False)


async def rows(sf):
    async with sf() as s:
        return dict((await s.execute(select(SystemSetting.key, SystemSetting.value).where(
            SystemSetting.key.like("platform_ai.%")))).all())


async def audits(sf):
    async with sf() as s:
        return (await s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == "admin.platform_ai.updated"))).all()


# ---- A1 --------------------------------------------------------------------

async def test_a1_org_admin_and_pat_are_refused(sf, users):
    owner = client(sf, users["owner"])
    assert owner.get(URL).status_code == 403
    assert owner.put(URL, json=GOOD).status_code == 403
    pat = client(sf, users["root"], auth="pat")
    assert pat.put(URL, json=GOOD).status_code == 403
    assert await rows(sf) == {}


# ---- D3 ----------------------------------------------------------------------

async def test_fence24_a_list_is_a_full_replace_and_audits_old_before_the_writes(sf, users):
    """FENCE 24. Kills an additive enable; ``old`` read after the UPDATEs."""
    await add(sf, model_row("b/m"), model_row("c/m", enabled=True))
    r = client(sf, users["root"]).put(URL, json={**GOOD, "models": ["b/m", "a/m"]})
    assert r.status_code == 200, r.text
    assert await flags(sf) == {"a/m": True, "b/m": True, "c/m": False}
    (ev,) = await audits(sf)
    assert ev.detail["old"]["models"] == ["c/m"]
    assert ev.detail["new"]["models"] == ["a/m", "b/m"]


_NOW = utcnow_naive()
UNOFFERABLE = {
    "no_zdr": {"zp": None, "zc": None}, "stale": {"seen": _NOW - datetime.timedelta(hours=49)},
    "expiring": {"expires": _NOW.date() + datetime.timedelta(days=3)}, "ctx": {"ctx": 64_000},
    "mct": {"mct": 2048},
}


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("bad", ["unknown", *UNOFFERABLE])
async def test_fence25_an_unofferable_id_is_400_and_changes_nothing(sf, users, bad, enabled):
    """FENCE 25. Kills enabling a row dispatch would silently never offer."""
    if bad != "unknown":
        await add(sf, model_row("x/m", **UNOFFERABLE[bad]))
    before = await flags(sf)
    r = client(sf, users["root"]).put(URL, json={**GOOD, "enabled": enabled, "models": ["a/m", "x/m"]})
    assert r.status_code == 400, r.text
    d = r.json()["detail"]
    assert (d["code"], d["model"]) == ("platform_model_not_offerable", "x/m")
    assert await rows(sf) == {} and await flags(sf) == before


@pytest.mark.parametrize("mid", ["a m", "a/\u00e9", "x" * 121])
async def test_fence26_ids_the_columns_cannot_hold_are_422(sf, users, mid):
    """FENCE 26. Kills ids the ledger/routing columns cannot hold."""
    assert client(sf, users["root"]).put(URL, json={**GOOD, "models": [mid]}).status_code == 422
    assert await rows(sf) == {}


async def test_fence27_kill_switch_saves_with_an_ineligible_row_and_no_key(sf, users, monkeypatch):
    """FENCE 27. Kills the kill switch blocked; omitted ``models`` read as "disable all"."""
    monkeypatch.setattr(app_settings, "platform_ai_openrouter_api_key", "")
    await add(sf, model_row("s/m", enabled=True, seen=_NOW - datetime.timedelta(hours=49)),
              model_row("k/m", enabled=True))
    before = await flags(sf)
    c = client(sf, users["root"])
    r = c.put(URL, json={"enabled": False, "global_monthly_cents": 0})
    assert r.status_code == 200, r.text
    assert c.get(URL).json()["enabled"] is False
    assert await flags(sf) == before


async def test_fence28_enabling_needs_the_key_only_for_a_non_empty_set(sf, users, monkeypatch):
    """FENCE 28."""
    monkeypatch.setattr(app_settings, "platform_ai_openrouter_api_key", "")
    c = client(sf, users["root"])
    r = c.put(URL, json=GOOD)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "platform_provider_key_missing"
    assert await rows(sf) == {} and await flags(sf) == {"a/m": False}
    r = c.put(URL, json={**GOOD, "models": []})
    assert r.status_code == 200, r.text
    assert r.json()["models"] == []


async def test_fence29_one_commit_a_failing_catalog_update_rolls_back_the_settings(sf, users, monkeypatch):
    """FENCE 29. Kills more than one commit."""
    await add(sf, model_row("b/m", enabled=True))
    real = AsyncSession.execute
    updates = []

    async def flaky(self, stmt, *a, **k):
        if isinstance(stmt, Update) and stmt.table.name == "platform_ai_models":
            updates.append(stmt)
            if len(updates) == 2:
                raise RuntimeError("boom")
        return await real(self, stmt, *a, **k)

    monkeypatch.setattr(AsyncSession, "execute", flaky)
    r = client(sf, users["root"]).put(URL, json=GOOD)
    assert r.status_code == 500 and len(updates) == 2
    monkeypatch.setattr(AsyncSession, "execute", real)
    assert await rows(sf) == {} and await flags(sf) == {"a/m": False, "b/m": True}


# ---- A5 / GUARD 30 ------------------------------------------------------------

async def test_a5_round_trip_catalog_and_audit(sf, users):
    await add(sf, model_row("z/dear", zp=10_000_000, zc=40_000_000),
              model_row("n/zdr", zp=None, zc=None))
    c = client(sf, users["root"])
    r = c.put(URL, json=GOOD)
    assert r.status_code == 200, r.text
    got = c.get(URL)
    assert got.status_code == 200 and got.json() == r.json()
    body = r.json()
    assert (body["enabled"], body["global_monthly_cents"], body["models"]) == (True, 5000, ["a/m"])
    assert body["env_floor"] is True
    assert body["providers"] == [{"key": "openrouter", "key_configured": True}]
    assert [m["openrouter_id"] for m in body["catalog"]] == ["a/m", "z/dear"]  # cheapest first, no NULL ZDR
    a, z = body["catalog"]
    assert (a["enabled"], a["offered"], a["tier"]) == (True, True, "budget")
    assert (z["enabled"], z["offered"], z["tier"]) == (False, False, "frontier")
    assert set(a) == {"openrouter_id", "name", "context_length", "max_completion_tokens",
                      "supports_structured_outputs", "zdr_prompt_micros_per_mtok",
                      "zdr_completion_micros_per_mtok", "tier", "expires_at", "last_seen_at", "enabled", "offered"}
    (ev,) = await audits(sf)
    assert ev.detail == {"old": {"enabled": False, "global_monthly_cents": 0, "models": []},
                         "new": {"enabled": True, "global_monthly_cents": 5000, "models": ["a/m"]}}
    assert ev.target_org_id is None
    assert SECRET not in r.text + got.text + json.dumps(ev.detail)


async def test_a5_an_enabled_row_time_made_unofferable_shows_offered_false(sf, users):
    await add(sf, model_row("s/m", enabled=True, seen=_NOW - datetime.timedelta(hours=49)))
    (s,) = [m for m in client(sf, users["root"]).get(URL).json()["catalog"] if m["openrouter_id"] == "s/m"]
    assert (s["enabled"], s["offered"]) == (True, False)


# x10,000 at the dispatch gate must still fit a BIGINT bind value (TBD-630).
CEILING_MAX_CENTS = (2**53 - 1) // 10_000


async def test_fence_ceiling_boundary_is_accepted(sf, users):
    r = client(sf, users["root"]).put(URL, json={**GOOD, "global_monthly_cents": CEILING_MAX_CENTS})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("bad", [
    {**GOOD, "extra": 1},
    {**GOOD, "enabled": "true"},
    {**GOOD, "global_monthly_cents": -1},
    {**GOOD, "global_monthly_cents": 1.5},
    {**GOOD, "global_monthly_cents": CEILING_MAX_CENTS + 1},
    {**GOOD, "models": {"openai": ["gpt-4o"]}},
    {**GOOD, "models": [""]},
    {**GOOD, "models": ["a/m"] * 51},
])
async def test_a5_schema_422(sf, users, bad):
    assert client(sf, users["root"]).put(URL, json=bad).status_code == 422
    assert await rows(sf) == {}
