"""Recurring transaction service — template management and auto-generation.

Generates pending transactions from recurring templates when their
next_due_date has passed. Advances next_due_date based on frequency.
"""

import dataclasses
import datetime

import logging
from sqlalchemy import delete, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.models.account import Account, AccountType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.schemas.recurring import RecurringCreate, RecurringResponse, RecurringUpdate
from app.services.billing_service import current_cycle_window
from app.services.date_utils import MAX_OCCURRENCE_ITERATIONS, advance_date
from app.services.currency_service import normalise_currency
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.recurring_filters import (
    active_series_filter,
    has_remaining_occurrences,
)
from app.services.transaction_filters import (
    REVERTED_RECONCILIATION_STATES,
    effective_period_date_expr,
    is_reciprocal_pair,
    non_reverted_transaction_filter,
)
from app.services.transaction_service import (
    _link_pair,
    _load_opts as _tx_load_opts,
    BatchSettlementPlan,
    _propagate_fields_to_series,
    apply_balance,
    apply_batch_settlement,
    get_account_for_update,
    plan_batch_settlement,
    validate_account,
    validate_category_for_type,
)

logger = logging.getLogger(__name__)

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


@dataclasses.dataclass(frozen=True)
class PendingRemovalResult:
    """What removing a template's pending rows actually did (TBD-312).

    A NAMED-FIELD type, not a bare tuple, and deliberately so: the two routes
    that consume this previously declared ``response_model=dict``, which
    validates nothing, so a tuple would have serialised as a JSON array and
    shipped. Naming the fields makes every consumer, tests included, say which
    number it means.

    ``demoted_ids`` are rows marked REJECTED because the row they matched is
    being deleted. That is irreversible through every API we expose, so the
    caller is obliged to surface it rather than report ``removed`` alone.
    """

    removed: int
    demoted_ids: list[int]


def _load_opts():
    return [
        selectinload(RecurringTransaction.account),
        selectinload(RecurringTransaction.to_account),
        selectinload(RecurringTransaction.category),
    ]


# ── Frontier lower bound (TBD-283) ────────────────────────────────────────────

async def frontier_lower_bound(
    db: AsyncSession, org_id: int, *, today: datetime.date | None = None
) -> datetime.date:
    """The earliest date a template's ``next_due_date`` may legally hold.

    ``current_cycle_window(org.billing_cycle_day, today)[0]`` -- ``p_start``,
    the start of the org's CURRENT billing cycle. One derivation, one caller
    surface: ``create_recurring``, ``update_recurring``,
    ``promote_to_recurring`` and ``_reanchor_frontier_on_resume`` all read the
    bound from here, so the write paths and the re-anchor cannot come to
    disagree about where the floor is.

    ⚠ Org-dependent, which is exactly why this lives in the service layer and
    not in a pydantic validator. A schema validator cannot see
    ``org.billing_cycle_day``; it could only encode some *other* rule
    (``>= today`` being the obvious one) and would then pre-empt this one on
    every request, re-creating the three-way disagreement TBD-283 removes.

    ⚠ Not ``today``. ``>= today`` looks equivalent and is not: it forbids
    anchoring a template anywhere in the already-open cycle, which is a legal
    and load-bearing position (``test_forecast_overdue_recurring`` F16 rewinds
    three frontiers onto ``p_start``, five days behind ``today``). On the
    cycle-start day the two rules coincide -- which is why the rejection
    message must name the actual boundary date rather than say "in the past".
    """
    if today is None:
        today = datetime.date.today()
    org = await db.scalar(select(Organization).where(Organization.id == org_id))
    cycle_day = org.billing_cycle_day if org else 1
    p_start, _ = current_cycle_window(cycle_day, today)
    return p_start


async def validate_frontier(
    db: AsyncSession, org_id: int, next_due_date: datetime.date,
    *, today: datetime.date | None = None,
) -> datetime.date:
    """Reject a ``next_due_date`` earlier than the current billing cycle start.

    Lower bound only -- there is deliberately NO upper bound. A far-future
    frontier costs nothing: generation simply does not select the template
    until its cycle arrives. A far-PAST frontier is what back-fills closed
    history, and (because ``stop_recurring`` nulls ``recurring_id`` on every
    surviving row) can re-materialise dates that were already materialised
    once, because the generation loop's idempotency probe keys on
    ``recurring_id`` and no longer sees them.

    Returns ``p_start`` so callers that also need the boundary do not re-read
    the org.

    ⚠ The message states a REMEDY as well as the boundary. ``update_recurring``
    validates the post-write PAIR, so a request carrying only
    ``{"frequency": ...}`` can be refused over a ``next_due_date`` the user
    never mentioned; naming the boundary alone leaves them with a rejection
    about a field that is not in their request and no stated way out.
    """
    p_start = await frontier_lower_bound(db, org_id, today=today)
    if next_due_date < p_start:
        raise ValidationError(
            f"Next due date cannot be earlier than {p_start.isoformat()}, "
            "the start of the current billing cycle. Send a next_due_date on "
            "or after that date; a frequency change on a template whose "
            "schedule is already behind must carry one."
        )
    return p_start


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
        to_account_id=r.to_account_id,
        to_account_name=r.to_account.name if r.to_account else "",
        frequency=r.frequency.value,
        next_due_date=r.next_due_date,
        auto_settle=r.auto_settle,
        is_active=r.is_active,
        occurrence_count=r.occurrence_count,
        occurrences_elapsed=r.occurrences_elapsed,
    )


async def _validate_transfer_accounts(
    db: AsyncSession, org_id: int, r_type: str,
    account_id: int, to_account_id: int | None,
) -> None:
    """TBD-600: ``to_account_id`` is required iff the series is a transfer, and
    then names another account of the same org and currency.

    Same currency because ``_link_pair`` refuses a cross-currency pair, and a
    template it refuses would fail every generation run, not just this request.
    """
    if r_type != "transfer":
        if to_account_id is not None:
            raise ValidationError("Only a transfer has a destination account")
        return
    if to_account_id is None:
        raise ValidationError("A transfer needs a destination account")
    if to_account_id == account_id:
        raise ValidationError("Source and destination accounts must be different")
    rows = {
        aid: (currency, slug, active) for aid, currency, slug, active in (await db.execute(
            select(Account.id, Account.currency, AccountType.slug, Account.is_active)
            .join(AccountType, AccountType.id == Account.account_type_id, isouter=True)
            .where(Account.id.in_([account_id, to_account_id]), Account.org_id == org_id)
        )).all()
    }
    if len(rows) != 2:
        raise ValidationError("Invalid account")
    # An account archived AFTER the series was set up keeps it running: its
    # leg is still generated, and the projections show only the active leg.
    if not all(active for _, _, active in rows.values()):
        raise ValidationError("A recurring transfer needs two active accounts")
    _refuse_card_or_loan_source(rows[account_id][1])
    if normalise_currency(rows[account_id][0]) != normalise_currency(rows[to_account_id][0]):
        raise ValidationError("Transfer accounts must have the same currency")


# TBD-789: a card or a loan may be the DESTINATION (a payment toward the card,
# extra principal on the loan; ``projection_kernel`` models both), never the
# source: a card as source is a cash advance, which is not supported.
_UNSUPPORTED_TRANSFER_SOURCE_SLUGS = {"credit_card", "loan"}


def _refuse_card_or_loan_source(slug) -> None:
    if slug in _UNSUPPORTED_TRANSFER_SOURCE_SLUGS:
        raise ValidationError(
            "A recurring transfer cannot come from a credit card or loan account."
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


async def create_recurring(
    db: AsyncSession, org_id: int, body: RecurringCreate,
    today: datetime.date | None = None,
) -> RecurringTransaction:
    """Create a recurring template.

    ``today`` is the caller's resolved clock, used only to derive the frontier
    lower bound. Passing None falls back to ``date.today()``.
    """
    # Validate refs. Category must be type-compatible with the template's
    # transaction type, generate_due_transactions writes Transaction rows
    # directly from the template and would otherwise emit mismatched rows
    # at every cycle, bypassing the guard on _create_transaction_no_commit.
    await validate_account(db, body.account_id, org_id)
    # A transfer validates as TransactionType.TRANSFER, which only a BOTH
    # category passes: both legs share it, as on every transfer pair.
    await validate_category_for_type(
        db, body.category_id, org_id, TransactionType(body.type)
    )
    await _validate_transfer_accounts(
        db, org_id, body.type, body.account_id, body.to_account_id
    )
    # TBD-283: one lower bound, enforced identically by all three write paths.
    await validate_frontier(db, org_id, body.next_due_date, today=today)

    r = RecurringTransaction(
        org_id=org_id,
        account_id=body.account_id,
        to_account_id=body.to_account_id,
        category_id=body.category_id,
        description=body.description,
        amount=body.amount,
        type=body.type,
        frequency=Frequency(body.frequency),
        next_due_date=body.next_due_date,
        auto_settle=body.auto_settle,
        occurrence_count=body.occurrence_count,
        # A template created directly has delivered NOTHING yet. Contrast
        # ``promote_to_recurring``, which seeds 1 because its source
        # transaction IS instalment 1 (TBD-275).
        occurrences_elapsed=0,
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

    ``today`` is the caller's resolved clock. It feeds TWO consumers here --
    ``_reanchor_frontier_on_resume`` and ``validate_frontier`` -- each of which
    derives its boundary from ``frontier_lower_bound``. Passing None falls back
    to ``date.today()``, resolved ONCE below and then threaded; do NOT rely on
    that fallback from any path that has already resolved a clock (TBD-284).

    ⚠ That normalisation is load-bearing, not tidiness. ``routers/recurring.py``
    passes no clock, so None IS the production path. Handing the raw None to
    both callees would make each resolve ``date.today()`` independently, and a
    request in flight across midnight would then re-anchor against one cycle
    start and validate against the next: the re-anchor sees a compliant
    frontier and walks zero steps, the validator computes a boundary a cycle
    later and 400s the whole update. Reachable once per cycle per org, on
    exactly the day that matters.

    ⚠ On a reactivation, a supplied ``body.next_due_date`` EARLIER than
    the current billing cycle start is SILENTLY OVERWRITTEN. Fields are applied
    first, then the re-anchor walks the frontier forward from whatever it now
    holds, so a past date is walked up onto the current cycle exactly as a
    frozen one would be. A supplied date at or after the cycle start survives
    untouched -- the walk makes zero passes. This is deliberate (a
    client-supplied past date IS the back-fill TBD-300 removes) and correct,
    but it means the returned ``next_due_date`` can differ from the one sent.
    On a plain update that does not flip ``is_active`` False->True, the
    supplied date is always kept verbatim.

    ``occurrence_count`` (TBD-275) is editable, an explicit null making the
    series open-ended again (TBD-762), and may be edited DOWNWARD below
    what the series has already delivered. That is honoured, not rejected:
    ``recurring_filters.remaining_occurrences`` goes negative,
    ``active_series_filter`` drops the template, and the series simply stops.
    Rejecting it would be worse -- the user's only escape from a runaway
    instalment plan would be to stop the template, which NULLs the
    ``recurring_id`` grouping on every row it ever produced.

    ⚠ An UPWARD edit that un-exhausts a finished series re-anchors the frontier
    exactly as a reactivation does, and for the same reason. See the comment on
    ``unexhausted`` below; the ``next_due_date``-is-silently-overwritten caveat
    two paragraphs up applies verbatim to that path too.

    ``occurrences_elapsed`` is NOT on ``RecurringUpdate`` and is never
    user-writable. ``generate_due_transactions`` is its only writer.
    """
    # ONE resolution for the whole request. See the docstring: two consumers
    # downstream, and a raw None makes each of them read the wall separately.
    if today is None:
        today = datetime.date.today()

    # TBD-756/TBD-760: an amount, name, category or account change moves the
    # pending occurrences still at the OLD value, so that old value must be
    # read under the lock. TBD-762 widens it to any request carrying more than
    # ``is_active``: generation locks the template too, so it cannot move the
    # frontier or add a row between this read and the commit. The template lock
    # alone does NOT make the next-due guard's row read current: on MySQL
    # REPEATABLE READ a plain SELECT reads the snapshot taken by this request's
    # first statement, from before it waited here, so the guard's read is a
    # locking read of its own. Resume alone stays a plain read, as it always
    # was. Lock order stays template -> rows.
    q = (
        select(RecurringTransaction)
        .options(*_load_opts())
        .where(RecurringTransaction.id == recurring_id, RecurringTransaction.org_id == org_id)
    )
    if body.model_fields_set - {"is_active"}:
        q = q.with_for_update().execution_options(populate_existing=True)
    r = (await db.execute(q)).scalar_one_or_none()
    if r is None:
        raise NotFoundError("Recurring transaction")

    # Captured BEFORE any field is applied, so the False->True transitions are
    # detectable below even though both fields are written in this same block.
    was_active = r.is_active
    had_remaining = has_remaining_occurrences(r)
    old_amount = r.amount
    old_type = TransactionType(r.type)
    old_description = r.description
    old_category_id = r.category_id
    old_account_id = r.account_id
    old_to_account_id = r.to_account_id
    old_next_due = r.next_due_date

    if body.account_id is not None:
        await validate_account(db, body.account_id, org_id)
        r.account_id = body.account_id
    if body.to_account_id is not None:
        r.to_account_id = body.to_account_id
    if body.account_id is not None or body.to_account_id is not None:
        await _validate_transfer_accounts(db, org_id, r.type, r.account_id, r.to_account_id)
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
    # TBD-762: an explicit null is "never ends", so this reads key presence,
    # not the value; an omitted key still leaves the count alone. Written here,
    # with the other fields, so ``unexhausted`` below sees it: a finished series
    # made open-ended resumes, and must take the re-anchor like any other.
    if "occurrence_count" in body.model_fields_set:
        r.occurrence_count = body.occurrence_count

    # Validate the post-update (type, category) pair when either changes.
    # Mirrors update_transaction's pattern: a partial update only touching
    # one of the two fields must still be compatible with the unchanged
    # one. validate_category_for_type also re-checks org ownership when a
    # new category_id is supplied.
    # TBD-600: a transfer's rows are linked pairs, and an income or expense
    # series writes single rows; neither can become the other in place.
    if body.type is not None and body.type != r.type and r.type == "transfer":
        raise ValidationError(
            "A recurring transfer cannot change type. Stop it and create a new series."
        )
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
    resumed_by_activation = body.is_active is True and not was_active
    # TBD-275: an UPWARD `occurrence_count` edit is the SECOND resume door, and
    # it is the one this ticket opened. An exhausted series keeps
    # `is_active = True` by design (`recurring_filters`), so it never trips the
    # transition above -- but it has been excluded from generation by
    # `active_series_filter` for however long it has been finished, and its
    # frontier has been frozen for exactly as long. Raising the count
    # un-exhausts it and hands the catch-up loop (which has no lower bound) the
    # whole finished stretch: a 12-instalment `auto_settle` plan last
    # materialised in January and raised to 14 in August back-fills two SETTLED
    # rows into closed periods and moves the account balance by 2x amount.
    # Same causal chain as TBD-300 -- frozen frontier + a filter that resumes --
    # so it takes the same re-anchor.
    unexhausted = has_remaining_occurrences(r) and not had_remaining
    # ⚠ ONE call, never two. Both transitions can fire in a single request (a
    # paused AND exhausted template reactivated with a higher count); a second
    # walk would start from the frontier the first one already left at or after
    # `p_start`, make zero passes and be harmless -- but "harmless today"
    # is not a reason to write it twice.
    if resumed_by_activation or unexhausted:
        await _reanchor_frontier_on_resume(db, org_id, r, today=today)

    # TBD-283: the frontier lower bound, on the POST-WRITE state.
    #
    # ⚠ GATED, not unconditional. Resume sends ``{"is_active": true}`` alone,
    # and the Edit series dialog (TBD-762) sends only the fields the user
    # changed. Re-checking the stored frontier on every update would 400 Resume
    # for exactly the long-paused templates users resume, and refuse a rename
    # over a date nobody touched.
    #
    # ⚠ The ``frequency`` clause is not decoration. Flipping a stale yearly
    # template to weekly multiplies its implied occurrences without a
    # ``next_due_date`` anywhere in the request, so the pair -- not the
    # supplied field -- is what has to be legal.
    #
    # ⚠ AFTER ``_reanchor_frontier_on_resume``, never before. The re-anchor
    # walks a resumed template's frontier FORWARD onto ``p_start``, so a resume
    # is compliant by construction; checking first would reject the very state
    # the re-anchor exists to produce.
    #
    # ⚠ "By construction" holds on every arm but ONE: the re-anchor breaks out
    # at ``_MAX_FRONTIER_ADVANCE_STEPS`` (~23 years of weekly occurrences) and
    # leaves ``next_due_date`` still behind ``p_start``, logging
    # ``recurring.resume.reanchor_cap``. On that arm the gate decides the
    # outcome for one and the same resulting state: ``{"is_active": true}``
    # keeps the gate closed and commits the illegal frontier with only the
    # warning, while ``{"is_active": true, "frequency": "weekly"}`` opens it and
    # 400s. Read this as a gate, not as an invariant that a resume always ends
    # up compliant.
    if body.next_due_date is not None or body.frequency is not None:
        await validate_frontier(db, org_id, r.next_due_date, today=today)

    # TBD-762: a next due date the user moved must clear the series' own rows.
    # Gated on the user's edit: the re-anchor only walks a frozen frontier
    # forward, and Resume alone must behave as it always has.
    if body.next_due_date is not None and r.next_due_date != old_next_due:
        await _check_frontier_clears_series(db, org_id, r, old_next_due=old_next_due)

    # TBD-756/TBD-760: the pending occurrences still at the old amount, name,
    # category or account follow the new one; a row whose value differs keeps
    # it. Compared with the old values, not key presence: a re-send of the same
    # value moves nothing. A ``next_due_date`` edit moves no materialised row:
    # the frontier is the next UN-materialised occurrence, and callers move it
    # by whole periods to skip or re-anchor. The helper's template writes are
    # no-ops here (the template already holds the new values).
    # Category only while the type is unchanged: a type change is out of scope
    # here (rows keep their type), and a category validated for the NEW type
    # must not land on old-type rows.
    amount_changed = r.amount != old_amount
    desc_changed = r.description != old_description
    cat_changed = r.category_id != old_category_id and TransactionType(r.type) == old_type
    acct_changed = r.account_id != old_account_id
    if amount_changed or desc_changed or cat_changed or acct_changed:
        await _propagate_fields_to_series(
            db, org_id, r.id, tx_type=old_type,
            description=r.description if desc_changed else None,
            description_was=old_description,
            category_id=r.category_id if cat_changed else None,
            category_was=old_category_id,
            account_id=r.account_id if acct_changed else None,
            account_was=old_account_id,
            amount=r.amount if amount_changed else None, amount_was=old_amount,
        )
    # TBD-600: that sync skips linked legs, so a transfer's pairs take their own.
    if r.type == "transfer" and (
        amount_changed or desc_changed or acct_changed
        or r.category_id != old_category_id or r.to_account_id != old_to_account_id
    ):
        await _sync_pending_transfer_pairs(
            db, org_id, r, amount_was=old_amount, description_was=old_description,
            category_was=old_category_id, account_was=old_account_id,
            to_account_was=old_to_account_id,
        )

    await db.commit()

    result = await db.execute(
        select(RecurringTransaction).options(*_load_opts()).where(RecurringTransaction.id == r.id)
    )
    return result.scalar_one()


async def _sync_pending_transfer_pairs(
    db: AsyncSession, org_id: int, r: RecurringTransaction, *,
    amount_was, description_was: str, category_was: int,
    account_was: int, to_account_was: int | None,
) -> None:
    """Move a transfer template's edit onto its PENDING pairs (TBD-600).

    The series sync (TBD-756/760, ``_propagate_fields_to_series``) skips
    linked legs because a bulk write cannot keep a pair's invariants; this
    writes both legs of each pair together. A field follows only while it
    still holds the old template value, so a leg changed by hand keeps its
    own: amount and category per pair (both legs share them), the name per
    leg, the source account on the expense leg and the destination on the
    income leg, never onto the partner's account. Pending rows only, so no
    balance logic; currency and the ``both`` category were validated on the
    template.

    Skipped, as ``_series_sync_targets`` skips them: reverted pairs, and legs
    a bank row was reconcile-matched against (a referrer other than the
    partner). Lock order template -> rows, as the other series writes.
    """
    # The template's accounts were validated by ``update_recurring``; checked
    # again here because this is what moves legs onto them.
    _refuse_card_or_loan_source(await db.scalar(
        select(AccountType.slug)
        .join(Account, Account.account_type_id == AccountType.id)
        .where(Account.id == r.account_id, Account.org_id == org_id)
    ))
    referrer = aliased(Transaction)
    legs = (await db.execute(
        select(Transaction)
        .where(
            Transaction.org_id == org_id,
            Transaction.recurring_id == r.id,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.linked_transaction_id.is_not(None),
            Transaction.reconciliation_state.not_in(REVERTED_RECONCILIATION_STATES),
            ~exists().where(
                referrer.org_id == org_id,
                referrer.linked_transaction_id == Transaction.id,
                referrer.id != Transaction.linked_transaction_id,
            ),
        )
        .order_by(Transaction.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all()
    by_id = {t.id: t for t in legs}
    for exp in legs:
        inc = by_id.get(exp.linked_transaction_id)
        if exp.type != TransactionType.EXPENSE or not is_reciprocal_pair(exp, inc):
            continue
        if exp.amount == inc.amount == amount_was:
            exp.amount = inc.amount = r.amount
        if exp.category_id == inc.category_id == category_was:
            exp.category_id = inc.category_id = r.category_id
        for leg in (exp, inc):
            if leg.description == description_was:
                leg.description = r.description
        if exp.account_id == account_was and r.account_id != inc.account_id:
            exp.account_id = r.account_id
        if inc.account_id == to_account_was and r.to_account_id != exp.account_id:
            inc.account_id = r.to_account_id


async def _check_frontier_clears_series(
    db: AsyncSession, org_id: int, r: RecurringTransaction,
    *, old_next_due: datetime.date,
) -> None:
    """Refuse a moved ``next_due_date`` that lands where the series already has
    an occurrence (TBD-762). Writes nothing.

    Rent on the 1st, the Nov 1 row exists, next due Dec 1. Moving the frontier
    to Nov 3 makes generation write a second November rent: its ``exists``
    probe and the forecasts' materialised probe key on ``(recurring_id, date)``
    and cannot see Nov 1. So the new frontier must be strictly after every row
    of the series, and either a whole period (on the post-edit frequency) after
    the latest one, or no earlier than the frontier it replaces: a later move
    adds no occurrence to a period that did not already have one coming.

    TBD-509: the same rule holds against ``last_spent_date``, the latest
    occurrence generation consumed, as if its row still existed. A frontier
    moved back onto a spent date would make generation spend that instalment
    again: through the ``exists`` branch while the row is linked, and by
    re-creating it once the row is deleted or unlinked by a stop. A template
    that spent before the column existed (NULL with ``occurrences_elapsed``)
    is bounded by the day before the frontier it replaces: no move earlier.
    That lifts only when a generation run spends an occurrence and sets the
    column, so a paused or exhausted legacy template keeps it.

    Runs only when the frontier MOVES. A frequency change that keeps it, e.g.
    monthly -> yearly with Nov 1 materialised and Dec 1 next, is accepted
    although Nov 1 and Dec 1 then share a yearly period: the user means "yearly
    from Dec 1", which is exactly what generation does.

    Every row counts, whatever its status or ``reconciliation_state``: a
    skipped occurrence was still that period's occurrence, and generation's
    probe does not read the state either.

    ⚠ A LOCKING read (shared), not a plain one. On MySQL REPEATABLE READ a plain
    SELECT reads the snapshot fixed by the request's first statement (the
    current-user lookup), from BEFORE this request waited on the template lock.
    A generation run holding that lock can insert Nov 1 and move the frontier to
    Dec 1; the waiting PUT then sees frontier Dec 1 but a snapshot without
    Nov 1, and accepts Nov 3. A locking read sees the latest committed rows.
    Lock order stays template -> rows. sqlite ignores the clause; the test
    asserts it on the statement.
    """
    latest = await db.scalar(
        select(Transaction.date)
        .where(Transaction.org_id == org_id, Transaction.recurring_id == r.id)
        .order_by(Transaction.date.desc())
        .limit(1)
        .with_for_update(read=True)
    )
    spent = r.last_spent_date
    if spent is None and r.occurrences_elapsed:
        spent = old_next_due - datetime.timedelta(days=1)
    bound = max((d for d in (latest, spent) if d is not None), default=None)
    if bound is None:
        return
    new = r.next_due_date
    floor = advance_date(bound, r.frequency)
    if new > bound and (new >= floor or new >= old_next_due):
        return
    # Exactly the earliest date the rule above accepts.
    earliest = max(bound + datetime.timedelta(days=1), min(floor, old_next_due))
    if bound == latest:
        raise ConflictError(
            f"The series has a transaction on {latest.isoformat()}, so its next due "
            f"date cannot be {new.isoformat()}. Choose {earliest.isoformat()} or later, "
            f"or move or delete that transaction first."
        )
    raise ConflictError(
        f"The series has already generated its occurrences through "
        f"{bound.isoformat()}, so its next due date cannot be {new.isoformat()}. "
        f"Choose {earliest.isoformat()} or later."
    )


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

    ⚠ ``is_active`` is not the only filter that resumes. ``active_series_filter``
    ALSO excludes an exhausted series from generation while its frontier stays
    frozen, and an upward ``occurrence_count`` edit un-excludes it (TBD-275) --
    the identical chain with a different gate, so ``update_recurring`` calls
    this on that transition too. Everything below applies unchanged; "paused"
    reads as "excluded from generation" throughout.

    ⚠ The idempotency probe is NOT part of that chain. An earlier version of
    this docstring joined the two with a "therefore" and the "therefore" was
    unearned. ``stop_recurring`` does null ``recurring_id`` on every surviving
    row, settled ones included, which blinds the generation loop's probe
    (``recurring_id == r.id AND date == due``) -- but the gap occurrences were
    never materialised at all (generation was off for the whole pause), so
    there are no rows at those dates for the probe to find or miss. The
    nulling matters on a DIFFERENT path: dates materialised BEFORE the pause
    become re-creatable once the frontier is moved backward onto them, which
    takes an explicit backward ``next_due_date`` write.

    ⚠ TBD-283 shipped and BOUNDED that path; it did not remove it, and this
    docstring's earlier "tracked, out of scope" note is closed.
    ``validate_frontier`` now floors every ``next_due_date`` write at
    ``p_start``, so a backward write can no longer reach the closed history
    where most of the un-probed rows sit. A rewind onto a date inside the OPEN
    cycle that was already materialised before the pause re-created that row
    until TBD-509: ``update_recurring`` now refuses a next due date on or before
    ``last_spent_date``, which ``_advance_frontier`` writes and neither the stop
    nor this re-anchor touches, so the rewind is refused although the rows are
    unlinked. A template that spent before that column existed is bounded by
    the frontier the edit replaces instead.

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

    ⚠ **This function must NEVER touch ``occurrences_elapsed``, and its absence
    here is load-bearing (TBD-275).** The frontier says WHERE the next
    occurrence lands; the counter says HOW MANY the series still delivers. They
    are independent, and a counted series therefore never forfeits instalments
    to a pause: a 12-instalment plan paused for three cycles resumes at the
    current cycle and still delivers all 12, just later. Advancing the counter
    alongside the frontier would silently bill the user for three instalments
    they never received -- and it would look like the obviously-symmetric thing
    to do, which is why it is called out rather than merely omitted.
    """
    # Same derivation the TBD-283 write-path guard uses, from the same helper:
    # the re-anchor's target and the validator's floor are one number, and a
    # second derivation here is how they would drift apart.
    p_start = await frontier_lower_bound(db, org_id, today=today)

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
            logger.warning(
                "recurring.resume.reanchor_cap",
                extra={
                    "org_id": org_id,
                    "recurring_id": r.id,
                    "next_due_date": str(r.next_due_date),
                },
            )
            break
        r.next_due_date = advance_date(r.next_due_date, r.frequency)
        steps += 1


@dataclasses.dataclass(frozen=True)
class PendingRemovalPlan:
    """What :func:`_remove_pending_transactions` will do, decided under its
    locks without writing (TBD-667): ``rows`` is the locked set, ``doomed`` the
    pending rows it deletes, ``settlement`` the demotions and batch counters
    (``None`` when nothing is doomed). An agent preview renders this plan, so
    the preview and the removal cannot disagree."""

    rows: dict[int, Transaction]
    doomed: frozenset[int]
    settlement: BatchSettlementPlan | None


async def plan_pending_removal(
    db: AsyncSession, org_id: int, recurring_id: int,
) -> PendingRemovalPlan:
    """The decision half of :func:`_remove_pending_transactions`: the reads and
    the locks, no write."""
    today = datetime.date.today()
    candidate_ids = set(
        (
            await db.scalars(
                select(Transaction.id).where(
                    Transaction.recurring_id == recurring_id,
                    Transaction.org_id == org_id,
                    Transaction.status == TransactionStatus.PENDING,
                    Transaction.date >= today,
                )
            )
        ).all()
    )
    if not candidate_ids:
        return PendingRemovalPlan(rows={}, doomed=frozenset(), settlement=None)

    referrer_ids = set(
        (
            await db.scalars(
                select(Transaction.id).where(
                    Transaction.org_id == org_id,
                    Transaction.linked_transaction_id.in_(candidate_ids),
                    Transaction.id.notin_(candidate_ids),
                )
            )
        ).all()
    )

    locked = await db.execute(
        select(Transaction)
        .where(
            Transaction.id.in_(sorted(candidate_ids | referrer_ids)),
            Transaction.org_id == org_id,
        )
        .order_by(Transaction.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked_rows = {r.id: r for r in locked.scalars().all()}

    # Re-derived on LOCKED state, never trusted from the pre-read: a row that
    # settled in between must survive, and a referrer that is itself a doomed
    # pending row of this template must be in ``deleted_ids`` so the helper's
    # first ``continue`` takes it rather than demoting a row about to vanish.
    doomed = {
        r.id
        for r in locked_rows.values()
        if r.recurring_id == recurring_id
        and r.status == TransactionStatus.PENDING
        and r.date >= today
    }
    # TBD-600: never split a transfer pair. A pending leg whose partner
    # survives (settled by hand, or past-dated) is KEPT: deleting it would let
    # the FK null the survivor's link and turn a settled transfer leg into a
    # reportable income/expense row. Kept, the pair stays a transfer and the
    # user can still settle or delete it whole.
    doomed -= {
        i for i in doomed
        if is_reciprocal_pair(
            locked_rows[i], locked_rows.get(locked_rows[i].linked_transaction_id)
        )
        and locked_rows[i].linked_transaction_id not in doomed
    }
    if not doomed:
        return PendingRemovalPlan(rows=locked_rows, doomed=frozenset(), settlement=None)
    return PendingRemovalPlan(
        rows=locked_rows, doomed=frozenset(doomed),
        settlement=plan_batch_settlement(org_id, locked_rows=locked_rows, deleted_ids=doomed),
    )


async def _remove_pending_transactions(
    db: AsyncSession, org_id: int, recurring_id: int,
) -> PendingRemovalResult:
    """Bulk-delete pending future transactions for a recurring template.

    Returns BOTH the number of rows removed and the ids demoted to REJECTED
    (TBD-312). The demotion is irreversible through every API we expose --
    REJECTED is terminal and ``reconciliation_state`` is not settable through
    ``TransactionUpdate`` -- so it must not be silent, and it used to be: this
    function computed the ids and threw them away, leaving the caller able to
    report only ``pending_removed``.

    THE THIRD DELETE PATH (TBD-294). ``transaction_service`` has two --
    ``delete_transaction`` and ``bulk_delete_transactions`` -- and this is the
    third; ``stop_recurring`` and ``delete_recurring`` both reach it. It is
    not user-CRUD-shaped (no requested id, no cascade, no balance revert,
    because every row it removes is PENDING) but it destroys rows all the
    same, and ``transactions.linked_transaction_id`` is ``ON DELETE SET NULL``
    for these rows exactly as it is for the other two paths.

    ``reconciliation_service._apply_match`` validates its target on org,
    existence and not-self and NOTHING else -- no status filter, no
    ``recurring_id`` filter -- so a settled bank row matched against the
    PENDING recurring row it settles is a legal, and in fact the most natural,
    reconcile action. Stopping that template used to delete the target, the FK
    nulled the link, and the matched duplicate re-entered
    ``balance_contribution_filter`` and ``reportable_transaction_filter``
    carrying an amount ``accounts.balance`` does not contain.

    So this routes through the two halves of ``_settle_batch_counters_and_demote_orphans``
    (``plan_batch_settlement`` in :func:`plan_pending_removal`, then
    ``apply_batch_settlement``) -- the SHARED entry point, deliberately not a third
    copy of the probe. A fourth delete path
    will appear; it should have one function to call.

    Lock discipline mirrors ``delete_transaction``: an UNLOCKED pre-read to
    discover the candidate set and its inbound referrers, then ONE ascending-id
    ``FOR UPDATE`` over the union. A second, separate lock could invert
    against ``update_transaction`` (which locks {row, link target} sorted) and
    deadlock. The DELETE set is then RE-DERIVED from the locked rows, so the
    predicate is evaluated on locked state and the pre-read cannot widen it.
    """
    plan = await plan_pending_removal(db, org_id, recurring_id)
    if not plan.doomed:
        return PendingRemovalResult(removed=0, demoted_ids=[])

    demoted_ids = await apply_batch_settlement(db, org_id, plan.settlement)

    result = await db.execute(
        delete(Transaction).where(
            Transaction.id.in_(plan.doomed), Transaction.org_id == org_id
        )
    )
    return PendingRemovalResult(removed=result.rowcount, demoted_ids=demoted_ids)


async def stop_recurring(
    db: AsyncSession, org_id: int, recurring_id: int
) -> PendingRemovalResult:
    """Deactivate the template and delete any pending future transactions it generated.

    Returns the count removed AND any ids demoted to REJECTED. Settled
    transactions are preserved.
    """
    result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.id == recurring_id, RecurringTransaction.org_id == org_id
        )
    )
    r = result.scalar_one_or_none()
    if r is None:
        raise NotFoundError("Recurring transaction")

    r.is_active = False
    outcome = await _remove_pending_transactions(db, org_id, recurring_id)

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
    return outcome


async def delete_recurring(
    db: AsyncSession, org_id: int, recurring_id: int
) -> PendingRemovalResult:
    """Permanently delete the template (only if already stopped/paused).

    Also removes any remaining pending future transactions. Returns the count
    removed AND any ids demoted to REJECTED.
    """
    result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.id == recurring_id, RecurringTransaction.org_id == org_id
        )
    )
    r = result.scalar_one_or_none()
    if r is None:
        raise NotFoundError("Recurring transaction")

    outcome = await _remove_pending_transactions(db, org_id, recurring_id)

    await db.delete(r)
    await db.commit()
    return outcome


# ── Generation ────────────────────────────────────────────────────────────────

def _advance_frontier(r: RecurringTransaction, due: datetime.date) -> None:
    """Consume the occurrence at ``due``: move the frontier, spend one instalment.

    A helper rather than two inline statements because the catch-up loop has
    TWO exits that consume an occurrence -- the ``exists`` branch (already
    materialised: skip and advance) and the create branch -- and TBD-275
    requires both to spend budget. Splitting the pair across those branches is
    how one of them comes to advance the frontier without spending, which makes
    the walk in ``forecast_service`` and the walk here disagree about how many
    occurrences the series has left, and ``forecast_net`` then moves across a
    generation run.

    Note this is why the counter is ``occurrences_elapsed`` and not
    ``occurrences_generated``: the ``exists`` branch increments it while
    creating no row at all, so the counter is deliberately NOT equal to
    ``COUNT(*) WHERE recurring_id = r.id``.
    """
    r.next_due_date = advance_date(due, r.frequency)
    r.occurrences_elapsed = (r.occurrences_elapsed or 0) + 1
    r.last_spent_date = due


async def _create_occurrence(
    db: AsyncSession, org_id: int, r: RecurringTransaction,
    due: datetime.date, today: datetime.date, *,
    settle: bool = True, skipped: bool = False,
) -> Transaction:
    """Write the occurrence of ``r`` at ``due``. Generation's create step.

    Shared by ``generate_due_transactions`` and ``materialise_next`` so an
    occurrence written ahead of generation is the row generation would have
    written. It does NOT advance the frontier; both callers do that.

    ⚠ ``settle=False`` (``materialise_next``, both flavours) forces PENDING and
    never touches the balance, even for an auto-settle template whose date has
    passed. A skipped occurrence was never paid; an edit-next row exists so its
    amount can be changed BEFORE it settles, and ``_settle_due_auto`` settles it
    at the edited amount on the next run. ``skipped`` only sets the state.

    A transfer series (TBD-600) writes ONE linked pair per occurrence, through
    ``_link_pair`` as ``create_transfer`` does: an EXPENSE leg on
    ``account_id`` and an INCOME leg on ``to_account_id``. Both legs carry
    ``recurring_id``, so generation's ``exists`` probe, the forecasts'
    materialised probe, ``_settle_due_auto`` and ``_remove_pending_transactions``
    see the pair with no change of their own. Returns the expense leg.
    """
    tx_status = (
        TransactionStatus.SETTLED
        if (settle and r.auto_settle and due <= today)
        else TransactionStatus.PENDING
    )
    if r.type == "transfer":
        legs = [(r.account_id, TransactionType.EXPENSE), (r.to_account_id, TransactionType.INCOME)]
    else:
        legs = [(r.account_id, TransactionType(r.type))]
    async with db.begin_nested():
        rows = []
        for account_id, leg_type in legs:
            tx = Transaction(
                org_id=org_id,
                account_id=account_id,
                category_id=r.category_id,
                description=r.description,
                amount=r.amount,
                type=leg_type,
                status=tx_status,
                date=due,
                settled_date=due if tx_status == TransactionStatus.SETTLED else None,
                recurring_id=r.id,
                recurring_origin_id=r.id if r.type == "transfer" else None,
            )
            if skipped:
                tx.reconciliation_state = "skipped"
            db.add(tx)
            rows.append(tx)
        if len(rows) == 2:
            await db.flush()
            await _link_pair(db, expense_tx=rows[0], income_tx=rows[1], recategorize=False)
        if tx_status == TransactionStatus.SETTLED:
            # Sorted account order, as create_transfer locks its pair.
            for tx in sorted(rows, key=lambda t: t.account_id):
                acct = await get_account_for_update(db, tx.account_id, org_id)
                apply_balance(acct, tx.amount, tx.type)
    return rows[0]


async def _load_transaction(db: AsyncSession, tx_id: int) -> Transaction:
    return (await db.execute(
        select(Transaction).options(*_tx_load_opts()).where(Transaction.id == tx_id)
    )).scalar_one()


async def materialise_next(
    db: AsyncSession, org_id: int, recurring_id: int, *,
    occurrence_date: datetime.date, skipped: bool,
    today: datetime.date | None = None,
) -> Transaction:
    """Write the template's NEXT occurrence now, and spend it (TBD-272/273).

    ``skipped=True`` is "skip next": the row is written PENDING in
    ``reconciliation_state='skipped'``, so it stays visible, leaves every
    aggregate, and the occurrence is consumed. ``skipped=False`` is "edit
    next": an ordinary PENDING row whose amount the normal transaction edit
    then changes. Either way nothing is written ahead of ``next_due_date``, and
    generation's ``exists`` branch plus the forecasts' materialised probe treat
    the row exactly like one generation wrote. The row is ALWAYS PENDING and
    never moves the balance, for both flavours.

    ``occurrence_date`` must equal the frontier, checked under the lock: a
    double-submitted request otherwise consumes two occurrences.

    Concurrency: the template is locked FOR UPDATE, as generation locks it, and
    the existence check runs only AFTER the lock. A generation run that got the
    lock first has either written the row (409 here) or moved the frontier past
    it (this call then acts on the new frontier). sqlite cannot prove this; no
    test claims to.
    """
    if today is None:
        today = datetime.date.today()
    r = (await db.execute(
        select(RecurringTransaction)
        .where(RecurringTransaction.id == recurring_id, RecurringTransaction.org_id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if r is None:
        raise NotFoundError("Recurring transaction")
    if r.next_due_date != occurrence_date:
        raise ConflictError(
            f"The next occurrence is {r.next_due_date.isoformat()}, not "
            f"{occurrence_date.isoformat()}. Refresh and try again."
        )
    if not r.is_active:
        raise ConflictError("This recurring transaction is stopped.")
    if not has_remaining_occurrences(r):
        raise ConflictError("This recurring transaction has no occurrences left.")
    # TBD-285: never write into a closed period from a user action.
    p_start = await frontier_lower_bound(db, org_id, today=today)
    if r.next_due_date < p_start:
        raise ConflictError(
            f"The next occurrence ({r.next_due_date.isoformat()}) is before the "
            f"current billing cycle ({p_start.isoformat()}). Generate transactions first."
        )
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
        raise ConflictError(
            f"The occurrence on {due.isoformat()} already exists; edit or skip that transaction instead."
        )

    tx = await _create_occurrence(db, org_id, r, due, today, settle=False, skipped=skipped)
    _advance_frontier(r, due)
    await db.commit()
    return await _load_transaction(db, tx.id)


async def skip_occurrence(db: AsyncSession, org_id: int, transaction_id: int) -> Transaction:
    """Skip an already-generated occurrence ("skip this month", TBD-272).

    Only a PENDING recurring row in the default ``accepted`` state, not linked
    in either direction and not from an import batch. It becomes ``skipped``:
    no frontier change (the occurrence was already spent when it was written)
    and no balance change (a PENDING row's amount was never in the balance).
    Terminal: there is no restore.

    ⚠ The import-batch refusal is not in the ruling's list. ``_reconcile_one``
    is the only maintainer of ``ImportBatch.accepted_count``; flipping an
    imported row here would strand that counter.

    Race with ``reconciliation_service._apply_match``: this row is locked
    first, then the referrer probe is a LOCKING read (on MySQL it also
    gap-locks the ``linked_transaction_id`` index, so a concurrent match cannot
    insert a link to this row until we commit), and ``_apply_match`` locks its
    target. Either the match lands first and this refuses, or the skip lands
    first and the match refuses the reverted target. sqlite has no row locks;
    nothing here can be proven by a test.
    """
    tx = (await db.execute(
        select(Transaction)
        .where(Transaction.id == transaction_id, Transaction.org_id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if tx is None:
        raise NotFoundError("Transaction")
    if tx.recurring_id is None:
        raise ConflictError("Only a recurring occurrence can be skipped.")
    if tx.status != TransactionStatus.PENDING:
        raise ConflictError("Only a pending occurrence can be skipped.")
    if tx.reconciliation_state != "accepted":
        raise ConflictError(f"This transaction is already {tx.reconciliation_state}.")
    if tx.import_batch_id is not None:
        raise ConflictError("An imported transaction cannot be skipped.")
    if tx.linked_transaction_id is not None:
        raise ConflictError("A linked transaction cannot be skipped; unlink it first.")
    referrer = await db.scalar(
        select(Transaction.id)
        .where(Transaction.org_id == org_id, Transaction.linked_transaction_id == tx.id)
        .limit(1)
        .with_for_update()
    )
    if referrer is not None:
        raise ConflictError(
            f"Transaction {referrer} is linked to this one; unlink it before skipping."
        )
    tx.reconciliation_state = "skipped"
    await db.commit()
    return await _load_transaction(db, tx.id)


async def due_auto_settle_units(
    db: AsyncSession, org_id: int, today: datetime.date, *, lock: bool,
) -> list[list[Transaction]]:
    """The PENDING rows of auto_settle templates that settle today, grouped
    into units: one row, or both legs of a transfer pair (TBD-600).

    TBD-791: a row is due on its expected settlement, ``coalesce(settled_date,
    date)``, so a user who pushed it out is not settled (nor the balance moved)
    early. A pair settles both legs together, once both are due. Generation
    writes pending occurrences with ``settled_date`` NULL, so an unmoved row is
    due on its date, as before. ``lock`` takes the sweep's row locks; the agent
    preview reads the same set unlocked."""
    q = (
        select(Transaction)
        .join(RecurringTransaction, Transaction.recurring_id == RecurringTransaction.id)
        .where(
            Transaction.org_id == org_id,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.recurring_id.is_not(None),
            effective_period_date_expr() <= today,
            RecurringTransaction.auto_settle == True,  # noqa: E712
            # TBD-272: a skipped occurrence is PENDING forever and was never
            # paid. Settling it would put a cancelled charge into the balance.
            non_reverted_transaction_filter(),
        )
        .order_by(Transaction.id)
    )
    q = (
        q.with_for_update(of=Transaction) if lock
        else q.execution_options(populate_existing=True)
    )
    rows = list((await db.execute(q)).scalars().all())
    by_id = {tx.id: tx for tx in rows}
    units = []
    for tx in rows:
        partner = by_id.get(tx.linked_transaction_id)
        if is_reciprocal_pair(tx, partner):
            if tx.type == TransactionType.EXPENSE:
                units.append([tx, partner])
            continue
        if tx.linked_transaction_id is not None:
            # The other leg is not due yet: the pair waits for it.
            other = (await db.execute(
                select(Transaction)
                .where(Transaction.id == tx.linked_transaction_id)
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if (
                is_reciprocal_pair(tx, other)
                and other.status == TransactionStatus.PENDING
                and other.reconciliation_state not in REVERTED_RECONCILIATION_STATES
            ):
                continue
        units.append([tx])
    return units


async def _settle_due_auto(db: AsyncSession, org_id: int, today: datetime.date) -> int:
    """Promote the due PENDING transactions of auto_settle templates
    (``due_auto_settle_units``) to SETTLED on their expected settlement,
    adjusting balance. Non-auto_settle pending items are never touched."""
    units = await due_auto_settle_units(db, org_id, today, lock=True)
    # Lock order: transaction rows first (the SELECT ... FOR UPDATE above),
    # then every account the sweep touches, ONCE, up front, in ascending id
    # order (TBD-600), so two sweeps never take account locks in opposite
    # orders. /generate is user-triggered and the generation loop's FOR UPDATE
    # on templates effectively serializes concurrent runs per org. Both
    # legs of a due transfer pair then settle in ONE savepoint, so a failure
    # can never settle one side only. The count is occurrences, not rows (a
    # pair counts once), as generation's ``created_settled`` is.
    accounts = {
        aid: await get_account_for_update(db, aid, org_id)
        for aid in sorted({tx.account_id for unit in units for tx in unit})
    }
    for unit in units:
        async with db.begin_nested():
            for tx in unit:
                tx.status = TransactionStatus.SETTLED
                tx.settled_date = tx.settled_date or tx.date
                apply_balance(accounts[tx.account_id], tx.amount, tx.type)
    return len(units)


async def generate_due_transactions(
    db: AsyncSession, org_id: int, today: datetime.date | None = None
) -> dict:
    """Materialize recurring instances due within the current billing cycle window.

    Window is derived purely from org.billing_cycle_day + today (no BillingPeriod
    row reads/writes). Future-in-period instances are PENDING; auto_settle only
    settles instances whose date has passed. Overdue prior-period instances are
    caught up. Idempotent: re-running advances next_due_date past the window end.

    Catch-up writes back-dated rows on their REAL nominal dates, even into a
    closed billing period (TBD-285 ruling). Every user-entry path floors the
    frontier at the cycle start AT WRITE TIME only; a frontier still falls
    behind as cycles pass with no successful generation run (automation off,
    scheduler down). Those occurrences are real obligations, and a closed
    period is a reporting boundary, not a write lock. It is reported, not
    silent: ``backfilled`` counts created rows dated before the cycle start
    (``frontier_lower_bound``, the same derivation the write paths use),
    logged as ``recurring.generate.backfill``.

    `today` is the caller's resolved clock. The scheduler passes the value the
    runner resolved once for the whole tick (``RecurringGenerationJob.run``), so
    one tick cannot straddle midnight and materialise rows against a different
    day than the one it decided was due (TBD-284). Passing None falls back to
    ``date.today()``; do NOT rely on that from any path that has already
    resolved a clock.
    Returns {"generated", "settled", "pending", "backfilled", "period_end"}.
    """
    if today is None:
        today = datetime.date.today()

    org = await db.scalar(select(Organization).where(Organization.id == org_id))
    cycle_day = org.billing_cycle_day if org else 1
    _, period_end = current_cycle_window(cycle_day, today)
    p_start = await frontier_lower_bound(db, org_id, today=today)

    settled_now = await _settle_due_auto(db, org_id, today)

    result = await db.execute(
        select(RecurringTransaction)
        .where(
            RecurringTransaction.org_id == org_id,
            active_series_filter(),
            RecurringTransaction.next_due_date <= period_end,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    due_items = list(result.scalars().all())
    created = 0
    created_settled = 0
    backfilled: list[datetime.date] = []

    for r in due_items:
        iterations = 0
        while r.next_due_date <= period_end:
            # Series budget (TBD-275). ``active_series_filter`` above dropped
            # the templates already exhausted at query time; this stops the
            # ones that exhaust DURING the catch-up walk. Neither subsumes the
            # other, and this is the arm ``forecast_service`` must agree with —
            # it spends the same budget over the same grid via
            # ``occurrences_in_window(budget=...)``.
            if not has_remaining_occurrences(r):
                break
            if iterations >= MAX_CATCHUP_ITERATIONS:
                logger.warning(
                    "recurring.generate.catchup_cap",
                    extra={
                        "org_id": org_id,
                        "recurring_id": r.id,
                        "next_due_date": str(r.next_due_date),
                    },
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
                _advance_frontier(r, due)
                continue

            tx = await _create_occurrence(db, org_id, r, due, today)
            tx_status = tx.status

            _advance_frontier(r, due)
            created += 1
            if tx_status == TransactionStatus.SETTLED:
                created_settled += 1
            if due < p_start:
                backfilled.append(due)

    await db.commit()
    if backfilled:
        logger.info(
            "recurring.generate.backfill",
            extra={
                "org_id": org_id,
                "count": len(backfilled),
                "earliest_date": str(min(backfilled)),
            },
        )
    return {
        "generated": created,
        "settled": created_settled + settled_now,
        "pending": created - created_settled,
        "backfilled": len(backfilled),
        "period_end": period_end.isoformat(),
    }
