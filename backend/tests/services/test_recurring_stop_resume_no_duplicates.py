"""Stop then Resume must not back-fill the paused gap (TBD-300).

## The defect this fences

``stop_recurring`` sets ``is_active = False`` and **freezes** ``next_due_date``
-- it does not advance it. ``generate_due_transactions`` filters on
``is_active == True``, so while a template is stopped its frontier does not
move: it falls one day further behind for every day paused.

``handleResume`` (``frontend/app/recurring/page.tsx``) then sends
``{is_active: true}`` and nothing else -- no date, and no UI affordance to
supply one. On the next scheduler tick the catch-up loop materialises **every
occurrence between the freeze point and today**, each one a money row, each one
written SETTLED when ``auto_settle`` is on, each one applying to the account
balance. Two clicks, silent.

## ⚠ The fixture MUST have a real time gap

The frontier only drifts *while time passes*. A fixture that generates, stops
and resumes at a single fixed ``today`` never opens a gap at all: generation
always advances ``next_due_date`` past everything it creates, so the frontier
ends up AHEAD of ``p_start`` and there is nothing to re-anchor.

An earlier version of this file did exactly that, and **all five fences were
green against every wrong implementation, including unmodified `main`.** Hence
the two clocks below. If you touch this fixture, re-run the injection gate.

## The rule (architect ruling, TBD-300)

Reactivating a template advances its frontier along its own ``advance_date``
grid to the first occurrence ``>= p_start`` (current billing cycle start).
Resume does **not** back-fill the paused gap.

``p_start`` rather than ``today`` on purpose: a template resumed mid-cycle must
still produce the current cycle's occurrence. Advancing to ``today`` silently
skips a charge the user is genuinely due.

⚠ Along the grid, never ``next_due_date = today``. The latter re-anchors the
series -- a rent template paused on the 1st and resumed on the 17th would bill
on the 17th forever, which is worse because it is invisible.

## One fixture cannot carry all of this

``CYCLE_DAY = 1`` with ``FIRST_DUE`` on the 5th is the *headline* fixture, and
it is degenerate in three separate ways that hide real mutants. Each of the
later tests therefore builds its own:

* ``current_cycle_window(1, today)[0]`` equals ``today.replace(day=1)``, so the
  whole ``p_start`` derivation is invisible -- hence ``billing_cycle_day = 15``
  in ``test_reanchor_target_is_the_orgs_cycle_start_not_the_first_of_the_month``.
* the walk never lands ON ``p_start``, only past it, so ``<`` vs ``<=`` is
  invisible -- hence the day-1 series in
  ``test_frontier_landing_exactly_on_the_cycle_start_is_not_advanced_past_it``.
* ``advance_date``'s month-end clamping is a no-op on the 5th, so iterated vs
  closed-form is invisible -- hence the Jan-31 series in
  ``test_resume_lands_on_generations_own_path_dependent_grid``.

That last test is a REPLACEMENT, not an addition (TBD-300 review, N4). It
previously asserted ``next_due_date.day == FIRST_DUE.day`` on the headline
fixture, which ``test_resume_still_produces_the_current_cycle_occurrence``
strictly implies by asserting the exact date -- every mutant it killed, that
one killed too. It was re-pointed rather than deleted because the property its
docstring CLAIMED (the series keeps its alignment) turned out to be both false
as stated and worth fencing in its corrected form: the walk reproduces
generation's own path-dependent grid. On the month-end fixture that is a
property nothing else in this file can see.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest_asyncio
import structlog.testing
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionType
from app.schemas.recurring import RecurringUpdate
from app.services import recurring_service
from app.services.recurring_service import Frequency


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# Fixed literals, not clock offsets: the quantity under test is a COUNT of
# monthly occurrences between two dates, which swings with day-of-month if the
# anchor moves (TBD-278 / reference_wall_clock_date_bomb_tests).
#
# T0 -> generate + stop.  T1 -> resume + generate.  The five-month gap between
# them is the whole point; see the module docstring.
T0 = datetime.date(2026, 1, 20)
T1 = datetime.date(2026, 6, 20)
CYCLE_DAY = 1
P_START_AT_T1 = datetime.date(2026, 6, 1)
FIRST_DUE = datetime.date(2026, 1, 5)
AMOUNT = Decimal("100.00")

# A day-of-month that makes the walk land EXACTLY on `p_start` (N2), and a
# cycle day that is not the 1st, so `p_start` is not `today.replace(day=1)`
# by coincidence (N3). Both are separate fixtures on purpose: CYCLE_DAY = 1
# plus FIRST_DUE on the 5th makes several distinct derivations agree.
DAY_ONE_DUE = datetime.date(2026, 1, 1)
CYCLE_DAY_MID = 15
P_START_AT_T1_MID = datetime.date(2026, 6, 15)
# Month-end anchor: the only shape where `advance_date`'s clamping is visible.
EOM_DUE = datetime.date(2026, 1, 31)


async def _seed(
    db: AsyncSession,
    *,
    cycle_day: int = CYCLE_DAY,
    first_due: datetime.date = FIRST_DUE,
    frequency: Frequency = Frequency.MONTHLY,
) -> dict:
    """One org + account + category + one active template.

    Every call makes its OWN org, so two seeds in one test are two independent
    universes and `_dates` (org-scoped) never mixes them.
    """
    org = Organization(name="T", billing_cycle_day=cycle_day)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("0.00"), opening_balance=Decimal("0.00"), currency="EUR",
    )
    db.add(acct)
    await db.flush()
    cat = Category(org_id=org.id, name="Rent", slug="rent", type=CategoryType.EXPENSE)
    db.add(cat)
    await db.flush()
    tpl = RecurringTransaction(
        org_id=org.id, account_id=acct.id, category_id=cat.id,
        description="Rent", amount=AMOUNT, type=TransactionType.EXPENSE,
        frequency=frequency, next_due_date=first_due,
        auto_settle=True, is_active=True,
    )
    db.add(tpl)
    await db.commit()
    return {"org_id": org.id, "account_id": acct.id, "template_id": tpl.id}


async def _dates(db: AsyncSession, org_id: int) -> list[datetime.date]:
    res = await db.execute(
        select(Transaction).where(Transaction.org_id == org_id).order_by(Transaction.date)
    )
    return [t.date for t in res.scalars().all()]


async def _template(db: AsyncSession, template_id: int) -> RecurringTransaction:
    return await db.scalar(
        select(RecurringTransaction).where(RecurringTransaction.id == template_id)
    )


async def _resume(db: AsyncSession, seed: dict, *, at: datetime.date) -> None:
    """Exactly what the Resume button does.

    ``handleResume`` sends ``{is_active: true}`` and nothing else. Sending only
    that field is the point: a fix that depended on the client supplying a date
    would not run here.
    """
    await recurring_service.update_recurring(
        db, seed["org_id"], seed["template_id"],
        RecurringUpdate(is_active=True), today=at,
    )


async def _paused_across_five_months(db: AsyncSession) -> dict:
    """Generate one occurrence at T0, stop, then resume at T1 five months later."""
    seed = await _seed(db)
    await recurring_service.generate_due_transactions(db, seed["org_id"], today=T0)
    await recurring_service.stop_recurring(db, seed["org_id"], seed["template_id"])
    await _resume(db, seed, at=T1)
    return seed


async def test_resume_does_not_backfill_the_paused_gap(db_session):
    """FENCE — the headline defect.

    Wrong implementation killed: leaving ``next_due_date`` frozen across the
    ``is_active`` False->True transition (i.e. `main`). Generation then walks
    the whole paused gap and materialises Feb, Mar, Apr and May as well.
    """
    seed = await _paused_across_five_months(db_session)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=T1)

    dates = await _dates(db_session, seed["org_id"])
    assert dates == [FIRST_DUE, datetime.date(2026, 6, 5)], (
        f"expected only the pre-pause occurrence and the current cycle's, got {dates}"
    )


async def test_resume_does_not_apply_the_paused_gap_to_the_balance(db_session):
    """FENCE — the money consequence, asserted independently.

    This and the row-count fence can fail separately: a fix that suppressed the
    rows but still moved the balance, or one that left the gap rows PENDING (no
    balance effect), would pass exactly one of them.

    Wrong implementation killed: same as above. Every back-filled occurrence is
    written SETTLED (auto_settle + past date), and settled rows apply.
    """
    seed = await _paused_across_five_months(db_session)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=T1)

    acct = await db_session.scalar(
        select(Account).where(Account.id == seed["account_id"])
    )
    await db_session.refresh(acct)
    assert acct.balance == Decimal("-200.00"), (
        f"balance {acct.balance}: expected one pre-pause charge plus the current "
        f"cycle's, not the whole paused gap"
    )


async def test_resume_lands_on_generations_own_path_dependent_grid(db_session):
    """FENCE — the walk must land where GENERATION's walk would have landed.

    Re-pointed (TBD-300 review, N4). This test used to assert
    ``next_due_date.day == FIRST_DUE.day`` on the five-month fixture, which
    ``test_resume_still_produces_the_current_cycle_occurrence`` strictly
    implies, and it justified itself as fencing "the series' alignment" — the
    one property that fixture cannot express, because ``FIRST_DUE`` is the 5th
    and ``advance_date``'s clamping is a no-op there. See the module docstring.

    The real property, and the one thing here nothing else kills: the walk is
    ITERATED, over ``advance_date``, so it reproduces generation's own
    PATH-DEPENDENT grid. A month-end series drifts off its anchor and never
    returns — Jan 31 -> Feb 28 -> Mar 28 -> ... -> Jun 28, not Jun 30 — and
    ``generate_due_transactions`` drifts identically because it calls the same
    function. The re-anchored frontier must therefore be a date generation
    itself visits.

    Wrong implementation killed: any closed-form jump onto the current cycle
    (``next_due + relativedelta(months=n)``, or projecting the day-of-month onto
    ``p_start``'s month). Both give 2026-06-30 here. Every other fence in this
    file stays GREEN under it, because their series is anchored on the 5th.

    No generation runs before the pause on purpose: one tick would advance the
    frontier off day 31 to Feb 28 and destroy the property under test.
    """
    paused = await _seed(db_session, first_due=EOM_DUE)
    await recurring_service.stop_recurring(
        db_session, paused["org_id"], paused["template_id"]
    )
    await _resume(db_session, paused, at=T1)
    frontier = (await _template(db_session, paused["template_id"])).next_due_date

    # The control is the SAME template in its own org, never paused. Its rows
    # are the grid generate_due_transactions actually walks — not a re-derivation
    # of it here, which would only restate advance_date to itself.
    control = await _seed(db_session, first_due=EOM_DUE)
    await recurring_service.generate_due_transactions(
        db_session, control["org_id"], today=T1
    )
    grid = await _dates(db_session, control["org_id"])

    assert frontier == datetime.date(2026, 6, 28), (
        f"frontier {frontier}: walking Jan 31 monthly to the first occurrence "
        f">= {P_START_AT_T1} lands on 2026-06-28; 2026-06-30 means the walk was "
        f"replaced by closed-form arithmetic"
    )
    assert frontier in grid, (
        f"frontier {frontier} is not a date generation produces ({grid}); the "
        f"re-anchor must land ON generation's grid, not beside it"
    )


async def test_resume_still_produces_the_current_cycle_occurrence(db_session):
    """FENCE — the fix must not overshoot.

    Wrong implementation killed: advancing to the first occurrence ``>= today``
    instead of ``>= p_start``. On this fixture that lands the frontier on
    2026-07-05, past the current cycle end, so the 2026-06-05 charge the user is
    genuinely due never appears. Both fences above stay green under it.
    """
    seed = await _paused_across_five_months(db_session)

    tpl = await _template(db_session, seed["template_id"])
    assert tpl.next_due_date == datetime.date(2026, 6, 5), (
        f"frontier {tpl.next_due_date} is not the current cycle's occurrence"
    )

    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=T1)
    assert datetime.date(2026, 6, 5) in await _dates(db_session, seed["org_id"]), (
        "the current cycle's charge was skipped"
    )


async def test_pause_and_resume_inside_one_cycle_leaves_the_frontier_alone(db_session):
    """GUARD — the common case must not regress.

    Pausing and resuming a template the same week is ordinary use. Its frontier
    is already ``>= p_start``, so the fix must leave it exactly where it is
    rather than advancing it a cycle and skipping a charge.

    Wrong implementation killed: advancing unconditionally, i.e. dropping the
    already-current early-out.
    """
    seed = await _seed(db_session)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=T1)
    frontier_before = (await _template(db_session, seed["template_id"])).next_due_date
    assert frontier_before >= P_START_AT_T1, "fixture precondition"

    await recurring_service.stop_recurring(db_session, seed["org_id"], seed["template_id"])
    await _resume(db_session, seed, at=T1)

    tpl = await _template(db_session, seed["template_id"])
    assert tpl.next_due_date == frontier_before


async def test_reanchor_is_gated_on_the_transition_not_on_the_field(db_session):
    """FENCE — ``and not was_active`` in the gate (N1).

    The gate reads ``body.is_active is True and not was_active``. The second
    conjunct was asserted by a comment ("a no-op update that re-sends
    ``is_active: true`` on an already-active template must not move the
    frontier") and by nothing else: every other test here reaches the gate
    through ``stop_recurring``, so ``was_active`` is already False and deleting
    the conjunct changes none of their outcomes.

    Wrong implementation killed: dropping ``and not was_active``, i.e. gating on
    ``body.is_active is True`` alone.

    This is not hypothetical traffic. ``PATCH`` bodies are partial, and any
    client that round-trips the template it just read back re-sends
    ``is_active: true`` on an active template. Under the mutant that silently
    fast-forwards the frontier — skipping the occurrences between it and the
    cycle start, which is a MISSING charge, the mirror image of the defect this
    ticket fixes.

    The template here is active and has never generated, so its frontier sits
    on ``FIRST_DUE``, five months behind ``p_start`` — the mutant has somewhere
    to move it to.
    """
    seed = await _seed(db_session)
    tpl = await _template(db_session, seed["template_id"])
    assert tpl.is_active is True, "fixture precondition: already active"
    assert tpl.next_due_date == FIRST_DUE and FIRST_DUE < P_START_AT_T1, (
        "fixture precondition: frontier is behind p_start, so a mutant that "
        "re-anchors unconditionally has room to move it"
    )

    # Byte-for-byte the Resume request — sent at a template that was never
    # stopped.
    await _resume(db_session, seed, at=T1)

    tpl = await _template(db_session, seed["template_id"])
    assert tpl.next_due_date == FIRST_DUE, (
        f"frontier moved to {tpl.next_due_date} on a no-op is_active:true; only "
        f"a False->True TRANSITION may re-anchor"
    )


async def test_frontier_landing_exactly_on_the_cycle_start_is_not_advanced_past_it(db_session):
    """FENCE — the ``<`` in ``while r.next_due_date < p_start`` (N2).

    Wrong implementation killed: ``<=``. The boundary was pinned from one side
    only (a frontier strictly after ``p_start``, by
    ``test_pause_and_resume_inside_one_cycle_leaves_the_frontier_alone``); the
    landing-exactly-on case was unfenced.

    The consequence is severe and needs no unusual config — MONTHLY on the 1st
    with the DEFAULT ``billing_cycle_day = 1``. Correct: the walk stops on
    2026-06-01 and June's rent is created. Under ``<=``: one more step to
    2026-07-01, past the period end, and **June's rent is silently never
    created**. Both the row-count and the frontier are asserted, because the
    off-by-one is only visible as a missing row a cycle later.
    """
    seed = await _seed(db_session, first_due=DAY_ONE_DUE)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=T0)
    await recurring_service.stop_recurring(db_session, seed["org_id"], seed["template_id"])
    await _resume(db_session, seed, at=T1)

    tpl = await _template(db_session, seed["template_id"])
    assert tpl.next_due_date == P_START_AT_T1, (
        f"frontier {tpl.next_due_date}: a walk that reaches p_start "
        f"({P_START_AT_T1}) exactly must STOP there, not take one more step"
    )

    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=T1)
    assert await _dates(db_session, seed["org_id"]) == [DAY_ONE_DUE, P_START_AT_T1], (
        "the current cycle's charge, due on the cycle start itself, was skipped"
    )


async def test_reanchor_target_is_the_orgs_cycle_start_not_the_first_of_the_month(db_session):
    """FENCE — the ``p_start`` derivation itself (N3).

    Every other test in this file runs at ``CYCLE_DAY = 1``, where
    ``current_cycle_window(1, today)[0]`` and ``today.replace(day=1)`` agree.
    So the org lookup and the ``current_cycle_window`` call were untested:
    replacing both with ``p_start = today.replace(day=1)`` left all five green.

    Wrong implementation killed: exactly that substitution.

    With ``billing_cycle_day = 15`` the current cycle at T1 is
    [2026-06-15, 2026-07-14], so the series' 2026-06-05 occurrence belongs to
    the PREVIOUS cycle — it is part of the paused gap and must not be
    back-filled. The first occurrence in the current cycle is 2026-07-05.
    Under the mutant ``p_start`` is 2026-06-01, the walk stops on 2026-06-05,
    and the next tick writes that gap charge SETTLED against the balance.
    """
    seed = await _seed(db_session, cycle_day=CYCLE_DAY_MID)
    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=T0)
    await recurring_service.stop_recurring(db_session, seed["org_id"], seed["template_id"])
    await _resume(db_session, seed, at=T1)

    tpl = await _template(db_session, seed["template_id"])
    assert tpl.next_due_date == datetime.date(2026, 7, 5), (
        f"frontier {tpl.next_due_date}: with billing_cycle_day="
        f"{CYCLE_DAY_MID} the cycle starts {P_START_AT_T1_MID} and the first "
        f"occurrence at or after it is 2026-07-05; 2026-06-05 means p_start was "
        f"derived as the 1st of the month instead of from the org"
    )

    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=T1)
    assert datetime.date(2026, 6, 5) not in await _dates(db_session, seed["org_id"]), (
        "a previous-cycle gap occurrence was back-filled"
    )


async def test_reanchor_cap_exhaustion_is_not_silent(db_session):
    """FENCE — the ``_MAX_FRONTIER_ADVANCE_STEPS`` arm logs (N6).

    On exhaustion the loop falls through, the caller commits, and the template
    is left ACTIVE with a frontier still behind ``p_start`` — whereupon the next
    tick back-fills up to ``MAX_CATCHUP_ITERATIONS`` rows, each SETTLED, against
    the balance. That is precisely the defect this ticket removes, re-entered.
    Its sibling in ``generate_due_transactions`` warns
    (``recurring.generate.catchup_cap``); this arm was silent.

    A weekly template anchored in 2000 is ~1378 steps behind the 2026 cycle
    start, so the 1200-step cap fires and the frontier is left short.
    """
    seed = await _seed(
        db_session, first_due=datetime.date(2000, 1, 5), frequency=Frequency.WEEKLY
    )
    await recurring_service.stop_recurring(db_session, seed["org_id"], seed["template_id"])

    with structlog.testing.capture_logs() as logs:
        await _resume(db_session, seed, at=T1)

    tpl = await _template(db_session, seed["template_id"])
    assert tpl.next_due_date < P_START_AT_T1, (
        "fixture precondition: the cap must actually be reached, leaving the "
        "frontier short of the cycle start"
    )
    assert tpl.is_active is True, (
        "the template is left ACTIVE and behind — which is why silence here is "
        "not acceptable"
    )

    capped = [e for e in logs if e.get("event") == "recurring.resume.reanchor_cap"]
    assert len(capped) == 1, f"expected one cap warning, got {logs}"
    assert capped[0]["log_level"] == "warning"
    assert capped[0]["org_id"] == seed["org_id"]
    assert capped[0]["recurring_id"] == seed["template_id"]
    assert capped[0]["next_due_date"] == str(tpl.next_due_date)
