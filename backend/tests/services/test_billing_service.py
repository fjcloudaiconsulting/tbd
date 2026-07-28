"""Tests for billing_service.close_period — covers the duplicate-stub regression
that PR #93 fixes, plus the race-recovery defensive layer.

TBD-241 (spec ``specs/2026-07-28-close-period-chain-close-design.md``) added the
chain-close clamp, the D1 upper bound, the D2 ``today=`` thread, D4's rewritten
re-entrancy and D5's budget-snapshot re-anchor. Its test plan is §5.

Dates in the TBD-241 tests are fixed calendar dates paired with an explicit
``today=`` kwarg rather than offsets from the wall clock: D1 rejects a close
date after ``today``, so an unpinned fixture would flip from green to red the
moment the clock crossed it (see the wall-clock date-bomb class the spec's D2
section calls out at ``test_scheduler_job_billing_close.py:54-66``).
"""
from __future__ import annotations

import asyncio
import datetime
import types
from decimal import Decimal

import pytest
import pytest_asyncio
import structlog.testing
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category
from app.models.user import Organization
from app.services import billing_service
from app.services.exceptions import ConflictError, ValidationError


@pytest_asyncio.fixture
async def session_factory():
    """In-memory SQLite shared across sessions via StaticPool."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_org_with_open_period(
    factory: async_sessionmaker[AsyncSession],
    *,
    org_id: int = 1,
    start: datetime.date | None = None,
) -> datetime.date:
    start = start or (datetime.date.today() - datetime.timedelta(days=10))
    async with factory() as db:
        db.add(Organization(id=org_id, name="test-org", billing_cycle_day=1))
        await db.commit()
        db.add(BillingPeriod(org_id=org_id, start_date=start, end_date=None))
        await db.commit()
    return start


async def _add_period(
    factory: async_sessionmaker[AsyncSession],
    org_id: int,
    start: datetime.date,
    end: datetime.date | None = None,
) -> int:
    async with factory() as db:
        period = BillingPeriod(org_id=org_id, start_date=start, end_date=end)
        db.add(period)
        await db.commit()
        return period.id


async def _add_category(
    factory: async_sessionmaker[AsyncSession], org_id: int, name: str = "Groceries"
) -> int:
    async with factory() as db:
        category = Category(org_id=org_id, name=name, slug=name.lower())
        db.add(category)
        await db.commit()
        return category.id


async def _add_budget(
    factory: async_sessionmaker[AsyncSession],
    org_id: int,
    category_id: int,
    start: datetime.date,
    end: datetime.date | None = None,
) -> int:
    async with factory() as db:
        budget = Budget(
            org_id=org_id,
            category_id=category_id,
            amount=Decimal("100.00"),
            period_start=start,
            period_end=end,
        )
        db.add(budget)
        await db.commit()
        return budget.id


async def _budget(factory: async_sessionmaker[AsyncSession], budget_id: int) -> Budget:
    async with factory() as db:
        return (
            await db.execute(select(Budget).where(Budget.id == budget_id))
        ).scalar_one()


async def _periods(
    factory: async_sessionmaker[AsyncSession], org_id: int
) -> list[BillingPeriod]:
    """Every period for the org, ordered by start_date."""
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(BillingPeriod)
                    .where(BillingPeriod.org_id == org_id)
                    .order_by(BillingPeriod.start_date)
                )
            ).scalars().all()
        )


def _patch_existence_check_miss(monkeypatch) -> dict:
    """Force step g's existence check to miss — EXACTLY ONCE.

    Both fixtures in this area use ``StaticPool`` over a single in-memory
    SQLite connection, so genuine concurrency is impossible and every racer
    must be simulated (spec §5, "Racer simulation"): the peer row is committed
    up front and our own existence check is patched to miss it, which drives
    the INSERT branch and trips ``IntegrityError`` on the commit.

    Two properties are load-bearing, and both were defects in the version this
    replaces:

    * **Semantic, not ordinal.** The old gate counted ``AsyncSession.scalar``
      calls and pinned the existence check at #2. It is #1 before TBD-241 and
      #2 after (D5's Budget SELECT at ``billing_service.py:336`` shifts it), so
      an ordinal gate silently stops matching. This one keys on the compiled
      statement instead.
    * **One-shot.** Step g issues the *same* statement again on D4 step 5's
      retry. A patch that missed both times would drive the retry down the
      INSERT branch too and raise the second ``IntegrityError`` D4 propagates,
      making "the commit succeeds" unassertable.

    The shape (``billing_periods`` + ``start_date``) also matches
    ``_next_period_start``'s ``SELECT min(billing_periods.start_date) ...``.
    That is why §2 pins ``_next_period_start`` and step b′ to
    ``db.execute``/``db.scalars``: if either used ``db.scalar`` it would
    consume the single firing, the clamp would be suppressed, step g would find
    the peer row for real and D4 would never be entered at all.
    """
    real_scalar = AsyncSession.scalar
    state = {"fired": False}

    async def patched_scalar(self, statement, *args, **kwargs):
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        if (
            not state["fired"]
            and "billing_periods" in compiled
            and "start_date" in compiled
        ):
            state["fired"] = True
            return None
        return await real_scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", patched_scalar)
    return state


def _count_close_steps(monkeypatch) -> dict:
    """Count ``_apply_close_step`` invocations.

    The helper is non-recursive by design (spec §2), so a second call can only
    originate in ``close_period``'s ``except IntegrityError`` arm. Counting
    calls is therefore the same signal as a counter placed inside that arm, and
    it needs no production hook.
    """
    real = billing_service._apply_close_step
    calls = {"n": 0}

    async def counting(*args, **kwargs):
        calls["n"] += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(billing_service, "_apply_close_step", counting)
    return calls


def _capture_locking_selects(monkeypatch) -> list[str]:
    """Record every statement issued through ``db.execute`` that carries
    ``FOR UPDATE``.

    Code review F1. The fixtures here run on SQLite over a single ``StaticPool``
    connection, so genuine concurrency is impossible and a row lock can never be
    *exercised* — and SQLite's dialect drops ``FOR UPDATE`` from the emitted SQL
    entirely. What CAN be pinned is the decision: the statement object carries
    ``_for_update_arg``, so this asserts the lock was asked for even though the
    dialect declines to take it.
    """
    real_execute = AsyncSession.execute
    seen: list[str] = []

    async def recording(self, statement, *args, **kwargs):
        if getattr(statement, "_for_update_arg", None) is not None:
            seen.append(str(statement))
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", recording)
    return seen


def _capture_reanchor(monkeypatch) -> list[dict]:
    """Record every ``reanchor_period_dependents`` call.

    Each entry carries the call's kwargs, its **return value** (the number of
    budget rows the narrowed UPDATE actually touched) and whether a
    ``BillingPeriod`` INSERT was already pending when it ran.

    Both extras answer a code-review finding:

    * ``rows`` replaces a tautological assertion (F6). A budget seeded with
      exactly the value D5 writes cannot distinguish "skipped" from "rewritten
      identically" by its own column; the narrowed UPDATE's rowcount can.
    * ``insert_pending`` is the real D5 ordering fence (F5). The claim it
      replaces — that a misplaced D5 "escapes ``close_period``'s try" — is
      false, because that try wraps the entire ``_apply_close_step`` call. What
      a misplaced D5 actually does is trip
      ``reanchor_period_dependents``' own ``IntegrityError`` backstop, which
      rolls back the close and re-reports it as a 409
      ``budget_period_conflict``, and escape for real from the retry
      invocation, which has no try around it.
    """
    real = billing_service.reanchor_period_dependents
    calls: list[dict] = []

    async def recording(db, **kwargs):
        # Record BEFORE the call, not after. A misplaced D5 raises from its own
        # autoflush, and an entry appended only on success would silently drop
        # the exact call the `insert_pending` fence exists to catch — which is
        # how the first draft of this helper passed against a deliberately
        # broken build.
        entry = {
            **kwargs,
            "rows": None,
            "insert_pending": any(
                isinstance(obj, BillingPeriod) for obj in db.sync_session.new
            ),
        }
        calls.append(entry)
        entry["rows"] = await real(db, **kwargs)
        return entry["rows"]

    monkeypatch.setattr(billing_service, "reanchor_period_dependents", recording)
    return calls


@pytest.mark.asyncio
async def test_close_period_inserts_new_open_period_when_no_stub_exists(
    session_factory,
):
    org_id = 1
    start = await _seed_org_with_open_period(session_factory, org_id=org_id)

    async with session_factory() as db:
        result = await billing_service.close_period(db, org_id)

    today = datetime.date.today()
    assert result.end_date is None
    assert result.start_date == today

    async with session_factory() as db:
        periods = (
            await db.execute(
                select(BillingPeriod)
                .where(BillingPeriod.org_id == org_id)
                .order_by(BillingPeriod.start_date)
            )
        ).scalars().all()
    assert [p.start_date for p in periods] == [start, today]
    assert periods[0].end_date == today - datetime.timedelta(days=1)
    assert periods[1].end_date is None


@pytest.mark.asyncio
async def test_close_period_revives_existing_stub_at_new_start(session_factory):
    """Reproduces PR #93: a future stub at close_date+1 must be revived,
    not duplicated."""
    org_id = 1
    today = datetime.date.today()
    start = today - datetime.timedelta(days=10)
    await _seed_org_with_open_period(session_factory, org_id=org_id, start=start)

    # Pre-existing stub at exactly close_date+1 (= today by default).
    stub_end = today + datetime.timedelta(days=29)
    async with session_factory() as db:
        db.add(BillingPeriod(org_id=org_id, start_date=today, end_date=stub_end))
        await db.commit()
        stub_id = (
            await db.scalar(
                select(BillingPeriod.id).where(
                    BillingPeriod.org_id == org_id,
                    BillingPeriod.start_date == today,
                )
            )
        )

    async with session_factory() as db:
        result = await billing_service.close_period(db, org_id)

    assert result.id == stub_id, "stub should be revived, not duplicated"
    assert result.end_date is None, "revived stub must be open (end_date=None)"
    assert result.start_date == today

    async with session_factory() as db:
        all_periods = (
            await db.execute(
                select(BillingPeriod).where(BillingPeriod.org_id == org_id)
            )
        ).scalars().all()
    assert len(all_periods) == 2, "no duplicate row created"


@pytest.mark.asyncio
async def test_close_period_recovers_from_integrity_error_on_concurrent_insert(
    session_factory, monkeypatch
):
    """TBD-241 test 10 — D4 step 5: our INSERT lost to a peer at ``new_start``.

    ``close_period`` must roll back, re-run ``_apply_close_step`` exactly once,
    and converge on the peer's row instead of returning a 500.

    ⚠ Re-anchored by TBD-241 §5. The previous gate was
    ``call_count["n"] == 2`` and **never matched**: ``get_current_period`` uses
    ``db.execute``, not ``db.scalar``, so the existence check was scalar call
    #1, the patch never forced a miss, the revive branch ran, and the
    ``IntegrityError`` recovery block had **zero** coverage while every
    assertion still passed. The gate is now semantic (statement shape) and
    one-shot, and the test additionally asserts the recovery path was entered.
    """
    org_id = 1
    today = datetime.date(2026, 7, 28)
    resolved = datetime.date(2026, 7, 27)   # default close date = today - 1
    new_start = datetime.date(2026, 7, 28)
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 7, 18)
    )
    category_id = await _add_category(session_factory, org_id)
    # A budget on the closing period, so D5's identity re-anchor has real work
    # to do — and, because the rollback discards it, has to be re-issued by the
    # retry rather than lost (spec D4 step 5).
    budget_id = await _add_budget(
        session_factory, org_id, category_id, datetime.date(2026, 7, 18)
    )

    # Simulate the race: a peer has already inserted at new_start, but our
    # existence-check is patched to miss it (as it would if our SELECT ran
    # before the peer's commit was visible). The INSERT then collides.
    peer_id = await _add_period(
        session_factory, org_id, new_start, datetime.date(2026, 8, 12)
    )

    miss = _patch_existence_check_miss(monkeypatch)
    steps = _count_close_steps(monkeypatch)

    async with session_factory() as db:
        result = await billing_service.close_period(db, org_id, today=today)

    assert miss["fired"] is True, "the existence check was never forced to miss"
    # The helper is non-recursive, so a SECOND call can only come from the
    # `except IntegrityError` arm. This is the recovery-entered assertion §5
    # requires so the gate cannot silently revert to trivial coverage.
    assert steps["n"] == 2, "D4's IntegrityError recovery never ran"

    assert result.id == peer_id, "must converge on the peer's row, not a new one"
    assert result.end_date is None

    periods = await _periods(session_factory, org_id)
    assert len(periods) == 2, "race recovery must not leave duplicates"
    closed = [p for p in periods if p.end_date is not None]
    open_ = [p for p in periods if p.end_date is None]
    assert len(closed) == 1 and len(open_) == 1
    assert closed[0].end_date == resolved

    # D5 was re-issued on the retry rather than lost to the rollback.
    budget = await _budget(session_factory, budget_id)
    assert budget.period_end == resolved


@pytest.mark.asyncio
async def test_close_period_rejects_close_date_before_period_start(session_factory):
    org_id = 1
    today = datetime.date.today()
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=today - datetime.timedelta(days=2)
    )

    with pytest.raises(ValidationError):
        async with session_factory() as db:
            await billing_service.close_period(
                db, org_id, close_date=today - datetime.timedelta(days=10)
            )


# ── TBD-241: chain-close ──────────────────────────────────────────────────
#
# Spec: specs/2026-07-28-close-period-chain-close-design.md
#
# The two tests at the top of this file are §5 test 1's regression fence: with
# no later rows the BillingPeriod outcomes are exactly what they were before
# TBD-241. Not byte identical — D5 now writes Budget.period_end where the old
# code wrote nothing — which is why test 1 is scoped to period rows.
#
# Every close below pins `today=` (D2). D1 rejects a close date after today, so
# a fixture built from the wall clock would be a date bomb.

_TODAY = datetime.date(2026, 7, 28)


@pytest.mark.asyncio
async def test_close_clamps_to_the_single_intervening_stub(session_factory):
    """§5 test 2 — the scheduler's one-jump close over a lapsed org.

    The requested date (07-24) reaches past the stub's start, so the close is
    clamped to `stub.start - 1` and the stub is revived through the existing
    exact-start revive. No row is deleted, no row is inserted.
    """
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 4, 25)
    )
    stub_id = await _add_period(
        session_factory, org_id, datetime.date(2026, 5, 25), datetime.date(2026, 6, 24)
    )

    async with session_factory() as db:
        result = await billing_service.close_period(
            db, org_id, datetime.date(2026, 7, 24), today=_TODAY
        )

    assert result.id == stub_id, "the stub at s0 becomes the new open period"
    assert result.start_date == datetime.date(2026, 5, 25)
    assert result.end_date is None

    periods = await _periods(session_factory, org_id)
    assert [p.start_date for p in periods] == [
        datetime.date(2026, 4, 25), datetime.date(2026, 5, 25),
    ], "no row inserted, no row deleted"
    assert periods[0].end_date == datetime.date(2026, 5, 24), "clamped to s0 - 1 day"


@pytest.mark.asyncio
async def test_close_clamps_to_the_first_of_three_stubs(session_factory):
    """§5 test 3 — one step per call: the two later stubs are untouched."""
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 4, 25)
    )
    for start, end in [
        (datetime.date(2026, 5, 25), datetime.date(2026, 6, 24)),
        (datetime.date(2026, 6, 25), datetime.date(2026, 7, 24)),
        (datetime.date(2026, 7, 25), datetime.date(2026, 8, 24)),
    ]:
        await _add_period(session_factory, org_id, start, end)

    async with session_factory() as db:
        result = await billing_service.close_period(
            db, org_id, datetime.date(2026, 7, 24), today=_TODAY
        )

    assert result.start_date == datetime.date(2026, 5, 25)

    periods = await _periods(session_factory, org_id)
    assert [(p.start_date, p.end_date) for p in periods] == [
        (datetime.date(2026, 4, 25), datetime.date(2026, 5, 24)),
        (datetime.date(2026, 5, 25), None),
        (datetime.date(2026, 6, 25), datetime.date(2026, 7, 24)),
        (datetime.date(2026, 7, 25), datetime.date(2026, 8, 24)),
    ]


@pytest.mark.asyncio
async def test_close_does_not_clamp_when_the_date_stops_short_of_the_stub(
    session_factory,
):
    """§5 test 4 — `s0 > close_date`, so the clamp does not fire."""
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 6, 25)
    )
    stub_id = await _add_period(
        session_factory, org_id, datetime.date(2026, 7, 25), datetime.date(2026, 8, 24)
    )

    async with session_factory() as db:
        result = await billing_service.close_period(
            db, org_id, datetime.date(2026, 7, 24), today=_TODAY
        )

    assert result.id == stub_id
    assert result.start_date == datetime.date(2026, 7, 25)
    periods = await _periods(session_factory, org_id)
    assert periods[0].end_date == datetime.date(2026, 7, 24), "the requested date"
    assert len(periods) == 2


@pytest.mark.asyncio
async def test_clamp_target_is_min_start_not_insert_order(session_factory):
    """§5 test 5 — the target is MIN(start_date), asserted against a
    deliberately unordered insert order."""
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 4, 25)
    )
    await _add_period(
        session_factory, org_id, datetime.date(2026, 7, 25), datetime.date(2026, 8, 24)
    )
    earliest_id = await _add_period(
        session_factory, org_id, datetime.date(2026, 5, 25), datetime.date(2026, 6, 24)
    )
    await _add_period(
        session_factory, org_id, datetime.date(2026, 6, 25), datetime.date(2026, 7, 24)
    )

    async with session_factory() as db:
        result = await billing_service.close_period(
            db, org_id, datetime.date(2026, 7, 24), today=_TODAY
        )

    assert result.id == earliest_id
    assert result.start_date == datetime.date(2026, 5, 25)


@pytest.mark.asyncio
async def test_straddling_row_is_excluded_from_clamp_selection_and_logged(
    session_factory,
):
    """§5 test 6 — D12.

    A row starting at or before `current.start_date` but ending after it is a
    pre-existing overlap. It is excluded from clamp selection (candidates are
    `start_date > current.start_date` *strictly*), so it can never drag the
    clamped date below the lower bound — the hazard that killed the 409 design.
    It is logged, not repaired.

    The second half pins the RESIDUAL: with no clamp candidate the close still
    INSERTs an open row *inside* the straddler. That is §1 defect 3, unrepaired
    and deliberately out of scope (TBD-235). Asserted so the limitation is
    recorded rather than discovered later.
    """
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 6, 25)
    )
    straddler_id = await _add_period(
        session_factory, org_id, datetime.date(2026, 6, 1), datetime.date(2026, 8, 31)
    )

    with structlog.testing.capture_logs() as logs:
        async with session_factory() as db:
            result = await billing_service.close_period(db, org_id, today=_TODAY)

    ignored = [e for e in logs if e.get("event") == "billing.close.straddling_row_ignored"]
    assert len(ignored) == 1
    assert ignored[0]["org_id"] == org_id
    assert ignored[0]["period_id"] == straddler_id
    assert not [e for e in logs if e.get("event") == "billing.close.clamped"]

    # The residual: an open row now sits wholly inside the straddler.
    assert result.start_date == datetime.date(2026, 7, 28)
    assert result.end_date is None
    periods = await _periods(session_factory, org_id)
    assert [(p.start_date, p.end_date) for p in periods] == [
        (datetime.date(2026, 6, 1), datetime.date(2026, 8, 31)),
        (datetime.date(2026, 6, 25), datetime.date(2026, 7, 27)),
        (datetime.date(2026, 7, 28), None),
    ]


@pytest.mark.asyncio
async def test_duplicate_open_row_is_not_reported_as_straddling(session_factory):
    """§5 test 6b — D12 excludes `end_date IS NULL`, so a second OPEN row at an
    earlier start is not a straddler. Only `get_current_period`'s own warning
    fires."""
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 4, 25)
    )
    await _add_period(session_factory, org_id, datetime.date(2026, 6, 25), None)

    with structlog.testing.capture_logs() as logs:
        async with session_factory() as db:
            await billing_service.close_period(db, org_id, today=_TODAY)

    assert not [
        e for e in logs if e.get("event") == "billing.close.straddling_row_ignored"
    ]
    assert [e for e in logs if e.get("event") == "multiple open billing periods"]


@pytest.mark.asyncio
async def test_duplicate_open_rows_are_neither_repaired_nor_worsened(session_factory):
    """§5 test 7 — non-interference.

    `get_current_period` returns the open row with the GREATEST start_date, and
    the clamp predicate is `start_date > current.start_date`, so a second open
    row at an earlier start is structurally invisible to clamp selection. The
    close operates on the newest open row and leaves the older one alone.
    Repair is TBD-235.
    """
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 4, 25)
    )
    older_id = await _add_period(session_factory, org_id, datetime.date(2026, 6, 25), None)
    # `_seed_org_with_open_period` created 04-25; the row above is the newer
    # open one, so `get_current_period` returns it.
    async with session_factory() as db:
        result = await billing_service.close_period(db, org_id, today=_TODAY)

    assert result.start_date == datetime.date(2026, 7, 28)

    periods = await _periods(session_factory, org_id)
    assert [(p.start_date, p.end_date) for p in periods] == [
        (datetime.date(2026, 4, 25), None),      # untouched, still open
        (datetime.date(2026, 6, 25), datetime.date(2026, 7, 27)),
        (datetime.date(2026, 7, 28), None),
    ]
    assert periods[1].id == older_id
    assert len(periods) == 3, "no third overlapping row beyond the new open one"


@pytest.mark.asyncio
async def test_close_date_in_the_future_is_rejected(session_factory):
    """§5 test 8 — D1. Strict `>`, so closing *today* stays legal."""
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 7, 1)
    )

    with pytest.raises(ValidationError) as excinfo:
        async with session_factory() as db:
            await billing_service.close_period(
                db, org_id, _TODAY + datetime.timedelta(days=1), today=_TODAY
            )
    # D7 pins the frontend predicate against this exact sentence.
    assert "cannot be in the future" in str(excinfo.value.detail).lower()

    async with session_factory() as db:
        result = await billing_service.close_period(db, org_id, _TODAY, today=_TODAY)
    assert result.start_date == _TODAY + datetime.timedelta(days=1)


@pytest.mark.asyncio
async def test_today_kwarg_is_authoritative_and_the_clock_is_not_consulted(
    session_factory, monkeypatch
):
    """§5 test 9 — D2.

    The scheduler's tick can straddle midnight, so `today` is threaded in
    rather than re-read. When it is passed, `close_period` must not consult
    `date.today()` at all on this path.
    """
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 7, 1)
    )

    class _NoClockDate(datetime.date):
        @classmethod
        def today(cls):
            raise AssertionError("close_period consulted the wall clock")

    monkeypatch.setattr(
        billing_service,
        "datetime",
        types.SimpleNamespace(
            date=_NoClockDate,
            timedelta=datetime.timedelta,
            datetime=datetime.datetime,
        ),
    )

    async with session_factory() as db:
        result = await billing_service.close_period(db, org_id, today=_TODAY)

    assert result.start_date == _TODAY, "closed yesterday relative to `today=`"


@pytest.mark.asyncio
async def test_racer_that_already_closed_our_period_returns_its_open_row(
    session_factory, monkeypatch
):
    """§5 test 11 — D4 step 4, the branch revision 1 turned into a 400.

    Construction (spec §5): seeding `current.end_date` up front cannot work —
    `get_current_period` filters `end_date IS NULL`, so it would auto-create a
    different period and `current.id` would never be the seeded row's. And
    under `StaticPool` a second session shares one transaction, so a racer's
    commit would be rolled back along with ours. The racer's close is therefore
    injected between D4's `rollback()` and its by-id re-fetch, where the
    session is momentarily clean.

    ⚠ One deviation from §5's recipe, which is not writable as literally
    stated. It says to seed the peer row at `resolved + 1` OPEN so D4 step 4's
    `end_date IS NULL` assertion passes. But `get_current_period` returns the
    open row with the GREATEST start_date, so an open row seeded at 07-28 IS
    the row `close_period` picks up as `current` — and `requested` (07-27) then
    trips the lower bound at step 5 before any of this is reached. The peer is
    therefore seeded CLOSED and the injected racer transaction both closes our
    row AND revives the peer, which is exactly the pair of writes a real racing
    `close_period` commits. Step 4 still sees the peer open, which is the
    property the recipe was after.
    """
    org_id = 1
    resolved = datetime.date(2026, 7, 27)
    new_start = datetime.date(2026, 7, 28)
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 6, 25)
    )
    async with session_factory() as db:
        current_id = await db.scalar(
            select(BillingPeriod.id).where(BillingPeriod.org_id == org_id)
        )
    # The row the racer will revive. Seeded closed — see the docstring.
    peer_id = await _add_period(
        session_factory, org_id, new_start, datetime.date(2026, 8, 27)
    )

    _patch_existence_check_miss(monkeypatch)
    steps = _count_close_steps(monkeypatch)

    real_rollback = AsyncSession.rollback
    injected = {"fired": False}

    async def patched_rollback(self):
        await real_rollback(self)
        if not injected["fired"]:
            injected["fired"] = True
            # The racer's whole transaction: close our period and revive the
            # row at `resolved + 1`.
            await self.execute(
                update(BillingPeriod)
                .where(BillingPeriod.id == current_id)
                .values(end_date=resolved)
            )
            await self.execute(
                update(BillingPeriod)
                .where(BillingPeriod.id == peer_id)
                .values(end_date=None)
            )
            await self.commit()

    monkeypatch.setattr(AsyncSession, "rollback", patched_rollback)

    async with session_factory() as db:
        result = await billing_service.close_period(db, org_id, today=_TODAY)

    assert injected["fired"] is True, "the racer was never injected"
    assert steps["n"] == 1, "D4 step 4 must NOT re-run the close step"
    assert result.id == peer_id
    assert result.start_date == new_start
    assert result.end_date is None

    periods = await _periods(session_factory, org_id)
    assert [(p.id, p.end_date) for p in periods] == [
        (current_id, resolved),
        (peer_id, None),
    ], "the racer's state is returned untouched"


@pytest.mark.asyncio
async def test_d5_runs_before_any_pending_insert_so_the_race_stays_recoverable(
    session_factory, monkeypatch
):
    """§5 test 12 — the D5 ordering fence.

    Placing D5 after `db.add(new_period)` is the naive choice, because D5 needs
    the clamped date. Two things then go wrong with a peer racing at
    `new_start`:

    (a) `reanchor_period_dependents` carries its OWN `IntegrityError` backstop
        around the shared-tail UPDATE. An autoflushed violation caught there is
        followed by `db.rollback()` — which discards the close — and re-reported
        as `ConflictError("budget_period_conflict")`: a *budget* conflict
        announced for a concurrent *period* close, which the route answers as a
        409 instead of recovering.
    (b) the RETRY invocation inside `close_period`'s `except` arm has no try
        around it (deliberately: the helper is non-recursive and a second
        `IntegrityError` is meant to propagate), so a misplaced D5 that raises
        there escapes as a genuine unhandled 500.

    ⚠ The claim this docstring used to make — that a misplaced D5 "raises
    IntegrityError OUTSIDE `close_period`'s try" — is **false**, and so was the
    matching sentence in the production docstring. That try wraps the *entire*
    `_apply_close_step` call, so an autoflush from anywhere in steps a-i is
    caught and drives D4. Both code reviewers flagged it independently (F5).

    That correction matters for the test, not just the prose: with D5 misplaced,
    this fixture would still CONVERGE — the raw `IntegrityError` reaches D4, the
    rollback clears the pending INSERT, and the retry finds the peer row for
    real and takes the revive branch, where nothing is pending and D5 is
    harmless. So "no ConflictError, no IntegrityError, right final state" does
    not discriminate on its own. The discriminating assertion is
    `insert_pending`: D5 must be called with NO `BillingPeriod` INSERT pending,
    on every invocation, which is the invariant the whole ordering exists to
    maintain.
    """
    org_id = 1
    resolved = datetime.date(2026, 7, 27)
    new_start = datetime.date(2026, 7, 28)
    closing_start = datetime.date(2026, 6, 25)
    await _seed_org_with_open_period(session_factory, org_id=org_id, start=closing_start)
    category_id = await _add_category(session_factory, org_id)
    other_category_id = await _add_category(session_factory, org_id, name="Transport")
    closing_budget = await _add_budget(session_factory, org_id, category_id, closing_start)
    # The peer row is closed and carries its own budget, so BOTH D5 calls do
    # real UPDATEs on the retry.
    peer_id = await _add_period(
        session_factory, org_id, new_start, datetime.date(2026, 8, 27)
    )
    peer_budget = await _add_budget(
        session_factory, org_id, other_category_id, new_start,
        datetime.date(2026, 8, 27),
    )

    _patch_existence_check_miss(monkeypatch)
    steps = _count_close_steps(monkeypatch)
    reanchors = _capture_reanchor(monkeypatch)

    try:
        async with session_factory() as db:
            result = await billing_service.close_period(db, org_id, today=_TODAY)
    except ConflictError as exc:  # pragma: no cover — the regression
        pytest.fail(f"a concurrent period close was reported as {exc.code}")
    except IntegrityError:  # pragma: no cover — the regression
        pytest.fail("IntegrityError escaped close_period's recovery (500)")

    assert steps["n"] == 2, "D4's recovery must have run"
    # The fence. Every D5 call — both the closing-row one and the revive-side
    # one, on the first attempt and on the retry — must run with no pending
    # BillingPeriod INSERT.
    assert reanchors, "D5 was never called"
    assert not any(c["insert_pending"] for c in reanchors), (
        "D5 ran with a BillingPeriod INSERT pending: its autoflush can now be "
        "converted into a 409 budget_period_conflict, or escape from the retry"
    )
    assert result.id == peer_id
    assert result.end_date is None
    assert (await _budget(session_factory, closing_budget)).period_end == resolved
    assert (await _budget(session_factory, peer_budget)).period_end is None


@pytest.mark.asyncio
async def test_d5_refreshes_the_budget_snapshot_on_both_rows(
    session_factory, monkeypatch
):
    """§5 test 13 — D5, both directions.

    `Budget.period_end` is a stored snapshot written at creation time. A budget
    created while its period was open carries NULL forever, and
    `_compute_spent` then drops its upper bound for a period that is in fact
    closed. The closing period's budgets are re-anchored to the resolved close
    date; the revived row's budgets are re-anchored back to NULL, because that
    row is open again.

    ⚠ `new_end` is the RESOLVED date, never the raw `close_date` parameter —
    which is `None` on every UI close and would blank `period_end` on every
    budget of the closing period.
    """
    org_id = 1
    closing_start = datetime.date(2026, 6, 25)
    stub_start = datetime.date(2026, 7, 25)
    await _seed_org_with_open_period(session_factory, org_id=org_id, start=closing_start)
    stub_id = await _add_period(
        session_factory, org_id, stub_start, datetime.date(2026, 8, 24)
    )
    groceries = await _add_category(session_factory, org_id)
    transport = await _add_category(session_factory, org_id, name="Transport")

    stale = await _add_budget(session_factory, org_id, groceries, closing_start)
    # Already carries the value D5 would write: the narrowed UPDATE must leave
    # it alone rather than over-report or 409 against itself.
    already_correct = await _add_budget(
        session_factory, org_id, transport, closing_start, datetime.date(2026, 7, 24)
    )
    revived_budget = await _add_budget(
        session_factory, org_id, groceries, stub_start, datetime.date(2026, 8, 24)
    )

    reanchors = _capture_reanchor(monkeypatch)

    async with session_factory() as db:
        result = await billing_service.close_period(
            db, org_id, datetime.date(2026, 7, 24), today=_TODAY
        )

    assert result.id == stub_id
    assert (await _budget(session_factory, stale)).period_end == datetime.date(2026, 7, 24)
    assert (
        await _budget(session_factory, already_correct)
    ).period_end == datetime.date(2026, 7, 24)
    assert (await _budget(session_factory, revived_budget)).period_end is None

    # ⚠ Asserting `already_correct.period_end` alone is TAUTOLOGICAL: it was
    # seeded with exactly the value D5 writes, so the column cannot distinguish
    # "left alone by the narrowed UPDATE" from "rewritten identically" (code
    # review F6). The narrowed UPDATE's ROWCOUNT can, and does: 1 of the 2
    # budgets at the closing start, not 2.
    closing_calls = [c for c in reanchors if c["old_start"] == closing_start]
    assert len(closing_calls) == 1
    assert closing_calls[0]["new_end"] == datetime.date(2026, 7, 24)
    assert closing_calls[0]["rows"] == 1, (
        "the identity re-anchor must narrow its UPDATE to the stale row; "
        "2 means it rewrote the already-correct budget as well"
    )
    revive_calls = [c for c in reanchors if c["old_start"] == stub_start]
    assert len(revive_calls) == 1
    assert revive_calls[0]["new_end"] is None
    assert revive_calls[0]["rows"] == 1


@pytest.mark.asyncio
async def test_revive_emits_the_overwritten_end_date_on_every_revive(session_factory):
    """§5 test 20b — D10's `revived_previous_end`.

    Nulling the revived row's `end_date` is chain-close's one irreversible
    write, and D8 concedes an admin can hand-build a settled closed period that
    the clamp reopens. The overwritten value therefore rides its OWN event, not
    `billing.close.clamped`: the unclamped revive is the common path and emits
    no `clamped` event at all.
    """
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 6, 25)
    )
    unclamped_id = await _add_period(
        session_factory, org_id, datetime.date(2026, 7, 25), datetime.date(2026, 8, 24)
    )

    # Unclamped revive: s0 == new_start already, so no clamp fires.
    with structlog.testing.capture_logs() as logs:
        async with session_factory() as db:
            await billing_service.close_period(
                db, org_id, datetime.date(2026, 7, 24), today=_TODAY
            )
    assert not [e for e in logs if e.get("event") == "billing.close.clamped"]
    revived = [e for e in logs if e.get("event") == "billing.close.revived"]
    assert len(revived) == 1
    assert revived[0]["org_id"] == org_id
    assert revived[0]["revived_period_id"] == unclamped_id
    assert revived[0]["revived_previous_end"] == "2026-08-24"

    # Clamped revive, on a fresh org.
    org_id = 2
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 4, 25)
    )
    clamped_id = await _add_period(
        session_factory, org_id, datetime.date(2026, 5, 25), datetime.date(2026, 6, 24)
    )
    second_id = await _add_period(
        session_factory, org_id, datetime.date(2026, 6, 25), datetime.date(2026, 7, 24)
    )

    with structlog.testing.capture_logs() as logs:
        async with session_factory() as db:
            await billing_service.close_period(
                db, org_id, datetime.date(2026, 7, 24), today=_TODAY
            )

    clamped = [e for e in logs if e.get("event") == "billing.close.clamped"]
    assert len(clamped) == 1
    assert clamped[0]["requested_close_date"] == "2026-07-24"
    assert clamped[0]["clamped_to"] == "2026-05-24"
    # Counterfactual: the rows the REQUESTED window would have swallowed, not
    # the (always empty) set the clamped window actually covers.
    assert clamped[0]["absorbed_period_ids"] == [clamped_id, second_id]
    revived = [e for e in logs if e.get("event") == "billing.close.revived"]
    assert len(revived) == 1
    assert revived[0]["revived_period_id"] == clamped_id
    assert revived[0]["revived_previous_end"] == "2026-06-24"


# ── TBD-241 code review: the folded findings ──────────────────────────────
#
# Spec §9. Everything below was added after two independent reviewers returned
# READY AFTER FIXES; each test names the finding it fences.


@pytest.mark.asyncio
async def test_close_takes_a_row_lock_on_the_open_period(session_factory, monkeypatch):
    """F1 — the concurrency hole `uq_billing_period_org_start` does NOT close.

    The unique constraint only collides writers that compute the SAME
    `new_start`, and the two production callers routinely compute different
    ones: the scheduler passes `boundary - 1`, a UI close passes yesterday. With
    org cycle_day 1, a single open row `[2026-06-01, NULL)` and a clock of
    2026-07-28, the scheduler resolves `new_start = 2026-07-01` while a
    concurrent admin click resolves `new_start = 2026-07-28`. Both commit,
    neither raises, D4 never runs and nothing is logged; the roster is left with
    `[06-01, 07-27]` overlapping `[07-01, NULL)` plus `[07-28, NULL)`.

    The fixtures cannot exercise real concurrency (`StaticPool`, one in-memory
    SQLite connection) and SQLite drops `FOR UPDATE` from the emitted SQL, so
    this pins the DECISION: a `FOR UPDATE` select against `billing_periods` is
    issued before anything is decided.
    """
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 6, 1)
    )
    seen = _capture_locking_selects(monkeypatch)

    async with session_factory() as db:
        await billing_service.close_period(db, org_id, today=_TODAY)

    assert seen, "close_period issued no FOR UPDATE select at all"
    assert any("billing_periods" in stmt for stmt in seen), (
        "the lock must be taken on the open BILLING PERIOD row: that is the "
        f"row every closer contends for. Locked instead: {seen}"
    )


@pytest.mark.asyncio
async def test_a_racer_that_closed_the_row_before_the_lock_is_not_closed_twice(
    session_factory, monkeypatch
):
    """F1, the behavioural half — the locked row comes back already closed.

    Once the lock serialises closers, the loser wakes up holding a row a racer
    has already closed. It must reuse D4 step 4's ruling (return the row at
    `end_date + 1 day`, asserted open) rather than close a second time, which is
    what produces the overlap plus the second open row.

    `get_current_period` filters `end_date IS NULL`, so it can never hand back a
    closed row on its own; the racer's window is between that call and the lock.
    Patching it is the only way to stand in that window under `StaticPool`.
    """
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 6, 25)
    )
    async with session_factory() as db:
        closed_id = await db.scalar(
            select(BillingPeriod.id).where(BillingPeriod.org_id == org_id)
        )
        await db.execute(
            update(BillingPeriod)
            .where(BillingPeriod.id == closed_id)
            .values(end_date=datetime.date(2026, 7, 27))
        )
        await db.commit()
    racer_open_id = await _add_period(
        session_factory, org_id, datetime.date(2026, 7, 28), None
    )

    async def _stale_current(db, _org_id):
        return (
            await db.execute(
                select(BillingPeriod).where(BillingPeriod.id == closed_id)
            )
        ).scalar_one()

    monkeypatch.setattr(billing_service, "get_current_period", _stale_current)
    steps = _count_close_steps(monkeypatch)

    async with session_factory() as db:
        result = await billing_service.close_period(db, org_id, today=_TODAY)

    assert steps["n"] == 0, "the racer's close must not be applied a second time"
    assert result.id == racer_open_id
    assert result.end_date is None
    assert [(p.start_date, p.end_date) for p in await _periods(session_factory, org_id)] == [
        (datetime.date(2026, 6, 25), datetime.date(2026, 7, 27)),
        (datetime.date(2026, 7, 28), None),
    ], "no overlap, and still exactly one open row"


@pytest.mark.asyncio
async def test_d5_anchors_budgets_to_the_clamped_date_not_the_requested_one(
    session_factory,
):
    """F4 — D5's headline rule, under a clamp.

    D5's own text says `new_end` is `resolved`, never `requested`. No test
    distinguished the two: tests 10, 12 and 13 all have `s0 > requested` so no
    clamp fires, and the router's clamped tests carry no budget on the CLOSING
    period. An implementation that passed `requested` would leave the whole
    suite green while writing a `Budget.period_end` that overshoots the actual
    close by two months.
    """
    org_id = 1
    closing_start = datetime.date(2026, 4, 25)
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=closing_start
    )
    stub_id = await _add_period(
        session_factory, org_id, datetime.date(2026, 5, 25), datetime.date(2026, 6, 24)
    )
    category_id = await _add_category(session_factory, org_id)
    budget_id = await _add_budget(session_factory, org_id, category_id, closing_start)

    async with session_factory() as db:
        result = await billing_service.close_period(
            db, org_id, datetime.date(2026, 7, 24), today=_TODAY
        )

    assert result.id == stub_id
    periods = await _periods(session_factory, org_id)
    assert periods[0].end_date == datetime.date(2026, 5, 24), "clamped"

    budget = await _budget(session_factory, budget_id)
    assert budget.period_end == datetime.date(2026, 5, 24), (
        "Budget.period_end must follow the CLAMPED close date"
    )
    assert budget.period_end != datetime.date(2026, 7, 24), (
        "the requested date overshoots the close by two months"
    )


@pytest.mark.asyncio
async def test_recovery_raises_loudly_when_the_closing_row_vanished(
    session_factory, monkeypatch
):
    """F7 — D4 step 3's `RuntimeError`, which shipped with zero coverage.

    `org_data_service.py:144` issues `delete(BillingPeriod).where(org_id == ...)`,
    so a wipe landing mid-close is reachable rather than theoretical. The code
    this replaced tolerated the `None` silently and fell through; D4 makes it
    loud, and the router audits it as a failure and re-raises.
    """
    org_id = 1
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 6, 25)
    )
    await _add_period(
        session_factory, org_id, datetime.date(2026, 7, 28), datetime.date(2026, 8, 27)
    )

    _patch_existence_check_miss(monkeypatch)

    real_rollback = AsyncSession.rollback
    injected = {"fired": False}

    async def patched_rollback(self):
        await real_rollback(self)
        if not injected["fired"]:
            injected["fired"] = True
            # The org-data wipe, landing between D4's rollback and its by-id
            # re-fetch.
            await self.execute(
                delete(BillingPeriod).where(BillingPeriod.org_id == org_id)
            )
            await self.commit()

    monkeypatch.setattr(AsyncSession, "rollback", patched_rollback)

    with pytest.raises(RuntimeError, match="vanished after IntegrityError"):
        async with session_factory() as db:
            await billing_service.close_period(db, org_id, today=_TODAY)

    assert injected["fired"] is True, "the wipe was never injected"


@pytest.mark.asyncio
async def test_recovery_raises_loudly_when_the_racers_open_row_is_missing(
    session_factory, monkeypatch
):
    """F7 — D4 step 4's `RuntimeError`, also shipped with zero coverage.

    The branch rests on an invariant: no writer may set `end_date` on a row
    without leaving a row at `end_date + 1 day`. When that is violated, returning
    the row anyway would make the route reply `{"end_date": None}` for a period
    that is closed. Here the injected racer closes our row but leaves the row at
    `resolved + 1` CLOSED, which is exactly the violation.
    """
    org_id = 1
    resolved = datetime.date(2026, 7, 27)
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=datetime.date(2026, 6, 25)
    )
    async with session_factory() as db:
        current_id = await db.scalar(
            select(BillingPeriod.id).where(BillingPeriod.org_id == org_id)
        )
    await _add_period(
        session_factory, org_id, datetime.date(2026, 7, 28), datetime.date(2026, 8, 27)
    )

    _patch_existence_check_miss(monkeypatch)

    real_rollback = AsyncSession.rollback
    injected = {"fired": False}

    async def patched_rollback(self):
        await real_rollback(self)
        if not injected["fired"]:
            injected["fired"] = True
            # Half a racer: our row is closed, but nothing is left open at
            # `end_date + 1 day`.
            await self.execute(
                update(BillingPeriod)
                .where(BillingPeriod.id == current_id)
                .values(end_date=resolved)
            )
            await self.commit()

    monkeypatch.setattr(AsyncSession, "rollback", patched_rollback)

    with pytest.raises(RuntimeError, match="missing or not open"):
        async with session_factory() as db:
            await billing_service.close_period(db, org_id, today=_TODAY)

    assert injected["fired"] is True, "the racer was never injected"
