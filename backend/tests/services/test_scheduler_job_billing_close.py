from __future__ import annotations

import datetime

import pytest
import pytest_asyncio
import structlog.testing
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.billing import BillingPeriod
from app.models.user import Organization
from app.services import billing_service
from app.services.scheduler.jobs.billing_close import BillingCloseJob


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(session_factory, cycle_day, period_start):
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=cycle_day)
        db.add(org); await db.flush()
        db.add(BillingPeriod(org_id=org.id, start_date=period_start))
        await db.commit(); await db.refresh(org)
        return org


async def test_not_due_before_boundary(session_factory, monkeypatch):
    _silence_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=1, period_start=datetime.date(2026, 7, 1))
    job = BillingCloseJob()
    async with session_factory() as db:
        # today is mid-cycle; boundary (Jul 1) == current start -> not due
        assert await job.is_due(db, org, datetime.date(2026, 7, 15)) is False


async def test_due_when_period_straddles_boundary(session_factory, monkeypatch):
    _silence_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=1, period_start=datetime.date(2026, 7, 1))
    job = BillingCloseJob()
    async with session_factory() as db:
        # today = Aug 3, boundary = Aug 1 > current start (Jul 1) -> due
        assert await job.is_due(db, org, datetime.date(2026, 8, 3)) is True


async def test_run_closes_and_is_idempotent(session_factory, monkeypatch):
    _silence_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=1, period_start=datetime.date(2026, 7, 1))
    job = BillingCloseJob()
    today = datetime.date(2026, 8, 3)
    async with session_factory() as db:
        res = await job.run(db, org, today)
    assert res.outcome == "success"
    # new open period starts on the boundary (Aug 1)
    async with session_factory() as db:
        cur = await billing_service.get_current_period(db, org.id)
        assert cur.start_date == datetime.date(2026, 8, 1)
        assert await job.is_due(db, org, today) is False  # idempotent


async def test_cycle_day_25_not_due_early_but_due_on_boundary_and_idempotent(session_factory, monkeypatch):
    # Regression guard for the premature-close bug: the old code used
    # billing_service._snap_to_cycle(today, cycle_day) directly, which pins the
    # day within today's OWN month and does not roll back. For cycle_day=25,
    # on 2026-07-01 that returned 2026-07-25 (a FUTURE boundary), which was
    # already > the open period's start_date (2026-06-25) -> is_due wrongly
    # returned True, closing the period ~24 days early.
    _silence_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=25, period_start=datetime.date(2026, 6, 25))
    job = BillingCloseJob()

    async with session_factory() as db:
        # 2026-07-01: still mid-cycle. Correct boundary (current_cycle_window)
        # rolls back to 2026-06-25 == current start -> NOT due.
        assert await job.is_due(db, org, datetime.date(2026, 7, 1)) is False

    async with session_factory() as db:
        # 2026-07-25: boundary is 2026-07-25 > current start (2026-06-25) -> due.
        assert await job.is_due(db, org, datetime.date(2026, 7, 25)) is True

    today = datetime.date(2026, 7, 25)
    async with session_factory() as db:
        res = await job.run(db, org, today)
    assert res.outcome == "success"

    async with session_factory() as db:
        cur = await billing_service.get_current_period(db, org.id)
        assert cur.start_date == datetime.date(2026, 7, 25)
        assert await job.is_due(db, org, today) is False  # idempotent


def _silence_side_effects(monkeypatch):
    async def _noop_audit(**k):
        return 1
    async def _noop_notify(*a, **k):
        return 0
    monkeypatch.setattr("app.services.scheduler.jobs.billing_close.record_run", _noop_audit)
    monkeypatch.setattr(
        "app.services.scheduler.jobs.billing_close.dispatch_notification_to_org_members", _noop_notify
    )


# ── TBD-241: convergence ──────────────────────────────────────────────────
#
# Spec: specs/2026-07-28-close-period-chain-close-design.md, §5 tests 18-23.
#
# `close_period` performs exactly ONE close per call; multi-cycle convergence
# is this job's responsibility. The alternative (one step per tick) would turn
# a three-cycle catch-up into three notifications to every org member, three
# audit rows and three tick-budget slots spread over 45 minutes.


def _capture_side_effects(monkeypatch) -> dict:
    """Like `_silence_side_effects`, but records what was suppressed.

    Convergence must emit exactly ONE audit row and ONE notification for the
    whole run, so the tests need to count them rather than merely silence them.
    """
    captured: dict = {"audits": [], "notifications": []}

    async def _audit(**k):
        captured["audits"].append(k)
        return 1

    async def _notify(*a, **k):
        captured["notifications"].append(k)
        return 0

    monkeypatch.setattr("app.services.scheduler.jobs.billing_close.record_run", _audit)
    monkeypatch.setattr(
        "app.services.scheduler.jobs.billing_close.dispatch_notification_to_org_members",
        _notify,
    )
    return captured


async def _add_stub(session_factory, org_id, start, end):
    async with session_factory() as db:
        db.add(BillingPeriod(org_id=org_id, start_date=start, end_date=end))
        await db.commit()


async def _roster(session_factory, org_id):
    async with session_factory() as db:
        return list(
            (
                await db.execute(
                    select(BillingPeriod)
                    .where(BillingPeriod.org_id == org_id)
                    .order_by(BillingPeriod.start_date)
                )
            ).scalars().all()
        )


async def _seed_lapsed_org(session_factory):
    """§0's roster: cycle day 25, open period two cycles behind, three stubs a
    Forecasts mount already created ahead of it."""
    org = await _seed(session_factory, cycle_day=25, period_start=datetime.date(2026, 4, 25))
    for start, end in [
        (datetime.date(2026, 5, 25), datetime.date(2026, 6, 24)),
        (datetime.date(2026, 6, 25), datetime.date(2026, 7, 24)),
        (datetime.date(2026, 7, 25), datetime.date(2026, 8, 24)),
    ]:
        await _add_stub(session_factory, org.id, start, end)
    return org


async def test_lapsed_org_converges_in_one_run(session_factory, monkeypatch):
    """§5 test 18 — the §0 Path 1 defect, fixed.

    Before TBD-241 this single `run` wrote `end_date = 2026-07-24` on the open
    row in ONE jump, swallowing two intact stub periods whole. Now the clamp
    advances the roster one cycle at a time and every stub survives as the
    closed period the user planned.
    """
    captured = _capture_side_effects(monkeypatch)
    org = await _seed_lapsed_org(session_factory)
    job = BillingCloseJob()
    today = datetime.date(2026, 7, 28)

    async with session_factory() as db:
        res = await job.run(db, org, today)

    assert res.outcome == "success"
    assert res.counts["steps"] == 3
    assert res.counts["new_period_start"] == "2026-07-25"
    # The LAST actually-applied close date, not `boundary - 1` (= 07-24 here,
    # which happens to coincide; test 21 pins the case where they differ).
    assert res.counts["closed_on"] == "2026-07-24"

    periods = await _roster(session_factory, org.id)
    assert [(p.start_date, p.end_date) for p in periods] == [
        (datetime.date(2026, 4, 25), datetime.date(2026, 5, 24)),
        (datetime.date(2026, 5, 25), datetime.date(2026, 6, 24)),
        (datetime.date(2026, 6, 25), datetime.date(2026, 7, 24)),
        (datetime.date(2026, 7, 25), None),
    ]
    assert res.counts["closed_period_ids"] == [p.id for p in periods[:3]]

    # ONE notification and ONE audit row for the whole convergence.
    assert len(captured["notifications"]) == 1
    assert len(captured["audits"]) == 1
    assert captured["audits"][0]["outcome"] == "success"
    assert captured["audits"][0]["detail"]["steps"] == 3

    async with session_factory() as db:
        assert await job.is_due(db, org, today) is False


async def test_convergence_leaves_no_intersecting_closed_windows(
    session_factory, monkeypatch
):
    """§5 test 19 — the global post-condition.

    Scoped to `end_date IS NOT NULL` deliberately. Asserting it over ALL rows
    would be false as a design invariant: the open row read as unbounded
    intersects every later stub, which is the normal, intended state and the
    one test 23 depends on.
    """
    _capture_side_effects(monkeypatch)
    org = await _seed_lapsed_org(session_factory)
    async with session_factory() as db:
        await BillingCloseJob().run(db, org, datetime.date(2026, 7, 28))

    periods = await _roster(session_factory, org.id)
    assert len([p for p in periods if p.end_date is None]) == 1
    closed = [p for p in periods if p.end_date is not None]
    for earlier, later in zip(closed, closed[1:]):
        assert earlier.end_date < later.start_date


async def test_mid_convergence_failure_propagates_and_the_next_tick_heals(
    session_factory, monkeypatch
):
    """§5 test 20 (job half) — D11, the ruling reached by SUBTRACTION.

    A mid-convergence exception propagates untouched: no rollback dance, no
    partial audit row, no partial notification. `runner.py:67-72` already
    catches every job exception, rolls back and writes a failure row, and the
    steps that committed stay durable — `close_period` commits internally, so
    the loop is N independent transactions, not one. `is_due` is still true, so
    the next tick resumes and notifies.

    The partial-notification handler an earlier revision required could not
    work: the exception D11 exists for leaves the `AsyncSession` deactivated,
    and `dispatch_notification_to_org_members` opens with `db.execute`, so the
    handler meant to guarantee a notification would guarantee its absence.
    """
    captured = _capture_side_effects(monkeypatch)
    org = await _seed_lapsed_org(session_factory)
    job = BillingCloseJob()
    today = datetime.date(2026, 7, 28)

    real_close = billing_service.close_period
    calls = {"n": 0}

    async def flaky_close(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise IntegrityError("stmt", {}, Exception("peer won at new_start"))
        return await real_close(*args, **kwargs)

    monkeypatch.setattr(billing_service, "close_period", flaky_close)

    with pytest.raises(IntegrityError):
        async with session_factory() as db:
            await job.run(db, org, today)

    # Steps 1 and 2 committed and stayed durable.
    periods = await _roster(session_factory, org.id)
    assert [(p.start_date, p.end_date) for p in periods] == [
        (datetime.date(2026, 4, 25), datetime.date(2026, 5, 24)),
        (datetime.date(2026, 5, 25), datetime.date(2026, 6, 24)),
        (datetime.date(2026, 6, 25), None),
        (datetime.date(2026, 7, 25), datetime.date(2026, 8, 24)),
    ]
    assert captured["notifications"] == []
    assert captured["audits"] == [], "no partial success row"

    async with session_factory() as db:
        assert await job.is_due(db, org, today) is True

    # The next tick converges the remainder and notifies.
    async with session_factory() as db:
        res = await job.run(db, org, today)
    assert res.outcome == "success"
    assert res.counts["steps"] == 1
    assert res.counts["new_period_start"] == "2026-07-25"
    assert len(captured["notifications"]) == 1
    assert len(captured["audits"]) == 1


async def test_convergence_stops_at_the_cap_and_reports_the_last_applied_date(
    session_factory, monkeypatch
):
    """§5 test 21 — the 24-step cap.

    Returns success for the steps taken and logs, rather than grinding through
    an unbounded backlog inside one tick. The next tick continues.
    `closed_on` must report the LAST APPLIED close date, which here is nowhere
    near `boundary - 1`.
    """
    captured = _capture_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=1, period_start=datetime.date(2024, 1, 1))
    # 27 monthly stubs, so the roster needs more than 24 steps to converge.
    for i in range(1, 28):
        start = _month_start(2024, 1 + i)
        end = _month_start(2024, 2 + i) - datetime.timedelta(days=1)
        await _add_stub(session_factory, org.id, start, end)

    job = BillingCloseJob()
    today = datetime.date(2026, 8, 3)

    with structlog.testing.capture_logs() as logs:
        async with session_factory() as db:
            res = await job.run(db, org, today)

    assert res.outcome == "success"
    assert res.counts["steps"] == 24
    assert res.counts["new_period_start"] == "2026-01-01"
    assert res.counts["closed_on"] == "2025-12-31"
    assert res.counts["closed_on"] != (
        datetime.date(2026, 8, 1) - datetime.timedelta(days=1)
    ).isoformat()
    capped = [e for e in logs if e.get("event") == "billing.close.convergence_capped"]
    assert len(capped) == 1
    assert capped[0]["org_id"] == org.id
    assert capped[0]["steps"] == 24
    assert len(captured["audits"]) == 1

    # Still due, so the next tick continues.
    async with session_factory() as db:
        assert await job.is_due(db, org, today) is True


def _month_start(year: int, month: int) -> datetime.date:
    """Normalise a possibly-overflowing month number to a first-of-month."""
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return datetime.date(year, month, 1)


async def test_run_forwards_today_to_close_period(session_factory, monkeypatch):
    """§5 test 22 — D2's deliverable.

    `run` receives `today` and must forward it. Without the forward,
    `close_period` falls back to the wall clock and D1 rejects the job's own
    close date whenever the test (or a real backlog) works with a `today` in
    the future — which is precisely what would have turned
    `test_run_closes_and_is_idempotent` red on merge.
    """
    _capture_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=1, period_start=datetime.date(2026, 7, 1))
    job = BillingCloseJob()
    today = datetime.date(2026, 8, 3)

    real_close = billing_service.close_period
    seen: list[dict] = []

    async def recording_close(*args, **kwargs):
        seen.append(kwargs)
        return await real_close(*args, **kwargs)

    monkeypatch.setattr(billing_service, "close_period", recording_close)

    async with session_factory() as db:
        await job.run(db, org, today)

    assert seen and all(k.get("today") == today for k in seen)


async def test_ensure_future_periods_after_a_clamped_close_stays_non_overlapping(
    session_factory, monkeypatch
):
    """§5 test 23 — `ensure_future_periods` arm 1 still covers its window.

    Arm 1 (exact start) exists because `close_period` REVIVES rather than
    inserts, so a row arm 2 matched as closed can become open at a start the
    stub loop is still proposing. Chain-close changes WHICH row is revived but
    not THAT it revives, and no `start_date` ever moves, so the argument holds.
    Any future design that moves a `start_date` breaks it.
    """
    _capture_side_effects(monkeypatch)
    org = await _seed_lapsed_org(session_factory)
    async with session_factory() as db:
        await BillingCloseJob().run(db, org, datetime.date(2026, 7, 28))

    async with session_factory() as db:
        await billing_service.ensure_future_periods(db, org.id, count=3)

    periods = await _roster(session_factory, org.id)
    assert len([p for p in periods if p.end_date is None]) == 1
    closed = [p for p in periods if p.end_date is not None]
    for earlier, later in zip(closed, closed[1:]):
        assert earlier.end_date < later.start_date
    # The open row's start is not duplicated by a freshly created stub.
    assert len({p.start_date for p in periods}) == len(periods)


# ── TBD-241 code review, F3: the convergence audit must not over-report ────
#
# `close_period` returns a perfectly good open row on two paths that WRITE
# NOTHING — the F1 row lock, and D4 step 4, both finding that a racer already
# closed this period. D10 defines `closed_period_ids` as the rows whose
# `end_date` THIS RUN wrote and `closed_on` as the last applied close date, so
# neither may be derived from a close the run merely observed.
#
# `close_period` reports what it applied through its `closed_ids`
# out-parameter, so the seam these two tests stand on is exactly that: a call
# that returns a row and leaves `closed_ids` untouched. Simulating it at the
# seam is deliberate — under `StaticPool` a real second session shares one
# transaction, so a genuine racer cannot be committed against the same fixture.


async def test_a_step_absorbed_by_a_racer_is_not_counted_as_ours(
    session_factory, monkeypatch
):
    """The first iteration is absorbed; only the two we applied are reported."""
    captured = _capture_side_effects(monkeypatch)
    org = await _seed_lapsed_org(session_factory)
    job = BillingCloseJob()
    today = datetime.date(2026, 7, 28)

    real_close = billing_service.close_period
    calls = {"n": 0}

    async def racer_wins_the_first_step(db, org_id, close_date=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # What `close_period` does on its racer branch: the roster advances,
            # a row comes back, and `closed_ids` is left untouched.
            kwargs.pop("closed_ids", None)
            return await real_close(db, org_id, close_date, **kwargs)
        return await real_close(db, org_id, close_date, **kwargs)

    monkeypatch.setattr(billing_service, "close_period", racer_wins_the_first_step)

    async with session_factory() as db:
        res = await job.run(db, org, today)

    assert calls["n"] == 3, "the loop must still converge in three iterations"
    assert res.counts["steps"] == 2, "the absorbed step is not one of ours"
    assert res.counts["closed_on"] == "2026-07-24"

    periods = await _roster(session_factory, org.id)
    assert res.counts["closed_period_ids"] == [p.id for p in periods[1:3]], (
        "only the rows this run closed itself"
    )
    assert captured["audits"][0]["detail"]["steps"] == 2


async def test_a_run_that_applied_nothing_reports_no_close_date(
    session_factory, monkeypatch
):
    """Every iteration absorbed: `closed_on` is null rather than a crash.

    Deriving `closed_on` only from applied steps means it can legitimately have
    no value, and `counts` must survive that. Before F3 the loop always had a
    date because it always counted; now it has to say so honestly.
    """
    _capture_side_effects(monkeypatch)
    org = await _seed(session_factory, cycle_day=25, period_start=datetime.date(2026, 6, 25))
    await _add_stub(
        session_factory, org.id, datetime.date(2026, 7, 25), datetime.date(2026, 8, 24)
    )
    job = BillingCloseJob()
    today = datetime.date(2026, 7, 28)

    real_close = billing_service.close_period

    async def always_absorbed(db, org_id, close_date=None, **kwargs):
        kwargs.pop("closed_ids", None)
        return await real_close(db, org_id, close_date, **kwargs)

    monkeypatch.setattr(billing_service, "close_period", always_absorbed)

    async with session_factory() as db:
        res = await job.run(db, org, today)

    assert res.outcome == "success"
    assert res.counts["steps"] == 0
    assert res.counts["closed_period_ids"] == []
    assert res.counts["closed_on"] is None
    assert res.counts["new_period_start"] == "2026-07-25"


async def test_convergence_survives_a_close_that_rolled_back_the_identity_map(
    session_factory, monkeypatch
):
    """F2 — the `MissingGreenlet` the loop was one attribute read away from.

    The loop reads the closing row's id and start AFTER calling `close_period`.
    `close_period`'s D4 path calls `db.rollback()`, which expires the whole
    identity map and then repopulates only the row at `current_id`; when
    `close_period`'s own `get_current_period` picked a different row than the
    loop's, the loop's instance stays expired and the attribute read fires a
    SYNC lazy-load inside an async session.

    Simulated at the seam, and precisely: expire everything (what the rollback
    does) and then repopulate only the row `close_period` returns (what its
    by-id re-fetch and its closing `db.refresh(new_period)` do). The loop's own
    instance is left expired, which is the exact production state. It must
    already have been snapshotted into plain values.
    """
    _capture_side_effects(monkeypatch)
    org = await _seed_lapsed_org(session_factory)
    job = BillingCloseJob()
    today = datetime.date(2026, 7, 28)

    real_close = billing_service.close_period

    async def close_then_expire(db, org_id, close_date=None, **kwargs):
        result = await real_close(db, org_id, close_date, **kwargs)
        # `close_period` commits internally, so a rollback here changes no data;
        # it only expires the identity map, which is the half of D4 that bites.
        await db.rollback()
        db.expire_all()
        await db.refresh(result)
        return result

    monkeypatch.setattr(billing_service, "close_period", close_then_expire)

    async with session_factory() as db:
        res = await job.run(db, org, today)

    assert res.counts["steps"] == 3
    assert res.counts["new_period_start"] == "2026-07-25"
