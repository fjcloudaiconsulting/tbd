"""Recurring transaction service — template management and auto-generation.

Generates pending transactions from recurring templates when their
next_due_date has passed. Advances next_due_date based on frequency.
"""

import datetime
from dateutil.relativedelta import relativedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.account import Account
from app.models.category import Category
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.recurring import RecurringCreate, RecurringResponse, RecurringUpdate
from app.services.exceptions import NotFoundError, ValidationError
from app.services.transaction_service import apply_balance, get_account_for_update


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


def _advance_date(current: datetime.date, freq: Frequency) -> datetime.date:
    if freq == Frequency.WEEKLY:
        return current + datetime.timedelta(weeks=1)
    elif freq == Frequency.BIWEEKLY:
        return current + datetime.timedelta(weeks=2)
    elif freq == Frequency.MONTHLY:
        return current + relativedelta(months=1)
    elif freq == Frequency.QUARTERLY:
        return current + relativedelta(months=3)
    elif freq == Frequency.YEARLY:
        return current + relativedelta(years=1)
    return current + relativedelta(months=1)


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
    # Validate refs
    acct = await db.scalar(select(Account.id).where(Account.id == body.account_id, Account.org_id == org_id))
    if acct is None:
        raise ValidationError("Invalid account")
    cat = await db.scalar(select(Category.id).where(Category.id == body.category_id, Category.org_id == org_id))
    if cat is None:
        raise ValidationError("Invalid category")

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
        acct = await db.scalar(select(Account.id).where(Account.id == body.account_id, Account.org_id == org_id))
        if acct is None:
            raise ValidationError("Invalid account")
        r.account_id = body.account_id
    if body.category_id is not None:
        cat = await db.scalar(select(Category.id).where(Category.id == body.category_id, Category.org_id == org_id))
        if cat is None:
            raise ValidationError("Invalid category")
        r.category_id = body.category_id
    if body.description is not None:
        r.description = body.description
    if body.amount is not None:
        r.amount = body.amount
    if body.type is not None:
        r.type = body.type
    if body.frequency is not None:
        r.frequency = Frequency(body.frequency)
    if body.next_due_date is not None:
        r.next_due_date = body.next_due_date
    if body.auto_settle is not None:
        r.auto_settle = body.auto_settle
    if body.is_active is not None:
        r.is_active = body.is_active

    await db.commit()

    result = await db.execute(
        select(RecurringTransaction).options(*_load_opts()).where(RecurringTransaction.id == r.id)
    )
    return result.scalar_one()


async def stop_recurring(db: AsyncSession, org_id: int, recurring_id: int) -> int:
    """Deactivate the template and delete any pending future transactions it generated.
    Returns the number of pending transactions removed. Settled transactions are preserved."""
    import datetime as dt

    result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.id == recurring_id, RecurringTransaction.org_id == org_id
        )
    )
    r = result.scalar_one_or_none()
    if r is None:
        raise NotFoundError("Recurring transaction")

    r.is_active = False

    # Bulk delete pending future transactions generated by this template
    today = dt.date.today()
    from sqlalchemy import delete
    result = await db.execute(
        delete(Transaction).where(
            Transaction.recurring_id == recurring_id,
            Transaction.org_id == org_id,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.date >= today,
        )
    )
    removed = result.rowcount

    await db.commit()
    return removed


async def delete_recurring(db: AsyncSession, org_id: int, recurring_id: int) -> int:
    """Permanently delete the template (only if already stopped/paused).
    Also removes any remaining pending future transactions.
    Returns count of pending transactions removed."""
    import datetime as dt

    result = await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.id == recurring_id, RecurringTransaction.org_id == org_id
        )
    )
    r = result.scalar_one_or_none()
    if r is None:
        raise NotFoundError("Recurring transaction")

    # Bulk delete remaining pending future transactions
    today = dt.date.today()
    from sqlalchemy import delete
    result = await db.execute(
        delete(Transaction).where(
            Transaction.recurring_id == recurring_id,
            Transaction.org_id == org_id,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.date >= today,
        )
    )
    removed = result.rowcount

    await db.delete(r)
    await db.commit()
    return removed


# ── Generation ────────────────────────────────────────────────────────────────

async def generate_due_transactions(db: AsyncSession, org_id: int) -> int:
    """Generate pending transactions for all due recurring templates in an org.
    Returns the number of transactions generated."""
    today = datetime.date.today()

    # Lock rows to prevent duplicate generation from concurrent requests
    result = await db.execute(
        select(RecurringTransaction)
        .where(
            RecurringTransaction.org_id == org_id,
            RecurringTransaction.is_active == True,
            RecurringTransaction.next_due_date <= today,
        )
        .with_for_update()
    )
    due_items = list(result.scalars().all())
    generated = 0

    for r in due_items:
        while r.next_due_date <= today:
            tx_status = TransactionStatus.SETTLED if r.auto_settle else TransactionStatus.PENDING

            async with db.begin_nested():
                tx = Transaction(
                    org_id=org_id,
                    account_id=r.account_id,
                    category_id=r.category_id,
                    description=r.description,
                    amount=r.amount,
                    type=TransactionType(r.type),
                    status=tx_status,
                    date=r.next_due_date,
                    settled_date=r.next_due_date if tx_status == TransactionStatus.SETTLED else None,
                    recurring_id=r.id,
                )
                db.add(tx)

                if tx_status == TransactionStatus.SETTLED:
                    acct = await get_account_for_update(db, r.account_id, org_id)
                    apply_balance(acct, r.amount, TransactionType(r.type))

            r.next_due_date = _advance_date(r.next_due_date, r.frequency)
            generated += 1

    await db.commit()
    return generated
