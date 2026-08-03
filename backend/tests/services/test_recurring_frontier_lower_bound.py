"""TBD-283 — ONE lower bound on ``next_due_date``, three write paths.

The rule: a recurring template's ``next_due_date`` may never be written
earlier than ``current_cycle_window(org.billing_cycle_day, today)[0]`` —
``p_start``, the start of the org's CURRENT billing cycle. No upper bound.
``create_recurring``, ``update_recurring`` and ``promote_to_recurring`` enforce
it identically, in the service layer.

Why the service layer and not pydantic: the bound reads
``org.billing_cycle_day``, which no schema validator can see. A validator could
only encode a DIFFERENT rule and would then pre-empt the real one on every
request — which is precisely the three-way disagreement this ticket removes.
``PromoteToRecurringRequest._next_due_date_not_past`` (``>= today``, 422) was
deleted for that reason, so promote is now RELAXED and its rejection moved from
422 to 400.

Why ``>= p_start`` and not the obvious ``>= today``: ``>= today`` breaks
``test_forecast_overdue_recurring`` F16, which rewinds three frontiers through
the real ``update_recurring`` onto ``p_start`` — five days behind ``today``.
F16 is one of PR 599's anti-double-count fences. ``>= p_start`` sits exactly ON
its rewind target.

⚠ Every clock here is INJECTED (``TODAY``), so the arithmetic is fixed
literals, not ``date.today() ± n``. ``test_fixture_geometry`` re-derives every
constant from ``current_cycle_window`` so the anchors cannot rot silently.

⚠ ONE exception, and it is the point of F7: that same habit is what let the
``today=None`` path — the ONLY path a route takes — go unfenced. F7 passes no
clock and instead monkeypatches ``date.today`` to TICK, which is the only way
to observe how many times a request resolves it.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.recurring import RecurringCreate, RecurringUpdate
from app.schemas.transaction import PromoteToRecurringRequest
from app.services import recurring_service, transaction_service
from app.services.billing_service import current_cycle_window
from app.services.exceptions import ValidationError

DAY = datetime.timedelta(days=1)

# The injected clock. Chosen so the two cycle days below straddle it in
# OPPOSITE months — see `test_fixture_geometry`.
TODAY = datetime.date(2026, 8, 3)

# billing_cycle_day = 1  -> the current cycle opened on the 1st of THIS month.
CYCLE_A = 1
P_START_A = datetime.date(2026, 8, 1)

# billing_cycle_day = 15 -> TODAY is before the 15th, so the current cycle
# opened on the 15th of LAST month.
CYCLE_B = 15
P_START_B = datetime.date(2026, 7, 15)

# Inside org B's open cycle, BEFORE org A's. The discriminator (F6).
STRADDLE = datetime.date(2026, 7, 31)

# ── F7's midnight pair ──────────────────────────────────────────────────────
# Org B's cycle rolls between these two adjacent days: CLOCK_EVE still sees the
# July cycle, CLOCK_TICK sees the August one. Deliberately NOT the same clock as
# TODAY -- F7 needs a day on which one more tick MOVES p_start.
CLOCK_EVE = datetime.date(2026, 8, 14)
CLOCK_TICK = datetime.date(2026, 8, 15)

# Compliant for org B on CLOCK_EVE, illegal on CLOCK_TICK. The single date that
# makes a second clock resolution observable at all.
EVE_ONLY = datetime.date(2026, 7, 20)


# ── harness ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db: AsyncSession, *, cycle_day: int = CYCLE_A) -> dict:
    org = Organization(name=f"Org-{cycle_day}", billing_cycle_day=cycle_day)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Acct", account_type_id=at.id,
        balance=Decimal("1000"), currency="EUR",
    )
    cat = Category(
        org_id=org.id, name="Rent", slug=f"rent-{cycle_day}",
        type=CategoryType.EXPENSE, is_system=False,
    )
    db.add_all([acct, cat])
    await db.commit()
    return {"org_id": org.id, "account_id": acct.id, "category_id": cat.id}


def _create_body(seed: dict, due: datetime.date, **over) -> RecurringCreate:
    kwargs = dict(
        account_id=seed["account_id"],
        category_id=seed["category_id"],
        description="Rent",
        amount=Decimal("500"),
        type="expense",
        frequency="monthly",
        next_due_date=due,
    )
    kwargs.update(over)
    return RecurringCreate(**kwargs)


async def _template(
    db: AsyncSession, seed: dict, *, due: datetime.date,
    is_active: bool = True, frequency: Frequency = Frequency.MONTHLY,
) -> RecurringTransaction:
    """Insert a template DIRECTLY, bypassing the service guard.

    Deliberate: the update/resume fences need a starting frontier that
    ``create_recurring`` would (correctly) refuse to write.
    """
    t = RecurringTransaction(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["category_id"], description="Rent",
        amount=Decimal("500"), type="expense", frequency=frequency,
        next_due_date=due, auto_settle=False, is_active=is_active,
    )
    db.add(t)
    await db.commit()
    return t


async def _reread(db: AsyncSession, template_id: int) -> RecurringTransaction:
    """Roll back the failed unit of work, then read the PERSISTED row.

    A rejected update leaves the in-session template dirty (fields are applied
    before the guard runs, exactly as ``validate_category_for_type`` already
    does). Asserting on that object would assert on uncommitted garbage; the
    claim under test is that nothing REACHED the database.
    """
    await db.rollback()
    return await db.scalar(
        select(RecurringTransaction).where(RecurringTransaction.id == template_id)
    )


async def _add_tx(db: AsyncSession, seed: dict) -> Transaction:
    tx = Transaction(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["category_id"], description="Coffee",
        amount=Decimal("12.50"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=datetime.date(2026, 7, 20), settled_date=datetime.date(2026, 7, 20),
    )
    db.add(tx)
    await db.commit()
    return tx


# ─────────────────────────────────────────────────────────────────────────────
# Fixture geometry — self-checking anchors.
# ─────────────────────────────────────────────────────────────────────────────

def test_fixture_geometry():
    """Every literal above, re-derived. Nothing here is assumed.

    ⚠ Without this the whole module can rot into agreement with a wrong
    implementation: if ``P_START_B`` silently stopped being July's, ``STRADDLE``
    would stop straddling and F6 — the only test that can tell this rule apart
    from a hardcoded ``>= today`` — would pass vacuously.
    """
    assert current_cycle_window(CYCLE_A, TODAY)[0] == P_START_A
    assert current_cycle_window(CYCLE_B, TODAY)[0] == P_START_B

    # The two orgs' cycles genuinely differ, and in opposite months.
    assert P_START_B < P_START_A
    # STRADDLE is legal for B, illegal for A — the discriminating gap.
    assert P_START_B <= STRADDLE < P_START_A
    # ...and it is in the PAST, so an org-blind `>= today` rule cannot accept
    # it either. That is what makes F6's ACCEPT arm load-bearing.
    assert STRADDLE < TODAY
    # p_start is strictly behind today for both orgs, so `>= p_start` and
    # `>= today` are distinguishable at all on this clock.
    assert P_START_A < TODAY

    # F7's midnight pair: one day apart, and org B's p_start MOVES across it.
    assert CLOCK_TICK == CLOCK_EVE + DAY
    assert current_cycle_window(CYCLE_B, CLOCK_EVE)[0] == P_START_B
    assert current_cycle_window(CYCLE_B, CLOCK_TICK)[0] == datetime.date(2026, 8, 15)
    # EVE_ONLY sits on the legal side of that move and only that side. Without
    # this, F7 could pass with a date that is compliant on BOTH clocks, and a
    # second resolution would be invisible.
    assert current_cycle_window(CYCLE_B, CLOCK_EVE)[0] <= EVE_ONLY
    assert EVE_ONLY < current_cycle_window(CYCLE_B, CLOCK_TICK)[0]


# ─────────────────────────────────────────────────────────────────────────────
# F1 — create, BOTH sides of the boundary.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f1_create_accepts_exactly_p_start(db_session):
    """FENCE. A boundary pinned from one side is not pinned; this is the IN side.

    Wrong implementations killed:
      * ``>`` for ``>=`` — creating ON ``p_start`` raises. (Same mutant F16
        catches from the update path; two independent detectors on purpose.)
      * ``>= today`` — ``P_START_A`` is two days behind ``TODAY``, so it raises.
    """
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    r = await recurring_service.create_recurring(
        db_session, seed["org_id"], _create_body(seed, P_START_A), today=TODAY
    )
    assert r.next_due_date == P_START_A


async def test_f1_create_rejects_one_day_before_p_start(db_session):
    """FENCE. The OUT side, one day out — and the message names the boundary.

    Wrong implementations killed:
      * no bound at all on create (i.e. `main`);
      * ``>= today``: it also rejects, but ``P_START_A - 1`` is not the date it
        would report, and the message assertion below is what separates them.

    ⚠ On the cycle-start day this rule silently IS ``>= today``. A message
    saying only "too far in the past" would be unactionable — the user has no
    way to learn which date is acceptable. Interpolating ``p_start`` is
    condition 3 of the ruling and is asserted, not assumed.
    """
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    with pytest.raises(ValidationError) as exc:
        await recurring_service.create_recurring(
            db_session, seed["org_id"], _create_body(seed, P_START_A - DAY),
            today=TODAY,
        )
    assert P_START_A.isoformat() in exc.value.detail

    # Nothing was written.
    await db_session.rollback()
    rows = (await db_session.execute(select(RecurringTransaction))).scalars().all()
    assert list(rows) == []


# ─────────────────────────────────────────────────────────────────────────────
# F2 — update, BOTH sides of the boundary.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f2_update_rewind_to_p_start_is_allowed(db_session):
    """FENCE. F16's rewind target, in miniature.

    ``test_forecast_overdue_recurring`` F16 rewinds three frontiers onto
    ``p_start`` through the real ``update_recurring``; a ``>= today`` (or ``>``)
    bound raises inside that fixture and kills one of PR 599's core
    anti-double-count fences. This pins the same allowance directly, so a future
    tightening fails HERE with a legible message rather than deep inside a
    forecast conservation test.
    """
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    t = await _template(db_session, seed, due=datetime.date(2026, 9, 1))

    updated = await recurring_service.update_recurring(
        db_session, seed["org_id"], t.id,
        RecurringUpdate(next_due_date=P_START_A), today=TODAY,
    )
    assert updated.next_due_date == P_START_A


async def test_f2_update_rewind_one_day_earlier_is_rejected(db_session):
    """FENCE. The OUT side of the update boundary, and no partial write."""
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    t = await _template(db_session, seed, due=datetime.date(2026, 9, 1))

    with pytest.raises(ValidationError) as exc:
        await recurring_service.update_recurring(
            db_session, seed["org_id"], t.id,
            RecurringUpdate(next_due_date=P_START_A - DAY, description="edited"),
            today=TODAY,
        )
    assert P_START_A.isoformat() in exc.value.detail

    # ⚠ Fields are applied BEFORE the guard, so "rejected" has to mean
    # "nothing committed", not "nothing assigned". The sibling field is checked
    # too: a guard that raised after a partial commit would leave it changed.
    persisted = await _reread(db_session, t.id)
    assert persisted.next_due_date == datetime.date(2026, 9, 1)
    assert persisted.description == "Rent"


# ─────────────────────────────────────────────────────────────────────────────
# F3 — promote is RELAXED: a past date INSIDE the open cycle now succeeds.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f3_promote_accepts_past_date_inside_the_open_cycle(db_session):
    """FENCE. Goes RED if the old ``< today`` rule survives ANYWHERE.

    ``P_START_A`` is two days behind ``TODAY``. Before this ticket promote
    rejected it twice over — once in ``PromoteToRecurringRequest``'s field
    validator (422) and once in ``promote_to_recurring``'s own guard (400).
    Both had to go; either one surviving turns this red.

    ⚠ The schema half is caught at CONSTRUCTION time, before the service is
    even called: the deleted validator ran on ``date.today()``, which is later
    than ``P_START_A`` on any real clock this suite runs on. So this fence also
    covers the deletion, not just the service edit.
    """
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    tx = await _add_tx(db_session, seed)

    body = PromoteToRecurringRequest(frequency="monthly", next_due_date=P_START_A)
    result = await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], tx.id, body, today=TODAY
    )
    assert result.recurring_id is not None

    tmpl = await db_session.scalar(
        select(RecurringTransaction).where(RecurringTransaction.id == result.recurring_id)
    )
    assert tmpl.next_due_date == P_START_A


async def test_f3_promote_still_rejects_before_p_start(db_session):
    """FENCE. Relaxed is not removed — and it is a 400-shaped domain error now.

    Wrong implementation killed: deleting the schema validator and forgetting
    to add the service bound, which would leave promote with NO lower bound at
    all while create and update both have one.
    """
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    # Captured BEFORE the rollback below: reading `tx.id` off an expired
    # instance triggers a sync lazy-load under asyncio and blows up with
    # MissingGreenlet, which would mask the assertion this test exists for.
    tx_id = (await _add_tx(db_session, seed)).id

    body = PromoteToRecurringRequest(
        frequency="monthly", next_due_date=P_START_A - DAY
    )
    with pytest.raises(ValidationError) as exc:
        await transaction_service.promote_to_recurring(
            db_session, seed["org_id"], tx_id, body, today=TODAY
        )
    assert P_START_A.isoformat() in exc.value.detail

    await db_session.rollback()
    refreshed = await db_session.scalar(
        select(Transaction).where(Transaction.id == tx_id)
    )
    assert refreshed.recurring_id is None


# ─────────────────────────────────────────────────────────────────────────────
# F4 — condition 1: the validation is GATED, not unconditional.
# ─────────────────────────────────────────────────────────────────────────────

_STALE = TODAY - datetime.timedelta(days=300)


async def test_f4a_resume_alone_on_a_300_day_stale_frontier_succeeds(db_session):
    """FENCE — the product's ONLY PUT, on the templates users actually resume.

    ``handleResume`` sends ``{"is_active": true}`` and nothing else. The
    re-anchor walks the frontier FORWARD onto the current cycle, so the
    post-write state is compliant by construction.

    Wrong implementation killed: an UNGATED check placed before
    ``_reanchor_frontier_on_resume`` — it sees the raw 300-day-stale frontier
    and 400s Resume in a UI with no field to fix it with.

    ⚠ Honest scope. Once TBD-300's re-anchor landed, this test alone kills
    NEITHER of the two mutants condition 1 is about, taken separately:
      * "drop the gate" (check kept after the re-anchor) — the frontier is
        already compliant by then, so the ungated check passes. F4b and F4c
        kill it.
      * "check before the re-anchor" (gate kept) — this body supplies no
        ``next_due_date`` and no ``frequency``, so the gate is closed and the
        misplaced check never runs. F4d kills it.
    Only the two mutants COMBINED are visible here. Stated plainly because the
    ruling's wording for condition 1 predates the re-anchor and implies this
    test carries more weight than it does.
    """
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    t = await _template(db_session, seed, due=_STALE, is_active=False)
    assert _STALE < P_START_A

    updated = await recurring_service.update_recurring(
        db_session, seed["org_id"], t.id,
        RecurringUpdate(is_active=True), today=TODAY,
    )
    assert updated.is_active is True
    assert updated.next_due_date >= P_START_A


async def test_f4d_resume_with_an_explicit_stale_date_is_reanchored_not_rejected(
    db_session,
):
    """FENCE — the ORDERING. Kills "validate BEFORE the re-anchor".

    A resume that ALSO supplies a ``next_due_date`` earlier than ``p_start``.
    The check's POSITION is observable only when the gate is open AND the
    re-anchor is eligible (False->True); this body arranges both, so the two
    orderings disagree. It is the only such TEST in this module, not the only
    such BODY — ``{"is_active": true, "frequency": "weekly"}`` has exactly the
    same shape and would serve as well.

    ``update_recurring``'s docstring already commits to the outcome: on
    reactivation a supplied past date is SILENTLY OVERWRITTEN, because a
    client-supplied past date IS the back-fill TBD-300 removed. Validating
    first would turn that documented overwrite into a 400 and re-break Resume
    for any client that echoes the template's own stored date back.

    Wrong implementation killed: moving the ``validate_frontier`` call above
    the ``_reanchor_frontier_on_resume`` call, gate unchanged.
    """
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    t = await _template(db_session, seed, due=_STALE, is_active=False)

    updated = await recurring_service.update_recurring(
        db_session, seed["org_id"], t.id,
        RecurringUpdate(is_active=True, next_due_date=_STALE), today=TODAY,
    )
    assert updated.is_active is True
    # Overwritten by the re-anchor, not echoed back and not rejected.
    assert updated.next_due_date >= P_START_A
    assert updated.next_due_date != _STALE


async def test_f4b_no_op_resume_on_an_active_stale_template_succeeds(db_session):
    """FENCE — condition 1 proper. Kills "drop the gate condition".

    An ALREADY-ACTIVE stale template re-sent ``{"is_active": true}``: the
    re-anchor is gated on the False->True TRANSITION (TBD-300), so it does NOT
    run and cannot rescue the frontier. The request touches neither
    ``next_due_date`` nor ``frequency``, so the bound must not be consulted.

    Wrong implementation killed: validating the post-update state
    unconditionally. The frontier is 300 days stale and untouched by this
    request, so an ungated check raises.

    ⚠ Reachable: a retried/duplicated Resume, or a client whose cached list is
    behind the server. And the general shape — any field edit on a template
    whose frontier is already behind — is the same one.
    """
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    t = await _template(db_session, seed, due=_STALE, is_active=True)

    updated = await recurring_service.update_recurring(
        db_session, seed["org_id"], t.id,
        RecurringUpdate(is_active=True), today=TODAY,
    )
    assert updated.is_active is True
    # Untouched: no re-anchor (no transition) AND no rejection (no gate hit).
    assert updated.next_due_date == _STALE


async def test_f4c_unrelated_field_edit_on_a_stale_template_succeeds(db_session):
    """FENCE — condition 1, second detector, on a PAUSED template.

    Editing the amount of a long-paused template must not 400 on a frontier the
    request never mentions.

    Wrong implementation killed: the unconditional check, again — here with no
    ``is_active`` in the body at all, so no re-anchor is even eligible to run
    and the mutant cannot hide behind it.
    """
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    t = await _template(db_session, seed, due=_STALE, is_active=False)

    updated = await recurring_service.update_recurring(
        db_session, seed["org_id"], t.id,
        RecurringUpdate(amount=Decimal("999")), today=TODAY,
    )
    assert updated.amount == Decimal("999")
    assert updated.next_due_date == _STALE


# ─────────────────────────────────────────────────────────────────────────────
# F5 — condition 2: the ``frequency`` clause is not decoration.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f5_frequency_only_flip_on_a_stale_frontier_is_rejected(db_session):
    """FENCE. Kills the gate written as ``body.next_due_date is not None`` alone.

    A stale YEARLY template flipped to WEEKLY, with no ``next_due_date``
    anywhere in the request. Nothing about the stored frontier changed, but the
    number of occurrences it implies over the stale gap multiplied by ~52. The
    bound is a property of the post-write PAIR, not of the field the caller
    happened to send.

    ⚠ ``frequency`` is the only other field that can do this. ``amount`` and
    ``description`` cannot (F4c), which is why they stay outside the gate.

    ⚠ This is also the ONE rejection a user can hit over a field they never
    sent — the request carries no ``next_due_date`` at all. Naming the boundary
    date is condition 3 and is not sufficient on its own here; the message must
    also say what to do about it, so the remedy clause is asserted below.
    """
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    t = await _template(
        db_session, seed, due=_STALE, is_active=True, frequency=Frequency.YEARLY
    )

    with pytest.raises(ValidationError) as exc:
        await recurring_service.update_recurring(
            db_session, seed["org_id"], t.id,
            RecurringUpdate(frequency="weekly"), today=TODAY,
        )
    assert P_START_A.isoformat() in exc.value.detail
    # The remedy, named. Wrong implementation killed: a message that states the
    # boundary and stops, leaving a frequency-only request refused over a field
    # the user cannot see they were supposed to send.
    assert "next_due_date" in exc.value.detail

    persisted = await _reread(db_session, t.id)
    assert persisted.frequency == Frequency.YEARLY
    assert persisted.next_due_date == _STALE


async def test_f5_frequency_flip_on_a_compliant_frontier_is_allowed(db_session):
    """FENCE — the IN side of F5. Without it F5 is satisfied by "always reject
    a frequency-only update", which is not the rule."""
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    t = await _template(
        db_session, seed, due=P_START_A, is_active=True, frequency=Frequency.YEARLY
    )

    updated = await recurring_service.update_recurring(
        db_session, seed["org_id"], t.id,
        RecurringUpdate(frequency="weekly"), today=TODAY,
    )
    assert updated.frequency == Frequency.WEEKLY
    assert updated.next_due_date == P_START_A


# ─────────────────────────────────────────────────────────────────────────────
# F6 — THE DISCRIMINATOR. The bound is org-dependent, not a hardcoded clock.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f6_the_bound_follows_billing_cycle_day_not_today(db_session):
    """FENCE — the one that matters most.

    ONE clock (``TODAY``), ONE date (``STRADDLE``), TWO orgs. Org B
    (``billing_cycle_day=15``) is mid-cycle since 2026-07-15 and ACCEPTS it.
    Org A (``billing_cycle_day=1``) opened its cycle on 2026-08-01 and REJECTS
    it. Same input, opposite outcomes, decided purely by
    ``org.billing_cycle_day``.

    Wrong implementations killed:
      * ``>= today`` — ``STRADDLE`` is three days behind ``TODAY``, so the
        ACCEPT arm raises. Every other fence in this module is also satisfied
        by some org-blind rule; without this one the entire ruling is unfenced
        and indistinguishable from a hardcoded clock comparison.
      * any bound derived from a fixed ``billing_cycle_day`` (e.g. the ``or 1``
        fallback applied unconditionally) — the ACCEPT arm raises.
      * reading the cycle day from the wrong org (a missing ``org_id`` filter,
        or the caller's org where the template's should be) — the two orgs are
        seeded in one session precisely so a cross-org read is visible.

    ⚠ Both arms in ONE test on purpose. Split, the REJECT arm alone is passed
    by ``>= today`` and reads as coverage it is not.
    """
    org_a = await _seed(db_session, cycle_day=CYCLE_A)
    org_b = await _seed(db_session, cycle_day=CYCLE_B)

    # ACCEPT arm — org B. This is the discriminating half.
    r = await recurring_service.create_recurring(
        db_session, org_b["org_id"], _create_body(org_b, STRADDLE), today=TODAY
    )
    assert r.next_due_date == STRADDLE

    # REJECT arm — org A, same clock, same date.
    with pytest.raises(ValidationError) as exc:
        await recurring_service.create_recurring(
            db_session, org_a["org_id"], _create_body(org_a, STRADDLE), today=TODAY
        )
    # The message names org A's boundary, not org B's and not TODAY.
    assert P_START_A.isoformat() in exc.value.detail
    assert P_START_B.isoformat() not in exc.value.detail
    assert TODAY.isoformat() not in exc.value.detail


async def test_f6_update_and_promote_use_the_same_org_dependent_bound(db_session):
    """FENCE. The discriminator, repeated on the OTHER two write paths.

    One bound, three enforcers: if ``update_recurring`` or
    ``promote_to_recurring`` re-derived the boundary independently (or kept a
    clock comparison), ``STRADDLE`` would be refused for org B here while
    ``create_recurring`` accepts it in the test above.
    """
    org_b = await _seed(db_session, cycle_day=CYCLE_B)

    t = await _template(db_session, org_b, due=datetime.date(2026, 9, 1))
    updated = await recurring_service.update_recurring(
        db_session, org_b["org_id"], t.id,
        RecurringUpdate(next_due_date=STRADDLE), today=TODAY,
    )
    assert updated.next_due_date == STRADDLE

    tx = await _add_tx(db_session, org_b)
    promoted = await transaction_service.promote_to_recurring(
        db_session, org_b["org_id"], tx.id,
        PromoteToRecurringRequest(frequency="monthly", next_due_date=STRADDLE),
        today=TODAY,
    )
    assert promoted.recurring_id is not None


# ─────────────────────────────────────────────────────────────────────────────
# F7 — ONE clock per request, on the ``today=None`` production path.
# ─────────────────────────────────────────────────────────────────────────────

class _TickingClock:
    """A ``datetime`` module stand-in whose ``date.today()`` advances after one read.

    First call returns ``first``; every call after it returns ``then``. Any
    implementation that resolves the wall clock more than once per request
    therefore sees two different days, which is precisely the condition a real
    midnight crossing produces and nothing else in this module can simulate.

    ``date`` is a genuine ``datetime.date`` SUBCLASS, so values it hands back
    still bind to SQLAlchemy columns and compare against plain dates; only
    ``today`` is replaced.
    """

    def __init__(self, first: datetime.date, then: datetime.date):
        self.calls = 0
        outer = self

        class _Date(datetime.date):
            @classmethod
            def today(cls) -> datetime.date:
                outer.calls += 1
                return first if outer.calls == 1 else then

        self.date = _Date
        self.datetime = datetime.datetime
        self.timedelta = datetime.timedelta


async def test_f7_update_resolves_the_clock_exactly_once(db_session, monkeypatch):
    """FENCE — ``update_recurring`` normalises ``today`` ONCE, before its callees.

    ``routers/recurring.py`` passes no clock, so ``today=None`` IS the
    production path. Threading that raw None onward gives TWO consumers their
    own ``date.today()``: ``_reanchor_frontier_on_resume`` (via
    ``frontier_lower_bound``) and ``validate_frontier`` (via the same helper).
    A request in flight across midnight then re-anchors against one cycle start
    and validates against the next.

    The fixture is that midnight in miniature. Org B (``billing_cycle_day=15``)
    resumes a template anchored on ``EVE_ONLY``:
      * on ``CLOCK_EVE``  ``p_start`` is 2026-07-15, the frontier is already
        compliant, the re-anchor walks ZERO steps and changes nothing;
      * on ``CLOCK_TICK`` ``p_start`` is 2026-08-15 and that same untouched
        frontier is illegal.
    So a second resolution 400s an update the first resolution just decided was
    fine — with no state anywhere for a row-count or balance assertion to
    notice. Reachable once per cycle per org, on exactly the day that matters.

    Wrong implementation killed: deleting the ``if today is None: today =
    datetime.date.today()`` normalisation at the top of ``update_recurring`` and
    passing ``today`` through raw. The clock hands out ``CLOCK_EVE`` once and
    ``CLOCK_TICK`` forever after, so the mutant raises ``ValidationError``
    naming 2026-08-15 and both assertions below fail.

    ⚠ ``today`` is deliberately NOT passed. Every other fence in this module
    and in the route file injects it explicitly, which is exactly why none of
    them can see this defect.

    ⚠ ``clock.calls == 1`` is the property proper, asserted rather than
    inferred: an implementation could resolve twice and still succeed on a day
    when the two resolutions happen to agree, and that is the vacuous version
    of this test.
    """
    clock = _TickingClock(CLOCK_EVE, CLOCK_TICK)
    monkeypatch.setattr(recurring_service, "datetime", clock)

    seed = await _seed(db_session, cycle_day=CYCLE_B)
    t = await _template(db_session, seed, due=EVE_ONLY, is_active=False)

    updated = await recurring_service.update_recurring(
        db_session, seed["org_id"], t.id,
        RecurringUpdate(is_active=True, next_due_date=EVE_ONLY),
    )

    assert updated.is_active is True
    # Compliant on CLOCK_EVE: the re-anchor made zero passes and the validator
    # accepted it. Nothing here is rescued by the re-anchor.
    assert updated.next_due_date == EVE_ONLY
    assert clock.calls == 1


# ─────────────────────────────────────────────────────────────────────────────
# No upper bound.
# ─────────────────────────────────────────────────────────────────────────────

async def test_no_upper_bound_on_the_frontier(db_session):
    """FENCE. The ruling is a LOWER bound only, on all three paths.

    A far-future frontier costs nothing — generation simply does not select the
    template until its cycle arrives. Wrong implementation killed: "clamp the
    frontier into the current window", i.e. adding a symmetric
    ``<= window_end`` arm, which would break every legitimately-scheduled
    future template.
    """
    seed = await _seed(db_session, cycle_day=CYCLE_A)
    far = datetime.date(2036, 1, 1)

    r = await recurring_service.create_recurring(
        db_session, seed["org_id"], _create_body(seed, far), today=TODAY
    )
    assert r.next_due_date == far

    updated = await recurring_service.update_recurring(
        db_session, seed["org_id"], r.id,
        RecurringUpdate(next_due_date=far + datetime.timedelta(days=365)),
        today=TODAY,
    )
    assert updated.next_due_date == far + datetime.timedelta(days=365)

    tx = await _add_tx(db_session, seed)
    promoted = await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], tx.id,
        PromoteToRecurringRequest(frequency="yearly", next_due_date=far),
        today=TODAY,
    )
    assert promoted.recurring_id is not None
