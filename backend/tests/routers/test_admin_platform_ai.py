"""TBD-586 PR2: the superadmin platform AI settings (GET/PUT /api/v1/admin/platform-ai)."""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.system_setting import SystemSetting
from app.models.user import Organization, Role, User
from app.routers import admin_features
from app.services import platform_ai, platform_ai_settings
from app.services.ai_pricing import MODEL_PRICING
from app.services.ai_token_estimate import _DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL

URL = "/api/v1/admin/platform-ai"
SECRET = "sk-env-SECRET-"
GOOD = {
    "enabled": True,
    "global_monthly_cents": 5000,
    "models": {
        "openai": ["gpt-4o-mini", "gpt-4o-mini", "text-embedding-3-small"],
        "anthropic": ["claude-haiku-4-5"],
        "openrouter": [],
    },
}
GOOD_MODELS = {"openai": ["gpt-4o-mini", "text-embedding-3-small"], "anthropic": ["claude-haiku-4-5"]}


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
    for p in ("openrouter", "gemini"):
        monkeypatch.setattr(app_settings, f"platform_ai_{p}_api_key", "")
    for p in ("openai", "anthropic"):
        monkeypatch.setattr(app_settings, f"platform_ai_{p}_api_key", SECRET + p)


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
        return {"root": root.id, "owner": owner.id}


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


# ---- A2 / B2a --------------------------------------------------------------

@pytest.mark.parametrize("provider,model", [
    ("openai", "_default"), ("openai", "no-such-model"), ("openai", "gpt-4o"), ("anthropic", "gpt-4o"),
])
async def test_a2_unofferable_model_is_400(sf, users, monkeypatch, provider, model):
    if model == "gpt-4o" and provider == "openai":
        monkeypatch.delitem(_DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL, "gpt-4o")
    body = {**GOOD, "models": {provider: ["gpt-4o-mini" if provider == "openai" else "claude-haiku-4-5", model]}}
    r = client(sf, users["root"]).put(URL, json=body)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == {"code": "platform_model_not_offerable", "provider": provider, "model": model,
                                  "message": r.json()["detail"]["message"]}
    assert await rows(sf) == {}


# ---- A3 --------------------------------------------------------------------

async def test_a3_key_missing_only_for_a_non_empty_list(sf, users, monkeypatch):
    monkeypatch.setattr(app_settings, "platform_ai_anthropic_api_key", "")
    c = client(sf, users["root"])
    r = c.put(URL, json={**GOOD, "models": {"openai": ["gpt-4o-mini"], "anthropic": ["claude-haiku-4-5"]}})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "platform_provider_key_missing"
    assert r.json()["detail"]["provider"] == "anthropic"
    assert await rows(sf) == {}
    # openrouter has no key either; an empty list for it (and for anthropic) saves.
    r = c.put(URL, json={**GOOD, "models": {"openai": ["gpt-4o-mini"], "anthropic": [], "openrouter": []}})
    assert r.status_code == 200, r.text
    assert r.json()["models"] == {"openai": ["gpt-4o-mini"]}


# ---- A4 --------------------------------------------------------------------

async def test_a4_a_refused_put_changes_no_row(sf, users):
    c = client(sf, users["root"])
    assert c.put(URL, json=GOOD).status_code == 200
    before = await rows(sf)
    r = c.put(URL, json={"enabled": True, "global_monthly_cents": 1,
                         "models": {"openai": ["gpt-4o", "_default"]}})
    assert r.status_code == 400
    assert await rows(sf) == before


async def test_a4_a_failure_on_the_third_upsert_rolls_back_the_first_two(sf, users, monkeypatch):
    c = client(sf, users["root"])
    assert c.put(URL, json=GOOD).status_code == 200
    before = await rows(sf)
    real = admin_features._upsert_system_setting
    calls = []

    async def flaky(db, key, value):
        calls.append(key)
        if len(calls) == 3:
            raise RuntimeError("boom")
        await real(db, key, value)

    monkeypatch.setattr(admin_features, "_upsert_system_setting", flaky)
    r = c.put(URL, json={"enabled": False, "global_monthly_cents": 7, "models": {"openai": ["gpt-4o"]}})
    assert r.status_code == 500 and len(calls) == 3
    assert await rows(sf) == before


# ---- A5 --------------------------------------------------------------------

async def test_a5_round_trip_and_audit(sf, users):
    c = client(sf, users["root"])
    r = c.put(URL, json=GOOD)
    assert r.status_code == 200, r.text
    got = c.get(URL)
    assert got.status_code == 200 and got.json() == r.json()
    async with sf() as s:
        conf = await platform_ai_settings.load(s)
    body = r.json()
    assert {k: body[k] for k in ("enabled", "global_monthly_cents", "models")} == asdict(conf)
    assert body["models"] == GOOD_MODELS
    assert body["env_floor"] is True
    assert body["providers"] == [
        {"key": "openrouter", "key_configured": False}, {"key": "openai", "key_configured": True},
        {"key": "anthropic", "key_configured": True}, {"key": "gemini", "key_configured": False},
    ]
    events = await audits(sf)
    assert len(events) == 1
    detail = events[0].detail
    assert detail["old"] == {"enabled": False, "global_monthly_cents": 0, "models": {}}
    assert detail["new"] == asdict(conf)
    assert events[0].target_org_id is None
    assert SECRET not in r.text + got.text + json.dumps(detail)


@pytest.mark.parametrize("bad", [
    {**GOOD, "extra": 1},
    {**GOOD, "enabled": "true"},
    {**GOOD, "global_monthly_cents": -1},
    {**GOOD, "global_monthly_cents": 1.5},
    {**GOOD, "models": {"native": ["gpt-4o"]}},
    {**GOOD, "models": {"openai": [""]}},
    {**GOOD, "models": {"openai": ["gpt-4o"] * 51}},
])
async def test_a5_schema_422(sf, users, bad):
    assert client(sf, users["root"]).put(URL, json=bad).status_code == 422
    assert await rows(sf) == {}


# ---- B3 --------------------------------------------------------------------

async def test_b3_kill_switch_always_saves(sf, users, monkeypatch):
    async with sf() as s:
        s.add_all([
            SystemSetting(key="platform_ai.enabled", value="on"),
            SystemSetting(key="platform_ai.global_monthly_cents", value="900"),
            SystemSetting(key="platform_ai.models", value=json.dumps({"gemini": ["gemini-x"]})),
        ])
        await s.commit()
    c = client(sf, users["root"])
    shown = c.get(URL).json()
    assert shown["enabled"] is True and shown["models"] == {"gemini": ["gemini-x"]}
    r = c.put(URL, json={"enabled": False, "global_monthly_cents": shown["global_monthly_cents"],
                         "models": shown["models"]})
    assert r.status_code == 200, r.text
    assert c.get(URL).json()["enabled"] is False


# ---- B2b (the real table; no fixture here patches PLATFORM_MODELS) ----------

def test_b2b_every_platform_model_is_priced_and_bounded():
    assert set(platform_ai.PLATFORM_MODELS) == set(platform_ai.PLATFORM_PROVIDERS)
    for provider, models in platform_ai.PLATFORM_MODELS.items():
        for m in models:
            assert m != "_default" and m in MODEL_PRICING and m in _DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL, m
            assert platform_ai.offerable_model(provider, m)
