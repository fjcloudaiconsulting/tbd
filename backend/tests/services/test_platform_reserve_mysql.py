"""TBD-586 PR1 kernel fences K2-K6 on REAL MySQL.

Run with PLATFORM_RESERVE_MYSQL_URL=mysql+aiomysql://... pointing at a
disposable database already at ``alembic upgrade head`` (``create_all`` does
not work on MySQL: ``roles.created_at DEFAULT now(6)`` on a DATETIME is error
1067). The fixture deletes the rows these tests write.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.util import await_only

from app.models.platform_ai_spend import PlatformAISpend
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.usage_counter import UsageCounter
from app.models.user import Organization
from app.services import platform_reserve as pr
from app.services.usage_service import PlanLimitReached

URL = os.environ.get("PLATFORM_RESERVE_MYSQL_URL")
pytestmark = pytest.mark.skipif(not URL, reason="PLATFORM_RESERVE_MYSQL_URL not set")

NOW = datetime(2026, 10, 15, 12, 0, 0)
OCT = date(2026, 10, 1)


@pytest_asyncio.fixture
async def factory():
    eng = create_async_engine(URL)
    async with eng.begin() as conn:
        for t in ("usage_counters", "platform_ai_spend", "subscriptions", "plans", "organizations"):
            await conn.execute(text(f"DELETE FROM {t}"))
    try:
        yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    finally:
        await eng.dispose()


def _limits(tokens=1000, cents=1000, tperiod="month", cperiod="month") -> dict:
    return {
        "platform_ai.tokens": {"period": tperiod, "limit": tokens},
        "platform_ai.cents": {"period": cperiod, "limit": cents},
    }


async def _org(f, limits: dict) -> int:
    async with f() as db:
        org = Organization(name=f"Acme {uuid.uuid4()}", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        plan = Plan(slug=f"p{org.id}", name="P", features={}, usage_limits=limits)
        db.add(plan)
        await db.flush()
        db.add(Subscription(org_id=org.id, plan_id=plan.id, status=SubscriptionStatus.ACTIVE))
        await db.commit()
        return org.id


async def _state(f, org_id=None):
    async with f() as db:
        q = select(UsageCounter)
        if org_id is not None:
            q = q.where(UsageCounter.org_id == org_id)
        counters = {(r.org_id, r.meter, r.period, r.period_start): r.value
                    for r in (await db.scalars(q)).all()}
        spend = {r.period_start: r.cents for r in (await db.scalars(select(PlatformAISpend))).all()}
        return counters, spend


async def _set_spend(f, start: date, cents: int):
    async with f() as db:
        await db.merge(PlatformAISpend(period_start=start, cents=cents))
        await db.commit()


class _Adapter:
    calls = 0

    def __call__(self):
        _Adapter.calls += 1
        return object()


# ── K2 ────────────────────────────────────────────────────────────────────

async def test_k2_ceiling_refusal_leaves_org_meters_unchanged(factory):
    org = await _org(factory, _limits())
    await _set_spend(factory, OCT, 95)
    make = _Adapter()
    _Adapter.calls = 0
    async with factory() as db:
        with pytest.raises(pr.PlatformAIUnavailable):
            await pr.reserve(db, org, 50, 10, 100, make, now=NOW)
    counters, spend = await _state(factory, org)
    assert set(counters.values()) == {0}
    assert spend == {OCT: 95}
    assert _Adapter.calls == 0


async def test_k2_org_meter_refusal_reserves_nothing(factory):
    org = await _org(factory, _limits(cents=5))
    make = _Adapter()
    _Adapter.calls = 0
    async with factory() as db:
        with pytest.raises(PlanLimitReached) as ei:
            await pr.reserve(db, org, 50, 10, 100, make, now=NOW)
    assert ei.value.meter == "platform_ai.cents"
    counters, spend = await _state(factory, org)
    assert set(counters.values()) == {0} and spend == {OCT: 0}
    assert _Adapter.calls == 0


@pytest.mark.parametrize("nth", [1, 2, 3], ids=["tokens", "cents", "global"])
async def test_k2_db_error_on_any_update_reserves_nothing(factory, nth):
    org = await _org(factory, _limits())
    seen = [0]

    def boom(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE"):
            seen[0] += 1
            if seen[0] == nth:
                raise OperationalError(statement, parameters, Exception("injected"))

    event.listen(factory.kw["bind"].sync_engine, "before_cursor_execute", boom)
    make = _Adapter()
    _Adapter.calls = 0
    async with factory() as db:
        with pytest.raises(pr.PlatformAIUnavailable):
            await pr.reserve(db, org, 50, 10, 100, make, now=NOW)
    event.remove(factory.kw["bind"].sync_engine, "before_cursor_execute", boom)
    counters, spend = await _state(factory, org)
    assert set(counters.values()) == {0} and spend == {OCT: 0}
    assert _Adapter.calls == 0


async def test_k2_refusal_rollback_expires_callers_orm_objects(factory):
    """Probe, not a fence: what a refusal does to the CALLER's session."""
    org = await _org(factory, _limits(cents=5))
    async with factory() as db:
        o = await db.get(Organization, org)
        with pytest.raises(PlanLimitReached):
            await pr.reserve(db, org, 50, 10, 100, _Adapter(), now=NOW)
        from sqlalchemy import inspect
        assert "name" in inspect(o).expired_attributes  # rollback expired it


# ── K3 ────────────────────────────────────────────────────────────────────

async def test_k3_two_orgs_race_at_ceiling_minus_one_exactly_one_admitted(factory):
    a = await _org(factory, _limits())
    b = await _org(factory, _limits())
    await _set_spend(factory, OCT, 99)
    barrier = asyncio.Barrier(2)
    held: set[int] = set()

    def hold(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE platform_ai_spend"):
            key = id(conn.connection.dbapi_connection)
            if key not in held:
                held.add(key)
                await_only(asyncio.wait_for(barrier.wait(), 10))

    event.listen(factory.kw["bind"].sync_engine, "before_cursor_execute", hold)

    async def one(org):
        async with factory() as db:
            await pr.reserve(db, org, 10, 1, 100, _Adapter(), now=NOW)

    res = await asyncio.gather(one(a), one(b), return_exceptions=True)
    assert sorted(type(r).__name__ for r in res) == ["NoneType", "PlatformAIUnavailable"]
    counters, spend = await _state(factory)
    assert spend == {OCT: 100}
    # the refused org's meters were rolled back with the ceiling miss
    assert sorted(v for (o, m, *_), v in counters.items() if m == "platform_ai.cents") == [0, 1]


# ── K4 ────────────────────────────────────────────────────────────────────

async def test_k4_settle_after_midnight_moves_only_the_reserved_period(factory):
    org = await _org(factory, _limits(tperiod="day", cperiod="month"))
    last = datetime(2026, 10, 31, 23, 59, 59)
    async with factory() as db:
        r = await pr.reserve(db, org, 100, 10, 1000, _Adapter(), now=last)
    # "after midnight": settle takes no clock; the handle carries the keys.
    async with factory() as db:
        await pr.settle(db, r.handle, 40, 4)
    counters, spend = await _state(factory, org)
    assert counters == {
        (org, "platform_ai.tokens", "day", date(2026, 10, 31)): 40,
        (org, "platform_ai.cents", "month", OCT): 4,
    }
    assert spend == {OCT: 4}


# ── K5 ────────────────────────────────────────────────────────────────────

async def test_k5_actual_above_reserved_lands_even_above_the_limit(factory):
    org = await _org(factory, _limits(tokens=150, cents=15))
    async with factory() as db:
        r = await pr.reserve(db, org, 100, 10, 20, _Adapter(), now=NOW)
        await pr.settle(db, r.handle, 400, 40)
    counters, spend = await _state(factory, org)
    assert counters == {
        (org, "platform_ai.tokens", "month", OCT): 400,
        (org, "platform_ai.cents", "month", OCT): 40,
    }
    assert spend == {OCT: 40}


# ── K6 ────────────────────────────────────────────────────────────────────

async def test_k6_settle_twice_applies_once(factory):
    org = await _org(factory, _limits())
    async with factory() as db:
        r = await pr.reserve(db, org, 100, 10, 1000, _Adapter(), now=NOW)
        await pr.settle(db, r.handle, 30, 3)   # success path
        await pr.settle(db, r.handle, 30, 3)   # generator finally
    counters, spend = await _state(factory, org)
    assert set(counters.values()) == {30, 3} and spend == {OCT: 3}


# ── extra: zero-delta UPDATE rowcount on MySQL (affected vs matched rows) ─

async def test_zero_cost_reserve_at_exact_ceiling_is_admitted(factory):
    org = await _org(factory, _limits())
    await _set_spend(factory, OCT, 100)
    async with factory() as db:
        await pr.reserve(db, org, 0, 0, 100, _Adapter(), now=NOW)
