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
from app.services.date_utils import advance_date
from app.services.exceptions import NotFoundError, ValidationError
from app.services.transaction_service import (
    apply_balance,
    get_account_for_update,
    validate_account,
    validate_category_for_type,
)

logger = structlog.stdlib.get_logger()

# Defensive cap so a pathologically stale template can't spin an unbounded loop.
MAX_CATCHUP_ITERATIONS = 500


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
    db: AsyncSession, org_id: int, recurring_id: int, body: RecurringUpdate
) -> RecurringTransaction:
    result = await db.execute(
        select(RecurringTransaction)
        .options(*_load_opts())
        .where(RecurringTransaction.id == recurring_id, RecurringTransaction.org_id == org_id)
    )
    r = result.scalar_one_or_none()
    if r is None:
        raise NotFoundError("Recurring transaction")

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

    await db.commit()

    result = await db.execute(
        select(RecurringTransaction).options(*_load_opts()).where(RecurringTransaction.id == r.id)
    )
    return result.scalar_one()


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

    `today` is injectable for tests; production passes None (uses date.today()).
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
