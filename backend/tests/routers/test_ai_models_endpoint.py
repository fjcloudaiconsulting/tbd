"""TBD-608: ``GET /{credential_id}/models`` and the routing-write gate."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app._time import utcnow_naive
from app.models.org_ai_routing import OrgAIDefaultRouting, OrgAIFeatureRouting
from app.models.usage_counter import UsageCounter
from app.models.user import Role, User
from app.services import feature_service
from app.services.ai_providers import ValidateResult
from app.services.usage_service import period_start

# Fixtures and helpers of the platform-credentials suite.
from tests.routers.test_ai_platform_credentials import (  # noqa: F401
    BASE, MODEL, _env, audits, client, mk_cred, mk_org, model_row, org, set_platform, sf,
)

CHAT_CAPS = ["chat", "function_call"]
S2A = ["vendor/x:free", "text-embedding-3-small", "whisper-1", "gpt-4o", "gpt-4o-mini",
       "mystery-1", "_default", "a" * 121, 42]


def put_default(c, cred, model):
    return c.put(f"{BASE}/routing/default", json={"credential_id": cred, "model": model})


def put_feature(c, cred, feature, model):
    return c.put(f"{BASE}/routing/features/{feature}", json={"credential_id": cred, "model": model})


def code(r):
    return r.json()["detail"]["code"]


async def rows(sf):
    async with sf() as s:
        return ((await s.scalars(select(OrgAIDefaultRouting))).all(),
                (await s.scalars(select(OrgAIFeatureRouting))).all())


async def audit_count(sf):
    return len([a for a in await audits(sf) if a.event_type.startswith("ai.routing.")])


async def add_models(sf, *models):
    async with sf() as s:
        s.add_all(models)
        await s.commit()


async def usage(sf, org_id, *, micros, tokens):
    async with sf() as s:
        for meter, value in (("platform_ai.micros", micros), ("platform_ai.tokens", tokens)):
            s.add(UsageCounter(org_id=org_id, meter=meter, period="month",
                               period_start=period_start("month", utcnow_naive()), value=value))
        await s.commit()


# ---- R1, G1, G2 ----------------------------------------------------------------

async def test_r1_another_orgs_credential_is_404(sf, org):
    other, _ = await mk_org(sf, "Other")
    theirs = await mk_cred(sf, other, None, discovered_models=["gpt-4o"])
    mine = await mk_cred(sf, org[0], None, discovered_models=["gpt-4o"])
    c = client(sf, org[1])
    assert c.get(f"{BASE}/{mine}/models").status_code == 200  # positive control
    assert c.get(f"{BASE}/{theirs}/models").status_code == 404


async def test_g1_member_is_403(sf, org):
    cred = await mk_cred(sf, org[0], None, discovered_models=["gpt-4o"])
    async with sf() as s:
        u = User(org_id=org[0], username="m", email="m@x.io", password_hash="h", role=Role.MEMBER, is_active=True)
        s.add(u)
        await s.commit()
        mid = u.id
    assert client(sf, mid).get(f"{BASE}/{cred}/models").status_code == 403


async def test_g2_unknown_feature_is_400(sf, org):
    cred = await mk_cred(sf, org[0], None, discovered_models=["gpt-4o"])
    r = client(sf, org[1]).get(f"{BASE}/{cred}/models?feature=bogus")
    assert r.status_code == 400 and code(r) == "unknown_feature"


# ---- R2-R4: own-key writes ---------------------------------------------------------

async def test_r2_own_key_writes_are_gated_by_the_list(sf, org):
    cred = await mk_cred(sf, org[0], None, discovered_models=["gpt-4o"], discovered_capabilities=CHAT_CAPS)
    c = client(sf, org[1])
    assert put_default(c, cred, "gpt-4o").status_code == 200
    assert put_feature(c, cred, "chat", "gpt-4o").status_code == 200
    before = (await rows(sf), await audit_count(sf))
    for r in (put_default(c, cred, "gpt-4o-mini"), put_feature(c, cred, "chat", "gpt-4o-mini")):
        assert r.status_code == 400 and code(r) == "model_not_listed", r.text
    after = (await rows(sf), await audit_count(sf))
    assert [len(x) for x in after[0]] == [len(x) for x in before[0]] and after[1] == before[1]
    assert (await rows(sf))[0][0].model == "gpt-4o"


async def test_r3_an_own_key_with_no_discovery_lists_nothing(sf, org):
    for found in (None, []):
        cred = await mk_cred(sf, org[0], None, discovered_models=found)
        r = put_default(client(sf, org[1]), cred, "gpt-4o")
        assert r.status_code == 400 and code(r) == "model_not_listed", (found, r.text)


async def test_r4_what_the_list_shows_the_write_accepts(sf, org):
    cred = await mk_cred(sf, org[0], None, discovered_models=S2A, discovered_capabilities=CHAT_CAPS)
    c = client(sf, org[1])
    listed = [m["id"] for m in c.get(f"{BASE}/{cred}/models").json()["models"]]
    assert listed == ["gpt-4o-mini", "gpt-4o", "mystery-1", "vendor/x:free"]
    for m in listed:
        assert put_default(c, cred, m).status_code == 200, m
    for m in ("text-embedding-3-small", "whisper-1", "_default"):
        r = put_default(c, cred, m)
        assert r.status_code == 400 and code(r) == "model_not_listed", m
    assert put_default(c, cred, "a" * 121).status_code == 422


# ---- R5-R6: platform writes ----------------------------------------------------------

async def test_r5_platform_background_features_accept_only_the_default(sf, org):
    await add_models(sf, model_row("vendor/model-b", zp=150_000, zc=600_000))
    pid = await mk_cred(sf, org[0], "openrouter")
    c = client(sf, org[1])
    r = put_feature(c, pid, "smart_budget", "vendor/model-b")
    assert r.status_code == 400 and code(r) == "model_not_listed"
    assert put_feature(c, pid, "smart_budget", MODEL).status_code == 200
    await add_models(sf, model_row("off/m", enabled=False))
    r = put_default(c, pid, "off/m")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "model_not_listed"


async def test_r6_frontier_is_pro_only_and_never_gates_own_keys(sf, org):
    await add_models(sf, model_row("vendor/frontier", zp=10_000_000, zc=40_000_000))
    pro, pro_user = await mk_org(sf, "ProCo", slug="pro")
    plain = await mk_cred(sf, org[0], "openrouter")
    pro_cred = await mk_cred(sf, pro, "openrouter")
    r = put_feature(client(sf, org[1]), plain, "chat", "vendor/frontier")
    assert r.status_code == 400 and code(r) == "model_not_listed"
    assert put_feature(client(sf, pro_user), pro_cred, "chat", "vendor/frontier").status_code == 200
    byok = await mk_cred(sf, org[0], None, discovered_models=["claude-fable-5-1"], discovered_capabilities=CHAT_CAPS)
    assert put_feature(client(sf, org[1]), byok, "chat", "claude-fable-5-1").status_code == 200


# ---- R7: stale rows are never rewritten ---------------------------------------------------

async def test_r7_validate_and_rotate_never_rewrite_a_stale_routing_row(sf, org):
    c = client(sf, org[1])
    first = AsyncMock(return_value=ValidateResult(ok=True, discovered_models=["gpt-4o"],
                                                  discovered_capabilities=CHAT_CAPS))
    later = AsyncMock(return_value=ValidateResult(ok=True, discovered_models=["gpt-4o-mini"],
                                                  discovered_capabilities=CHAT_CAPS))
    target = "app.services.ai_credential_service.get_adapter"
    with patch(target, return_value=type("A", (), {"validate": first})()):
        cred = c.post(BASE, json={"provider": "openai", "api_key": "sk-test-r7-aaaa"}).json()["id"]
    assert put_default(c, cred, "gpt-4o").status_code == 200
    assert put_feature(c, cred, "chat", "gpt-4o").status_code == 200

    async def snap():
        d, f = await rows(sf)
        return [(r.model, r.credential_id, r.updated_at) for r in (*d, *f)]

    before = await snap()
    with patch(target, return_value=type("A", (), {"validate": later})()):
        assert c.post(f"{BASE}/{cred}/validate").status_code == 200
        assert c.post(f"{BASE}/{cred}/rotate", json={"api_key": "sk-test-r7-bbbb"}).status_code == 200
    assert await snap() == before
    assert [m["id"] for m in c.get(f"{BASE}/{cred}/models").json()["models"]] == ["gpt-4o-mini"]


# ---- R8, R10: replies_left ------------------------------------------------------------------

async def test_r8_replies_left_is_the_tighter_of_both_meters(sf):
    await set_platform(sf)
    await add_models(sf, model_row("vendor/model-b", zp=150_000, zc=600_000))
    oid, uid = await mk_org(sf, "Acme2", micros=1_000_000, tokens=6_000_000)
    await usage(sf, oid, micros=200_000, tokens=420_000)
    pid = await mk_cred(sf, oid, "openrouter")
    c = client(sf, uid)
    for url in (f"{BASE}/{pid}/models?feature=chat", f"{BASE}/{pid}/models"):
        body = c.get(url).json()
        assert {m["id"]: m["replies_left"] for m in body["models"]} == {MODEL: 300, "vendor/model-b": 261}, body
        assert body["replies_period"] == "month"
    body = c.get(f"{BASE}/{pid}/models?feature=smart_budget").json()
    assert body["replies_period"] is None and [m["replies_left"] for m in body["models"]] == [None]
    byok = await mk_cred(sf, oid, None, discovered_models=["gpt-4o"], discovered_capabilities=CHAT_CAPS)
    body = c.get(f"{BASE}/{byok}/models").json()
    assert body["replies_period"] is None and [m["replies_left"] for m in body["models"]] == [None]
    closed, cuid = await mk_org(sf, "Closed", micros=0)
    cpid = await mk_cred(sf, closed, "openrouter")
    body = client(sf, cuid).get(f"{BASE}/{cpid}/models?feature=chat").json()
    assert body["replies_period"] is None and {m["replies_left"] for m in body["models"]} == {0}


async def test_r10_a_daily_meter_makes_the_period_day(sf):
    await set_platform(sf)
    oid, uid = await mk_org(sf, "Daily", micros=1_000_000, tokens=6_000_000, tokens_period="day")
    pid = await mk_cred(sf, oid, "openrouter")
    body = client(sf, uid).get(f"{BASE}/{pid}/models?feature=chat").json()
    assert body["replies_period"] == "day"
    assert {m["id"]: m["replies_left"] for m in body["models"]} == {MODEL: 322}


# ---- R9: unreadable entitlements --------------------------------------------------------------

async def test_r9_bad_entitlement_data_is_402_for_platform_and_irrelevant_to_own_keys(sf, org, monkeypatch):
    pid = await mk_cred(sf, org[0], "openrouter")
    byok = await mk_cred(sf, org[0], None, discovered_models=["gpt-4o"], discovered_capabilities=CHAT_CAPS)

    async def boom(*a, **k):
        raise KeyError("x")

    monkeypatch.setattr(feature_service, "get_entitlements", boom)
    c = client(sf, org[1])
    r = c.get(f"{BASE}/{pid}/models")
    assert r.status_code == 402 and r.json()["detail"] == {"code": "platform_ai_unavailable"}
    assert c.get(f"{BASE}/{byok}/models").status_code == 200


# ---- G3, G4 ---------------------------------------------------------------------------------------

async def test_g3_shape_and_types(sf, org):
    await add_models(sf, model_row("a/same", zp=150_000, zc=600_000), model_row("b/same", zp=150_000, zc=600_000))
    pid = await mk_cred(sf, org[0], "openrouter")
    body = client(sf, org[1]).get(f"{BASE}/{pid}/models").json()
    assert set(body) == {"default", "replies_period", "models"} and body["default"] == MODEL
    assert [m["id"] for m in body["models"]] == [MODEL, "a/same", "b/same"]
    first = body["models"][0]
    assert set(first) == {"id", "label", "hint", "tier", "prompt_micros_per_mtok",
                          "completion_micros_per_mtok", "replies_left"}
    assert first["label"] == MODEL and first["hint"] is None and first["tier"] == "budget"
    assert (first["prompt_micros_per_mtok"], first["completion_micros_per_mtok"]) == (100_000, 400_000)
    assert isinstance(first["replies_left"], int)


async def test_g4_a_pat_can_read_the_list(sf, org):
    cred = await mk_cred(sf, org[0], None, discovered_models=["gpt-4o"], discovered_capabilities=CHAT_CAPS)
    assert client(sf, org[1], auth="pat").get(f"{BASE}/{cred}/models").status_code == 200
