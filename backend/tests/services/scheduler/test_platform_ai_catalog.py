"""TBD-631: the daily OpenRouter sync into ``platform_ai_models``.

``plan_sync`` is pure and carries most fences; the DB-backed fences drive
``run_platform_ai_catalog_sync`` over in-memory SQLite. No test reaches the
network: feeds are fixtures, ``fetch_feeds`` runs over ``httpx.MockTransport``.
"""
from __future__ import annotations

import asyncio
import datetime
import functools
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models import Base
from app.models.platform_ai_model import PlatformAIModel
from app.services.scheduler.jobs import platform_ai_catalog as job
from app.services.scheduler.jobs.platform_ai_catalog import (
    CatalogFeedError,
    fetch_feeds,
    offerable,
    parse_price,
    plan_sync,
    run_platform_ai_catalog_sync,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "openrouter"
TODAY = datetime.date(2026, 10, 15)
NOW = datetime.datetime(2026, 10, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
NAIVE_NOW = NOW.replace(tzinfo=None)


# ── builders ─────────────────────────────────────────────────────────────────


def model(mid, *, ctx=100_000, name=None, prompt="0.0000001", completion="0.0000005", exp=None):
    return {
        "id": mid,
        "name": name if name is not None else f"Name {mid}",
        "context_length": ctx,
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": ["tools"],
        "expiration_date": exp,
    }


def ep(mid, prompt="0.0000001", completion="0.0000005", *, tools=True, structured=False,
       ctx=100_000, mct=8192, discount=None, overrides=None):
    pricing = {"prompt": prompt, "completion": completion}
    if discount is not None:
        pricing["discount"] = discount
    if overrides is not None:
        pricing["overrides"] = overrides
    params = (["tools"] if tools else []) + (["structured_outputs"] if structured else [])
    return {
        "model_id": mid, "context_length": ctx, "max_completion_tokens": mct,
        "pricing": pricing, "supported_parameters": params, "tag": "x", "status": 0,
    }


def mfeed(*models):
    return {"data": list(models), "total_count": len(models), "links": {"next": None}}


def zfeed(*eps):
    return {"data": list(eps)}


def row(mid, *, enabled=False, zp=100_000, zc=500_000, mct=8192, ctx=100_000,
        expires_at=None, seen=NAIVE_NOW - datetime.timedelta(hours=30)):
    return PlatformAIModel(
        openrouter_id=mid, name=f"Name {mid}", context_length=ctx, max_completion_tokens=mct,
        supports_structured_outputs=False, headline_prompt_micros_per_mtok=100_000,
        headline_completion_micros_per_mtok=500_000, zdr_prompt_micros_per_mtok=zp,
        zdr_completion_micros_per_mtok=zc, expires_at=expires_at, last_seen_at=seen, enabled=enabled,
    )


def healthy(mid, **kw):
    """A model plus one ZDR tool endpoint, so it is neither vanished nor no_zdr."""
    return model(mid), ep(mid, **kw)


def feeds(*mids):
    ms, es = zip(*[healthy(m) for m in mids])
    return mfeed(*ms), zfeed(*es)


def plan(existing, mf, zf, today=TODAY):
    return plan_sync({r.openrouter_id: r for r in existing}, mf, zf, today)


def fx(name):
    return json.loads((FIXTURES / name).read_text())


# ── 12: parse_price ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,want", [
    ("0.000000025", 25_000), ("0.000000000061", 61), ("0.000000000031", 31),
    ("0.0000000000615", 62),
    ("-1", None), ("NaN", None), ("Infinity", None), ("abc", None), (None, None),
    ("0.011", None), ([], None),
])
def test_parse_price(raw, want):
    assert parse_price(raw) == want


# ── 1-4: plan rules ──────────────────────────────────────────────────────────


def test_enabled_row_absent_from_feed_is_disabled_vanished():
    mf, zf = feeds("a/keep")
    p = plan([row("a/keep", enabled=True), row("a/gone", enabled=True), row("a/gone2", enabled=True),
              row("a/k3", enabled=True)], *feeds("a/keep", "a/k3"))
    assert ("a/gone", "vanished") in p.disables
    assert not p.refused  # 2 of 4 is not a majority


def test_new_model_inserted_disabled_and_existing_disabled_stays():
    mf, zf = feeds("a/new", "a/old")
    p = plan([row("a/old", enabled=False)], mf, zf)
    assert "a/new" in p.upserts and "a/old" in p.upserts
    assert all("enabled" not in d for d in p.upserts.values())
    assert p.disables == []


def test_vanished_then_reappeared_never_re_enabled():
    mf, zf = feeds("a/back")
    p = plan([row("a/back", enabled=False)], mf, zf)
    assert "enabled" not in p.upserts["a/back"] and p.disables == []


def test_price_change_updates_all_four_price_columns_not_enabled():
    mf = mfeed(model("a/m", prompt="0.0000002", completion="0.000001"))
    zf = zfeed(ep("a/m", prompt="0.0000003", completion="0.0000015"))
    p = plan([row("a/m", enabled=True)], mf, zf)
    u = p.upserts["a/m"]
    assert (u["headline_prompt_micros_per_mtok"], u["headline_completion_micros_per_mtok"]) == (200_000, 1_000_000)
    assert (u["zdr_prompt_micros_per_mtok"], u["zdr_completion_micros_per_mtok"]) == (300_000, 1_500_000)
    assert "enabled" not in u


# ── 6-11: per-model facts ────────────────────────────────────────────────────


def test_no_zdr_model_has_null_ceilings_never_offerable_and_disables():
    mf = mfeed(model("a/nz"), model("a/ok"))
    zf = zfeed(ep("a/ok"))
    p = plan([row("a/nz", enabled=True)], mf, zf)
    u = p.upserts["a/nz"]
    assert u["zdr_prompt_micros_per_mtok"] is None and u["zdr_completion_micros_per_mtok"] is None
    assert u["max_completion_tokens"] is None
    assert u["headline_prompt_micros_per_mtok"] == 100_000  # headline still stored
    assert p.disables == [("a/nz", "no_zdr")]
    r = PlatformAIModel(openrouter_id="a/nz", enabled=True, last_seen_at=NAIVE_NOW,
                        **{k: v for k, v in u.items()})
    assert offerable(r, NAIVE_NOW) is False


def test_offerable_requires_everything():
    good = row("a/g", enabled=True)
    assert offerable(good, NAIVE_NOW) is True
    assert offerable(row("a/g", enabled=False), NAIVE_NOW) is False
    assert offerable(row("a/g", enabled=True, mct=None), NAIVE_NOW) is False
    assert offerable(row("a/g", enabled=True, seen=NAIVE_NOW - datetime.timedelta(hours=49)), NAIVE_NOW) is False
    assert offerable(row("a/g", enabled=True, expires_at=TODAY + datetime.timedelta(days=14)), NAIVE_NOW) is False


def test_zdr_endpoints_without_tools_count_as_no_zdr():
    p = plan([], mfeed(model("a/m")), zfeed(ep("a/m", tools=False)))
    assert p.upserts["a/m"]["zdr_prompt_micros_per_mtok"] is None


def test_ceiling_is_max_over_zdr_tool_endpoints():
    zf = zfeed(ep("a/m", prompt="0.0000001"), ep("a/m", prompt="0.00000011"), ep("a/m", prompt="0.00000005"))
    u = plan([], mfeed(model("a/m")), zf).upserts["a/m"]
    assert u["zdr_prompt_micros_per_mtok"] == 110_000


def test_every_override_raises_the_ceiling_live_haiku_shape():
    p = plan([], fx("models.json"), fx("zdr.json"))
    u = p.upserts["anthropic/claude-haiku-5.5"]
    assert (u["zdr_prompt_micros_per_mtok"], u["zdr_completion_micros_per_mtok"]) == (550_000, 2_750_000)
    zf = zfeed(ep("a/m", overrides=[{"utc_start": "00:00", "prompt": "0.0000009", "completion": "0.000004"}]))
    u = plan([], mfeed(model("a/m")), zf).upserts["a/m"]
    assert (u["zdr_prompt_micros_per_mtok"], u["zdr_completion_micros_per_mtok"]) == (900_000, 4_000_000)


def test_discount_is_grossed_up():
    def ceil(prompt, discount, overrides=None):
        zf = zfeed(ep("a/m", prompt=prompt, discount=discount, overrides=overrides))
        return plan([], mfeed(model("a/m")), zf).upserts["a/m"]["zdr_prompt_micros_per_mtok"]
    assert ceil("0.000000075", 0.75) == 300_000
    assert ceil("0.00000075", 0.5) == 1_500_000
    assert ceil("0.0000001", 0.5, [{"min_prompt_tokens": 1, "prompt": "0.0000003", "completion": "0.000001"}]) == 600_000
    for bad in (1, 1.2, -0.1, "NaN", "x"):
        assert ceil("0.0000001", bad) is None
    assert ceil("0.000000465", 0.07) == 500_000  # float division says 500_001
    assert ceil("0.0000001", 0.99999999999999) is None  # cap applies AFTER the gross-up


def test_max_completion_tokens_and_context_are_min_over_zdr_tool_endpoints():
    p = plan([], fx("models.json"), fx("zdr.json"))
    assert p.upserts["meta-llama/llama-3.3-70b-instruct"]["max_completion_tokens"] == 2048
    zf = zfeed(ep("a/m", mct=128000, ctx=50_000), ep("a/m", mct=2048, ctx=70_000))
    u = plan([], mfeed(model("a/m", ctx=999)), zf).upserts["a/m"]
    assert (u["max_completion_tokens"], u["context_length"]) == (2048, 50_000)


def test_structured_outputs_any_zdr_tool_endpoint():
    zf = zfeed(ep("a/m"), ep("a/m", structured=True))
    assert plan([], mfeed(model("a/m")), zf).upserts["a/m"]["supports_structured_outputs"] is True


# ── 13-14: item parsing and expiry ───────────────────────────────────────────


def test_bad_items_are_skipped_the_rest_applies_and_bad_expiry_fails_closed():
    ms = [model("a/z", ctx=0), model("a/s", ctx="8k"), model("a/n", name="x" * 256),
          model("a/soon", exp="soon"), model("a/good", prompt="0.0000002")]
    zf = zfeed(*[ep(m["id"]) for m in ms])
    existing = [row(i, enabled=True) for i in ("a/z", "a/soon", "a/good", "a/g2", "a/g3")]
    ms2 = ms + [model("a/g2"), model("a/g3")]
    zf = zfeed(*[ep(m["id"]) for m in ms2])
    p = plan(existing, mfeed(*ms2), zf)
    assert "a/z" not in p.upserts and "a/s" not in p.upserts and "a/n" not in p.upserts
    assert p.upserts["a/good"]["headline_prompt_micros_per_mtok"] == 200_000
    assert p.upserts["a/soon"]["expires_at"] == TODAY
    assert ("a/z", "vanished") in p.disables and ("a/soon", "expiring") in p.disables
    assert not p.refused


def test_expiry_boundary_is_today_plus_14():
    ms = [model("a/in14", exp=str(TODAY + datetime.timedelta(days=14))),
          model("a/in15", exp=str(TODAY + datetime.timedelta(days=15)))]
    p = plan([row("a/in14", enabled=True), row("a/in15", enabled=True)],
             mfeed(*ms), zfeed(ep("a/in14"), ep("a/in15")))
    assert p.disables == [("a/in14", "expiring")]


# ── 15-16: refusal and ceiling drops ─────────────────────────────────────────


def test_mass_disable_refusal_rules():
    three = [row(f"a/{i}", enabled=True) for i in range(3)]
    assert plan(three, *feeds("a/0")).refused is True  # 2 of 3 vanish
    two = [row("a/0", enabled=True), row("a/1", enabled=True)]
    assert plan(two, *feeds("a/0")).refused is False  # 1 of 2
    one = [row("a/0", enabled=True)]
    assert plan(one, mfeed(), zfeed()).refused is False  # enabled_count < 2
    exp = str(TODAY + datetime.timedelta(days=1))
    ms = [model(f"a/{i}", exp=exp if i < 2 else None) for i in range(3)]
    p = plan(three, mfeed(*ms), zfeed(*[ep(m["id"]) for m in ms]))
    assert p.refused is False and len(p.disables) == 2


def test_ceiling_drop_alerts_and_applies_rise_and_disabled_do_not():
    mf = mfeed(model("a/en"), model("a/dis"), model("a/rise"))
    zf = zfeed(ep("a/en", prompt="0.0000001"), ep("a/dis", prompt="0.0000001"), ep("a/rise", prompt="0.0000003"))
    existing = [row("a/en", enabled=True, zp=200_000), row("a/dis", enabled=False, zp=200_000),
                row("a/rise", enabled=True, zp=200_000)]
    p = plan(existing, mf, zf)
    assert not p.refused
    assert [d["openrouter_id"] for d in p.ceiling_drops] == ["a/en"]
    d = p.ceiling_drops[0]
    assert d["old"] == (200_000, 500_000) and d["new"] == (100_000, 500_000)
    assert p.upserts["a/en"]["zdr_prompt_micros_per_mtok"] == 100_000


# ── DB-backed ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def sf() -> AsyncIterator:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _ai_on(monkeypatch):
    monkeypatch.setattr(settings, "ai_native_enabled", True)


@pytest.fixture
def audits(monkeypatch):
    calls: list[dict] = []

    async def spy(session_factory, **kw):
        calls.append(kw)

    monkeypatch.setattr(job.audit_service, "record_audit_event", spy)
    return calls


def fake(mf, zf):
    calls = {"n": 0}

    async def _fetch():
        calls["n"] += 1
        return mf, zf

    _fetch.calls = calls
    return _fetch


async def seed(sf, *rows):
    async with sf() as db:
        db.add_all(rows)
        await db.commit()


async def snapshot(sf):
    async with sf() as db:
        rs = (await db.execute(select(PlatformAIModel).order_by(PlatformAIModel.openrouter_id))).scalars().all()
        return {r.openrouter_id: {c.name: getattr(r, c.name) for c in PlatformAIModel.__table__.columns} for r in rs}


async def test_db_new_model_inserted_disabled(sf, audits):
    out = await run_platform_ai_catalog_sync(sf, fetch=fake(*feeds("a/new")), now=NOW)
    assert out == "synced"
    assert (await snapshot(sf))["a/new"]["enabled"] is False


async def test_db_enabled_survives_a_sync_that_changes_prices(sf, audits):
    await seed(sf, row("a/m", enabled=True, zp=1, zc=1))
    mf, zf = mfeed(model("a/m", prompt="0.0000002")), zfeed(ep("a/m", prompt="0.0000003"))
    assert await run_platform_ai_catalog_sync(sf, fetch=fake(mf, zf), now=NOW) == "synced"
    r = (await snapshot(sf))["a/m"]
    assert r["enabled"] is True and r["zdr_prompt_micros_per_mtok"] == 300_000


async def test_db_disabled_row_stays_disabled_when_present_and_after_returning(sf, audits):
    await seed(sf, row("a/m", enabled=False))
    assert await run_platform_ai_catalog_sync(sf, fetch=fake(*feeds("a/m")), now=NOW) == "synced"
    assert (await snapshot(sf))["a/m"]["enabled"] is False


async def test_db_refused_plan_writes_nothing_and_audits_once(sf, audits):
    await seed(sf, *[row(f"a/{i}", enabled=True, zp=1, zc=1) for i in range(3)])
    before = await snapshot(sf)
    mf, zf = mfeed(model("a/0", prompt="0.0000009")), zfeed(ep("a/0", prompt="0.0000009"))
    assert await run_platform_ai_catalog_sync(sf, fetch=fake(mf, zf), now=NOW) == "refused"
    assert await snapshot(sf) == before
    assert [c["event_type"] for c in audits] == ["platform_ai.catalog.sync_refused"]
    assert audits[0]["outcome"] == "failure"


# 17: the REAL fetch_feeds over a MockTransport


def transport(models_resp, zdr_resp):
    def handler(request: httpx.Request) -> httpx.Response:
        resp = zdr_resp if request.url.path.endswith("/endpoints/zdr") else models_resp
        if isinstance(resp, Exception):
            raise resp
        return resp
    return httpx.MockTransport(handler)


def ok_models():
    return httpx.Response(200, json=fx("models.json"))


def ok_zdr():
    return httpx.Response(200, json=fx("zdr.json"))


def _mod(**over):
    d = fx("models.json")
    d.update(over)
    return httpx.Response(200, json=d)


BAD_FETCHES = {
    "total_count_mismatch": (_mod(total_count=99), ok_zdr()),
    "links_next": (_mod(links={"next": "https://openrouter.ai/api/v1/models?page=2"}), ok_zdr()),
    "models_empty": (_mod(data=[]), ok_zdr()),
    "zdr_empty": (ok_models(), httpx.Response(200, json={"data": []})),
    "http_500": (httpx.Response(500, json=fx("models.json")), ok_zdr()),
    "zdr_500": (ok_models(), httpx.Response(500, json=fx("zdr.json"))),
    "not_json": (httpx.Response(200, text="<html>"), ok_zdr()),
    "connect_error": (httpx.ConnectError("boom"), ok_zdr()),
}


@pytest.mark.parametrize("case", sorted(BAD_FETCHES))
async def test_real_fetch_failures_change_nothing(sf, audits, case):
    await seed(sf, row("a/m", enabled=True, zp=1, zc=1))
    before = await snapshot(sf)
    fetch = functools.partial(fetch_feeds, transport=transport(*BAD_FETCHES[case]))
    assert await run_platform_ai_catalog_sync(sf, fetch=fetch, now=NOW) == "fetch_failed"
    assert await snapshot(sf) == before
    assert audits == []


async def test_real_fetch_happy_path_syncs_fixtures(sf, audits):
    fetch = functools.partial(fetch_feeds, transport=transport(ok_models(), ok_zdr()))
    assert await run_platform_ai_catalog_sync(sf, fetch=fetch, now=NOW) == "synced"
    assert len(await snapshot(sf)) == 5


# 18


async def test_fetch_urls_no_auth_and_overall_deadline(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return ok_zdr() if request.url.path.endswith("/zdr") else ok_models()

    await fetch_feeds(transport=httpx.MockTransport(handler))
    assert sorted(str(r.url) for r in seen) == [
        "https://openrouter.ai/api/v1/endpoints/zdr",
        "https://openrouter.ai/api/v1/models?supported_parameters=tools",
    ]
    assert all("authorization" not in r.headers for r in seen)

    async def slow(request):
        await asyncio.sleep(1)
        return ok_models()

    monkeypatch.setattr(job, "FETCH_DEADLINE_SECONDS", 0.05)
    with pytest.raises(TimeoutError):
        await fetch_feeds(transport=httpx.MockTransport(slow))


async def test_fetch_feeds_error_type_is_exported():
    assert issubclass(CatalogFeedError, Exception)


# 19-20


async def test_ai_off_means_no_fetch(sf, audits, monkeypatch):
    monkeypatch.setattr(settings, "ai_native_enabled", False)
    f = fake(*feeds("a/m"))
    assert await run_platform_ai_catalog_sync(sf, fetch=f, now=NOW) == "off"
    assert f.calls["n"] == 0


async def test_due_window_and_hourly_backoff(sf, audits):
    from tests.conftest import expire_lease

    await seed(sf, row("a/m", seen=NAIVE_NOW - datetime.timedelta(hours=1)))
    f = fake(*feeds("a/m"))
    assert await run_platform_ai_catalog_sync(sf, fetch=f, now=NOW) == "fresh"
    assert f.calls["n"] == 0

    async with sf() as db:
        r = await db.get(PlatformAIModel, "a/m")
        r.last_seen_at = NAIVE_NOW - datetime.timedelta(hours=23, minutes=30)
        await db.commit()

    async def boom():
        raise httpx.ConnectError("x")

    assert await run_platform_ai_catalog_sync(sf, fetch=boom, now=NOW) == "fetch_failed"
    assert await run_platform_ai_catalog_sync(sf, fetch=f, now=NOW) == "backoff"
    assert f.calls["n"] == 0
    expire_lease("platform_ai_catalog:attempt")
    assert await run_platform_ai_catalog_sync(sf, fetch=f, now=NOW) == "synced"


# 21


async def test_disable_audits_after_commit_per_model(sf, monkeypatch):
    await seed(sf, *[row(f"a/{i}", enabled=True) for i in range(4)])
    seen: list[tuple[dict, bool]] = []

    async def spy(session_factory, **kw):
        async with sf() as db:
            r = await db.get(PlatformAIModel, kw["detail"]["openrouter_id"])
            seen.append((kw, r.enabled))

    monkeypatch.setattr(job.audit_service, "record_audit_event", spy)
    # a/0 and a/1 stay; a/2 vanishes (1 of 4 absent: no refusal)
    mf, zf = feeds("a/0", "a/1", "a/3")
    assert await run_platform_ai_catalog_sync(sf, fetch=fake(mf, zf), now=NOW) == "synced"
    assert len(seen) == 1
    kw, enabled_after = seen[0]
    assert kw["event_type"] == "platform_ai.catalog.model_disabled"
    assert kw["detail"] == {"openrouter_id": "a/2", "reason": "vanished"}
    assert kw["target_org_id"] is None and kw["actor_email"] == "system" and kw["outcome"] == "success"
    assert enabled_after is False  # the row was committed before the audit


async def test_failed_commit_means_no_audit_and_not_synced(sf, audits):
    await seed(sf, *[row(f"a/{i}", enabled=True) for i in range(4)])

    def bad_factory():
        s = sf()

        async def boom():
            raise RuntimeError("commit failed")

        s.commit = boom
        return s

    mf, zf = feeds("a/0", "a/1", "a/3")
    out = await run_platform_ai_catalog_sync(bad_factory, fetch=fake(mf, zf), now=NOW)
    assert out != "synced" and audits == []


async def test_ceiling_drop_is_audited_after_commit(sf, audits):
    await seed(sf, row("a/m", enabled=True, zp=900_000))
    assert await run_platform_ai_catalog_sync(sf, fetch=fake(*feeds("a/m")), now=NOW) == "synced"
    assert [c["event_type"] for c in audits] == ["platform_ai.catalog.ceiling_dropped"]
    assert audits[0]["detail"]["old"] == [900_000, 500_000]


# 22-23


async def test_tz_aware_now_is_stored_naive_utc(sf, audits):
    tz2 = datetime.timezone(datetime.timedelta(hours=2))
    now = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=tz2)
    assert await run_platform_ai_catalog_sync(sf, fetch=fake(*feeds("a/m")), now=now) == "synced"
    assert (await snapshot(sf))["a/m"]["last_seen_at"] == datetime.datetime(2026, 10, 15, 10, 0)


async def test_job_swallows_errors(sf, audits, monkeypatch):
    def explode(*a, **k):
        raise ValueError("secret detail")

    monkeypatch.setattr(job, "plan_sync", explode)
    out = await run_platform_ai_catalog_sync(sf, fetch=fake(*feeds("a/m")), now=NOW)
    assert out == "error"
