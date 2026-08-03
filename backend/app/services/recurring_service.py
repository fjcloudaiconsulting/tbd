"""Recurring transaction service — template management and auto-generation.

Generates pending transactions from recurring templates when their
next_due_date has passed. Advances next_due_date based on frequency.
"""

import datetime

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.schemas.recurring import RecurringCreate, RecurringResponse, RecurringUpdate
from app.services.billing_service import current_cycle_window
from app.services.date_utils import MAX_OCCURRENCE_ITERATIONS, advance_date
from app.services.exceptions import NotFoundError, ValidationError
from app.services.transaction_service import (
    apply_balance,
    get_account_for_update,
    validate_account,
    validate_category_for_type,
)

logger = structlog.stdlib.get_logger()

# Defensive per-RUN cap so a pathologically stale template can't spin an
# unbounded loop inside one scheduler tick.
#
# An ALIAS of ``date_utils.MAX_OCCURRENCE_ITERATIONS``, deliberately not a
# second literal: ``forecast_service`` projects the occurrences this loop has
# not yet materialised, walking the same grid from the same origin, and sizing
# the two walks from two literals is how they drift.
#
# ⚠ Aliasing does NOT by itself make the two walks truncate at the same place,
# and no comment here should claim it does. This cap MAKES PROGRESS — it
# mutates ``next_due_date`` forward, so the next run resumes further along —
# while a cap on a projection makes none. That asymmetry is why
# ``occurrences_in_window``'s fast-forward carries no cap at all;
# ``test_forecast_overdue_recurring.py`` F17 fences the conservation this cap
# used to break, and pins the alias with an AST guard.
MAX_CATCHUP_ITERATIONS = MAX_OCCURRENCE_ITERATIONS


def _load_opts():
    return [selectinload(RecurringTransaction.account), selectinload(RecurringTransaction.category)]


def to_response(r: RecurringTransaction) -> RecurringResponse:
    return RecurringResponse(
        id=r.id,
        account_id=r.account_id,
        account_name=r.account.name if r.account else "",
        category_id=r.category_id,
        category_name=r.category.name if r.category else "",
        description=r.description,
        amount=r.amount,
        type=r.type,
        frequency=r.frequency.value,
        next_due_date=r.next_due_date,
        auto_settle=r.auto_settle,
        is_active=r.is_active,
    )


# ── CRUD ──────────────────────────────────────────────────────────────────────

async def list_recurring(db: AsyncSession, org_id: int) -> list[RecurringTransaction]:
    result = await db.execute(
        select(RecurringTransaction)
        .options(*_load_opts())
        .where(RecurringTransaction.org_id == org_id)
        .order_by(RecurringTransaction.next_due_date)
    )
    return list(result.scalars().all())


async def create_recurring(db: AsyncSession, org_id: int, body: RecurringCreate) -> RecurringTransaction:
    # Validate refs. Category must be type-compatible with the template's
    # transaction type, generate_due_transactions writes Transaction rows
    # directly from the template and would otherwise emit mismatched rows
    # at every cycle, bypassing the guard on _create_transaction_no_commit.
    await validate_account(db, body.account_id, org_id)
    await validate_category_for_type(
        db, body.category_id, org_id, TransactionType(body.type)
    )

    r = RecurringTransaction(
        org_id=org_id,
        account_id=body.account_id,
        category_id=body.category_id,
        description=body.description,
        amount=body.amount,
        type=body.type,
        frequency=Frequency(body.frequency),
        next_due_date=body.next_due_date,
        auto_settle=body.auto_settle,
    )
    db.add(r)
    await db.commit()

    result = await db.execute(
        select(RecurringTransaction).options(*_load_opts()).where(RecurringTransaction.id == r.id)
    )
    return result.scalar_one()


async def update_recurring(
    db: AsyncSession, org_id: int, recurring_id: int, body: RecurringUpdate,
    today: datetime.date | None = None,
) -> RecurringTransaction:
    """Update a recurring template.

    ``today`` is the caller's resolved clock, used only when this update
    REACTIVATES a stopped template (see ``_reanchor_frontier_on_resume``).
    Passing None falls back to ``date.today()``; do NOT rely on that from any
    path that has already resolved a clock (TBD-284).

    ⚠ On that same reactivation, a supplied ``body.next_due_date`` EARLIER than
    the current billing cycle start is SILENTLY OVERWRITTEN. Fields are applied
    first, then the re-anchor walks the frontier forward from whatever it now
    holds, so a past date is walked up onto the current cycle exactly as a
    frozen one would be. A supplied date at or after the cycle start survives
    untouched -- the walk makes zero passes. This is deliberate (a
    client-supplied past date IS the back-fill TBD-300 removes) and correct,
    but it means the returned ``next_due_date`` can differ from the one sent.
    On a plain update that does not flip ``is_active`` False->True, the
    supplied date is always kept verbatim.
    """
    result = await db.execute(
        select(RecurringTransaction)
        .options(*_load_opts())
        .where(RecurringTransaction.id == recurring_id, RecurringTransaction.org_id == org_id)
    )
    r = result.scalar_one_or_none()
    if r is None:
        raise NotFoundError("Recurring transaction")

    # Captured BEFORE any field is applied, so the False->True transition is
    # detectable below even though `is_active` is written in this same block.
    was_active = r.is_active

    if body.account_id is not None:
        await validate_account(db, body.account_id, org_id)
        r.account_id = body.account_id
    if body.description is not None:
        r.description = body.description
    if body.amount is not None:
        r.amount = body.amount
    if body.frequency is not None:
        r.frequency = Frequency(body.frequency)
    if body.next_due_date is not None:
        r.next_due_date = body.next_due_date
    if body.auto_settle is not None:
        r.auto_settle = body.auto_settle
    if body.is_active is not None:
        r.is_active = body.is_active

    # Validate the post-update (type, category) pair when either changes.
    # Mirrors update_transaction's pattern: a partial update only touching
    # one of the two fields must still be compatible with the unchanged
    # one. validate_category_for_type also re-checks org ownership when a
    # new category_id is supplied.
    if body.type is not None or body.category_id is not None:
        new_type = TransactionType(body.type) if body.type is not None else TransactionType(r.type)
        new_category_id = body.category_id if body.category_id is not None else r.category_id
        await validate_category_for_type(db, new_category_id, org_id, new_type)
        if body.type is not None:
            r.type = body.type
        if body.category_id is not None:
            r.category_id = body.category_id

    # TBD-300: re-anchor the frontier when this update REACTIVATES a stopped
    # template. Must run after `is_active` is applied and before the commit.
    # Gated on the transition, not on `body.is_active is True`: a no-op update
    # that re-sends `is_active: true` on an already-active template must not
    # move the frontier.
    if body.is_active is True and not was_active:
        await _reanchor_frontier_on_resume(db, org_id, r, today=today)

    await db.commit()

    result = await db.execute(
        select(RecurringTransaction).options(*_load_opts()).where(RecurringTransaction.id == r.id)
    )
    return result.scalar_one()


# Runaway guard only, not policy: ~23 years of weekly occurrences. A frontier
# further behind than this is corrupt data, not a paused template.
#
# ⚠ Deliberately its OWN literal, NOT an alias of
# ``date_utils.MAX_OCCURRENCE_ITERATIONS`` -- which the ``MAX_CATCHUP_ITERATIONS``
# comment above otherwise insists every walk over a template's occurrence grid
# be sized by. A reader who took that comment at face value would expect a
# third alias here, so: the alias rule is about walks that must TRUNCATE
# TOGETHER. Generation materialises occurrences and forecast projects the ones
# it has not yet materialised; they walk one grid from one origin, and two
# literals are how they come to disagree about it. This walk has no
# counterpart to agree with -- nothing else re-anchors a frontier -- and it
# bounds a different quantity: staleness of ONE template at ONE resume, not
# occurrences per window. Aliasing would tie "500 occurrences in a billing
# period" to "23 years behind", two numbers with no reason to move together.
#
# Exhausting it is NOT benign, and the loop logs when it happens: the template
# is left active with a frontier still behind ``p_start``, so the next tick
# back-fills up to ``MAX_CATCHUP_ITERATIONS`` rows. See
# ``recurring.resume.reanchor_cap``.
_MAX_FRONTIER_ADVANCE_STEPS = 1200


async def _reanchor_frontier_on_resume(
    db: AsyncSession, org_id: int, r: RecurringTransaction,
    *, today: datetime.date | None = None,
) -> None:
    """Advance a resumed template's frontier onto the current billing cycle.

    ``stop_recurring`` freezes ``next_due_date``, and generation filters on
    ``is_active``, so a paused template's frontier falls one day further behind
    for every day it is paused. Resuming without re-anchoring therefore hands
    the catch-up loop the entire paused gap, and it materialises EVERY
    occurrence in it -- each one written SETTLED when ``auto_settle`` is on,
    each one applying to the account balance. Reachable in two UI clicks, with
    no date supplied by anyone (TBD-300). Frozen frontier + ``is_active``
    filter => back-fill on resume: that is the whole causal chain, and it is
    the only thing this function is here to break.

    ⚠ The idempotency probe is NOT part of that chain. An earlier version of
    this docstring joined the two with a "therefore" and the "therefore" was
    unearned. ``stop_recurring`` does null ``recurring_id`` on every surviving
    row, settled ones included, which blinds the generation loop's probe
    (``recurring_id == r.id AND date == due``) -- but the gap occurrences were
    never materialised at all (generation was off for the whole pause), so
    there are no rows at those dates for the probe to find or miss. The
    nulling matters on a DIFFERENT and still-open path: dates materialised
    BEFORE the pause become re-creatable once the frontier is moved backward
    onto them, which takes an explicit backward ``next_due_date`` write that no
    UI affordance issues today. Tracked as TBD-283, out of scope here.

    ⚠ Never ``next_due_date = today``. That stops the duplication too, so the
    row-count and balance fences cannot tell the two implementations apart --
    but it silently RE-ANCHORS the series: a rent template paused on the 1st
    and resumed on the 17th would bill on the 17th forever. Worse than the bug
    it replaces, because it is invisible.

    ⚠ ``p_start``, not ``today``: a template resumed mid-cycle must still
    produce the current cycle's own occurrence. Advancing to ``today`` silently
    skips a charge the user is genuinely due.

    A frontier already at or after ``p_start`` is left exactly where it is.
    Pausing and resuming inside one cycle is ordinary use and must not cost the
    user that cycle's charge.
    """
    if today is None:
        today = datetime.date.today()

    org = await db.scalar(select(Organization).where(Organization.id == org_id))
    cycle_day = org.billing_cycle_day if org else 1
    p_start, _ = current_cycle_window(cycle_day, today)

    # Walk the template's OWN grid, iterated, never closed-form.
    #
    # NOT because `advance_date` preserves the series' alignment -- it does the
    # opposite. It is PATH-DEPENDENT for month-end templates (Jan 31 -> Feb 28
    # -> Mar 28, never back to 31; `date_utils.advance_date` is
    # `current + relativedelta(months=1)` and the clamping is destructive). This
    # walk inherits every bit of that drift.
    #
    # The point is that it inherits EXACTLY that drift. `generate_due_transactions`
    # advances its own frontier with the identical `advance_date` call, so
    # walking Jan 31 -> Feb 28 -> ... -> Jun 28 lands on precisely the frontier
    # main's back-fill loop would have reached while materialising those same
    # rows. Same grid, same drift, same landing point -- the fix introduces no
    # NEW misalignment, which is the only alignment claim available here. A
    # closed-form jump (Jan 31 + 5 months = Jun 30) would land on a date
    # generation never visits. `occurrences_in_window` argues this for the same
    # reason (`date_utils.py`).
    #
    # The loop condition IS the already-current guard: a frontier at or after
    # `p_start` makes zero passes and is left exactly where it is, which is what
    # a pause-and-resume inside one cycle needs. An explicit early-return above
    # this loop was removed as dead code -- no test could kill it, because the
    # condition it checked is the negation of the loop's own.
    steps = 0
    while r.next_due_date < p_start:
        # Mirrors generation's own cap arm (`recurring.generate.catchup_cap`).
        # Falling through leaves the template ACTIVE with a frontier still
        # behind `p_start`, so the next tick back-fills up to
        # MAX_CATCHUP_ITERATIONS rows -- this ticket's defect, re-entered. It
        # must not be silent.
        if steps >= _MAX_FRONTIER_ADVANCE_STEPS:
            await logger.awarning(
                "recurring.resume.reanchor_cap",
                org_id=org_id, recurring_id=r.id, next_due_date=str(r.next_due_date),
            )
            break
        r.next_due_date = advance_date(r.next_due_date, r.frequency)
        steps += 1


async def _remove_pending_transactions(
    db: AsyncSession, org_id: int, recurring_id: int,
) -> int:
    """Bulk-delete pending future transactions for a recurring template.
    Returns the number of rows removed."""
    today = datetime.date.today()
    result = await db.execute(
        delete(Transaction).where(
            Transaction.recurring_id == recurring_id,
            Transaction.org_id == org_id,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.date >= today,
        )
    )
    return result.rowcount


async def stop_recurring(db: AsyncSession, org_id: int, recurring_id: int) -> int:
    """Deactivate the template and delete any pending future transactions it generated.
    Returns the number of pending transactions removed. Settled transactions are preserved."""
    result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.id == recurring_id, RecurringTransaction.org_id == org_id
        )
    )
    r = result.scalar_one_or_none()
    if r is None:
        raise NotFoundError("Recurring transaction")

    r.is_active = False
    removed = await _remove_pending_transactions(db, org_id, recurring_id)

    # Clear the now-defunct recurring link on all surviving rows (settled, plus
    # any past-dated pending) so the "Recurring" badge disappears, mirroring
    # delete's ON DELETE SET NULL.
    await db.execute(
        update(Transaction)
        .where(
            Transaction.recurring_id == recurring_id,
            Transaction.org_id == org_id,
        )
        .values(recurring_id=None)
    )

    await db.commit()
    return removed


async def delete_recurring(db: AsyncSession, org_id: int, recurring_id: int) -> int:
    """Permanently delete the template (only if already stopped/paused).
    Also removes any remaining pending future transactions.
    Returns count of pending transactions removed."""
    result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.id == recurring_id, RecurringTransaction.org_id == org_id
        )
    )
    r = result.scalar_one_or_none()
    if r is None:
        raise NotFoundError("Recurring transaction")

    removed = await _remove_pending_transactions(db, org_id, recurring_id)

    await db.delete(r)
    await db.commit()
    return removed


# ── Generation ────────────────────────────────────────────────────────────────

async def _settle_due_auto(db: AsyncSession, org_id: int, today: datetime.date) -> int:
    """Promote PENDING transactions that originated from an auto_settle template
    and whose date has now passed (date <= today) to SETTLED, adjusting balance.
    Non-auto_settle pending items are never touched."""
    result = await db.execute(
        select(Transaction)
        .join(RecurringTransaction, Transaction.recurring_id == RecurringTransaction.id)
        .where(
            Transaction.org_id == org_id,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.recurring_id.is_not(None),
            Transaction.date <= today,
            RecurringTransaction.auto_settle == True,  # noqa: E712
        )
        .with_for_update(of=Transaction)
    )
    rows = list(result.scalars().all())
    # Lock order: transaction rows first (the SELECT ... FOR UPDATE above),
    # then the account row per item. /generate is user-triggered and the
    # generation loop's FOR UPDATE on templates effectively serializes
    # concurrent runs per org, so account locks are not contended across the
    # sweep and the loop.
    for tx in rows:
        async with db.begin_nested():
            tx.status = TransactionStatus.SETTLED
            tx.settled_date = tx.date
            acct = await get_account_for_update(db, tx.account_id, org_id)
            apply_balance(acct, tx.amount, tx.type)
    return len(rows)


async def generate_due_transactions(
    db: AsyncSession, org_id: int, today: datetime.date | None = None
) -> dict:
    """Materialize recurring instances due within the current billing cycle window.

    Window is derived purely from org.billing_cycle_day + today (no BillingPeriod
    row reads/writes). Future-in-period instances are PENDING; auto_settle only
    settles instances whose date has passed. Overdue prior-period instances are
    caught up. Idempotent: re-running advances next_due_date past the window end.

    `today` is the caller's resolved clock. The scheduler passes the value the
    runner resolved once for the whole tick (``RecurringGenerationJob.run``), so
    one tick cannot straddle midnight and materialise rows against a different
    day than the one it decided was due (TBD-284). Passing None falls back to
    ``date.today()``; do NOT rely on that from any path that has already
    resolved a clock.
    Returns {"generated", "settled", "pending", "period_end"}.
    """
    if today is None:
        today = datetime.date.today()

    org = await db.scalar(select(Organization).where(Organization.id == org_id))
    cycle_day = org.billing_cycle_day if org else 1
    _, period_end = current_cycle_window(cycle_day, today)

    settled_now = await _settle_due_auto(db, org_id, today)

    result = await db.execute(
        select(RecurringTransaction)
        .where(
            RecurringTransaction.org_id == org_id,
            RecurringTransaction.is_active == True,  # noqa: E712
            RecurringTransaction.next_due_date <= period_end,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    due_items = list(result.scalars().all())
    created = 0
    created_settled = 0

    for r in due_items:
        iterations = 0
        while r.next_due_date <= period_end:
            if iterations >= MAX_CATCHUP_ITERATIONS:
                await logger.awarning(
                    "recurring.generate.catchup_cap",
                    org_id=org_id, recurring_id=r.id, next_due_date=str(r.next_due_date),
                )
                break
            iterations += 1
            due = r.next_due_date

            exists = await db.scalar(
                select(Transaction.id)
                .where(
                    Transaction.org_id == org_id,
                    Transaction.recurring_id == r.id,
                    Transaction.date == due,
                )
                .limit(1)
            )
            if exists:
                r.next_due_date = advance_date(due, r.frequency)
                continue

            tx_status = (
                TransactionStatus.SETTLED
                if (r.auto_settle and due <= today)
                else TransactionStatus.PENDING
            )
            async with db.begin_nested():
                tx = Transaction(
                    org_id=org_id,
                    account_id=r.account_id,
                    category_id=r.category_id,
                    description=r.description,
                    amount=r.amount,
                    type=TransactionType(r.type),
                    status=tx_status,
                    date=due,
                    settled_date=due if tx_status == TransactionStatus.SETTLED else None,
                    recurring_id=r.id,
                )
                db.add(tx)
                if tx_status == TransactionStatus.SETTLED:
                    acct = await get_account_for_update(db, r.account_id, org_id)
                    apply_balance(acct, r.amount, TransactionType(r.type))

            r.next_due_date = advance_date(due, r.frequency)
            created += 1
            if tx_status == TransactionStatus.SETTLED:
                created_settled += 1

    await db.commit()
    return {
        "generated": created,
        "settled": created_settled + settled_now,
        "pending": created - created_settled,
        "period_end": period_end.isoformat(),
    }
