"""TBD-586 PR1 stage 2: platform dispatch through the five wrappers.

Real adapters, fake provider over ``httpx.MockTransport``. The DB-level race
fences (K2-K6) live in ``test_platform_reserve_mysql.py``.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.feature_catalog import PlanUsageLimits
from app.config import settings as app_settings
from app.models import Base
from app.models.ai_usage_ledger import AIUsageLedger
from app.models.org_ai_caps import OrgAIDefaultCaps
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.org_ai_routing import OrgAIDefaultRouting
from app.models.platform_ai_spend import PlatformAISpend
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.system_setting import SystemSetting
from app.models.usage_counter import UsageCounter
from app.models.user import Organization
from app.services import agent_chat, ai_dispatch, platform_ai, platform_reserve
from app.services.ai_credential_crypto import encrypt
from app.services.ai_dispatch import (
    AICapabilityNotSupported,
    AIDispatchFailed,
    AIPlanLimitReached,
    AIPlatformProjectionFailed,
    AIPlatformUnavailable,
    http_for_dispatch_error,
)
from app.services.ai_pricing import MODEL_PRICING, ModelPricing, estimate_cost_cents
from app.services.ai_providers import NativeNotAvailable
from app.services.ai_providers.base import AIProviderError, CapabilityNotSupported
from app.services.ai_token_estimate import (
    _DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL,
    estimate_prompt_tokens_from_messages,
)
from app.services.usage_service import PlanLimitReached

APP = Path(__file__).resolve().parents[2] / "app"
MSGS = [{"role": "user", "content": "hi"}]
TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
SCHEMA = {"type": "object", "required": ["a"]}
HOSTS = {
    "openrouter": "openrouter.ai", "openai": "api.openai.com",
    "anthropic": "api.anthropic.com", "gemini": "generativelanguage.googleapis.com",
}
MODELS = {
    "openrouter": "vendor/model-a", "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5", "gemini": "gemini-x",
}


# ---- fixtures ------------------------------------------------------------

@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk(dbapi_conn, _r):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def db(sf):
    async with sf() as s:
        yield s


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr("app.services.ai_dispatch.redis_client.get_client", lambda: None)
    monkeypatch.setattr(app_settings, "ai_native_enabled", True)
    for p in HOSTS:
        monkeypatch.setattr(app_settings, f"platform_ai_{p}_api_key", f"env-key-{p}")
    monkeypatch.setitem(MODEL_PRICING, "vendor/model-a", ModelPricing(100, 400))
    monkeypatch.setitem(MODEL_PRICING, "gemini-x", ModelPricing(100, 400))
    monkeypatch.setitem(_DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL, "vendor/model-a", 2000)
    monkeypatch.setitem(_DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL, "gemini-x", 2000)
    monkeypatch.setattr(app_settings, "ai_dispatch_timeout_s", 5.0)


class Fake:
    """Fake provider. ``usages`` / ``contents`` are consumed per call."""

    def __init__(self):
        self.reqs: list[httpx.Request] = []
        self.usage = (20, 7)
        self.usages: list[tuple[int, int]] = []
        self.contents: list[str] = []
        self.content = "hello"
        self.status = 200
        self.delay = 0.0
        self.stream_usage = True
        self.stream_done = True
        self.embed_tokens = 15

    @property
    def bodies(self):
        return [json.loads(r.content) for r in self.reqs]

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.reqs.append(request)
        body = json.loads(request.content)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status != 200:
            return httpx.Response(self.status, json={})
        p, c = self.usages.pop(0) if self.usages else self.usage
        text = self.contents.pop(0) if self.contents else self.content
        host, path = request.url.host, request.url.path
        if host == "api.anthropic.com":
            if body.get("stream"):
                ev = [{"type": "message_start", "message": {"usage": {"input_tokens": p, "output_tokens": 1}}},
                      {"type": "content_block_delta", "delta": {"text": "he"}}]
                if self.stream_usage:
                    ev += [{"type": "message_delta", "usage": {"output_tokens": c}}, {"type": "message_stop"}]
                return httpx.Response(200, content="".join(f"data: {json.dumps(e)}\n\n" for e in ev).encode())
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": text}], "model": body["model"],
                "usage": {"input_tokens": p, "output_tokens": c}})
        if path.endswith("/embeddings"):
            return httpx.Response(200, json={
                "data": [{"embedding": [0.1]}], "model": "provider-returned-model",
                "usage": {"prompt_tokens": self.embed_tokens}})
        if body.get("stream"):
            lines = ['data: {"choices":[{"delta":{"content":"he"}}]}']
            if self.stream_usage:
                lines.append('data: ' + json.dumps({"choices": [], "usage": {"prompt_tokens": p, "completion_tokens": c}}))
            if self.stream_done:
                lines.append("data: [DONE]")
            return httpx.Response(200, content=("\n\n".join(lines) + "\n\n").encode())
        return httpx.Response(200, json={
            "choices": [{"message": {"content": text}}], "model": body["model"],
            "usage": {"prompt_tokens": p, "completion_tokens": c}})


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    transport = httpx.MockTransport(f)
    orig = httpx.AsyncClient.__init__

    def _init(self, *a, **kw):
        kw["transport"] = transport
        orig(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
    return f


@pytest.fixture
def spy(monkeypatch):
    calls = []
    real = platform_reserve.reserve

    async def wrapper(db, org_id, cred, tokens, cents, ceiling, **kw):
        calls.append(SimpleNamespace(tokens=tokens, cents=cents, ceiling=ceiling))
        return await real(db, org_id, cred, tokens, cents, ceiling, **kw)

    monkeypatch.setattr(platform_reserve, "reserve", wrapper)
    return calls


async def set_platform(db, *, enabled="on", cents="1000", models=None):
    models = models if models is not None else {
        p: [MODELS[p]] for p in HOSTS} | {"openai": ["gpt-4o-mini", "text-embedding-3-small", "text-embedding-3-large"]}
    for k, v in (("enabled", enabled), ("global_monthly_cents", cents), ("models", json.dumps(models))):
        existing = await db.scalar(select(SystemSetting).where(SystemSetting.key == f"platform_ai.{k}"))
        if existing:
            existing.value = v
        else:
            db.add(SystemSetting(key=f"platform_ai.{k}", value=v))
    await db.commit()


async def mk_org(db, *, tokens=100_000, cents=1000, name="Acme", extra=None):
    org = Organization(name=name, billing_cycle_day=1)
    db.add(org)
    await db.flush()
    limits = PlanUsageLimits().model_dump(by_alias=True)
    limits["platform_ai.tokens"] = {"period": "month", "limit": tokens}
    limits["platform_ai.cents"] = {"period": "month", "limit": cents}
    limits.update(extra or {})
    plan = Plan(slug=f"p{org.id}", name="P", features={"ai.agent": True}, usage_limits=limits)
    db.add(plan)
    await db.flush()
    db.add(Subscription(org_id=org.id, plan_id=plan.id, status=SubscriptionStatus.ACTIVE))
    await db.commit()
    return org.id


_ADAPTER_ENUM = {"openrouter": AiProvider.OPENAI_COMPATIBLE, "gemini": AiProvider.OPENAI_COMPATIBLE,
                 "openai": AiProvider.OPENAI, "anthropic": AiProvider.ANTHROPIC}


async def mk_platform(db, org_id, platform="openai", model=None, **cols):
    cred = OrgAICredential(
        org_id=org_id, provider=cols.pop("provider", _ADAPTER_ENUM[platform]),
        platform_provider=platform,
        discovered_capabilities=list(platform_ai.PLATFORM_CAPABILITIES[platform]), **cols)
    db.add(cred)
    await db.flush()
    db.add(OrgAIDefaultRouting(org_id=org_id, credential_id=cred.id, model=model or MODELS[platform]))
    await db.commit()
    return cred


async def counters(sf, org_id):
    async with sf() as s:
        rows = (await s.scalars(select(UsageCounter).where(
            UsageCounter.org_id == org_id, UsageCounter.meter.like("platform_ai.%")))).all()
        return {r.meter: r.value for r in rows}


async def spend(sf):
    async with sf() as s:
        return sum((r.cents for r in (await s.scalars(select(PlatformAISpend))).all()), 0)


async def ledger(sf):
    async with sf() as s:
        return (await s.scalars(select(AIUsageLedger).order_by(AIUsageLedger.id))).all()


async def setup(db, *, provider="openai", model=None, **org_kw):
    await set_platform(db)
    org = await mk_org(db, **org_kw)
    cred = await mk_platform(db, org, provider, model)
    return org, cred


# ---- the five entry points -------------------------------------------------

async def _chat(db, org, mt=100):
    return await ai_dispatch.call_llm(db, org_id=org, feature_key="chat",
                                      request_payload={"messages": MSGS, "max_tokens": mt})


async def _structured(db, org, mt=100):
    return await ai_dispatch.call_llm_structured(db, org_id=org, feature_key="chat", messages=MSGS,
                                                 response_schema=SCHEMA, max_tokens=mt)


async def _function(db, org, mt=100):
    return await ai_dispatch.call_llm_function(db, org_id=org, feature_key="chat", messages=MSGS,
                                               tools=TOOLS, max_tokens=mt)


async def _stream(db, org, mt=100):
    return [c async for c in ai_dispatch.call_llm_stream(
        db, org_id=org, feature_key="chat", messages=MSGS, max_tokens=mt)]


async def _embed(db, org, mt=None):
    return await ai_dispatch.call_llm_embed(db, org_id=org, feature_key="chat", texts=["hi"],
                                            model="text-embedding-3-small")


ENTRIES = {"call_llm": _chat, "structured": _structured, "embed": _embed,
           "function": _function, "stream": _stream}
ALL = pytest.mark.parametrize("entry", list(ENTRIES))


@pytest.fixture(autouse=True)
def _json_content(fake):
    fake.content = '{"a": 1}'  # valid for structured, harmless elsewhere


# ---- F-Q1 ------------------------------------------------------------------

@ALL
async def test_f_q1_default_limits_cannot_dispatch_through_a_platform_row(db, sf, fake, entry):
    org, _ = await setup(db, tokens=0, cents=0)
    with pytest.raises(AIPlanLimitReached) as ei:
        await ENTRIES[entry](db, org)
    assert ei.value.code == "plan_limit_reached" and ei.value.meter == "platform_ai.tokens"
    assert fake.reqs == [] and await counters(sf, org) == {} and await spend(sf) == 0


# ---- reserve / settle on every entry point, billing source, bound ------------

@ALL
async def test_every_entry_point_reserves_then_settles_to_actual(db, sf, fake, spy, entry):
    org, _ = await setup(db)
    await ENTRIES[entry](db, org)
    assert len(spy) == 1
    rows = await ledger(sf)
    assert [r.billing_source for r in rows] == ["platform"]
    p, c = (fake.embed_tokens, 0) if entry == "embed" else fake.usage
    model = rows[0].model
    assert await counters(sf, org) == {
        "platform_ai.tokens": p + c,
        "platform_ai.cents": estimate_cost_cents(model=model, prompt_tokens=p, completion_tokens=c)}
    assert await spend(sf) == estimate_cost_cents(model=model, prompt_tokens=p, completion_tokens=c)
    if entry != "embed":
        assert all(b["max_tokens"] == 100 for b in fake.bodies)


async def test_byok_dispatch_never_reserves_and_is_org_key(db, sf, fake, spy):
    await set_platform(db)
    org = await mk_org(db)
    cred = OrgAICredential(org_id=org, provider=AiProvider.OPENAI, encrypted_api_key=encrypt("sk-byok"))
    db.add(cred)
    await db.flush()
    db.add(OrgAIDefaultRouting(org_id=org, credential_id=cred.id, model="gpt-4o-mini"))
    await db.commit()
    await _chat(db, org, None)
    assert spy == [] and await counters(sf, org) == {}
    assert [r.billing_source for r in await ledger(sf)] == ["org_key"]
    assert fake.reqs[0].headers["authorization"] == "Bearer sk-byok"
    assert "max_tokens" not in fake.bodies[0]  # BYOK bytes unchanged


# ---- F-V1: all four providers, bound, key, host, zero usage ------------------

@pytest.mark.parametrize("provider", list(HOSTS))
async def test_f_v1_explicit_bound_key_and_host_on_every_provider(db, sf, fake, spy, provider):
    org, _ = await setup(db, provider=provider)
    await _chat(db, org, None)  # caller pins nothing -> the model's ceiling
    req = fake.reqs[0]
    assert req.url.host == HOSTS[provider]
    key = req.headers.get("x-api-key") or req.headers["authorization"].removeprefix("Bearer ")
    assert key == f"env-key-{provider}"
    ceiling = _DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL[MODELS[provider]]
    assert fake.bodies[0]["max_tokens"] == ceiling
    assert spy[0].tokens == estimate_prompt_tokens_from_messages(MSGS) + ceiling


@pytest.mark.parametrize("provider", list(HOSTS))
async def test_f_v1_zero_usage_keeps_the_reservation(db, sf, fake, spy, provider):
    org, _ = await setup(db, provider=provider)
    fake.usage = (0, 0)
    await _chat(db, org, 100)
    assert (await counters(sf, org))["platform_ai.tokens"] == spy[0].tokens
    assert (await counters(sf, org))["platform_ai.cents"] == spy[0].cents


async def test_bound_is_min_of_caller_and_ceiling(db, sf, fake):
    org, _ = await setup(db)
    await _chat(db, org, 10**6)
    assert fake.bodies[0]["max_tokens"] == 4096


# ---- F-Q7 -----------------------------------------------------------------

async def test_f_q7_a_second_dispatch_that_does_not_fit_is_refused(db, sf, fake, spy):
    org, _ = await setup(db, tokens=250)
    await _chat(db, org, 100)             # reserves 101, settles to 27
    fake.usage = (100, 100)
    await _chat(db, org, 100)             # counter 27 + 101 <= 250, settles to 227
    n = len(fake.reqs)
    with pytest.raises(AIPlanLimitReached):
        await _chat(db, org, 100)         # 227 + 101 > 250
    assert len(fake.reqs) == n and (await counters(sf, org))["platform_ai.tokens"] == 227


@pytest.mark.parametrize("status", [500, 429])
async def test_f_q7_provider_error_keeps_the_full_reservation(db, sf, fake, spy, status):
    org, _ = await setup(db)
    fake.status = status
    with pytest.raises(AIDispatchFailed):
        await _chat(db, org)
    assert (await counters(sf, org))["platform_ai.tokens"] == spy[0].tokens
    assert (await ledger(sf))[0].billing_source == "platform"


async def test_f_q7_dispatch_timeout_keeps_the_full_reservation(db, sf, fake, spy, monkeypatch):
    org, _ = await setup(db)
    monkeypatch.setattr(app_settings, "ai_dispatch_timeout_s", 0.05)
    fake.delay = 0.5
    with pytest.raises(AIDispatchFailed) as ei:
        await _chat(db, org)
    assert ei.value.code == "provider_timeout"
    assert await counters(sf, org) == {"platform_ai.tokens": spy[0].tokens, "platform_ai.cents": spy[0].cents}
    assert await spend(sf) == spy[0].cents


async def test_f_q7_pre_send_refusal_reserves_nothing(db, sf, fake, spy):
    org, _ = await setup(db)
    await set_platform(db, models={"openai": ["something-else"]})
    with pytest.raises(NativeNotAvailable):
        await _chat(db, org)
    assert spy == [] and await counters(sf, org) == {} and fake.reqs == []


async def test_f_q7_tokens_meter_full_with_cents_free_moves_neither(db, sf, fake):
    org, _ = await setup(db, tokens=50)
    with pytest.raises(AIPlanLimitReached):
        await _chat(db, org, 100)
    assert await counters(sf, org) == {} and await spend(sf) == 0


# ---- F-Q3 / F-Q4 -------------------------------------------------------------

async def test_f_q3_byok_spend_does_not_reduce_the_platform_allowance(db, sf, fake, spy):
    org, cred = await setup(db)
    for cid in (cred.id, None):
        db.add(AIUsageLedger(org_id=org, credential_id=cid, feature_key="chat", model="gpt-4o",
                             prompt_tokens=1, completion_tokens=1, total_tokens=10**6,
                             est_cost_cents=600, latency_ms=1, success=True, billing_source="org_key"))
    await db.commit()
    await _chat(db, org)
    assert len(fake.reqs) == 1


async def test_f_q4_a_huge_org_cap_does_not_lift_the_plan_limit(db, sf, fake):
    org, _ = await setup(db, tokens=100)
    db.add(OrgAIDefaultCaps(org_id=org, hard_cap_cents=10**9))
    await db.commit()
    with pytest.raises(AIPlanLimitReached):
        await _chat(db, org, 100)  # 101 > 100
    assert fake.reqs == []


# ---- F-Q8 ------------------------------------------------------------------

async def test_f_q8_projection_failure_refuses_a_platform_dispatch(db, sf, fake, monkeypatch):
    org, _ = await setup(db)

    def boom(_m):
        raise ValueError("x")

    monkeypatch.setattr(ai_dispatch, "estimate_prompt_tokens_from_messages", boom)
    with pytest.raises(AIPlatformProjectionFailed) as ei:
        await _chat(db, org)
    assert http_for_dispatch_error(ei.value).status_code == 402
    assert fake.reqs == [] and await counters(sf, org) == {}


async def test_f_q8_byok_still_pins_zero_and_dispatches(db, sf, fake, monkeypatch):
    await set_platform(db)
    org = await mk_org(db)
    cred = OrgAICredential(org_id=org, provider=AiProvider.OPENAI, encrypted_api_key=encrypt("sk-b"))
    db.add(cred)
    await db.flush()
    db.add(OrgAIDefaultRouting(org_id=org, credential_id=cred.id, model="gpt-4o-mini"))
    db.add(OrgAIDefaultCaps(org_id=org, hard_cap_cents=10**6))
    await db.commit()
    monkeypatch.setattr(ai_dispatch, "estimate_prompt_tokens_from_messages",
                        lambda _m: (_ for _ in ()).throw(ValueError("x")))
    await _chat(db, org, None)
    assert len(fake.reqs) == 1


# ---- F-N1 / F-S1 / C9 / C12 / K11 ------------------------------------------

async def test_f_s1_env_floor_off_refuses_with_setting_on(db, sf, fake, monkeypatch):
    org, _ = await setup(db)
    monkeypatch.setattr(app_settings, "ai_native_enabled", False)
    with pytest.raises(NativeNotAvailable):
        await _chat(db, org)
    assert http_for_dispatch_error(NativeNotAvailable()).detail == {"code": "ai_native_not_available"}


async def test_f_s1_setting_off_refuses_with_env_on(db, sf, fake):
    org, _ = await setup(db)
    await set_platform(db, enabled="off")
    with pytest.raises(NativeNotAvailable):
        await _chat(db, org)
    assert fake.reqs == []


async def test_f_s1_global_ceiling_zero_refuses_and_absent_is_not_unlimited(db, sf, fake):
    org, _ = await setup(db)
    for cents in ("0", "nope"):
        await set_platform(db, cents=cents)
        with pytest.raises(AIPlatformUnavailable):
            await _chat(db, org)
    assert fake.reqs == [] and await counters(sf, org) == {}


async def test_f_n1_non_allowlisted_model_is_refused(db, sf, fake):
    org, _ = await setup(db, model="gpt-4o")  # priced, not allowlisted
    with pytest.raises(NativeNotAvailable):
        await _chat(db, org)
    assert fake.reqs == []


async def test_c9_priced_at_dispatch_a_deleted_pricing_row_refuses_before_reserve(db, sf, fake, spy, monkeypatch):
    org, _ = await setup(db, provider="gemini")
    monkeypatch.delitem(MODEL_PRICING, "gemini-x")
    with pytest.raises(NativeNotAvailable):
        await _chat(db, org)
    assert spy == [] and fake.reqs == []


async def test_c9_unknown_output_ceiling_refuses(db, sf, fake, spy, monkeypatch):
    org, _ = await setup(db, provider="gemini")
    monkeypatch.delitem(_DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL, "gemini-x")
    with pytest.raises(NativeNotAvailable):
        await _chat(db, org)
    assert spy == []


async def test_c9_the_default_pricing_row_is_never_priced(db, sf, fake, spy, monkeypatch):
    monkeypatch.setitem(_DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL, "_default", 100)
    await set_platform(db, models={"gemini": ["_default"]})
    org = await mk_org(db)
    await mk_platform(db, org, "gemini", "_default")
    with pytest.raises(NativeNotAvailable):
        await _chat(db, org)
    assert spy == []


async def test_c12_a_priced_openai_model_not_vetted_for_max_tokens_is_refused(db, sf, fake, spy, monkeypatch):
    monkeypatch.setitem(MODEL_PRICING, "o3-mini", ModelPricing(1, 1))
    monkeypatch.setitem(_DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL, "o3-mini", 100)
    await set_platform(db, models={"openai": ["o3-mini"]})
    org = await mk_org(db)
    await mk_platform(db, org, "openai", "o3-mini")
    with pytest.raises(NativeNotAvailable):
        await _chat(db, org)
    assert spy == [] and fake.reqs == []


async def test_k11_key_removed_after_allowlisting_refuses_before_reserve(db, sf, fake, spy, monkeypatch):
    org, _ = await setup(db)
    monkeypatch.setattr(app_settings, "platform_ai_openai_api_key", "")
    with pytest.raises(NativeNotAvailable):
        await _chat(db, org)
    assert spy == [] and fake.reqs == []


async def test_k11_tampered_platform_row_reaches_the_preset_host_with_the_env_key(db, sf, fake):
    await set_platform(db)
    org = await mk_org(db)
    await mk_platform(db, org, "openrouter", provider=AiProvider.OLLAMA,
                      base_url="https://attacker.example", base_url_is_api_root=False,
                      encrypted_api_key=encrypt("stored-key"), encrypted_bearer_token=encrypt("stored-bearer"))
    await _chat(db, org)
    req = fake.reqs[0]
    assert (req.url.host, req.url.path) == ("openrouter.ai", "/api/v1/chat/completions")
    assert req.headers["authorization"] == "Bearer env-key-openrouter"


async def test_k11_a_byok_row_never_receives_an_env_key(db, sf, fake):
    await set_platform(db)
    org = await mk_org(db)
    cred = OrgAICredential(org_id=org, provider=AiProvider.OPENAI_COMPATIBLE, encrypted_api_key=encrypt("sk-mine"),
                           base_url="https://byok.example/v1", base_url_is_api_root=True)
    db.add(cred)
    await db.flush()
    db.add(OrgAIDefaultRouting(org_id=org, credential_id=cred.id, model="m"))
    await db.commit()
    await _chat(db, org, None)
    auth = fake.reqs[0].headers["authorization"]
    assert auth == "Bearer sk-mine" and "env-key" not in auth and fake.reqs[0].url.host == "byok.example"


# ---- F-N4: OpenRouter data-collection deny ---------------------------------

@ALL
async def test_f_n4_openrouter_bodies_carry_the_deny_preference(db, sf, fake, entry):
    if entry == "embed":
        pytest.skip("embeddings body has no provider routing")
    org, _ = await setup(db, provider="openrouter")
    await ENTRIES[entry](db, org)
    assert all(b["provider"] == {"data_collection": "deny"} for b in fake.bodies)


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
async def test_f_n4_control_other_providers_do_not_get_it(db, sf, fake, provider):
    org, _ = await setup(db, provider=provider)
    await _chat(db, org)
    assert "provider" not in fake.bodies[0]


# ---- R4: include_usage stream only --------------------------------------------

@pytest.mark.parametrize("provider", ["openrouter", "openai", "gemini"])
async def test_r4_include_usage_only_on_stream_bodies(db, sf, fake, provider):
    org, _ = await setup(db, provider=provider)
    await _chat(db, org)
    await _stream(db, org)
    plain, streamed = fake.bodies
    assert "stream_options" not in plain
    assert streamed["stream_options"] == {"include_usage": True}


# ---- K7 / K8 / K9 / R19 ----------------------------------------------------

POST_SEND = [
    AIProviderError(code="boom"), asyncio.TimeoutError(), NotImplementedError(), ValueError("parse"),
    CapabilityNotSupported(model="m"), asyncio.CancelledError(),
]


@pytest.mark.parametrize("exc", POST_SEND, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("entry", ["call_llm", "function", "structured"])
async def test_k7_every_post_send_failure_keeps_the_reservation(db, sf, fake, spy, monkeypatch, exc, entry):
    org, _ = await setup(db)
    method = {"call_llm": "chat", "function": "function_call", "structured": "chat_structured"}[entry]

    class A:
        async def __getattr__(self, n):  # pragma: no cover
            raise AttributeError

    async def raiser(**kw):
        raise exc

    adapter = SimpleNamespace(**{method: raiser})
    monkeypatch.setattr(platform_ai, "build_adapter", lambda _p: adapter)
    with pytest.raises(BaseException):
        await ENTRIES[entry](db, org)
    c = await counters(sf, org)
    assert c["platform_ai.tokens"] == spy[0].tokens and c["platform_ai.cents"] == spy[0].cents


@pytest.mark.parametrize("usage", [(10, 0), (0, 10), (0, 0)])
@pytest.mark.parametrize("entry", ["call_llm", "function", "stream"])
async def test_k8_incomplete_usage_keeps_the_reservation(db, sf, fake, spy, entry, usage):
    org, _ = await setup(db)
    fake.usage = usage
    await ENTRIES[entry](db, org)
    assert (await counters(sf, org))["platform_ai.tokens"] == spy[0].tokens


@ALL
async def test_k9_ledger_commit_failure_after_settle_leaves_actual(db, sf, fake, spy, monkeypatch, entry):
    org, _ = await setup(db)

    async def bad(*a, **kw):
        raise OperationalError("x", {}, Exception("ledger down"))

    monkeypatch.setattr(ai_dispatch, "_write_ledger_row", bad)
    with pytest.raises(OperationalError):
        await ENTRIES[entry](db, org)
    p, c = (fake.embed_tokens, 0) if entry == "embed" else fake.usage
    assert (await counters(sf, org))["platform_ai.tokens"] == p + c


@ALL
async def test_r19_settle_failure_still_returns_writes_ledger_and_keeps_reservation(db, sf, fake, spy, monkeypatch, entry):
    org, _ = await setup(db)

    async def bad(db_, handle, tokens, cents):
        raise OperationalError("x", {}, Exception("settle down"))

    monkeypatch.setattr(platform_reserve, "settle", bad)
    result = await ENTRIES[entry](db, org)
    assert result is not None
    assert len(await ledger(sf)) == 1
    assert (await counters(sf, org))["platform_ai.tokens"] == spy[0].tokens


# ---- K10 / R9 ----------------------------------------------------------------

async def test_k10_structured_bound_on_all_attempts_triple_reservation_summed_settle(db, sf, fake, spy):
    org, _ = await setup(db)
    fake.contents = ["nope", "{}", '{"a": 1}']
    fake.usages = [(10, 5), (11, 6), (12, 7)]
    await _structured(db, org, 100)
    assert [b["max_tokens"] for b in fake.bodies] == [100, 100, 100]
    one = estimate_prompt_tokens_from_messages(MSGS + [{"role": "user", "content": json.dumps(SCHEMA)}]) + 100
    assert spy[0].tokens == 3 * one
    assert (await counters(sf, org))["platform_ai.tokens"] == 15 + 17 + 19


async def test_k10_one_incomplete_attempt_keeps_the_reservation(db, sf, fake, spy):
    org, _ = await setup(db)
    fake.contents = ["nope", '{"a": 1}']
    fake.usages = [(10, 5), (11, 0)]
    await _structured(db, org, 100)
    assert (await counters(sf, org))["platform_ai.tokens"] == spy[0].tokens


async def test_k10_exhausted_attempts_settle_to_the_summed_actual(db, sf, fake, spy):
    org, _ = await setup(db)
    fake.contents = ["x", "y", "z"]
    fake.usages = [(10, 5)] * 3
    with pytest.raises(ai_dispatch.StructuredOutputError):
        await _structured(db, org, 100)
    assert (await counters(sf, org))["platform_ai.tokens"] == 45


async def test_r9_platform_structured_projection_includes_the_schema(db, sf, fake, spy):
    org, _ = await setup(db)
    big = {"type": "object", "required": ["a"], "properties": {"a": {"description": "x" * 3500}}}
    await ai_dispatch.call_llm_structured(db, org_id=org, feature_key="chat", messages=MSGS,
                                          response_schema=big, max_tokens=100)
    assert spy[0].tokens >= 3 * (1000 + 100)


# ---- R6 / K12 embed ------------------------------------------------------------

async def test_r6_platform_embed_prices_and_ledgers_the_model_sent(db, sf, fake, spy):
    org, _ = await setup(db)
    await _embed(db, org)
    rows = await ledger(sf)
    assert rows[0].model == "text-embedding-3-small"  # not the provider-returned name
    assert rows[0].est_cost_cents == estimate_cost_cents(
        model="text-embedding-3-small", prompt_tokens=15, completion_tokens=0)


async def test_k12_embed_override_is_projected_on_the_model_sent(db, sf, fake, spy):
    org, _ = await setup(db, tokens=10**7)
    big = ["x" * 3_500_000]
    await ai_dispatch.call_llm_embed(db, org_id=org, feature_key="chat", texts=big,
                                     model="text-embedding-3-large")
    assert spy[0].cents == estimate_cost_cents(
        model="text-embedding-3-large", prompt_tokens=1_000_000, completion_tokens=0)
    assert fake.bodies[0]["model"] == "text-embedding-3-large"


async def test_k12_non_allowlisted_embed_override_is_refused(db, sf, fake, spy):
    org, _ = await setup(db)
    await set_platform(db, models={"openai": ["gpt-4o-mini", "text-embedding-3-small"]})
    with pytest.raises(NativeNotAvailable):
        await ai_dispatch.call_llm_embed(db, org_id=org, feature_key="chat", texts=["hi"],
                                         model="text-embedding-3-large")
    assert spy == [] and fake.reqs == []


async def test_anthropic_platform_row_cannot_embed(db, sf, fake, spy):
    org, _ = await setup(db, provider="anthropic")
    with pytest.raises(AICapabilityNotSupported):
        await ai_dispatch.call_llm_embed(db, org_id=org, feature_key="chat", texts=["hi"])
    assert spy == []


# ---- stream: usage_final, finally settle, R10 ---------------------------------

async def test_stream_without_a_final_usage_chunk_keeps_the_reservation(db, sf, fake, spy):
    org, _ = await setup(db)
    fake.stream_usage = False
    await _stream(db, org)
    assert (await counters(sf, org))["platform_ai.tokens"] == spy[0].tokens


@pytest.mark.parametrize("provider", ["openrouter", "openai", "gemini"])
async def test_r5_openai_shape_usage_without_done_is_not_final(db, sf, fake, spy, provider):
    org, _ = await setup(db, provider=provider)
    fake.stream_done = False  # connection dropped after the usage chunk
    await _stream(db, org)
    assert (await counters(sf, org))["platform_ai.tokens"] == spy[0].tokens


async def test_anthropic_stream_message_start_alone_is_not_final(db, sf, fake, spy):
    org, _ = await setup(db, provider="anthropic")
    fake.stream_usage = False  # message_start reports output_tokens=1, nothing after
    await _stream(db, org)
    assert (await counters(sf, org))["platform_ai.tokens"] == spy[0].tokens


async def test_stream_consumer_closing_after_done_still_settles_once(db, sf, fake, spy):
    org, _ = await setup(db)
    gen = ai_dispatch.call_llm_stream(db, org_id=org, feature_key="chat", messages=MSGS, max_tokens=100)
    async for chunk in gen:
        if chunk.done:
            break
    await gen.aclose()
    assert (await counters(sf, org))["platform_ai.tokens"] == 27


async def test_r10_finally_settle_survives_cancellation_during_aclose(db, sf, fake, spy, monkeypatch):
    org, _ = await setup(db)
    real, finished = platform_reserve.settle, asyncio.Event()

    async def slow(db_, handle, tokens, cents):
        await asyncio.sleep(0.1)
        await real(db_, handle, tokens, cents)
        finished.set()

    monkeypatch.setattr(platform_reserve, "settle", slow)
    gen = ai_dispatch.call_llm_stream(db, org_id=org, feature_key="chat", messages=MSGS, max_tokens=100)
    async for chunk in gen:
        if chunk.done:
            break
    task = asyncio.ensure_future(gen.aclose())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(finished.wait(), 2)
    assert (await counters(sf, org))["platform_ai.tokens"] == 27


async def test_stream_finally_db_error_is_swallowed(db, sf, fake, spy, monkeypatch):
    org, _ = await setup(db)

    async def bad(*a, **k):
        raise OperationalError("x", {}, Exception("down"))

    monkeypatch.setattr(platform_reserve, "settle", bad)
    gen = ai_dispatch.call_llm_stream(db, org_id=org, feature_key="chat", messages=MSGS, max_tokens=100)
    async for chunk in gen:
        if chunk.done:
            break
    await gen.aclose()  # must not raise
    assert (await counters(sf, org))["platform_ai.tokens"] == spy[0].tokens


# ---- K1 / R7 / R15 ------------------------------------------------------------

async def test_k1_prepare_dispatch_is_pure_and_returns_no_platform_adapter(db, sf, fake):
    org, _ = await setup(db)
    prepared = await ai_dispatch._prepare_dispatch(
        db, org_id=org, feature_key="chat", capability="function_call", messages=MSGS, max_tokens=None)
    assert prepared.adapter is None and prepared.platform is not None
    assert await counters(sf, org) == {} and await spend(sf) == 0


class _Redis:
    async def set(self, *a, **k):
        return True

    async def eval(self, *a, **k):
        return 1


@pytest.fixture
def chat_env(monkeypatch):
    monkeypatch.setattr(agent_chat.redis_client, "get_client", lambda: _Redis())


async def test_r7_r15_preflight_on_an_exhausted_platform_org_is_402_with_detail(db, sf, fake, chat_env):
    org, _ = await setup(db, tokens=10, extra={"assistant.turns": {"period": "day", "limit": 5}})
    user = SimpleNamespace(org_id=org)
    with pytest.raises(HTTPException) as ei:
        await agent_chat.preflight(db, user, MSGS, TOOLS)
    assert ei.value.status_code == 402
    d = ei.value.detail
    assert (d["code"], d["meter"], d["limit"], d["period"]) == ("plan_limit_reached", "platform_ai.tokens", 10, "month")
    assert d["resets_at"]
    async with sf() as s:  # refused before assistant.turns was admitted, nothing platform moved
        turns = await s.scalar(select(UsageCounter).where(UsageCounter.meter == "assistant.turns"))
    assert turns is None and await counters(sf, org) == {} and await spend(sf) == 0


async def test_r7_preflight_global_ceiling_exhausted_is_402_platform_ai_unavailable(db, sf, fake, chat_env):
    org, _ = await setup(db, extra={"assistant.turns": {"period": "day", "limit": 5}})
    db.add(PlatformAISpend(period_start=platform_reserve.period_start("month", platform_reserve.utcnow_naive()),
                           cents=1000))
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await agent_chat.preflight(db, SimpleNamespace(org_id=org), MSGS, TOOLS)
    assert (ei.value.status_code, ei.value.detail["code"]) == (402, "platform_ai_unavailable")


@pytest.mark.parametrize("exc", [
    AIPlanLimitReached(PlanLimitReached("platform_ai.tokens", 1, "month", None)),
    AIPlatformUnavailable(), AIPlatformProjectionFailed(),
], ids=lambda e: e.code)
async def test_r15_every_dispatch_error_mapper_gives_402(exc, monkeypatch):
    assert http_for_dispatch_error(exc).status_code == 402
    assert http_for_dispatch_error(exc).detail["code"] == exc.code
    from app.routers import ai_categorize as r

    async def raiser(*a, **k):
        raise exc

    monkeypatch.setattr(r.ai_categorize_service, "suggest_category", raiser)
    monkeypatch.setattr(r, "get_client_ip", lambda _r: "127.0.0.1")
    with pytest.raises(HTTPException) as ei:
        await r.categorize_transaction(
            SimpleNamespace(transaction_id=1), None, None, None,
            SimpleNamespace(org_id=1, id=1, email="a@b.c"), {})
    assert ei.value.status_code == 402 and ei.value.detail["code"] == exc.code


async def test_round_n_refusal_in_the_stream_loop_is_its_own_code_not_internal_error(db, sf, fake, monkeypatch):
    org, _ = await setup(db, tokens=10)

    async def run():
        out = []
        async for ev in agent_chat.stream_turn(lambda: db, 1, org, "n", MSGS, TOOLS):
            out.append(ev)
        return "".join(out)

    monkeypatch.setattr(agent_chat, "release_lock", lambda *a, **k: asyncio.sleep(0))
    body = await run()
    assert '"code":"plan_limit_reached"' in body and "internal_error" not in body


# ---- parsed checks (C3 / K7 / K12) ------------------------------------------

def _tree(name):
    return ast.parse((APP / name).read_text())


def test_k12_enforce_cap_is_called_exactly_once_and_call_llm_goes_through_prepare():
    tree = _tree("services/ai_dispatch.py")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_enforce_cap"]
    assert len(calls) == 1
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llm")
    names = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_prepare_dispatch" in names and "_resolve_caps_and_cost" not in names


def test_k7_only_settle_can_decrement_a_counter():
    tree = _tree("services/platform_reserve.py")
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))):
        subs = [n for n in ast.walk(fn) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub)]
        assert not subs or fn.name == "settle", fn.name
    d = _tree("services/ai_dispatch.py")
    used = {n.id for n in ast.walk(d) if isinstance(n, ast.Name)} | {
        a.name for n in ast.walk(d) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not used & {"UsageCounter", "PlatformAISpend"}
