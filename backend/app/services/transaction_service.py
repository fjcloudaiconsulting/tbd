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

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.account import Account
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.transaction import TransactionCreate, TransactionResponse, TransactionUpdate, TransferCreate
from app.services.category_rules_service import learn_from_choice
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.transaction_filters import is_reportable_transaction

logger = structlog.get_logger()


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
    # populate_existing=True: every FOR UPDATE in this codebase MUST repopulate
    # the ORM identity-map entry so callers see the locked row state, not
    # stale attributes left over from a prior unlocked read (e.g. a
    # selectinload of Transaction.account before this call).
    result = await db.execute(
        select(Account)
        .where(Account.id == account_id, Account.org_id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
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

async def _create_transaction_no_commit(
    db: AsyncSession,
    org_id: int,
    body: TransactionCreate,
    *,
    is_imported: bool = False,
) -> Transaction:
    """Internal create primitive that flushes but does NOT commit.

    Used by:
      - public create_transaction (wraps with commit + best-effort learning)
      - import_service.execute_import pair_with_existing branch (calls
        directly, then _link_pair, all inside the outer execute_import
        transaction so the pair is atomic).

    Caller owns transaction scope. This function does NOT open db.begin_nested
    or commit; balance application happens unconditionally for SETTLED rows
    and rolls back with the caller's outer transaction if anything raises.
    """
    await validate_account(db, body.account_id, org_id)
    await validate_category(db, body.category_id, org_id)
    tx_type = TransactionType(body.type)
    tx_status = TransactionStatus(body.status)

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
    await db.flush()
    return tx


async def create_transaction(
    db: AsyncSession, org_id: int, body: TransactionCreate, *, is_imported: bool = False
) -> Transaction:
    """Public create endpoint. Owns transaction scope: wraps the no-commit
    primitive in begin_nested, commits, then runs best-effort smart-rules
    learning post-commit.
    """
    async with db.begin_nested():
        tx = await _create_transaction_no_commit(db, org_id, body, is_imported=is_imported)
    await db.commit()

    # Learn from the explicit category pick on MANUAL creates only.
    # Imports own their own learning in execute_import, with accept-vs-override
    # awareness. Adding a learn here would double-write and clobber user_pick
    # semantics.
    #
    # Learning is best-effort: a failure here must NOT surface as a 500
    # to the caller — the user's transaction is already committed above.
    if not is_imported:
        try:
            await learn_from_choice(
                db,
                org_id=org_id,
                description=body.description,
                category_id=body.category_id,
                source="user_edit",
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            await logger.awarning(
                "smart_rules.learn_failed",
                org_id=org_id,
                op="create_transaction",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    result = await db.execute(
        select(Transaction).options(*_load_opts()).where(Transaction.id == tx.id)
    )
    return result.scalar_one()


async def update_transaction(
    db: AsyncSession, org_id: int, transaction_id: int, body: TransactionUpdate
) -> Transaction:
    """F2 policy: per-leg edits on linked rows under invariant guards. Amount
    mirrors atomically. Type and linked_transaction_id immutable on linked rows.
    See spec §5.3 (`~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-03-transfers-between-accounts-design.md`).
    """
    # 1. Pre-read to discover partner (unlocked) and lock both in sorted ID order
    preview = await db.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id, Transaction.org_id == org_id
        )
    )
    if preview is None:
        raise NotFoundError("Transaction")

    ids_to_lock = [transaction_id]
    if preview.linked_transaction_id is not None:
        ids_to_lock.append(preview.linked_transaction_id)
    ids_to_lock.sort()

    locked = await db.execute(
        select(Transaction)
        .options(*_load_opts())
        .where(Transaction.id.in_(ids_to_lock), Transaction.org_id == org_id)
        .order_by(Transaction.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rows = {r.id: r for r in locked.scalars().all()}
    tx = rows.get(transaction_id)
    if tx is None:
        raise NotFoundError("Transaction")
    partner = rows.get(tx.linked_transaction_id) if tx.linked_transaction_id is not None else None

    # 2. Linked-row schema-level guards
    if partner is not None:
        if body.type is not None:
            raise ValidationError("Type is immutable on transfer legs")
        if body.account_id is not None:
            new_acct = await db.scalar(
                select(Account).where(
                    Account.id == body.account_id, Account.org_id == org_id
                )
            )
            if new_acct is None:
                raise ValidationError("Account not found")
            if new_acct.id == partner.account_id:
                raise ValidationError("Account must differ from partner's account")
            if new_acct.currency != partner.account.currency:
                raise ValidationError("New account currency must match partner's currency")

    # Validate references regardless of linked status
    if body.account_id is not None and body.account_id != tx.account_id and partner is None:
        await validate_account(db, body.account_id, org_id)
    if body.category_id is not None:
        await validate_category(db, body.category_id, org_id)

    old_account_id = tx.account_id
    old_amount = tx.amount
    old_type = tx.type
    old_status = tx.status
    old_category_id = tx.category_id

    new_account_id = body.account_id if body.account_id is not None else old_account_id
    new_status = TransactionStatus(body.status) if body.status is not None else old_status

    # 3. Lock affected accounts in sorted ID order
    account_ids_to_lock: set[int] = set()
    if old_status == TransactionStatus.SETTLED or new_status == TransactionStatus.SETTLED:
        account_ids_to_lock.add(old_account_id)
        account_ids_to_lock.add(new_account_id)
    if partner is not None and body.amount is not None:
        if partner.status == TransactionStatus.SETTLED:
            account_ids_to_lock.add(partner.account_id)
    accounts: dict[int, Account] = {}
    for aid in sorted(account_ids_to_lock):
        accounts[aid] = await get_account_for_update(db, aid, org_id)

    amount_was_changed = body.amount is not None
    pre_edit_amount = old_amount  # for telemetry

    async with db.begin_nested():
        # 4a: revert this leg if currently SETTLED
        if old_status == TransactionStatus.SETTLED:
            revert_balance(accounts[old_account_id], old_amount, old_type)
        # 4b: revert partner if linked + amount-change + partner currently SETTLED
        if partner is not None and amount_was_changed and partner.status == TransactionStatus.SETTLED:
            revert_balance(accounts[partner.account_id], partner.amount, partner.type)

        # 4c: apply per-leg field updates
        _apply_field_updates(tx, body)
        if body.category_id is not None:
            tx.category_id = body.category_id
        if body.account_id is not None and body.account_id != old_account_id:
            tx.account_id = body.account_id
        if body.status is not None:
            tx.status = new_status
        # settled_date semantics (§5.2):
        # - status pending → settled_date = None
        # - status settled with body.settled_date → use it
        # - transition to settled with no settled_date → today
        if body.status is not None and new_status == TransactionStatus.PENDING:
            tx.settled_date = None
        elif new_status == TransactionStatus.SETTLED:
            if body.settled_date is not None:
                tx.settled_date = body.settled_date
            elif old_status != TransactionStatus.SETTLED:
                tx.settled_date = datetime.date.today()
        elif body.settled_date is not None:
            # Status unchanged but settled_date provided
            if tx.status == TransactionStatus.SETTLED:
                tx.settled_date = body.settled_date

        # 4d: amount mirror to partner
        if partner is not None and amount_was_changed:
            partner.amount = body.amount

        # 4e: apply this leg with new state if SETTLED
        if tx.status == TransactionStatus.SETTLED:
            apply_balance(accounts[tx.account_id], tx.amount, tx.type)
        # 4f: apply partner with new state if linked + amount change + partner SETTLED
        if partner is not None and amount_was_changed and partner.status == TransactionStatus.SETTLED:
            apply_balance(accounts[partner.account_id], partner.amount, partner.type)

        await db.flush()

        # 5. Post-update invariant re-check (only when linked)
        if partner is not None:
            if tx.org_id != partner.org_id:
                raise ValidationError("Pair org mismatch after edit")
            if tx.account_id == partner.account_id:
                raise ValidationError("Pair on same account after edit")
            if abs(tx.amount) != abs(partner.amount):
                raise ValidationError("Pair amount mismatch after edit")
            if {tx.type, partner.type} != {TransactionType.EXPENSE, TransactionType.INCOME}:
                raise ValidationError("Pair must have opposite types after edit")
            if tx.account.currency != partner.account.currency:
                raise ValidationError("Pair currencies differ after edit")

        if amount_was_changed and partner is not None:
            await logger.ainfo(
                "transfers.edit_mirrored",
                org_id=org_id,
                edited_id=tx.id,
                partner_id=partner.id,
                old_amount=str(pre_edit_amount),
                new_amount=str(tx.amount),
            )

    await db.commit()

    # Category-learning gate: only learn from reportable rows (not transfer legs).
    # Wrapped in is_reportable_transaction(tx) — flips from previous "transfers
    # raise ConflictError above" assumption now that linked rows are editable.
    if (
        is_reportable_transaction(tx)
        and body.category_id is not None
        and body.category_id != old_category_id
    ):
        try:
            await learn_from_choice(
                db,
                org_id=org_id,
                description=tx.description,
                category_id=body.category_id,
                source="user_edit",
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            await logger.awarning(
                "smart_rules.learn_failed",
                org_id=org_id,
                op="update_transaction",
                error=str(exc),
                error_type=type(exc).__name__,
            )

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
    # Unlocked pre-read to discover any transfer pair, then acquire tx-row
    # locks in one FOR UPDATE query ordered by ascending id. This matches
    # the order update_transaction and bulk_delete_transactions use, so
    # concurrent deletes of opposite halves — or any delete racing a bulk
    # delete — can't lock the halves in opposite orders and deadlock.
    preview = await db.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id, Transaction.org_id == org_id
        )
    )
    if preview is None:
        raise NotFoundError("Transaction")

    ids_to_lock = [transaction_id]
    if preview.linked_transaction_id is not None:
        ids_to_lock.append(preview.linked_transaction_id)
    ids_to_lock.sort()

    # populate_existing=True refreshes the preview's ORM instances with the
    # locked DB state so we revert balances from the current row values, not
    # the pre-lock snapshot (status/account_id/amount may have just changed).
    locked = await db.execute(
        select(Transaction)
        .where(Transaction.id.in_(ids_to_lock), Transaction.org_id == org_id)
        .order_by(Transaction.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rows = {r.id: r for r in locked.scalars().all()}
    tx = rows.get(transaction_id)
    if tx is None:
        # Raced with another delete between preview and lock.
        raise NotFoundError("Transaction")
    linked_tx = (
        rows.get(tx.linked_transaction_id)
        if tx.linked_transaction_id is not None
        else None
    )

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

    # Unlocked preview to collect the full set of ids, including any
    # transfer halves linked to the requested rows. Locking everything in
    # a single FOR UPDATE query ordered by ascending id keeps the lock
    # acquisition order strictly ascending across the whole set — same
    # pattern as delete_transaction — so two bulk deletes (or a bulk delete
    # racing a single delete) touching opposite halves of a transfer can't
    # lock them in opposite orders and deadlock.
    preview_result = await db.execute(
        select(Transaction).where(
            Transaction.id.in_(requested), Transaction.org_id == org_id
        )
    )
    preview = list(preview_result.scalars().all())
    all_ids_to_lock = {tx.id for tx in preview} | {
        tx.linked_transaction_id
        for tx in preview
        if tx.linked_transaction_id is not None
    }

    if not all_ids_to_lock:
        return (0, list(requested))

    # populate_existing=True refreshes the preview's ORM instances with the
    # locked DB state — otherwise SQLAlchemy returns the identity-map copy
    # and we'd revert balances from stale status/account_id/amount values.
    result = await db.execute(
        select(Transaction)
        .where(Transaction.id.in_(all_ids_to_lock), Transaction.org_id == org_id)
        .order_by(Transaction.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    found = list(result.scalars().all())
    found_ids = {tx.id for tx in found}
    skipped_ids = [i for i in requested if i not in found_ids]

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


async def _link_pair(
    db: AsyncSession,
    *,
    expense_tx: Transaction,
    income_tx: Transaction,
    recategorize: bool = True,
    transfer_category_id: int | None = None,
) -> tuple[Transaction, Transaction]:
    """Validate transfer-pair invariants and link the rows bidirectionally.

    Caller MUST hold FOR UPDATE locks on both rows (and any accounts whose
    balances are about to mutate) in sorted-ID order. Caller owns transaction
    scope (db.begin_nested / commit) — _link_pair flushes only.

    Re-validates ALL invariants from spec §1.6 after applying mutations. Raises
    ValidationError naming the violated invariant.

    The currency invariant requires that both rows' Account relationships are
    loaded before calling. Caller is responsible for ensuring the relationship
    is populated (typically via a select(Transaction).options(selectinload(...))
    or by passing rows that were just queried with `_load_opts()`).
    """
    if expense_tx.org_id != income_tx.org_id:
        raise ValidationError("Transfer legs must belong to the same org")
    if expense_tx.type != TransactionType.EXPENSE:
        raise ValidationError("Expense leg must have type=EXPENSE")
    if income_tx.type != TransactionType.INCOME:
        raise ValidationError("Income leg must have type=INCOME")
    if expense_tx.account_id == income_tx.account_id:
        raise ValidationError("Transfer legs must be on different accounts")
    if abs(expense_tx.amount) != abs(income_tx.amount):
        raise ValidationError("Transfer legs must have equal absolute amounts")
    # Invariant 7 (strict): neither row may already be linked before pairing.
    # _link_pair is the single creator of linked_transaction_id and refuses to
    # operate on rows that already carry one, even self-referentially. Callers
    # that need to re-link an existing pair must unpair it first via
    # unpair_transactions.
    if expense_tx.linked_transaction_id is not None:
        raise ValidationError("Expense leg is already linked")
    if income_tx.linked_transaction_id is not None:
        raise ValidationError("Income leg is already linked")
    # Currency check. Requires .account relationship to be loaded.
    expense_account = expense_tx.__dict__.get("account")
    income_account = income_tx.__dict__.get("account")
    if expense_account is not None and income_account is not None:
        if expense_account.currency != income_account.currency:
            raise ValidationError("Transfer legs must have the same currency")
    # If account relationship is not loaded, fall back to a query
    else:
        result = await db.execute(
            select(Account.id, Account.currency).where(
                Account.id.in_([expense_tx.account_id, income_tx.account_id]),
                Account.org_id == expense_tx.org_id,
            )
        )
        currencies = {row.id: row.currency for row in result.all()}
        if currencies.get(expense_tx.account_id) != currencies.get(income_tx.account_id):
            raise ValidationError("Transfer legs must have the same currency")

    # Recategorize if requested
    if recategorize:
        cat_id = transfer_category_id
        if cat_id is None:
            cat_id = await db.scalar(
                select(Category.id).where(
                    Category.slug == "transfer", Category.org_id == expense_tx.org_id
                )
            )
            if cat_id is None:
                new_cat = Category(
                    org_id=expense_tx.org_id, name="Transfer", slug="transfer",
                    description="Internal transfers between accounts",
                    type=CategoryType.BOTH, is_system=True,
                )
                db.add(new_cat)
                await db.flush()
                cat_id = new_cat.id
        else:
            await validate_category(db, cat_id, expense_tx.org_id)
        expense_tx.category_id = cat_id
        income_tx.category_id = cat_id

    # Link bidirectionally
    expense_tx.linked_transaction_id = income_tx.id
    income_tx.linked_transaction_id = expense_tx.id
    await db.flush()
    return expense_tx, income_tx


async def find_match_candidates(
    db: AsyncSession,
    org_id: int,
    *,
    source_type: TransactionType,
    amount: Decimal,
    account_id_excluded: int,
    date: datetime.date,
    currency: str,
) -> list[Transaction]:
    """Returns un-linked, settled, non-recurring rows on different accounts in
    the same org with same `currency`, type == opposite(source_type),
    abs(amount) == amount, date within ±3 days.

    Caller passes ``source_type``; helper computes opposite internally. Never
    call this with an already-flipped type.

    Ordered by abs(date_diff) ASC, id ASC. Capped at 25 candidates AFTER
    Python sort (no SQL LIMIT — without ORDER BY in SQL, LIMIT could exclude
    the closest candidate).
    """
    target_type = (
        TransactionType.INCOME if source_type == TransactionType.EXPENSE else TransactionType.EXPENSE
    )
    window_start = date - datetime.timedelta(days=3)
    window_end = date + datetime.timedelta(days=3)

    q = (
        select(Transaction)
        .options(*_load_opts())
        .join(Account, Transaction.account_id == Account.id)
        .where(
            Transaction.org_id == org_id,
            Transaction.account_id != account_id_excluded,
            Transaction.type == target_type,
            Transaction.amount == amount,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.linked_transaction_id.is_(None),
            Transaction.recurring_id.is_(None),
            Transaction.date >= window_start,
            Transaction.date <= window_end,
            Account.currency == currency,
        )
        # NOTE: no SQL .limit() — the ±3-day window + strict filter set keeps
        # the result set naturally tiny. Sort in Python, then slice.
    )
    result = await db.execute(q)
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: (abs((r.date - date).days), r.id))
    return rows[:25]  # defensive cap AFTER sorting


async def find_duplicate_of_linked_leg(
    db: AsyncSession,
    org_id: int,
    *,
    account_id: int,
    amount: Decimal,
    type: TransactionType,
    date: datetime.date,
    currency: str,
) -> list[Transaction]:
    """Returns up to 10 already-linked rows on the SAME account that match the
    CSV row's (type, amount, currency) within ±3 days. Used by import preview
    to flag bank rows that duplicate a synthetic leg created via Op-3.

    Ordered by abs(date_diff) ASC, id ASC. Capped at 10 AFTER Python sort
    (no SQL LIMIT — without ORDER BY in SQL, LIMIT could exclude the
    closest candidate).
    """
    window_start = date - datetime.timedelta(days=3)
    window_end = date + datetime.timedelta(days=3)

    q = (
        select(Transaction)
        .options(*_load_opts())
        .join(Account, Transaction.account_id == Account.id)
        .where(
            Transaction.org_id == org_id,
            Transaction.account_id == account_id,
            Transaction.type == type,
            Transaction.amount == amount,
            Transaction.linked_transaction_id.is_not(None),
            Transaction.date >= window_start,
            Transaction.date <= window_end,
            Account.currency == currency,
        )
        # NOTE: no SQL .limit() — see find_match_candidates for rationale.
    )
    result = await db.execute(q)
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: (abs((r.date - date).days), r.id))
    return rows[:10]  # defensive cap AFTER sorting


async def pair_existing_transactions(
    db: AsyncSession,
    org_id: int,
    expense_tx_id: int,
    income_tx_id: int,
    *,
    recategorize: bool = True,
    transfer_category_id: int | None = None,
) -> tuple[Transaction, Transaction]:
    """Link two existing un-linked rows as a transfer pair.

    Owns transaction scope. Locks both rows in sorted-ID order via SELECT FOR
    UPDATE, validates via _link_pair, links bidirectionally, optionally
    recategorizes both legs to the system Transfer category. No balance changes
    (both rows already exist with correct per-leg balance contributions).

    Raises ValidationError on identical IDs or invariant violations,
    NotFoundError if either row is missing in this org.
    """
    if expense_tx_id == income_tx_id:
        raise ValidationError("Expense and income IDs must differ")

    ids_sorted = sorted([expense_tx_id, income_tx_id])
    locked = await db.execute(
        select(Transaction)
        .options(*_load_opts())
        .where(Transaction.id.in_(ids_sorted), Transaction.org_id == org_id)
        .order_by(Transaction.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rows = list(locked.scalars().all())
    if len(rows) != 2:
        raise NotFoundError("Transaction")
    rows_by_id = {r.id: r for r in rows}
    expense_tx = rows_by_id[expense_tx_id]
    income_tx = rows_by_id[income_tx_id]

    async with db.begin_nested():
        await _link_pair(
            db,
            expense_tx=expense_tx,
            income_tx=income_tx,
            recategorize=recategorize,
            transfer_category_id=transfer_category_id,
        )
    await db.commit()

    await logger.ainfo(
        "transfers.linked",
        org_id=org_id,
        expense_id=expense_tx.id,
        income_id=income_tx.id,
        source="bulk_link",
        recategorized=recategorize,
    )
    return expense_tx, income_tx


async def convert_and_create_leg(
    db: AsyncSession,
    org_id: int,
    source_tx_id: int,
    *,
    destination_account_id: int,
    recategorize: bool = True,
    transfer_category_id: int | None = None,
) -> tuple[Transaction, Transaction]:
    """Convert an un-linked source row into a transfer leg by creating the
    matching partner leg on the destination account, then linking the pair.

    Owns transaction scope. See pair_existing_transactions for the locking
    discipline. Source may be SETTLED or PENDING; partner mirrors source
    status. Same-currency only.

    Lock order: source transaction FIRST, then accounts (sorted-ID). This
    matches update_transaction's order (tx then accounts) so concurrent
    operations on overlapping (transaction, account) pairs cannot deadlock.
    """
    # 1. Pre-read source transaction (no lock) only to collect IDs needed for
    # subsequent lock acquisition + early existence check.
    source_pre = await db.scalar(
        select(Transaction).where(
            Transaction.id == source_tx_id, Transaction.org_id == org_id
        )
    )
    if source_pre is None:
        raise NotFoundError("Transaction")
    pre_account_id = source_pre.account_id
    pre_linked = source_pre.linked_transaction_id
    pre_recurring = source_pre.recurring_id

    # 2. Pre-validate destination account + early invariants on the unlocked
    # read. These will be re-checked under the lock to defeat races.
    if pre_linked is not None:
        raise ValidationError("Source row is already a transfer leg")
    if pre_recurring is not None:
        raise ValidationError("Recurring rows cannot be converted to transfer legs")
    if pre_account_id == destination_account_id:
        raise ValidationError("Source and destination accounts must differ")

    src_account_pre = await db.scalar(
        select(Account).where(Account.id == pre_account_id, Account.org_id == org_id)
    )
    dst_account_pre = await db.scalar(
        select(Account).where(Account.id == destination_account_id, Account.org_id == org_id)
    )
    if dst_account_pre is None:
        raise NotFoundError("Account")
    if src_account_pre.currency != dst_account_pre.currency:
        raise ValidationError("Source and destination accounts must have the same currency")

    # 3. Lock the source transaction FIRST (before accounts) to match the
    # lock-acquisition order used by update_transaction. Refresh with eager
    # relationships so downstream code sees the locked row state.
    locked = await db.execute(
        select(Transaction)
        .options(*_load_opts())
        .where(Transaction.id == source_tx_id, Transaction.org_id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    source = locked.scalar_one_or_none()
    if source is None:
        # Row vanished between pre-read and FOR UPDATE.
        raise ConflictError("Source row state changed; refresh and retry")

    # 4. Re-validate locked source state. If anything changed since the
    # unlocked read, abort with ConflictError so the caller can refresh.
    if source.linked_transaction_id is not None:
        raise ConflictError("Source row state changed; refresh and retry")
    if source.recurring_id is not None:
        raise ConflictError("Source row state changed; refresh and retry")
    if source.account_id == destination_account_id:
        raise ConflictError("Source row state changed; refresh and retry")

    # 5. Lock both accounts in sorted-ID order using the locked source's
    # account_id (paranoia: in case it differs from the pre-read).
    first_id, second_id = sorted([source.account_id, destination_account_id])
    first_acct = await get_account_for_update(db, first_id, org_id)
    second_acct = await get_account_for_update(db, second_id, org_id)
    src_locked = first_acct if source.account_id == first_id else second_acct
    dst_locked = first_acct if destination_account_id == first_id else second_acct

    # 6. Re-validate currency on the locked accounts.
    if src_locked.currency != dst_locked.currency:
        raise ConflictError("Account currencies changed; refresh and retry")

    # Determine partner type by mirroring source.
    partner_type = (
        TransactionType.INCOME if source.type == TransactionType.EXPENSE
        else TransactionType.EXPENSE
    )

    async with db.begin_nested():
        partner = Transaction(
            org_id=org_id,
            account_id=destination_account_id,
            category_id=source.category_id,
            description=source.description,
            amount=source.amount,
            type=partner_type,
            status=source.status,
            date=source.date,
            settled_date=source.settled_date,
            is_imported=False,
        )
        db.add(partner)
        await db.flush()

        # Apply balance to destination only when SETTLED.
        if partner.status == TransactionStatus.SETTLED:
            apply_balance(dst_locked, partner.amount, partner_type)

        # Determine which leg is expense vs income for _link_pair.
        if source.type == TransactionType.EXPENSE:
            expense_tx, income_tx = source, partner
        else:
            expense_tx, income_tx = partner, source

        # Re-fetch partner with .account loaded so _link_pair's currency check
        # uses the eager path. Source already has it from _load_opts above.
        await db.refresh(partner, attribute_names=["account"])

        await _link_pair(
            db,
            expense_tx=expense_tx,
            income_tx=income_tx,
            recategorize=recategorize,
            transfer_category_id=transfer_category_id,
        )
    await db.commit()

    await logger.ainfo(
        "transfers.linked",
        org_id=org_id,
        expense_id=expense_tx.id,
        income_id=income_tx.id,
        source="convert_create",
        recategorized=recategorize,
    )
    return expense_tx, income_tx


async def unpair_transactions(
    db: AsyncSession,
    org_id: int,
    transaction_id: int,
    *,
    expense_fallback_category_id: int,
    income_fallback_category_id: int,
) -> tuple[Transaction, Transaction]:
    """Break a transfer pair without deleting either row. The only sanctioned
    code path that clears linked_transaction_id.

    Owns transaction scope. Locks both rows in sorted-ID order via SELECT FOR
    UPDATE. NULLs both link columns atomically. Sets each leg's category_id to
    the type-matched fallback. No balance changes.
    """
    preview = await db.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id, Transaction.org_id == org_id
        )
    )
    if preview is None:
        raise NotFoundError("Transaction")
    if preview.linked_transaction_id is None:
        raise ValidationError("Transaction is not part of a transfer pair")

    # Validate fallback categories upfront (raises NotFoundError on miss).
    await validate_category(db, expense_fallback_category_id, org_id)
    await validate_category(db, income_fallback_category_id, org_id)

    # Enforce type compatibility for each fallback. validate_category only
    # checks org/existence; the docstring promises type-matched fallbacks so
    # the API must reject INCOME-only categories for the expense leg, and
    # EXPENSE-only categories for the income leg.
    exp_cat = await db.scalar(
        select(Category).where(
            Category.id == expense_fallback_category_id, Category.org_id == org_id
        )
    )
    inc_cat = await db.scalar(
        select(Category).where(
            Category.id == income_fallback_category_id, Category.org_id == org_id
        )
    )
    if exp_cat.type not in (CategoryType.EXPENSE, CategoryType.BOTH):
        raise ValidationError("expense_fallback_category_id must be EXPENSE or BOTH")
    if inc_cat.type not in (CategoryType.INCOME, CategoryType.BOTH):
        raise ValidationError("income_fallback_category_id must be INCOME or BOTH")

    ids_sorted = sorted([transaction_id, preview.linked_transaction_id])
    locked = await db.execute(
        select(Transaction)
        .options(*_load_opts())
        .where(Transaction.id.in_(ids_sorted), Transaction.org_id == org_id)
        .order_by(Transaction.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rows = list(locked.scalars().all())
    if len(rows) != 2:
        raise ConflictError("Pair partner not found; refresh and retry")

    rows_by_type = {r.type: r for r in rows}
    expense_tx = rows_by_type.get(TransactionType.EXPENSE)
    income_tx = rows_by_type.get(TransactionType.INCOME)
    if expense_tx is None or income_tx is None:
        raise ConflictError("Pair has invalid type composition")

    async with db.begin_nested():
        expense_tx.linked_transaction_id = None
        income_tx.linked_transaction_id = None
        expense_tx.category_id = expense_fallback_category_id
        income_tx.category_id = income_fallback_category_id
        await db.flush()
    await db.commit()

    await logger.ainfo(
        "transfers.unpaired",
        org_id=org_id,
        expense_id=expense_tx.id,
        income_id=income_tx.id,
        expense_fallback_category_id=expense_fallback_category_id,
        income_fallback_category_id=income_fallback_category_id,
    )
    return expense_tx, income_tx


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

        await _link_pair(
            db,
            expense_tx=expense_tx,
            income_tx=income_tx,
            recategorize=False,
            transfer_category_id=category_id,
        )

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
