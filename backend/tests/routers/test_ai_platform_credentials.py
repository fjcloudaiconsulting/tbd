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
from app.models.platform_ai_model import PlatformAIModel
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.system_setting import SystemSetting
from app.models.user import Organization, Role, User
from app.routers import ai_providers
from app.services import ai_credential_service, feature_service
from app.services.ai_providers import ValidateResult
from app.services.usage_service import PlanLimitReached

BASE = "/api/v1/settings/ai-providers"
SECRET = "env-key-SECRET-"
MODEL = "vendor/model-a"
PLATFORM = f"{BASE}/platform/openrouter"


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
    # BYOK rows are encrypted at rest; CI has no key in its env.
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key",
                        base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key_prev", "")


async def set_platform(sf, *, enabled="on", cents="1000"):
    async with sf() as s:
        if await s.get(PlatformAIModel, MODEL) is None:
            s.add(model_row(MODEL))
        for k, v in (("enabled", enabled), ("global_monthly_cents", cents)):
            row = await s.scalar(select(SystemSetting).where(SystemSetting.key == f"platform_ai.{k}"))
            if row:
                row.value = v
            else:
                s.add(SystemSetting(key=f"platform_ai.{k}", value=v))
        await s.commit()


def model_row(mid, *, enabled=True, mct=8192, zp=100_000, zc=400_000, structured=True):
    return PlatformAIModel(
        openrouter_id=mid, name=mid, context_length=200_000, max_completion_tokens=mct,
        supports_structured_outputs=structured, zdr_prompt_micros_per_mtok=zp,
        zdr_completion_micros_per_mtok=zc, last_seen_at=utcnow_naive(), enabled=enabled)


async def set_enabled(sf, mid, enabled):
    async with sf() as s:
        (await s.get(PlatformAIModel, mid)).enabled = enabled
        await s.commit()


async def consent(sf, org_id, *, version=None, revoked=False):
    async with sf() as s:
        s.add(OrgAIConsent(org_id=org_id, consent_version=version or app_settings.ai_native_current_consent_version,
                           revoked_at=utcnow_naive() if revoked else None))
        await s.commit()


async def mk_org(sf, name, *, tokens=100_000, micros=1000, with_consent=True, slug=None, tokens_period="month",
                 micros_period="month"):
    async with sf() as s:
        org = Organization(name=name, billing_cycle_day=1)
        s.add(org)
        await s.flush()
        limits = PlanUsageLimits().model_dump(by_alias=True)
        limits["platform_ai.tokens"] = {"period": tokens_period, "limit": tokens}
        limits["platform_ai.micros"] = {"period": micros_period, "limit": micros}
        plan = Plan(slug=slug or f"p{org.id}", name="P", features={}, usage_limits=limits)
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


async def mk_cred(sf, org_id, platform=None, provider=None, discovered_models=None, discovered_capabilities=None):
    provider = provider or (AiProvider.OPENAI if platform is None else AiProvider.OPENAI_COMPATIBLE)
    async with sf() as s:
        cred = OrgAICredential(org_id=org_id, provider=provider, platform_provider=platform,
                               label="mine" if platform is None else None,
                               discovered_models=discovered_models,
                               discovered_capabilities=discovered_capabilities or ["chat"])
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
    monkeypatch.setattr(app_settings, "platform_ai_openrouter_api_key", "")


async def _not_allowlisted(sf):
    await set_enabled(sf, MODEL, False)


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
    r = client(sf, org[1]).post(PLATFORM)
    assert r.status_code == 412 and r.json()["detail"]["code"] == "ai_native_not_available"
    assert await platform_rows(sf) == []


@pytest.mark.parametrize("tokens,micros,meter", [(0, 500, "platform_ai.tokens"), (500, 0, "platform_ai.micros")])
async def test_p1_each_meter_zero_is_402(sf, monkeypatch, tokens, micros, meter):
    await set_platform(sf)
    _, uid = await mk_org(sf, "Zero", tokens=tokens, micros=micros)
    r = client(sf, uid).post(PLATFORM)
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
    oid, uid = await mk_org(sf, "Plan0", tokens=0, micros=0)
    await _override(sf, oid, "platform_ai.tokens", 100)
    await _override(sf, oid, "platform_ai.micros", 100)
    assert client(sf, uid).post(PLATFORM).status_code == 201
    oid2, uid2 = await mk_org(sf, "Over0")
    await _override(sf, oid2, "platform_ai.micros", 0)
    r = client(sf, uid2).post(PLATFORM)
    assert r.status_code == 402 and r.json()["detail"]["meter"] == "platform_ai.micros"
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
    r = client(sf, uid).post(PLATFORM)
    assert r.status_code == 412, r.text
    assert r.json()["detail"]["code"] == "ai_consent_required"
    assert r.json()["detail"]["current_consent_version"] == app_settings.ai_native_current_consent_version
    assert await platform_rows(sf) == []


async def test_p1_second_post_is_409(sf, org):
    c = client(sf, org[1])
    assert c.post(PLATFORM).status_code == 201
    r = c.post(PLATFORM)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "platform_credential_exists"
    assert len(await platform_rows(sf)) == 1


@pytest.mark.parametrize("p", ["native", "openai", "anthropic", "gemini"])
async def test_fence31_only_openrouter_is_a_platform_provider(sf, org, p):
    """FENCE 31. Kills old providers creatable."""
    c = client(sf, org[1])
    assert c.post(f"{BASE}/platform/{p}").status_code == 422
    assert c.delete(f"{BASE}/platform/{p}").status_code == 422
    assert await platform_rows(sf) == []


async def test_p1_entitlements_error_is_402_not_500(sf, org, monkeypatch):
    async def boom(*a, **k):
        raise KeyError("platform_ai.tokens")

    monkeypatch.setattr(feature_service, "get_entitlements", boom)
    r = client(sf, org[1]).post(PLATFORM)
    assert r.status_code == 402, r.text
    assert r.json()["detail"]["code"] == "platform_ai_unavailable"
    assert await platform_rows(sf) == []


# ---- P2 --------------------------------------------------------------------

async def test_p2_integrity_error_is_409_not_500(sf, org, monkeypatch):
    await mk_cred(sf, org[0], "openrouter")

    async def miss(*a, **k):
        return None

    monkeypatch.setattr(ai_credential_service, "get_platform_credential", miss)
    r = client(sf, org[1]).post(PLATFORM)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "platform_credential_exists"
    assert len(await platform_rows(sf)) == 1


# ---- P3 --------------------------------------------------------------------

async def test_fence31_p3_created_row_response_and_audit(sf, org):
    """FENCE 31. Kills offering with an empty catalog; embed slipping into the set."""
    r = client(sf, org[1]).post(PLATFORM)
    assert r.status_code == 201, r.text
    body = r.json()
    (row,) = await platform_rows(sf)
    assert row.org_id == org[0] and row.platform_provider == "openrouter"
    assert body["provider"] == "openai_compatible" and row.provider == AiProvider.OPENAI_COMPATIBLE
    assert row.discovered_capabilities == ["chat", "structured_output", "function_call", "stream"]
    for col in ("encrypted_api_key", "encrypted_bearer_token", "key_fingerprint", "last_four",
                "base_url", "discovered_models", "label"):
        assert getattr(row, col) is None, col
    assert body["id"] == row.id and body["platform_provider"] == "openrouter"
    assert body["last_four"] is None and body["key_fingerprint"] is None and body["base_url"] is None
    (ev,) = await audits(sf, "ai.platform.enabled")
    assert ev.target_org_id == org[0]
    assert ev.detail == {"credential_id": row.id, "platform_provider": "openrouter",
                         "consent_version": app_settings.ai_native_current_consent_version}
    assert SECRET not in r.text + json.dumps(ev.detail)


async def test_fence31_no_offerable_model_is_412(sf, org):
    await set_enabled(sf, MODEL, False)
    async with sf() as s:
        s.add(model_row("short/m", mct=2048))  # enabled, but under the output floor
        await s.commit()
    r = client(sf, org[1]).post(PLATFORM)
    assert r.status_code == 412 and r.json()["detail"]["code"] == "ai_native_not_available"
    assert await platform_rows(sf) == []


# ---- P4 --------------------------------------------------------------------

async def test_p4_delete_is_unconditional_scoped_and_404_when_absent(sf, org, monkeypatch):
    other, _ = await mk_org(sf, "Other")
    mine = await mk_cred(sf, org[0], "openrouter")
    byok = await mk_cred(sf, org[0], None, AiProvider.OPENAI)
    theirs = await mk_cred(sf, other, "openrouter")
    _env_off(monkeypatch)
    await _setting_off(sf)
    c = client(sf, org[1])
    r = c.delete(PLATFORM)
    assert r.status_code == 204, r.text
    async with sf() as s:
        left = {row.id for row in (await s.scalars(select(OrgAICredential))).all()}
    assert left == {byok, theirs}
    (ev,) = await audits(sf, "ai.platform.disabled")
    assert ev.detail == {"credential_id": mine, "platform_provider": "openrouter"}
    assert c.delete(PLATFORM).status_code == 404


# ---- P5 --------------------------------------------------------------------

async def test_p5_post_via_pat_is_403(sf, org):
    assert client(sf, org[1], auth="pat").post(PLATFORM).status_code == 403
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
    pid = await mk_cred(sf, org[0], "openrouter")
    byok = await mk_cred(sf, org[0], None, discovered_models=["gpt-4o"],
                         discovered_capabilities=["chat", "function_call"])
    theirs = await mk_cred(sf, other, "openrouter")
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
                                            "platform_provider": "openrouter"})
    assert r.status_code == 422
    async with sf() as s:
        assert (await s.scalars(select(OrgAICredential))).all() == []


# ---- R1 --------------------------------------------------------------------

async def _routing_rows(sf):
    async with sf() as s:
        return ((await s.scalars(select(OrgAIDefaultRouting))).all(),
                (await s.scalars(select(OrgAIFeatureRouting))).all())


@pytest.mark.parametrize("which", ["default", "feature"])
async def test_fence32_routing_write_to_a_platform_row(sf, org, which):
    """FENCE 32."""
    async with sf() as s:
        s.add_all([model_row("off/m", enabled=False), model_row("short/m", mct=2048)])
        await s.commit()
    other, _ = await mk_org(sf, "Other")
    pid = await mk_cred(sf, org[0], "openrouter")
    byok = await mk_cred(sf, org[0], None, discovered_models=["gpt-4o"],
                         discovered_capabilities=["chat", "function_call"])
    theirs = await mk_cred(sf, other, "openrouter")
    c = client(sf, org[1])
    url = f"{BASE}/routing/default" if which == "default" else f"{BASE}/routing/features/chat"

    for model in ("gpt-4o", "off/m", "short/m"):  # code table only / disabled / incapable
        r = c.put(url, json={"credential_id": pid, "model": model})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "model_not_listed", r.text
    assert await _routing_rows(sf) == ([], [])
    r = c.put(url, json={"credential_id": theirs, "model": "gpt-4o"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "cross_org_routing_denied"
    assert c.put(url, json={"credential_id": byok, "model": "gpt-4o"}).status_code == 200
    assert c.put(url, json={"credential_id": pid, "model": MODEL}).status_code == 200


# ---- O1 --------------------------------------------------------------------

@pytest.mark.parametrize("off", [None, "env", "setting", "key", "allowlist"])
async def test_o1_options_lists_exactly_the_offered_provider(sf, org, monkeypatch, off):
    if off == "env":
        _env_off(monkeypatch)
    elif off == "setting":
        await _setting_off(sf)
    elif off == "key":
        _key_off(monkeypatch)
    elif off == "allowlist":
        await _not_allowlisted(sf)
    r = client(sf, org[1]).get(f"{BASE}/options")
    assert r.status_code == 200
    want = [] if off else [{"key": "openrouter", "label": "Included AI", "models": [MODEL]}]
    assert r.json()["platform_providers"] == want
    assert SECRET not in r.text
