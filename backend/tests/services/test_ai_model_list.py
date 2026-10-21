"""TBD-608: ``ai_routing_service.list_models`` and the platform pieces it shares with dispatch."""
from __future__ import annotations

import pytest

from app.config import settings as app_settings
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.org_ai_routing import ROUTABLE_FEATURE_NAMES
from app.models.platform_ai_model import PlatformAIModel
from app.services import ai_dispatch, ai_routing_service, platform_ai
from app.services.ai_pricing import MODEL_PRICING, ModelPricing
from app.services.ai_providers import NativeNotAvailable
from tests.factories.ai import AI_ALL

# Fixtures and helpers of the platform dispatch suite.
from tests.services.test_ai_dispatch_platform import (  # noqa: F401
    Fake, _env, _json_content, fake, db, sf, counters, mk_model, mk_org, mk_platform,
    set_platform, setup, spy, MODEL, MSGS, SCHEMA, TOOLS,
)

ALL_FEATURES = {k: True for k in AI_ALL}
CAPS = ["chat", "function_call", "structured_output"]


async def own(db, models, caps=None):
    org_id = await mk_org(db)
    cred = OrgAICredential(org_id=org_id, provider=AiProvider.OPENAI, discovered_models=models,
                           discovered_capabilities=caps if caps is not None else ["chat", "function_call"])
    db.add(cred)
    await db.commit()
    return cred


def ids(lst):
    return [o.id for o in lst.options]


async def platform_cred(db, org, model=MODEL):
    return await mk_platform(db, org, model)


# ---- S1-S3: own keys ---------------------------------------------------------

async def test_s1_unknown_model_is_unpriced_and_never_the_default(db):
    lst = await ai_routing_service.list_models(db, await own(db, ["mystery-1"]), None)
    (o,) = lst.options
    assert (o.id, o.tier, o.prompt_micros_per_mtok, o.completion_micros_per_mtok) == ("mystery-1", None, None, None)
    assert lst.default is None


async def test_s2a_family_filter_id_filter_and_order(db):
    found = ["vendor/x:free", "text-embedding-3-small", "whisper-1", "gpt-4o", "gpt-4o-mini",
             "mystery-1", "_default", "a" * 121, 42]
    lst = await ai_routing_service.list_models(db, await own(db, found), None)
    assert ids(lst) == ["gpt-4o-mini", "gpt-4o", "mystery-1", "vendor/x:free"]
    assert lst.default == "gpt-4o-mini"


async def test_s2b_order_is_the_three_to_one_blend(db, monkeypatch):
    monkeypatch.setitem(MODEL_PRICING, "t/c", ModelPricing(5, 320))
    found = ["gemini-3.5-flash-lite", "gpt-5.6-luna", "t/c"]
    lst = await ai_routing_service.list_models(db, await own(db, found), None)
    assert ids(lst) == ["gpt-5.6-luna", "t/c", "gemini-3.5-flash-lite"]
    assert lst.default == "gpt-5.6-luna"


async def test_s3_own_key_capability_follows_the_feature(db):
    cred = await own(db, ["gpt-4o"], caps=["chat", "function_call"])
    assert ids(await ai_routing_service.list_models(db, cred, "smart_budget")) == []
    assert (await ai_routing_service.list_models(db, cred, "smart_budget")).default is None
    assert ids(await ai_routing_service.list_models(db, cred, "chat")) == ["gpt-4o"]
    assert ids(await ai_routing_service.list_models(db, cred, None)) == ["gpt-4o"]


# ---- S4-S5: the free-model rules ---------------------------------------------

async def test_s4_a_free_row_is_listed_but_never_the_default_or_a_fallback(db):
    org, cred = await setup(db)
    await mk_model(db, "x/free", 0, 0)
    lst = await ai_routing_service.list_models(db, cred, None)
    assert lst.default == MODEL and "x/free" in ids(lst)
    pk = await platform_ai.pick(db, "function_call", None, frontier=False)
    assert pk.model == MODEL and pk.models == [MODEL]


async def test_s5_only_free_rows_have_no_default_but_a_routed_free_row_is_honoured(db):
    await set_platform(db)
    await mk_model(db, "x/free", 0, 0)
    await mk_model(db, "y/free", 0, 0)
    org = await mk_org(db)
    cred = await platform_cred(db, org, "x/free")
    assert (await ai_routing_service.list_models(db, cred, None)).default is None
    assert await platform_ai.pick(db, "function_call", None, frontier=False) is None
    pk = await platform_ai.pick(db, "function_call", "x/free", frontier=False)
    assert pk.model == "x/free" and pk.models == ["x/free", "y/free"]


# ---- S6-S7: frontier is Pro only ---------------------------------------------

async def test_s6_frontier_is_listed_and_picked_for_pro_only(db):
    await set_platform(db)
    await mk_model(db, MODEL, 100_000, 400_000)
    await mk_model(db, "vendor/frontier", 10_000_000, 40_000_000)
    plain = await platform_cred(db, await mk_org(db, name="Plain"))
    pro = await platform_cred(db, await mk_org(db, name="Pro", slug="pro"))
    assert "vendor/frontier" not in ids(await ai_routing_service.list_models(db, plain, None))
    assert (await platform_ai.pick(db, "function_call", "vendor/frontier", frontier=False)).model == MODEL
    assert "vendor/frontier" in ids(await ai_routing_service.list_models(db, pro, None))
    assert (await platform_ai.pick(db, "function_call", "vendor/frontier", frontier=True)).model == "vendor/frontier"


async def test_s7_dispatch_never_runs_a_frontier_model_for_a_non_pro_org(db, sf, fake, spy):
    await set_platform(db)
    await mk_model(db, "vendor/frontier", 10_000_000, 40_000_000)
    org = await mk_org(db)
    cred = await mk_platform(db, org, "vendor/frontier")
    lst = await ai_routing_service.list_models(db, cred, None)
    assert (ids(lst), lst.default) == ([], None)
    with pytest.raises(NativeNotAvailable) as ei:
        await ai_dispatch.call_llm_function(db, org_id=org, feature_key="chat", messages=MSGS,
                                            tools=TOOLS, max_tokens=100)
    assert ei.value.code == "platform_model_not_allowed"
    assert fake.reqs == [] and await counters(sf, org) == {}


# ---- S8-S9: background features always run the default -----------------------

async def test_s8_only_chat_honours_the_routed_model(db, sf, fake):
    await set_platform(db)
    await mk_model(db, MODEL, 100_000, 400_000)
    await mk_model(db, "vendor/model-b", 150_000, 600_000)
    org = await mk_org(db, slug="pro", features=ALL_FEATURES)
    cred = await mk_platform(db, org, "vendor/model-b")
    await ai_dispatch.call_llm_structured(db, org_id=org, feature_key="smart_budget", messages=MSGS,
                                          response_schema=SCHEMA, max_tokens=100)
    assert fake.bodies[-1]["model"] == MODEL
    await ai_dispatch.call_llm_function(db, org_id=org, feature_key="chat", messages=MSGS,
                                        tools=TOOLS, max_tokens=100)
    assert fake.bodies[-1]["model"] == "vendor/model-b"
    assert ids(await ai_routing_service.list_models(db, cred, "smart_budget")) == [MODEL]


async def test_s9_every_features_default_is_what_its_dispatch_sends(db, sf, fake):
    await set_platform(db)
    await mk_model(db, "A", 100_000, 400_000, structured=False)
    await mk_model(db, "B", 100_000, 500_000)
    await mk_model(db, "C", 1_000_000, 4_000_000)
    org = await mk_org(db, slug="pro", features=ALL_FEATURES)
    cred = await mk_platform(db, org, "gone/m")
    seen = {}
    for feature in sorted(ROUTABLE_FEATURE_NAMES):
        if feature == "chat":
            await ai_dispatch.call_llm_function(db, org_id=org, feature_key=feature, messages=MSGS,
                                                tools=TOOLS, max_tokens=100)
        else:
            await ai_dispatch.call_llm_structured(db, org_id=org, feature_key=feature, messages=MSGS,
                                                  response_schema=SCHEMA, max_tokens=100)
        seen[feature] = (fake.bodies[-1]["model"],
                         (await ai_routing_service.list_models(db, cred, feature)).default)
    assert all(sent == default for sent, default in seen.values()), seen
    assert seen["chat"][0] == "A" and seen["smart_plan"][0] == "B"


# ---- S10-S12 -----------------------------------------------------------------

@pytest.mark.parametrize("off", ["env", "setting", "key"])
async def test_s10_switch_off_lists_nothing(db, monkeypatch, off):
    org, cred = await setup(db)
    if off == "env":
        monkeypatch.setattr(app_settings, "ai_native_enabled", False)
    elif off == "setting":
        await set_platform(db, enabled="off")
    else:
        monkeypatch.setattr(app_settings, "platform_ai_openrouter_api_key", "")
    lst = await ai_routing_service.list_models(db, cred, None)
    assert (ids(lst), lst.default) == ([], None)


def test_s11_replies_left():
    f = platform_ai.replies_left
    assert f(800_000, 5_580_000, 100_000, 400_000) == 300
    assert f(800_000, 5_580_000, 150_000, 600_000) == 261
    assert f(10**9, 18_599, 0, 0) == 0
    assert f(-5, 100_000, 100_000, 400_000) == 0


async def test_s12_an_unstorable_platform_id_is_neither_listed_nor_picked(db):
    org, cred = await setup(db)
    await mk_model(db, "v/" + "x" * 119, 1, 1)
    row = await db.get(PlatformAIModel, "v/" + "x" * 119)
    assert platform_ai.can(row, "function_call") is False
    assert ids(await ai_routing_service.list_models(db, cred, None)) == [MODEL]
    assert (await platform_ai.pick(db, "function_call", None, frontier=False)).model == MODEL
