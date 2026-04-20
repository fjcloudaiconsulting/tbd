"""Transaction business logic — balance mutations, validation, and guards.

All balance-affecting operations go through this module so they can be reused
from HTTP routers, recurring transaction jobs, or any future entry point.

Key rule: only SETTLED transactions affect account balance.
Pending transactions are recorded but do not change the balance.

Raises domain exceptions (NotFoundError, ValidationError, ConflictError)
instead of HTTPException — callers map these to the appropriate response.
"""

import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.transaction import TransactionCreate, TransactionResponse, TransactionUpdate, TransferCreate
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


# ── Response helpers ──────────────────────────────────────────────────────────

def _load_opts():
    return [selectinload(Transaction.account), selectinload(Transaction.category)]


def to_response(tx: Transaction) -> TransactionResponse:
    return TransactionResponse(
        id=tx.id,
        account_id=tx.account_id,
        account_name=tx.account.name if tx.account else "",
        category_id=tx.category_id,
        category_name=tx.category.name if tx.category else "",
        description=tx.description,
        amount=tx.amount,
        type=tx.type.value,
        status=tx.status.value,
        linked_transaction_id=tx.linked_transaction_id,
        recurring_id=tx.recurring_id,
        date=tx.date,
        settled_date=tx.settled_date,
        is_imported=tx.is_imported,
    )


# ── Validation ────────────────────────────────────────────────────────────────

async def validate_account(db: AsyncSession, account_id: int, org_id: int) -> None:
    """Check that account exists and belongs to the org (no row lock)."""
    acct = await db.scalar(
        select(Account.id).where(Account.id == account_id, Account.org_id == org_id)
    )
    if acct is None:
        raise ValidationError("Invalid account")


async def validate_category(db: AsyncSession, category_id: int, org_id: int) -> None:
    cat = await db.scalar(
        select(Category.id).where(Category.id == category_id, Category.org_id == org_id)
    )
    if cat is None:
        raise ValidationError("Invalid category")


async def get_account_for_update(db: AsyncSession, account_id: int, org_id: int) -> Account:
    result = await db.execute(
        select(Account)
        .where(Account.id == account_id, Account.org_id == org_id)
        .with_for_update()
    )
    acct = result.scalar_one_or_none()
    if acct is None:
        raise ValidationError("Invalid account")
    return acct


async def assert_no_dependents(
    db: AsyncSession,
    model: type,
    filters: list,
    noun: str,
    resource: str,
) -> None:
    """Raise ConflictError if any rows match the given filters."""
    count = await db.scalar(
        select(func.count()).select_from(model).where(*filters)
    )
    if count and count > 0:
        raise ConflictError(f"Cannot delete: {count} {noun}(s) use this {resource}")


# ── Balance logic ─────────────────────────────────────────────────────────────

def apply_balance(account: Account, amount: Decimal, tx_type: TransactionType) -> None:
    if tx_type == TransactionType.TRANSFER:
        raise ValidationError("Cannot apply balance for TRANSFER type directly")
    if tx_type == TransactionType.INCOME:
        account.balance += amount
    else:
        account.balance -= amount


def revert_balance(account: Account, amount: Decimal, tx_type: TransactionType) -> None:
    if tx_type == TransactionType.TRANSFER:
        raise ValidationError("Cannot revert balance for TRANSFER type directly")
    if tx_type == TransactionType.INCOME:
        account.balance -= amount
    else:
        account.balance += amount


# ── CRUD operations ───────────────────────────────────────────────────────────

async def create_transaction(
    db: AsyncSession, org_id: int, body: TransactionCreate, *, is_imported: bool = False
) -> Transaction:
    await validate_account(db, body.account_id, org_id)
    await validate_category(db, body.category_id, org_id)
    tx_type = TransactionType(body.type)
    tx_status = TransactionStatus(body.status)

    async with db.begin_nested():
        if tx_status == TransactionStatus.SETTLED:
            acct = await get_account_for_update(db, body.account_id, org_id)
            apply_balance(acct, body.amount, tx_type)

        tx = Transaction(
            org_id=org_id,
            account_id=body.account_id,
            category_id=body.category_id,
            description=body.description,
            amount=body.amount,
            type=tx_type,
            status=tx_status,
            date=body.date,
            settled_date=body.date if tx_status == TransactionStatus.SETTLED else None,
            is_imported=is_imported,
        )
        db.add(tx)

    await db.commit()

    result = await db.execute(
        select(Transaction).options(*_load_opts()).where(Transaction.id == tx.id)
    )
    return result.scalar_one()


async def update_transaction(
    db: AsyncSession, org_id: int, transaction_id: int, body: TransactionUpdate
) -> Transaction:
    result = await db.execute(
        select(Transaction)
        .options(*_load_opts())
        .where(Transaction.id == transaction_id, Transaction.org_id == org_id)
    )
    tx = result.scalar_one_or_none()
    if tx is None:
        raise NotFoundError("Transaction")

    if tx.linked_transaction_id is not None:
        raise ConflictError("Cannot edit a transfer transaction. Delete and recreate it instead.")

    # Validate references regardless of status
    if body.account_id is not None and body.account_id != tx.account_id:
        await validate_account(db, body.account_id, org_id)
    if body.category_id is not None:
        await validate_category(db, body.category_id, org_id)

    old_account_id = tx.account_id
    old_amount = tx.amount
    old_type = tx.type
    old_status = tx.status

    new_account_id = body.account_id if body.account_id is not None else old_account_id
    new_status = TransactionStatus(body.status) if body.status is not None else old_status

    async with db.begin_nested():
        # Revert old balance if it was settled
        if old_status == TransactionStatus.SETTLED:
            if new_account_id == old_account_id:
                account = await get_account_for_update(db, old_account_id, org_id)
                revert_balance(account, old_amount, old_type)
            else:
                first_id, second_id = sorted([old_account_id, new_account_id])
                first = await get_account_for_update(db, first_id, org_id)
                second = await get_account_for_update(db, second_id, org_id)
                old_account = first if old_account_id == first_id else second
                revert_balance(old_account, old_amount, old_type)

        # Apply field updates
        _apply_field_updates(tx, body)
        if body.category_id is not None:
            tx.category_id = body.category_id
        if body.account_id is not None and body.account_id != old_account_id:
            tx.account_id = body.account_id
        if body.status is not None:
            tx.status = new_status
            if new_status == TransactionStatus.SETTLED and old_status != TransactionStatus.SETTLED:
                tx.settled_date = datetime.date.today()
            elif new_status == TransactionStatus.PENDING and old_status == TransactionStatus.SETTLED:
                tx.settled_date = None

        # Apply new balance if now settled
        if new_status == TransactionStatus.SETTLED:
            new_account = await get_account_for_update(db, tx.account_id, org_id)
            apply_balance(new_account, tx.amount, tx.type)

    await db.commit()

    result = await db.execute(
        select(Transaction).options(*_load_opts()).where(Transaction.id == tx.id)
    )
    return result.scalar_one()


def _apply_field_updates(tx: Transaction, body: TransactionUpdate) -> None:
    if body.description is not None:
        tx.description = body.description
    if body.amount is not None:
        tx.amount = body.amount
    if body.type is not None:
        tx.type = TransactionType(body.type)
    if body.date is not None:
        tx.date = body.date


async def delete_transaction(db: AsyncSession, org_id: int, transaction_id: int) -> None:
    result = await db.execute(
        select(Transaction).where(
            Transaction.id == transaction_id, Transaction.org_id == org_id
        )
    )
    tx = result.scalar_one_or_none()
    if tx is None:
        raise NotFoundError("Transaction")

    # Collect linked transaction (transfer pair) if any
    linked_tx = None
    if tx.linked_transaction_id:
        linked_result = await db.execute(
            select(Transaction).where(
                Transaction.id == tx.linked_transaction_id, Transaction.org_id == org_id
            )
        )
        linked_tx = linked_result.scalar_one_or_none()

    async with db.begin_nested():
        # For transfers, lock both accounts in deterministic order
        if linked_tx and tx.status == TransactionStatus.SETTLED:
            first_id, second_id = sorted([tx.account_id, linked_tx.account_id])
            first = await get_account_for_update(db, first_id, org_id)
            second = await get_account_for_update(db, second_id, org_id)
            tx_acct = first if tx.account_id == first_id else second
            linked_acct = first if linked_tx.account_id == first_id else second
            revert_balance(tx_acct, tx.amount, tx.type)
            revert_balance(linked_acct, linked_tx.amount, linked_tx.type)
        elif tx.status == TransactionStatus.SETTLED:
            acct = await get_account_for_update(db, tx.account_id, org_id)
            revert_balance(acct, tx.amount, tx.type)

        if linked_tx:
            await db.delete(linked_tx)
        await db.delete(tx)

    await db.commit()


async def bulk_delete_transactions(
    db: AsyncSession, org_id: int, ids: list[int]
) -> tuple[int, list[int]]:
    """Delete multiple transactions in one atomic commit.

    Returns (deleted_count, skipped_ids). Cross-org IDs are silently
    skipped. Transfer-pair halves cascade: deleting one half also deletes
    the linked half. Balance reverts applied per transaction for settled rows
    under SELECT FOR UPDATE locks acquired in sorted-ID order to prevent
    lost updates and deadlocks.
    """
    if not ids:
        return (0, [])

    # Dedupe input — caller may select both halves of a transfer
    requested = list(dict.fromkeys(ids))

    # Fetch all requested transactions scoped to this org. Lock the rows
    # FOR UPDATE (ordered by id) so a concurrent delete of the same rows
    # waits on us instead of producing stale-rowcount errors at flush time.
    result = await db.execute(
        select(Transaction)
        .where(
            Transaction.id.in_(requested),
            Transaction.org_id == org_id,
        )
        .order_by(Transaction.id)
        .with_for_update()
    )
    found = list(result.scalars().all())
    found_ids = {tx.id for tx in found}
    skipped_ids = [i for i in requested if i not in found_ids]

    # Expand transfer pairs — collect linked IDs not already in the list
    linked_ids_to_fetch = {
        tx.linked_transaction_id
        for tx in found
        if tx.linked_transaction_id is not None
        and tx.linked_transaction_id not in found_ids
    }
    if linked_ids_to_fetch:
        linked_result = await db.execute(
            select(Transaction)
            .where(
                Transaction.id.in_(linked_ids_to_fetch),
                Transaction.org_id == org_id,
            )
            .order_by(Transaction.id)
            .with_for_update()
        )
        found.extend(linked_result.scalars().all())

    # Collect distinct account IDs that will need a balance revert
    account_ids_to_lock = sorted({
        tx.account_id
        for tx in found
        if tx.status == TransactionStatus.SETTLED
    })

    async with db.begin_nested():
        # Lock each affected account in sorted order to prevent deadlocks
        accounts: dict[int, Account] = {}
        for aid in account_ids_to_lock:
            accounts[aid] = await get_account_for_update(db, aid, org_id)

        # Break linked-transfer FK cycles before deletion so SQLAlchemy can flush
        # the deletes without hitting a circular dependency error
        for tx in found:
            if tx.linked_transaction_id is not None:
                tx.linked_transaction_id = None
        await db.flush()

        # Revert balances for settled rows, then delete every row
        for tx in found:
            if tx.status == TransactionStatus.SETTLED:
                acct = accounts.get(tx.account_id)
                if acct is not None:
                    revert_balance(acct, tx.amount, tx.type)
            await db.delete(tx)

    await db.commit()
    return (len(found), skipped_ids)


async def create_transfer(
    db: AsyncSession, org_id: int, body: TransferCreate, *, is_imported: bool = False
) -> tuple[Transaction, Transaction]:
    """Create a linked pair of transactions for a transfer between accounts."""
    if body.from_account_id == body.to_account_id:
        raise ValidationError("Source and destination accounts must be different")

    await validate_account(db, body.from_account_id, org_id)
    await validate_account(db, body.to_account_id, org_id)

    # Auto-generate description if not provided
    description = body.description.strip()
    if not description:
        from_name = await db.scalar(
            select(Account.name).where(Account.id == body.from_account_id, Account.org_id == org_id)
        )
        to_name = await db.scalar(
            select(Account.name).where(Account.id == body.to_account_id, Account.org_id == org_id)
        )
        description = f"Transfer from {from_name} to {to_name}"

    # Auto-assign Transfer category if not provided
    category_id = body.category_id
    if category_id is None:
        transfer_cat = await db.scalar(
            select(Category.id).where(Category.slug == "transfer", Category.org_id == org_id)
        )
        if transfer_cat is None:
            # Auto-create the Transfer category if missing (legacy data)
            from app.models.category import CategoryType
            new_cat = Category(
                org_id=org_id, name="Transfer", slug="transfer",
                description="Internal transfers between accounts",
                type=CategoryType.BOTH, is_system=True,
            )
            db.add(new_cat)
            await db.flush()
            transfer_cat = new_cat.id
        category_id = transfer_cat
    else:
        await validate_category(db, category_id, org_id)

    tx_status = TransactionStatus(body.status)

    async with db.begin_nested():
        # Expense side (source account) — uses EXPENSE type so existing
        # balance logic (apply/revert) works unchanged
        settled = body.date if tx_status == TransactionStatus.SETTLED else None
        expense_tx = Transaction(
            org_id=org_id,
            account_id=body.from_account_id,
            category_id=category_id,
            description=description,
            amount=body.amount,
            type=TransactionType.EXPENSE,
            status=tx_status,
            date=body.date,
            settled_date=settled,
            is_imported=is_imported,
        )
        # Income side (destination account)
        income_tx = Transaction(
            org_id=org_id,
            account_id=body.to_account_id,
            category_id=category_id,
            description=description,
            amount=body.amount,
            type=TransactionType.INCOME,
            status=tx_status,
            date=body.date,
            settled_date=settled,
            is_imported=is_imported,
        )
        db.add(expense_tx)
        db.add(income_tx)
        await db.flush()

        expense_tx.linked_transaction_id = income_tx.id
        income_tx.linked_transaction_id = expense_tx.id

        if tx_status == TransactionStatus.SETTLED:
            first_id, second_id = sorted([body.from_account_id, body.to_account_id])
            first = await get_account_for_update(db, first_id, org_id)
            second = await get_account_for_update(db, second_id, org_id)
            from_acct = first if body.from_account_id == first_id else second
            to_acct = first if body.to_account_id == first_id else second
            from_acct.balance -= body.amount
            to_acct.balance += body.amount

    await db.commit()

    result = await db.execute(
        select(Transaction).options(*_load_opts()).where(
            Transaction.id.in_([expense_tx.id, income_tx.id])
        ).order_by(Transaction.id)
    )
    tx_by_id = {tx.id: tx for tx in result.scalars().all()}
    return tx_by_id[expense_tx.id], tx_by_id[income_tx.id]


async def get_transaction(db: AsyncSession, org_id: int, transaction_id: int) -> Transaction:
    result = await db.execute(
        select(Transaction)
        .options(*_load_opts())
        .where(Transaction.id == transaction_id, Transaction.org_id == org_id)
    )
    tx = result.scalar_one_or_none()
    if tx is None:
        raise NotFoundError("Transaction")
    return tx


async def list_transactions(
    db: AsyncSession,
    org_id: int,
    account_id: int | None = None,
    category_id: int | None = None,
    tx_type: str | None = None,
    status: str | None = None,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Transaction]:
    q = (
        select(Transaction)
        .options(*_load_opts())
        .where(Transaction.org_id == org_id)
    )
    if account_id is not None:
        q = q.where(Transaction.account_id == account_id)
    if category_id is not None:
        q = q.where(Transaction.category_id == category_id)
    if tx_type is not None:
        q = q.where(Transaction.type == TransactionType(tx_type))
    if status is not None:
        q = q.where(Transaction.status == TransactionStatus(status))
    if date_from is not None:
        q = q.where(Transaction.date >= date_from)
    if date_to is not None:
        q = q.where(Transaction.date <= date_to)
    if search is not None:
        q = q.where(Transaction.description.ilike(f"%{search}%"))
    q = q.order_by(Transaction.date.desc(), Transaction.id.desc())
    q = q.limit(limit).offset(offset)

    result = await db.execute(q)
    return list(result.scalars().all())


async def reconcile_account(
    db: AsyncSession, org_id: int, account: Account
) -> tuple[Decimal, Decimal, bool]:
    """Returns (stored_balance, computed_balance, is_consistent).
    Only settled transactions are included in the computation."""
    income = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.account_id == account.id,
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.INCOME,
            Transaction.status == TransactionStatus.SETTLED,
        )
    )
    expense = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.account_id == account.id,
            Transaction.org_id == org_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.SETTLED,
        )
    )
    computed = income - expense
    return account.balance, computed, account.balance == computed
