"""TBD-585: ``usage_service.admit``, the usage-meter admission.

Fences F-Q2 (race, warm and cold row, plus the MySQL upsert compile), the
pending-changes guard, F-Q10, F-Q11 (admission half) and the 402 handler. On a
file-backed SQLite so two sessions are two real connections. Every fence names
the wrong implementation it kills.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.util import await_only

from app.models import Base
from app.models.settings import OrgSetting
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.usage_counter import UsageCounter
from app.models.user import Organization
from app.services import usage_service
from app.services.usage_service import PlanLimitReached, admit, period_start, resets_at

NOW = datetime(2026, 9, 30, 12, 0, 0)


@pytest_asyncio.fixture
async def factory(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/u.db")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    finally:
        await eng.dispose()


async def _org(f, usage_limits: dict | None) -> int:
    async with f() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        plan = Plan(slug=f"p{org.id}", name="P", features={})
        if usage_limits is not None:
            plan.usage_limits = usage_limits
        db.add(plan)
        await db.flush()
        db.add(Subscription(org_id=org.id, plan_id=plan.id, status=SubscriptionStatus.ACTIVE))
        await db.commit()
        return org.id


async def _counters(f, org_id) -> dict[tuple[str, str, date], int]:
    async with f() as db:
        rows = (await db.scalars(select(UsageCounter).where(UsageCounter.org_id == org_id))).all()
        return {(r.meter, r.period, r.period_start): r.value for r in rows}


def _mcp(period: str, limit: int | None) -> dict:
    return {"mcp.calls": {"period": period, "limit": limit}}


# ── F-Q2: the race ────────────────────────────────────────────────────────

def _barrier_on_counter_writes(factory, barrier: asyncio.Barrier) -> None:
    """Hold each connection's FIRST write to ``usage_counters`` (UPDATE or
    INSERT, however issued: Core or an ORM flush) on ``barrier``, so two admits
    are forced past everything they read before either writes. The engine is
    per test (the fixture disposes it), so the listener needs no removal."""
    held: set[int] = set()

    def hook(conn, cursor, statement, parameters, context, executemany):
        head = statement.lstrip().upper()
        # The idempotent upsert is not the counting write: hold the first
        # UPDATE (or a plain INSERT) so a read-then-write mutant that keeps
        # the upsert still reads before the barrier.
        if (
            "usage_counters" in statement
            and head.startswith(("UPDATE", "INSERT"))
            and "ON CONFLICT" not in head
            and "ON DUPLICATE KEY" not in head
        ):
            key = id(conn.connection.dbapi_connection)
            if key not in held:
                held.add(key)
                await_only(asyncio.wait_for(barrier.wait(), 10))

    event.listen(factory.kw["bind"].sync_engine, "before_cursor_execute", hook)


@pytest.mark.parametrize("warm", [True, False], ids=["warm_row", "cold_row"])
async def test_fq2_two_admits_at_the_limit_exactly_one_passes(factory, warm):
    """FENCE F-Q2. Two admits, forced past their reads by a barrier on the
    first write to the counter, with one unit of headroom: exactly one passes and the
    counter lands ON the limit. Warm: row at limit-1 (limit 3). Cold: no row
    (limit 1), so the upsert itself is raced too.

    Wrong implementations killed: SELECT-then-increment (both read limit-1,
    both pass, counter limit+1), and an uncommitted upsert (the second
    connection blocks on the first's write lock and the admission errors)."""
    limit = 3 if warm else 1
    org = await _org(factory, _mcp("month", limit))
    start = period_start("month", NOW)
    if warm:
        async with factory() as db:
            db.add(UsageCounter(org_id=org, meter="mcp.calls", period="month",
                                period_start=start, value=limit - 1))
            await db.commit()

    _barrier_on_counter_writes(factory, asyncio.Barrier(2))

    async def one():
        async with factory() as db:
            await admit(db, org, "mcp.calls", now=NOW)

    results = await asyncio.gather(one(), one(), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["NoneType", "PlanLimitReached"]
    assert await _counters(factory, org) == {("mcp.calls", "month", start): limit}


async def test_fq2_mysql_upsert_never_resets_the_counter():
    """FENCE F-Q2 (MySQL half). The MySQL upsert compiles to an
    ``ON DUPLICATE KEY UPDATE`` that keeps the value; the conditional UPDATE
    carries the limit. Wrong implementations: ``value = 0`` / ``VALUES(value)``
    on duplicate (resets the period's count), an unconditional increment."""
    seen: list = []

    class _Fake:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

        async def execute(self, stmt):
            seen.append(stmt)
            return SimpleNamespace(rowcount=1)

        async def commit(self):
            seen.append("commit")

    assert await usage_service._try_increment(
        _Fake(), 1, "mcp.calls", "month", date(2026, 9, 1), 1, 5) is True
    upsert, c1, upd, c2 = seen
    assert (c1, c2) == ("commit", "commit")
    sql = " ".join(str(upsert.compile(dialect=mysql.dialect())).split())
    assert sql.endswith("ON DUPLICATE KEY UPDATE value = usage_counters.value"), sql
    usql = " ".join(str(upd.compile(dialect=mysql.dialect())).split())
    assert "SET value=(usage_counters.value + %s)" in usql, usql
    assert "usage_counters.period = %s" in usql
    assert usql.endswith("AND usage_counters.value + %s <= %s"), usql


# ── guard ─────────────────────────────────────────────────────────────────

async def test_guard_pending_orm_change_refuses_and_counts_nothing(factory):
    """FENCE (guard). ``admit`` commits the caller's session, so a pending ORM
    change would be persisted by it. Wrong implementations: no guard (the
    pending row is committed and the call counted), a guard that skips
    admission instead of refusing (the call is never counted)."""
    org = await _org(factory, None)
    async with factory() as db:
        db.add(OrgSetting(org_id=org, key="orgpref.x", value="1"))
        with pytest.raises(RuntimeError):
            await admit(db, org, "mcp.calls", now=NOW)
        await db.rollback()
    async with factory() as db:
        o = await db.get(Organization, org)
        o.name = "dirty"
        with pytest.raises(RuntimeError):
            await admit(db, org, "mcp.calls", now=NOW)
    async with factory() as db:
        assert await db.scalar(select(OrgSetting).where(OrgSetting.key == "orgpref.x")) is None
        assert (await db.get(Organization, org)).name == "Acme"
    assert await _counters(factory, org) == {}


async def test_unknown_meter_is_a_programmer_error(factory):
    org = await _org(factory, None)
    async with factory() as db:
        with pytest.raises(ValueError):
            await admit(db, org, "mcp.call", now=NOW)


# ── F-Q10 / F-Q11 / periods ───────────────────────────────────────────────

async def test_fq10_period_kind_is_part_of_the_key(factory):
    """FENCE F-Q10. On the 1st the day start equals the month start. A month
    row at 7, the plan edited to a day period: the admit opens a fresh day row
    at 1 and leaves the month row at 7. Wrong implementation: a counter keyed
    without the period kind (the day admit lands on the month row: 8, or is
    refused against a limit the month count already spent)."""
    now = datetime(2026, 10, 1, 0, 0, 5)
    first = date(2026, 10, 1)
    org = await _org(factory, _mcp("day", 10))
    async with factory() as db:
        db.add(UsageCounter(org_id=org, meter="mcp.calls", period="month",
                            period_start=first, value=7))
        await db.commit()
    async with factory() as db:
        await admit(db, org, "mcp.calls", now=now)
    assert await _counters(factory, org) == {
        ("mcp.calls", "month", first): 7, ("mcp.calls", "day", first): 1,
    }


@pytest.mark.parametrize("stored", [{}, {"assistant.turns": {"period": "day", "limit": 2}}])
async def test_fq11_plan_json_without_the_meter_admits_on_defaults(factory, stored):
    """FENCE F-Q11. Wrong implementation: admission reading the raw plan JSON
    (``{}`` or a plan without ``mcp.calls`` raises KeyError / reads None)."""
    org = await _org(factory, stored)
    async with factory() as db:
        await admit(db, org, "mcp.calls", now=NOW)
    assert await _counters(factory, org) == {("mcp.calls", "month", date(2026, 9, 1)): 1}


async def test_limit_zero_refuses_and_never_resets(factory):
    org = await _org(factory, _mcp("day", 0))
    async with factory() as db:
        with pytest.raises(PlanLimitReached) as exc:
            await admit(db, org, "mcp.calls", now=NOW)
    e = exc.value
    assert (e.meter, e.limit, e.period) == ("mcp.calls", 0, "day")
    assert e.resets_at is None  # a 0 limit never resets; a real limit does (below)


async def test_limit_reached_resets_at_the_next_boundary(factory):
    org = await _org(factory, _mcp("day", 1))
    async with factory() as db:
        await admit(db, org, "mcp.calls", now=NOW)
        with pytest.raises(PlanLimitReached) as exc:
            await admit(db, org, "mcp.calls", now=NOW)
    assert exc.value.resets_at == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_period_boundaries():
    assert period_start("day", NOW) == date(2026, 9, 30)
    assert period_start("month", NOW) == date(2026, 9, 1)
    dec = datetime(2026, 12, 31, 23, 59, 59)
    assert resets_at("month", dec) == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert resets_at("day", dec) == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert resets_at("month", datetime(2026, 1, 31)) == datetime(2026, 2, 1, tzinfo=timezone.utc)


# ── 402 ───────────────────────────────────────────────────────────────────

async def test_402_the_real_app_handler_maps_plan_limit_reached():
    """FENCE 402. The handler registered on the real app answers 402 with the
    four facts. Wrong implementations: no handler (500), a 429/403, the reset
    time missing."""
    import json

    from app.main import app

    handler = app.exception_handlers[PlanLimitReached]
    exc = PlanLimitReached("mcp.calls", 5, "day", datetime(2026, 10, 1, tzinfo=timezone.utc))
    resp = await handler(None, exc)
    assert resp.status_code == 402
    assert json.loads(resp.body) == {"detail": {
        "code": "plan_limit_reached", "meter": "mcp.calls", "limit": 5, "period": "day",
        "resets_at": "2026-10-01T00:00:00+00:00",
    }}


async def test_402_handler_carries_null_resets_at_for_a_zero_limit():
    import json

    from app.main import app

    resp = await app.exception_handlers[PlanLimitReached](
        None, PlanLimitReached("mcp.calls", 0, "day", None))
    assert json.loads(resp.body)["detail"]["resets_at"] is None


# ── TBD-581: current_usage (GET /ai/status usage) ─────────────────────────

async def test_f581_meter_current_usage_reads_only_this_periods_row(factory):
    """FENCE F-581-METER. Wrong implementations killed: summing every counter
    row of a meter (the August row and the day-kind row would count), and
    ignoring the period kind (the day row shares the month row's meter; the
    month row of a now-daily meter sorts last, so last-row-wins reads it)."""
    org = await _org(factory, {
        "mcp.calls": {"period": "month", "limit": 5},
        "assistant.turns": {"period": "day", "limit": 3},
    })
    async with factory() as db:
        db.add_all([
            UsageCounter(org_id=org, meter="mcp.calls", period="month",
                         period_start=date(2026, 9, 1), value=2),
            UsageCounter(org_id=org, meter="mcp.calls", period="month",
                         period_start=date(2026, 8, 1), value=9),
            UsageCounter(org_id=org, meter="mcp.calls", period="day",
                         period_start=date(2026, 9, 30), value=7),
            UsageCounter(org_id=org, meter="assistant.turns", period="day",
                         period_start=date(2026, 9, 30), value=1),
            UsageCounter(org_id=org, meter="assistant.turns", period="day",
                         period_start=date(2026, 9, 29), value=3),
            # Left from when the plan metered turns monthly; sorts after the day row.
            UsageCounter(org_id=org, meter="assistant.turns", period="month",
                         period_start=date(2026, 9, 1), value=5),
        ])
        await db.commit()
        out = await usage_service.current_usage(db, org, include_platform=True, now=NOW)
    midnight = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert out == {
        "assistant.turns": {"used": 1, "limit": 3, "period": "day", "resets_at": midnight},
        "mcp.calls": {"used": 2, "limit": 5, "period": "month", "resets_at": midnight},
    }


async def test_f581_platform_meters_admin_only_and_dark_at_zero(factory):
    """FENCE. Wrong implementations killed: showing the org's platform spend
    to every member, and listing a 0-limit platform meter (dark platform AI
    would show to every org). A 0 limit on a product meter IS shown (it closes
    the surface) and never resets."""
    org = await _org(factory, {
        "mcp.calls": {"period": "day", "limit": 0},
        "platform_ai.cents": {"period": "month", "limit": 500},
    })
    async with factory() as db:
        admin = await usage_service.current_usage(db, org, include_platform=True, now=NOW)
        member = await usage_service.current_usage(db, org, include_platform=False, now=NOW)
    assert sorted(admin) == ["assistant.turns", "mcp.calls", "platform_ai.cents"]
    assert sorted(member) == ["assistant.turns", "mcp.calls"]
    assert member["mcp.calls"] == {"used": 0, "limit": 0, "period": "day", "resets_at": None}
    assert member["assistant.turns"]["limit"] is None  # catalog default: unlimited
