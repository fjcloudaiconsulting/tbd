"""TBD-633: the daily OpenRouter spend reconciliation and the spike alarm.

DB-backed over in-memory SQLite; the network is a fake ``fetch`` or ``httpx.MockTransport``. The
event names and key sets asserted here are the contract with the infra session's Grafana rules.
"""
from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models import Base
from app.models.ai_usage_ledger import AIUsageLedger
from app.models.system_setting import SystemSetting
from app.models.user import Organization
from app.services.scheduler.jobs import platform_ai_spend_watch as job
from app.services.scheduler.jobs.platform_ai_spend_watch import (
    fetch_key_usage,
    run_platform_ai_spend_watch,
)
from tests.conftest import expire_lease

KEY = "sk-or-TBD633-canary"
FP = hashlib.sha256(KEY.encode()).hexdigest()[:16]
NOW = dt.datetime(2026, 10, 20, 12, 0, 0)
H = dt.timedelta(hours=1)
SNAP, SPIKE = "platform_ai_spend.snapshot", "platform_ai_spend.spike_day"
HEAD = {"ts", "level", "logger", "msg"}


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
def _on(monkeypatch):
    monkeypatch.setattr(settings, "ai_native_enabled", True)
    monkeypatch.setattr(settings, "platform_ai_openrouter_api_key", KEY)
    expire_lease("platform_ai_spend:attempt")


# ── helpers ──────────────────────────────────────────────────────────────────


async def put(sf, key, value):
    async with sf() as db:
        db.add(SystemSetting(key=key, value=value))
        await db.commit()


async def get(sf, key):
    async with sf() as db:
        return await db.scalar(select(SystemSetting.value).where(SystemSetting.key == key))


async def snap(sf, usage, at, fp=FP):
    await put(sf, SNAP, json.dumps({"usage_micros": usage, "at": at.isoformat(), "key_fp": fp}))


async def orgs(sf, n=1):
    async with sf() as db:
        rows = [Organization(name=f"o{i}", billing_cycle_day=1) for i in range(n)]
        db.add_all(rows)
        await db.commit()
        return [r.id for r in rows]


async def led(sf, org_id, micros, at, *, source="platform", success=True):
    async with sf() as db:
        db.add(AIUsageLedger(
            org_id=org_id, credential_id=None, feature_key="chat", model="m", prompt_tokens=1,
            completion_tokens=1, total_tokens=2, cost_micros=micros, latency_ms=1, success=success,
            billing_source=source, dispatched_at=at))
        await db.commit()


def fetch_of(micros):
    calls = {"n": 0}

    async def _f():
        calls["n"] += 1
        return {"usage": micros / 1_000_000}

    _f.calls = calls
    return _f


def named(log_lines, name):
    return [e for e in log_lines() if e["msg"] == name]


def keys(e):
    return set(e) - HEAD


async def drift_case(sf, log_lines, or_micros, ledger_micros):
    (o,) = await orgs(sf)
    await snap(sf, 1_000_000, NOW - 24 * H)
    await led(sf, o, ledger_micros, NOW - 12 * H)
    out = await run_platform_ai_spend_watch(sf, fetch=fetch_of(1_000_000 + or_micros), now=NOW)
    return out, named(log_lines, "platform_ai_spend.drift")


# ── F1 / F2 / F7: the drift rule ─────────────────────────────────────────────


@pytest.mark.parametrize("or_micros,ledger_micros,drift", [
    (10_000_000, 9_700_000, True),     # 3% above the floor
    (10_000_000, 9_900_000, False),    # 1%
    (5_000_000, 4_900_000, False),     # exactly 2% (gap = floor): strictly greater
    (4_900_000, 5_000_000, False),     # ledger above OpenRouter, 2.0% of the max
    (4_850_000, 5_000_000, True),      # ledger above, 3.0% of the max
    (1_100_000, 1_000_000, True),      # gap == floor, far above 2%
    (1_000, 2_000, False),             # 2x but under the floor
    (1_000_000, 0, True),              # ledger 0, OpenRouter shows $1: a leaked key
    (0, 1_000_000, True),
])
async def test_f1_drift_rule(sf, log_lines, or_micros, ledger_micros, drift):
    """FENCE F1/F2/F7. Kills a wrong threshold, `>=`, always/never alert, dividing by or_delta
    instead of the max, a missing floor, and skipping when the ledger is 0."""
    out, events = await drift_case(sf, log_lines, or_micros, ledger_micros)
    assert (out, bool(events)) == (("drift" if drift else "reconciled"), drift)
    (hb,) = named(log_lines, "platform_ai_spend.reconciled")
    assert hb["drift"] is drift and hb["openrouter_micros"] == or_micros and hb["ledger_micros"] == ledger_micros


async def test_idle_day_heartbeats_with_zero_gap(sf, log_lines):
    out, events = await drift_case(sf, log_lines, 0, 0)
    (hb,) = named(log_lines, "platform_ai_spend.reconciled")
    assert (out, events, hb["gap_bp"], hb["drift"]) == ("reconciled", [], 0, False)


async def test_gap_is_signed_and_basis_points(sf, log_lines):
    await drift_case(sf, log_lines, 10_000_000, 9_700_000)
    (hb,) = named(log_lines, "platform_ai_spend.reconciled")
    assert (hb["gap_micros"], hb["gap_bp"], hb["window_s"]) == (300_000, 300, 24 * 3600)


# ── F3: which ledger rows count ──────────────────────────────────────────────


async def test_f3_only_platform_rows_count_and_failures_do(sf, log_lines):
    """FENCE F3. Kills a missing billing_source filter and a `success == True` filter."""
    (o,) = await orgs(sf)
    await snap(sf, 0, NOW - 24 * H)
    await led(sf, o, 200_000, NOW - 12 * H)
    await led(sf, o, 300_000, NOW - 11 * H, success=False)
    await led(sf, o, 9_000_000, NOW - 10 * H, source="org_key")
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(500_000), now=NOW) == "reconciled"
    (hb,) = named(log_lines, "platform_ai_spend.reconciled")
    assert hb["ledger_micros"] == 500_000


# ── F4: the window ───────────────────────────────────────────────────────────


async def test_f4_window_is_open_closed_on_whole_seconds(sf, log_lines):
    """FENCE F4. Kills open/closed window errors and all-time sums."""
    (o,) = await orgs(sf)
    prev = NOW - 24 * H
    await snap(sf, 0, prev)
    s = dt.timedelta(seconds=1)
    for micros, at in ((1_000_000, prev), (2_000_000, prev + s), (4_000_000, NOW), (8_000_000, NOW + s),
                       (16_000_000, prev - 5 * H)):
        await led(sf, o, micros, at)
    await run_platform_ai_spend_watch(sf, fetch=fetch_of(6_000_000), now=NOW)
    (hb,) = named(log_lines, "platform_ai_spend.reconciled")
    assert hb["ledger_micros"] == 6_000_000 and hb["drift"] is False


async def test_f4_now_is_floored_to_the_second(sf, log_lines):
    (o,) = await orgs(sf)
    await snap(sf, 0, NOW - 24 * H)
    await led(sf, o, 4_000_000, NOW + dt.timedelta(milliseconds=500))
    await run_platform_ai_spend_watch(sf, fetch=fetch_of(0), now=NOW + dt.timedelta(milliseconds=700))
    (hb,) = named(log_lines, "platform_ai_spend.reconciled")
    assert hb["ledger_micros"] == 0


# ── F5: fetch failures ───────────────────────────────────────────────────────


def _raises(exc):
    async def _f():
        raise exc
    return _f


def _returns(v):
    async def _f():
        return v
    return _f


BAD = {
    "raises": _raises(RuntimeError("x")),
    "list": _returns([]),
    "no_usage": _returns({}),
    "none": _returns({"usage": None}),
    "bool": _returns({"usage": True}),
    "string": _returns({"usage": "abc"}),
    "negative": _returns({"usage": -1}),
    "nan": _returns({"usage": float("nan")}),
    "over_cap": _returns({"usage": 1e10}),
}


@pytest.mark.parametrize("case", sorted(BAD))
async def test_f5_a_bad_fetch_changes_nothing_and_the_next_success_covers_the_gap(sf, log_lines, case):
    """FENCE F5. Kills silently advancing the snapshot (a lost day) and a swallowed outcome."""
    (o,) = await orgs(sf)
    await snap(sf, 1_000_000, NOW - 24 * H)
    before = await get(sf, SNAP)
    assert await run_platform_ai_spend_watch(sf, fetch=BAD[case], now=NOW) == "fetch_failed"
    assert await get(sf, SNAP) == before
    (f,) = named(log_lines, "platform_ai_spend.reconcile_failed")
    assert f["hours_since_success"] == 24.0 and "error" in f
    expire_lease("platform_ai_spend:attempt")
    await led(sf, o, 500_000, NOW - H)
    later = NOW + dt.timedelta(minutes=30)
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(1_500_000), now=later) == "reconciled"
    (hb,) = named(log_lines, "platform_ai_spend.reconciled")
    assert hb["window_s"] == int((24.5 * H).total_seconds()) and hb["ledger_micros"] == 500_000


async def test_failures_back_off_for_an_hour(sf):
    await snap(sf, 1_000_000, NOW - 24 * H)
    assert await run_platform_ai_spend_watch(sf, fetch=BAD["raises"], now=NOW) == "fetch_failed"
    f = fetch_of(1_000_000)
    assert await run_platform_ai_spend_watch(sf, fetch=f, now=NOW) == "backoff"
    assert f.calls["n"] == 0


async def test_f5_a_non_200_carries_its_status_never_the_body(sf, log_lines):
    await snap(sf, 1_000_000, NOW - 24 * H)
    fetch = functools.partial(fetch_key_usage, transport=httpx.MockTransport(lambda r: httpx.Response(500, text="secret-body")))
    assert await run_platform_ai_spend_watch(sf, fetch=fetch, now=NOW) == "fetch_failed"
    (f,) = named(log_lines, "platform_ai_spend.reconcile_failed")
    assert f["status"] == 500 and "secret-body" not in json.dumps(f)


async def test_f5_a_lifetime_counter_of_twenty_thousand_dollars_parses(sf, log_lines):
    """Kills running the lifetime counter through the per-call cap."""
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(20_000_000_000), now=NOW) == "baselined"
    assert json.loads(await get(sf, SNAP))["usage_micros"] == 20_000_000_000
    (hb,) = named(log_lines, "platform_ai_spend.reconciled")
    assert hb["baseline"] is True


# ── F6: rebaseline ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("fp,usage,reason", [
    ("0" * 16, 9_000_000, "key_changed"),
    (FP, 4_000_000, "usage_decreased"),
])
async def test_f6_rebaseline_does_not_compare(sf, log_lines, fp, usage, reason):
    """FENCE F6. Kills reporting a negative delta as a 100% gap."""
    (o,) = await orgs(sf)
    await snap(sf, 5_000_000, NOW - 24 * H, fp=fp)
    await led(sf, o, 3_000_000, NOW - 12 * H)
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(usage), now=NOW) == "rebaselined"
    (r,) = named(log_lines, "platform_ai_spend.rebaselined")
    assert r["level"] == "WARNING" and r["reason"] == reason
    assert not named(log_lines, "platform_ai_spend.drift") and not named(log_lines, "platform_ai_spend.reconciled")
    assert json.loads(await get(sf, SNAP)) == {"usage_micros": usage, "at": NOW.isoformat(), "key_fp": FP}


async def test_a_tz_aware_snapshot_keeps_its_window(sf):
    """Kills letting an aware `at` reach the naive subtraction (TypeError -> "error"), and
    dropping it (a rebaseline hides that window's drift)."""
    await put(sf, SNAP, json.dumps({"usage_micros": 0, "at": "2026-10-19T14:00:00+02:00", "key_fp": FP}))
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(1_000_000), now=NOW) == "drift"


async def test_a_malformed_snapshot_is_treated_as_absent(sf):
    await put(sf, SNAP, "{not json")
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(1_000_000), now=NOW) == "baselined"


# ── gate and cadence (G1, G2) ────────────────────────────────────────────────


@pytest.mark.parametrize("flag,key", [(False, KEY), (True, "")])
async def test_g1_off_makes_no_call_and_no_write(sf, monkeypatch, flag, key):
    monkeypatch.setattr(settings, "ai_native_enabled", flag)
    monkeypatch.setattr(settings, "platform_ai_openrouter_api_key", key)
    f = fetch_of(1)
    assert await run_platform_ai_spend_watch(sf, fetch=f, now=NOW) == "off"
    assert f.calls["n"] == 0
    async with sf() as db:
        assert (await db.scalars(select(SystemSetting))).all() == []


async def test_g2_fresh_below_23h_due_at_23h(sf):
    await snap(sf, 1_000_000, NOW - (23 * H - dt.timedelta(seconds=1)))
    f = fetch_of(1_000_000)
    assert await run_platform_ai_spend_watch(sf, fetch=f, now=NOW) == "fresh" and f.calls["n"] == 0
    async with sf() as db:
        row = await db.scalar(select(SystemSetting).where(SystemSetting.key == SNAP))
        row.value = json.dumps({"usage_micros": 1_000_000, "at": (NOW - 23 * H).isoformat(), "key_fp": FP})
        await db.commit()
    assert await run_platform_ai_spend_watch(sf, fetch=f, now=NOW) == "reconciled"


async def test_the_job_never_raises(sf):
    class Boom:
        def __call__(self):
            raise RuntimeError("no db")

    assert await run_platform_ai_spend_watch(Boom(), fetch=fetch_of(1), now=NOW) == "error"


# ── F10 / F11 / G6: spikes ───────────────────────────────────────────────────

DAY = dt.datetime(2026, 10, 19, 12, 0)
WINDOW = dt.datetime(2026, 10, 15, 12, 0)


async def spikes(sf, log_lines, *, day, window):
    """day / window: sequences of micros, one entry per org. The reconcile stays out (fresh)."""
    ids = await orgs(sf, len(day))
    await snap(sf, 1, NOW - H)
    for o, d, w in zip(ids, day, window):
        if d:
            await led(sf, o, d, DAY)
        if w:
            await led(sf, o, w, WINDOW)
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(1), now=NOW) == "fresh"
    return ids, named(log_lines, "platform_ai_spend.spike")


@pytest.mark.parametrize("day,window,fires", [
    (4_000_000, 7_000_000, True),      # 4x the mean, above the floor
    (2_000_000, 7_000_000, False),     # 2x
    (3_000_000, 7_000_000, False),     # exactly 3x: strictly greater
    (3_000_002, 7_000_006, False),     # under 3x only by integer math (kills a truncated mean)
    (500_000, 350_000, False),         # 10x but under the floor
    (1_500_000, 0, True),              # a new org
    (1_000_000, 1_000_000, True),      # exactly the floor
    (999_999, 0, False),
])
async def test_f10_org_spike_rule(sf, log_lines, day, window, fires):
    """FENCE F10. Kills a bare ratio, no floor, ZeroDivision, `>` on the floor."""
    ids, evs = await spikes(sf, log_lines, day=[day], window=[window])
    org_evs = [e for e in evs if e["scope"] == "org"]
    assert bool(org_evs) is fires
    if fires:
        (e,) = org_evs
        assert e["level"] == "ERROR" and e["org_id"] == ids[0] and e["day"] == "2026-10-19"
        assert e["day_micros"] == day and e["baseline_micros"] == window // 7
        assert e.get("ratio_x100") == (day * 700 // window if window else None)


async def test_f10_global_sums_the_ledger_not_the_org_verdicts(sf, log_lines):
    """Kills deriving the global verdict from per-org verdicts."""
    _, evs = await spikes(sf, log_lines, day=[600_000] * 10, window=[0] * 10)
    assert [e["scope"] for e in evs] == ["global"]
    assert evs[0]["day_micros"] == 6_000_000 and "org_id" not in evs[0] and "ratio_x100" not in evs[0]


async def test_f10_global_floor_is_not_the_org_floor(sf, log_lines):
    """Kills ORG_FLOOR used for global: $3 is above the org floor, under the global one."""
    _, evs = await spikes(sf, log_lines, day=[3_000_000], window=[0])
    assert [e["scope"] for e in evs] == ["org"]


async def test_f11_spikes_are_evaluated_once_per_day_and_survive_the_reconcile(sf, log_lines):
    """FENCE F11. Kills double alerts and a reconcile that clobbers spike_day."""
    (o,) = await orgs(sf)
    await snap(sf, 0, NOW - 24 * H)
    await led(sf, o, 2_000_000, DAY)
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(2_000_000), now=NOW) in ("drift", "reconciled")
    assert await get(sf, SPIKE) == "2026-10-19"
    n = len(named(log_lines, "platform_ai_spend.spike"))
    assert n >= 1
    await run_platform_ai_spend_watch(sf, fetch=fetch_of(2_000_000), now=NOW + 2 * H)
    assert len(named(log_lines, "platform_ai_spend.spike")) == n
    await run_platform_ai_spend_watch(sf, fetch=fetch_of(2_000_000), now=NOW + 24 * H)
    assert await get(sf, SPIKE) == "2026-10-20"


async def test_f11_a_spike_day_in_the_future_is_stale_and_evaluated(sf, log_lines):
    """Kills `>=` on the dedup: a clock that moved back must not silence the alarm."""
    await put(sf, SPIKE, "2026-10-25")
    (o,) = await orgs(sf)
    await snap(sf, 1, NOW - H)
    await led(sf, o, 4_000_000, DAY)
    await run_platform_ai_spend_watch(sf, fetch=fetch_of(1), now=NOW)
    assert [e["scope"] for e in named(log_lines, "platform_ai_spend.spike")] == ["org"]
    assert await get(sf, SPIKE) == "2026-10-19"


async def test_spike_window_edges(sf, log_lines):
    """Day is [yesterday 00:00:00, today 00:00:00); window is [day-7 00:00:00, day)."""
    (o,) = await orgs(sf)
    await snap(sf, 1, NOW - H)
    await led(sf, o, 1_500_000, dt.datetime(2026, 10, 19, 0, 0, 0))     # in the day (start inclusive)
    await led(sf, o, 10_000_000, dt.datetime(2026, 10, 20, 0, 0, 0))    # today: in neither
    await led(sf, o, 700_000, dt.datetime(2026, 10, 12, 0, 0, 0))       # window start, inclusive
    await led(sf, o, 7_000_000, dt.datetime(2026, 10, 11, 23, 59, 59))  # just before the window
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(1), now=NOW) == "fresh"
    (e,) = named(log_lines, "platform_ai_spend.spike")
    assert e["scope"] == "org" and e["day_micros"] == 1_500_000 and e["baseline_micros"] == 100_000


def failing_commit(sf, key, state):
    """A factory whose sessions fail to commit a write of ``key`` while ``state["fail"]``."""
    def factory():
        s = sf()
        real = s.commit

        async def commit():
            if state["fail"] and any(isinstance(x, SystemSetting) and x.key == key for x in (*s.new, *s.dirty)):
                raise OperationalError("commit", {}, Exception("db down"))
            await real()

        s.commit = commit
        return s

    return factory


async def test_drift_is_logged_only_after_the_snapshot_commits(sf, log_lines):
    """Kills logging before the commit: a failed commit would re-alarm on every retry."""
    (o,) = await orgs(sf)
    await put(sf, SPIKE, "2026-10-19")
    await snap(sf, 0, NOW - 24 * H)
    await led(sf, o, 5_000_000, NOW - 12 * H)
    state = {"fail": True}
    factory = failing_commit(sf, SNAP, state)
    assert await run_platform_ai_spend_watch(factory, fetch=fetch_of(10_000_000), now=NOW) == "error"
    assert not named(log_lines, "platform_ai_spend.drift") and not named(log_lines, "platform_ai_spend.reconciled")
    state["fail"] = False
    expire_lease("platform_ai_spend:attempt")
    assert await run_platform_ai_spend_watch(factory, fetch=fetch_of(10_000_000), now=NOW) == "drift"
    assert len(named(log_lines, "platform_ai_spend.drift")) == 1


async def test_spike_is_logged_only_after_spike_day_commits(sf, log_lines):
    (o,) = await orgs(sf)
    await snap(sf, 1, NOW - H)
    await led(sf, o, 4_000_000, DAY)
    state = {"fail": True}
    factory = failing_commit(sf, SPIKE, state)
    assert await run_platform_ai_spend_watch(factory, fetch=fetch_of(1), now=NOW) == "fresh"
    assert not named(log_lines, "platform_ai_spend.spike")
    assert len(named(log_lines, "platform_ai_spend.spike_failed")) == 1
    state["fail"] = False
    await run_platform_ai_spend_watch(factory, fetch=fetch_of(1), now=NOW)
    assert len(named(log_lines, "platform_ai_spend.spike")) == 1


async def test_g6_a_failing_spike_query_does_not_block_the_reconcile(sf, log_lines):
    await snap(sf, 0, NOW - 24 * H)
    n = {"n": 0}

    def factory():
        s = sf()
        real = s.execute

        async def execute(stmt, *a, **k):
            if "ai_usage_ledger" in str(stmt) and n["n"] == 0:
                n["n"] += 1
                raise OperationalError("select", {}, Exception("db down"))
            return await real(stmt, *a, **k)

        s.execute = execute
        return s

    assert await run_platform_ai_spend_watch(factory, fetch=fetch_of(0), now=NOW) == "reconciled"
    (f,) = named(log_lines, "platform_ai_spend.spike_failed")
    assert f["error"] == "OperationalError"
    assert len(named(log_lines, "platform_ai_spend.reconciled")) == 1


# ── F9: the event contract ───────────────────────────────────────────────────


async def test_f9_event_names_and_key_sets(sf, log_lines):
    """FENCE F9. Kills a silent rename or key drop that blinds the infra alert rules."""
    (o,) = await orgs(sf)
    await led(sf, o, 6_000_000, DAY)
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(1_000_000), now=NOW) == "baselined"
    await snap_replace(sf, 0, NOW - 24 * H)
    expire_lease("platform_ai_spend:attempt")
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(8_000_000), now=NOW + H) == "drift"
    await snap_replace(sf, 0, NOW - 24 * H, fp="1" * 16)
    expire_lease("platform_ai_spend:attempt")
    await run_platform_ai_spend_watch(sf, fetch=fetch_of(1), now=NOW + 2 * H)
    await snap_replace(sf, 0, NOW - 24 * H)
    expire_lease("platform_ai_spend:attempt")
    await run_platform_ai_spend_watch(sf, fetch=BAD["raises"], now=NOW + 3 * H)
    base = {"openrouter_micros", "ledger_micros", "gap_micros", "gap_bp", "window_s", "drift"}
    rec = named(log_lines, "platform_ai_spend.reconciled")
    assert keys(rec[0]) == {"openrouter_micros", "baseline"} and rec[0]["level"] == "INFO"
    assert keys(rec[1]) == base
    (d,) = named(log_lines, "platform_ai_spend.drift")
    assert keys(d) == base and d["level"] == "ERROR"
    (r,) = named(log_lines, "platform_ai_spend.rebaselined")
    assert keys(r) == {"reason"}
    (f,) = named(log_lines, "platform_ai_spend.reconcile_failed")
    assert keys(f) == {"error", "hours_since_success"}
    (s,) = [e for e in named(log_lines, "platform_ai_spend.spike") if e["scope"] == "org"]
    assert keys(s) == {"scope", "org_id", "day", "day_micros", "baseline_micros"}


async def snap_replace(sf, usage, at, fp=FP):
    async with sf() as db:
        row = await db.scalar(select(SystemSetting).where(SystemSetting.key == SNAP))
        row.value = json.dumps({"usage_micros": usage, "at": at.isoformat(), "key_fp": fp})
        await db.commit()


# ── G4: the key never leaves ─────────────────────────────────────────────────


async def test_g4_the_key_is_sent_but_never_stored_or_logged(sf, log_lines):
    await snap(sf, 1_000_000, NOW - 24 * H)
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(401, text=f"bad {KEY}")

    fetch = functools.partial(fetch_key_usage, transport=httpx.MockTransport(handler))
    assert await run_platform_ai_spend_watch(sf, fetch=fetch, now=NOW) == "fetch_failed"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    async with sf() as db:
        stored = " ".join((await db.scalars(select(SystemSetting.value))).all())
    assert KEY not in stored and KEY not in json.dumps(log_lines())
    expire_lease("platform_ai_spend:attempt")
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(1_000_000), now=NOW + H) == "reconciled"
    assert KEY not in (await get(sf, SNAP)) and KEY not in json.dumps(log_lines())


# ── the real fetch ───────────────────────────────────────────────────────────


async def test_fetch_key_usage_url_auth_data_and_limits(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": {"usage": 1.5, "limit": None}})

    assert await fetch_key_usage(transport=httpx.MockTransport(handler)) == {"usage": 1.5, "limit": None}
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/key"
    assert seen[0].headers["accept-encoding"] == "identity"  # the byte cap must bound decoded bytes
    monkeypatch.setattr(job, "MAX_BODY_BYTES", 10)
    with pytest.raises(job.SpendFetchError):
        await fetch_key_usage(transport=httpx.MockTransport(handler))
    monkeypatch.undo()
    kwargs = {}
    real = httpx.AsyncClient

    def spy(**kw):
        kwargs.update(kw)
        return real(**kw)

    monkeypatch.setattr(job.httpx, "AsyncClient", spy)
    monkeypatch.setattr(settings, "platform_ai_openrouter_api_key", KEY)
    await fetch_key_usage(transport=httpx.MockTransport(handler))
    assert kwargs["trust_env"] is False


# ── metrics: the alarm path infra alerts on (TBD logs do not reach Grafana) ──


def points(reader, name):
    data = reader.get_metrics_data()
    return [(dict(p.attributes), p.value)
            for rm in (data.resource_metrics if data else []) for sm in rm.scope_metrics
            for m in sm.metrics if m.name == name for p in m.data.data_points]


EPOCH = NOW.replace(tzinfo=dt.timezone.utc).timestamp()


async def test_metrics_drift_counts_and_the_heartbeat_moves(sf, metric_reader):
    """FENCE. Kills a log-only alarm (no event point) and a heartbeat that never advances."""
    await snap(sf, 0, NOW - 24 * H)
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(1_000_000), now=NOW) == "drift"
    assert points(metric_reader, "tbd.platform_ai.events") == [({"event": "drift"}, 1)]
    assert points(metric_reader, "tbd.platform_ai.reconciled_last_success") == [({}, EPOCH)]
    assert points(metric_reader, "tbd.platform_ai.house_key_configured") == [({}, 1)]


async def test_metrics_a_fresh_tick_reports_the_stored_success(sf, metric_reader):
    """Kills a process-local heartbeat: after a restart the next tick must report the DB snapshot."""
    await snap(sf, 0, NOW - 2 * H)
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(0), now=NOW) == "fresh"
    assert points(metric_reader, "tbd.platform_ai.reconciled_last_success") == [({}, EPOCH - 7200)]
    assert points(metric_reader, "tbd.platform_ai.events") == []


async def test_metrics_never_succeeded_is_zero_not_absent(sf, metric_reader):
    """Kills an unset gauge when armed with no snapshot: an absent series keeps the alarm silent."""

    async def broken():
        raise job.SpendFetchError("status", 503)

    assert await run_platform_ai_spend_watch(sf, fetch=broken, now=NOW) == "fetch_failed"
    assert points(metric_reader, "tbd.platform_ai.reconciled_last_success") == [({}, 0)]
    assert points(metric_reader, "tbd.platform_ai.events") == [({"event": "reconcile_failed"}, 1)]


async def test_metrics_off_reports_unarmed(sf, metric_reader, monkeypatch):
    monkeypatch.setattr(settings, "platform_ai_openrouter_api_key", "")
    assert await run_platform_ai_spend_watch(sf, fetch=fetch_of(1), now=NOW) == "off"
    assert points(metric_reader, "tbd.platform_ai.house_key_configured") == [({}, 0)]


async def test_metrics_spikes_carry_scope_never_org_id(sf, log_lines, metric_reader):
    """Kills org_id as a metric attribute (unbounded cardinality) and a dropped scope."""
    await spikes(sf, log_lines, day=[6_000_000], window=[0])
    got = sorted(points(metric_reader, "tbd.platform_ai.events"), key=lambda p: p[0]["scope"])
    assert got == [({"event": "spike", "scope": "global"}, 1), ({"event": "spike", "scope": "org"}, 1)]


async def test_metrics_rebaseline_and_spike_failure_count(sf, metric_reader):
    await snap(sf, 0, NOW - 24 * H, fp="0" * 16)

    def factory():
        s = sf()
        real = s.execute

        async def execute(stmt, *a, **k):
            if "ai_usage_ledger" in str(stmt):
                raise OperationalError("select", {}, Exception("db down"))
            return await real(stmt, *a, **k)

        s.execute = execute
        return s

    assert await run_platform_ai_spend_watch(factory, fetch=fetch_of(5), now=NOW) == "rebaselined"
    got = sorted(points(metric_reader, "tbd.platform_ai.events"), key=lambda p: p[0]["event"])
    assert got == [({"event": "rebaselined"}, 1), ({"event": "spike_failed"}, 1)]
