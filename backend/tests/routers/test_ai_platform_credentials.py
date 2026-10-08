"""TBD-586 PR2: the org's platform AI switch, F-S3 key-route refusals, routing
writes to a platform row, and ``/options.platform_providers``."""
from __future__ import annotations

import base64
import json
import os
from datetime import timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app._time import utcnow_naive
from app.auth.feature_catalog import PlanUsageLimits
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.main import plan_limit_handler
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.limit_override import OrgLimitOverride
from app.models.org_ai_consent import OrgAIConsent
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.org_ai_routing import OrgAIDefaultRouting, OrgAIFeatureRouting
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.system_setting import SystemSetting
from app.models.user import Organization, Role, User
from app.routers import ai_providers
from app.services import ai_credential_service, platform_ai
from app.services.ai_providers import ValidateResult
from app.services.usage_service import PlanLimitReached

BASE = "/api/v1/settings/ai-providers"
SECRET = "env-key-SECRET-"
MODELS = {"openai": ["gpt-4o-mini"], "anthropic": ["claude-haiku-4-5"]}


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
    # BYOK rows are encrypted at rest; CI has no key in its env.
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key",
                        base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key_prev", "")


async def set_platform(sf, *, enabled="on", cents="1000", models=None):
    async with sf() as s:
        for k, v in (("enabled", enabled), ("global_monthly_cents", cents),
                     ("models", json.dumps(MODELS if models is None else models))):
            row = await s.scalar(select(SystemSetting).where(SystemSetting.key == f"platform_ai.{k}"))
            if row:
                row.value = v
            else:
                s.add(SystemSetting(key=f"platform_ai.{k}", value=v))
        await s.commit()


async def consent(sf, org_id, *, version=None, revoked=False):
    async with sf() as s:
        s.add(OrgAIConsent(org_id=org_id, consent_version=version or app_settings.ai_native_current_consent_version,
                           revoked_at=utcnow_naive() if revoked else None))
        await s.commit()


async def mk_org(sf, name, *, tokens=100_000, cents=1000, with_consent=True):
    async with sf() as s:
        org = Organization(name=name, billing_cycle_day=1)
        s.add(org)
        await s.flush()
        limits = PlanUsageLimits().model_dump(by_alias=True)
        limits["platform_ai.tokens"] = {"period": "month", "limit": tokens}
        limits["platform_ai.cents"] = {"period": "month", "limit": cents}
        plan = Plan(slug=f"p{org.id}", name="P", features={}, usage_limits=limits)
        s.add(plan)
        await s.flush()
        s.add(Subscription(org_id=org.id, plan_id=plan.id, status=SubscriptionStatus.ACTIVE))
        user = User(org_id=org.id, username=f"u{org.id}", email=f"u{org.id}@x.io", password_hash="h",
                    role=Role.OWNER, is_active=True)
        s.add(user)
        await s.commit()
        ids = org.id, user.id
    if with_consent:
        await consent(sf, ids[0])
    return ids


async def mk_cred(sf, org_id, platform=None, provider=AiProvider.OPENAI):
    async with sf() as s:
        cred = OrgAICredential(org_id=org_id, provider=provider, platform_provider=platform,
                               label="mine" if platform is None else None,
                               discovered_capabilities=["chat"])
        s.add(cred)
        await s.commit()
        return cred.id


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
    app.add_exception_handler(PlanLimitReached, plan_limit_handler)
    app.include_router(ai_providers.router)
    return TestClient(app, raise_server_exceptions=False)


async def platform_rows(sf):
    async with sf() as s:
        return (await s.scalars(select(OrgAICredential).where(
            OrgAICredential.platform_provider.is_not(None)))).all()


async def audits(sf, event_type=None):
    async with sf() as s:
        q = select(AuditEvent)
        if event_type:
            q = q.where(AuditEvent.event_type == event_type)
        return (await s.scalars(q)).all()


@pytest_asyncio.fixture
async def org(sf):
    await set_platform(sf)
    return await mk_org(sf, "Acme")


# ---- P1 --------------------------------------------------------------------

def _env_off(monkeypatch):
    monkeypatch.setattr(app_settings, "ai_native_enabled", False)


async def _setting_off(sf):
    await set_platform(sf, enabled="off")


def _key_off(monkeypatch):
    monkeypatch.setattr(app_settings, "platform_ai_openai_api_key", "")


async def _not_allowlisted(sf):
    await set_platform(sf, models={"anthropic": ["claude-haiku-4-5"]})


@pytest.mark.parametrize("off", ["env", "setting", "key", "allowlist"])
async def test_p1_not_offered_is_412(sf, org, monkeypatch, off):
    if off == "env":
        _env_off(monkeypatch)
    elif off == "setting":
        await _setting_off(sf)
    elif off == "key":
        _key_off(monkeypatch)
    else:
        await _not_allowlisted(sf)
    r = client(sf, org[1]).post(f"{BASE}/platform/openai")
    assert r.status_code == 412 and r.json()["detail"]["code"] == "ai_native_not_available"
    assert await platform_rows(sf) == []


@pytest.mark.parametrize("tokens,cents,meter", [(0, 500, "platform_ai.tokens"), (500, 0, "platform_ai.cents")])
async def test_p1_each_meter_zero_is_402(sf, monkeypatch, tokens, cents, meter):
    await set_platform(sf)
    _, uid = await mk_org(sf, "Zero", tokens=tokens, cents=cents)
    r = client(sf, uid).post(f"{BASE}/platform/openai")
    assert r.status_code == 402, r.text
    d = r.json()["detail"]
    assert d["code"] == "plan_limit_reached" and d["meter"] == meter and d["limit"] == 0
    assert d["period"] == "month" and d["resets_at"] is None
    assert await platform_rows(sf) == []


async def _override(sf, org_id, meter, value):
    async with sf() as s:
        s.add(OrgLimitOverride(org_id=org_id, meter=meter, period="month", limit_value=value,
                               expires_at=utcnow_naive() + timedelta(days=1)))
        await s.commit()


async def test_p1_entitlements_include_overrides(sf):
    await set_platform(sf)
    oid, uid = await mk_org(sf, "Plan0", tokens=0, cents=0)
    await _override(sf, oid, "platform_ai.tokens", 100)
    await _override(sf, oid, "platform_ai.cents", 100)
    assert client(sf, uid).post(f"{BASE}/platform/openai").status_code == 201
    oid2, uid2 = await mk_org(sf, "Over0")
    await _override(sf, oid2, "platform_ai.cents", 0)
    r = client(sf, uid2).post(f"{BASE}/platform/openai")
    assert r.status_code == 402 and r.json()["detail"]["meter"] == "platform_ai.cents"
    assert [c.org_id for c in await platform_rows(sf)] == [oid]


@pytest.mark.parametrize("state", ["none", "revoked", "old_version"])
async def test_p1_consent_is_412(sf, state):
    await set_platform(sf)
    oid, uid = await mk_org(sf, "C", with_consent=False)
    if state == "revoked":
        await consent(sf, oid)
        await consent(sf, oid, revoked=True)
    elif state == "old_version":
        await consent(sf, oid, version="ai-tos-1999-01-01")
    r = client(sf, uid).post(f"{BASE}/platform/openai")
    assert r.status_code == 412, r.text
    assert r.json()["detail"]["code"] == "ai_consent_required"
    assert r.json()["detail"]["current_consent_version"] == app_settings.ai_native_current_consent_version
    assert await platform_rows(sf) == []


async def test_p1_second_post_is_409(sf, org):
    c = client(sf, org[1])
    assert c.post(f"{BASE}/platform/openai").status_code == 201
    r = c.post(f"{BASE}/platform/openai")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "platform_credential_exists"
    assert len(await platform_rows(sf)) == 1


async def test_p1_unknown_provider_is_422(sf, org):
    assert client(sf, org[1]).post(f"{BASE}/platform/native").status_code == 422


# ---- P2 --------------------------------------------------------------------

async def test_p2_integrity_error_is_409_not_500(sf, org, monkeypatch):
    await mk_cred(sf, org[0], "openai")

    async def miss(*a, **k):
        return None

    monkeypatch.setattr(ai_credential_service, "get_platform_credential", miss)
    r = client(sf, org[1]).post(f"{BASE}/platform/openai")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "platform_credential_exists"
    assert len(await platform_rows(sf)) == 1


# ---- P3 --------------------------------------------------------------------

@pytest.mark.parametrize("p", ["openai", "anthropic"])
async def test_p3_created_row_response_and_audit(sf, org, p):
    r = client(sf, org[1]).post(f"{BASE}/platform/{p}")
    assert r.status_code == 201, r.text
    body = r.json()
    (row,) = await platform_rows(sf)
    assert row.org_id == org[0] and row.platform_provider == p
    assert row.provider == platform_ai.PLATFORM_ADAPTER[p]
    assert row.discovered_capabilities == platform_ai.PLATFORM_CAPABILITIES[p]
    for col in ("encrypted_api_key", "encrypted_bearer_token", "key_fingerprint", "last_four",
                "base_url", "discovered_models", "label"):
        assert getattr(row, col) is None, col
    assert body["id"] == row.id and body["platform_provider"] == p
    assert body["last_four"] is None and body["key_fingerprint"] is None and body["base_url"] is None
    (ev,) = await audits(sf, "ai.platform.enabled")
    assert ev.target_org_id == org[0]
    assert ev.detail == {"credential_id": row.id, "platform_provider": p,
                         "consent_version": app_settings.ai_native_current_consent_version}
    assert SECRET not in r.text + json.dumps(ev.detail)


# ---- P4 --------------------------------------------------------------------

async def test_p4_delete_is_unconditional_scoped_and_404_when_absent(sf, org, monkeypatch):
    other, _ = await mk_org(sf, "Other")
    mine = await mk_cred(sf, org[0], "openai")
    byok = await mk_cred(sf, org[0], None, AiProvider.OPENAI)
    theirs = await mk_cred(sf, other, "openai")
    _env_off(monkeypatch)
    await _setting_off(sf)
    c = client(sf, org[1])
    r = c.delete(f"{BASE}/platform/openai")
    assert r.status_code == 204, r.text
    async with sf() as s:
        left = {row.id for row in (await s.scalars(select(OrgAICredential))).all()}
    assert left == {byok, theirs}
    (ev,) = await audits(sf, "ai.platform.disabled")
    assert ev.detail == {"credential_id": mine, "platform_provider": "openai"}
    assert c.delete(f"{BASE}/platform/openai").status_code == 404
    assert c.delete(f"{BASE}/platform/anthropic").status_code == 404


# ---- P5 --------------------------------------------------------------------

async def test_p5_post_via_pat_is_403(sf, org):
    assert client(sf, org[1], auth="pat").post(f"{BASE}/platform/openai").status_code == 403
    assert await platform_rows(sf) == []


# ---- S1 / S2 ---------------------------------------------------------------

KEY_ROUTES = [
    ("patch", "", {"label": "x"}),
    ("post", "/rotate", {"api_key": "sk-abcdefghijklmnop"}),
    ("post", "/validate", None),
    ("delete", "", None),
]


@pytest.fixture
def hits(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_providers.rate_limit_db, "hit", lambda *a: calls.append(a) or 1)
    return calls


async def _ok_validate(**kw):
    return ValidateResult(ok=True, discovered_capabilities=["chat"], discovered_models=["m"])


@pytest.mark.parametrize("method,suffix,body", KEY_ROUTES)
async def test_s1_key_routes_refuse_a_platform_row(sf, org, hits, monkeypatch, method, suffix, body):
    monkeypatch.setattr(ai_credential_service, "_run_validate", _ok_validate)
    other, other_uid = await mk_org(sf, "Other")
    pid = await mk_cred(sf, org[0], "openai")
    byok = await mk_cred(sf, org[0], None)
    theirs = await mk_cred(sf, other, "openai")
    c = client(sf, org[1])
    kw = {"json": body} if body is not None else {}
    r = c.request(method.upper(), f"{BASE}/{pid}{suffix}", **kw)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "platform_credential"
    assert hits == [] and await audits(sf) == []
    async with sf() as s:
        row = await s.get(OrgAICredential, pid)
        assert row is not None and row.label is None and row.encrypted_api_key is None
    # Another org's platform id stays an indistinguishable 404.
    assert c.request(method.upper(), f"{BASE}/{theirs}{suffix}", **kw).status_code == 404
    # A BYOK row in the same org is untouched by the refusal.
    assert c.request(method.upper(), f"{BASE}/{byok}{suffix}", **kw).status_code in (200, 204)


async def test_s2_byok_post_cannot_carry_platform_provider(sf, org):
    r = client(sf, org[1]).post(BASE, json={"provider": "openai", "api_key": "sk-abcdefghijklmnop",
                                            "platform_provider": "openai"})
    assert r.status_code == 422
    async with sf() as s:
        assert (await s.scalars(select(OrgAICredential))).all() == []


# ---- R1 --------------------------------------------------------------------

async def _routing_rows(sf):
    async with sf() as s:
        return ((await s.scalars(select(OrgAIDefaultRouting))).all(),
                (await s.scalars(select(OrgAIFeatureRouting))).all())


@pytest.mark.parametrize("which", ["default", "feature"])
async def test_r1_routing_write_to_a_platform_row(sf, org, which):
    await set_platform(sf, models={**MODELS, "openai": ["gpt-4o-mini", "o3-mini"]})
    other, _ = await mk_org(sf, "Other")
    pid = await mk_cred(sf, org[0], "openai")
    byok = await mk_cred(sf, org[0], None)
    theirs = await mk_cred(sf, other, "openai")
    c = client(sf, org[1])
    url = f"{BASE}/routing/default" if which == "default" else f"{BASE}/routing/features/chat"

    for model in ("gpt-4o", "o3-mini"):  # not allowlisted / allowlisted but not offerable
        r = c.put(url, json={"credential_id": pid, "model": model})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "platform_model_not_allowed", r.text
    assert await _routing_rows(sf) == ([], [])
    r = c.put(url, json={"credential_id": theirs, "model": "gpt-4o"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "cross_org_routing_denied"
    assert c.put(url, json={"credential_id": byok, "model": "gpt-4o"}).status_code == 200
    assert c.put(url, json={"credential_id": pid, "model": "gpt-4o-mini"}).status_code == 200


# ---- O1 --------------------------------------------------------------------

@pytest.mark.parametrize("off", [None, "env", "setting", "key", "allowlist"])
async def test_o1_options_lists_exactly_the_offered_providers(sf, org, monkeypatch, off):
    expected = ["openai", "anthropic"]
    if off == "env":
        _env_off(monkeypatch)
        expected = []
    elif off == "setting":
        await _setting_off(sf)
        expected = []
    elif off == "key":
        _key_off(monkeypatch)
        expected = ["anthropic"]
    elif off == "allowlist":
        await _not_allowlisted(sf)
        expected = ["anthropic"]
    r = client(sf, org[1]).get(f"{BASE}/options")
    assert r.status_code == 200
    assert r.json()["platform_providers"] == [
        {"key": p, "label": platform_ai.PLATFORM_LABELS[p], "models": MODELS[p]} for p in expected]
    assert SECRET not in r.text
