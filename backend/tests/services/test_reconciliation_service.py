"""Tests for the reconciliation service (L3.2 Wave 2B).

Covers:

* The state-machine guard rejects every disallowed transition with
  ``ConflictError`` (-> 409).
* Cross-batch membership: a transition on a transaction that doesn't
  belong to the batch returns ``ValidationError`` (-> 422).
* Atomicity: a failing transition rolls back the whole request.
* Auto-close: the last pending row flips the batch to ``CLOSED``.
* Counter bookkeeping stays in sync as rows transition.
* CSV / OFX confirm paths create a batch and link rows; manual entry
  does not.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import (
    Account,
    AccountType,
    Category,
    ImportBatch,
    ImportBatchStatus,
    ImportSourceFormat,
    Organization,
    User,
)
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import (
    Transaction,
    TransactionStatus,
    TransactionType,
)
from app.schemas.import_reconciliation import (
    ReconcileBatchRequest,
    ReconciliationEdits,
    ReconciliationState,
    ReconciliationTransition,
)
from app.services import reconciliation_service
from app.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


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
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db: AsyncSession) -> dict:
    """Seed one org with one account, one category, one user, and a
    fresh ``ImportBatch`` with three imported transactions in the
    ``PENDING_REVIEW`` state."""
    org = Organization(name="Primary", billing_cycle_day=1)
    db.add(org)
    await db.flush()

    user = User(
        username="seed_user",
        email="u@example.com",
        password_hash="x",
        org_id=org.id,
        is_superadmin=False,
    )
    db.add(user)
    await db.flush()

    at = AccountType(
        org_id=org.id, name="Checking", slug="checking", is_system=True
    )
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id,
        name="Cash",
        account_type_id=at.id,
        balance=Decimal("1000.00"),
        currency="EUR",
    )
    db.add(acct)
    await db.flush()

    cat = Category(
        org_id=org.id,
        name="Groceries",
        slug="groceries",
        type=CategoryType.EXPENSE,
    )
    db.add(cat)
    await db.flush()

    batch = ImportBatch(
        org_id=org.id,
        account_id=acct.id,
        source_format=ImportSourceFormat.CSV,
        file_name="seed.csv",
        created_by_user_id=user.id,
        status=ImportBatchStatus.OPEN,
        row_count=3,
        accepted_count=0,
        pending_count=3,
    )
    db.add(batch)
    await db.flush()

    txs: list[Transaction] = []
    for i in range(3):
        tx = Transaction(
            org_id=org.id,
            account_id=acct.id,
            category_id=cat.id,
            description=f"Row {i}",
            amount=Decimal("12.50"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=date(2026, 5, 10),
            settled_date=date(2026, 5, 10),
            is_imported=True,
            import_batch_id=batch.id,
            reconciliation_state="pending_review",
        )
        db.add(tx)
        txs.append(tx)
    await db.commit()

    return {
        "org_id": org.id,
        "user_id": user.id,
        "account_id": acct.id,
        "category_id": cat.id,
        "batch_id": batch.id,
        "tx_ids": [t.id for t in txs],
    }


# ── State-machine guard ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disallowed_transition_returns_conflict(db_session):
    """SKIPPED is terminal; trying to move out of it raises
    ``ConflictError`` (-> 409 at the router)."""
    seed = await _seed(db_session)

    # Flip row 0 to SKIPPED first (allowed: pending_review -> skipped).
    body = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=seed["tx_ids"][0],
                to_state=ReconciliationState.SKIPPED,
            )
        ]
    )
    await reconciliation_service.reconcile_request(
        db_session,
        org_id=seed["org_id"],
        batch_id=seed["batch_id"],
        request=body,
    )

    # Now try to move it out of SKIPPED -- this is terminal.
    bad = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=seed["tx_ids"][0],
                to_state=ReconciliationState.ACCEPTED,
            )
        ]
    )
    with pytest.raises(ConflictError) as exc_info:
        await reconciliation_service.reconcile_request(
            db_session,
            org_id=seed["org_id"],
            batch_id=seed["batch_id"],
            request=bad,
        )
    # Error message names both ends of the disallowed transition.
    assert "skipped" in str(exc_info.value).lower()
    assert "accepted" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_pending_review_to_accepted_succeeds(db_session):
    """Happy path: PENDING_REVIEW -> ACCEPTED, counters update."""
    seed = await _seed(db_session)

    body = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=seed["tx_ids"][0],
                to_state=ReconciliationState.ACCEPTED,
            )
        ]
    )
    response = await reconciliation_service.reconcile_request(
        db_session,
        org_id=seed["org_id"],
        batch_id=seed["batch_id"],
        request=body,
    )
    assert response.transitioned == [seed["tx_ids"][0]]
    assert response.remaining_pending == 2
    assert response.batch_status == "open"


# ── Auto-close ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_last_pending_row_auto_closes_batch(db_session):
    """Reconciling the last PENDING row drives ``pending_count`` to 0
    and ``close_batch_if_complete`` flips the batch to CLOSED."""
    seed = await _seed(db_session)

    # Move all three rows out of PENDING_REVIEW in one request.
    body = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=tid,
                to_state=ReconciliationState.ACCEPTED,
            )
            for tid in seed["tx_ids"]
        ]
    )
    response = await reconciliation_service.reconcile_request(
        db_session,
        org_id=seed["org_id"],
        batch_id=seed["batch_id"],
        request=body,
    )
    assert response.remaining_pending == 0
    assert response.batch_status == "closed"


# ── Membership invariant ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transition_on_foreign_transaction_is_422(db_session):
    """A transaction that belongs to a different batch returns
    ``ValidationError`` (-> 422). Spec §3.4 invariant 4."""
    seed = await _seed(db_session)

    # Create a SECOND batch with one transaction.
    other_batch = ImportBatch(
        org_id=seed["org_id"],
        account_id=seed["account_id"],
        source_format=ImportSourceFormat.OFX,
        file_name="other.ofx",
        created_by_user_id=seed["user_id"],
        status=ImportBatchStatus.OPEN,
        row_count=1,
        accepted_count=0,
        pending_count=1,
    )
    db_session.add(other_batch)
    await db_session.flush()
    other_tx = Transaction(
        org_id=seed["org_id"],
        account_id=seed["account_id"],
        category_id=seed["category_id"],
        description="Foreign",
        amount=Decimal("3.00"),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=date(2026, 5, 11),
        settled_date=date(2026, 5, 11),
        is_imported=True,
        import_batch_id=other_batch.id,
        reconciliation_state="pending_review",
    )
    db_session.add(other_tx)
    await db_session.commit()

    body = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=other_tx.id,
                to_state=ReconciliationState.ACCEPTED,
            )
        ]
    )
    with pytest.raises(ValidationError):
        await reconciliation_service.reconcile_request(
            db_session,
            org_id=seed["org_id"],
            batch_id=seed["batch_id"],
            request=body,
        )


# ── Atomicity ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_is_atomic_on_failure(db_session):
    """A mid-request failure rolls back every transition in the same
    request. The first transition's state change must NOT persist."""
    seed = await _seed(db_session)

    body = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=seed["tx_ids"][0],
                to_state=ReconciliationState.ACCEPTED,
            ),
            ReconciliationTransition(
                # Row 1 in the batch is in PENDING_REVIEW. Attempting
                # MATCHED without ``match_with_transaction_id`` raises
                # ``ValidationError`` -- the whole request rolls back.
                transaction_id=seed["tx_ids"][1],
                to_state=ReconciliationState.SKIPPED,
            ),
            ReconciliationTransition(
                # The third transition is intentionally disallowed:
                # SKIPPED is terminal, so this 409s. The previous two
                # transitions in the same request must roll back.
                transaction_id=seed["tx_ids"][2],
                to_state=ReconciliationState.ACCEPTED,
                # Tricky: we need a disallowed transition. Use SKIPPED
                # below as a fresh source state isn't available, so
                # craft a different failure: amount=0 in edits.
            ),
        ]
    )
    # The above doesn't actually fail; let's craft one that does.
    bad = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=seed["tx_ids"][0],
                to_state=ReconciliationState.ACCEPTED,
            ),
            ReconciliationTransition(
                # EDITED requires `edits`; this raises ValidationError.
                transaction_id=seed["tx_ids"][1],
                to_state=ReconciliationState.EDITED,
            ),
        ]
    )
    with pytest.raises(ValidationError):
        await reconciliation_service.reconcile_request(
            db_session,
            org_id=seed["org_id"],
            batch_id=seed["batch_id"],
            request=bad,
        )

    # Reload row 0: state should still be PENDING_REVIEW (rollback worked).
    refreshed = await db_session.scalar(
        select(Transaction).where(Transaction.id == seed["tx_ids"][0])
    )
    assert refreshed.reconciliation_state == "pending_review"


# ── Edits ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edited_transition_applies_edits(db_session):
    """PENDING_REVIEW -> EDITED with a description change rewrites the
    transaction's description in place."""
    seed = await _seed(db_session)

    body = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=seed["tx_ids"][0],
                to_state=ReconciliationState.EDITED,
                edits=ReconciliationEdits(description="Corrected"),
            )
        ]
    )
    response = await reconciliation_service.reconcile_request(
        db_session,
        org_id=seed["org_id"],
        batch_id=seed["batch_id"],
        request=body,
    )
    assert response.transitioned == [seed["tx_ids"][0]]

    refreshed = await db_session.scalar(
        select(Transaction).where(Transaction.id == seed["tx_ids"][0])
    )
    assert refreshed.description == "Corrected"
    assert refreshed.reconciliation_state == "edited"


# ── Cross-org isolation ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_other_org_cannot_reconcile_batch(db_session):
    """Org-scoped 404: a different org gets ``NotFoundError`` (not 403)
    when reconciling a batch it doesn't own."""
    seed = await _seed(db_session)

    other_org = Organization(name="Other", billing_cycle_day=1)
    db_session.add(other_org)
    await db_session.commit()

    body = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=seed["tx_ids"][0],
                to_state=ReconciliationState.ACCEPTED,
            )
        ]
    )
    with pytest.raises(NotFoundError):
        await reconciliation_service.reconcile_request(
            db_session,
            org_id=other_org.id,
            batch_id=seed["batch_id"],
            request=body,
        )


# ── Batch creation helper ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_import_batch_links_transactions(db_session):
    """``create_import_batch`` creates a header row and backfills
    ``transactions.import_batch_id`` on every provided ID."""
    seed = await _seed(db_session)

    # Make a NEW unlinked transaction (no import_batch_id).
    new_tx = Transaction(
        org_id=seed["org_id"],
        account_id=seed["account_id"],
        category_id=seed["category_id"],
        description="Standalone",
        amount=Decimal("9.99"),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=date(2026, 5, 12),
        settled_date=date(2026, 5, 12),
        is_imported=True,
        reconciliation_state="accepted",
    )
    db_session.add(new_tx)
    await db_session.flush()
    assert new_tx.import_batch_id is None

    batch = await reconciliation_service.create_import_batch(
        db_session,
        org_id=seed["org_id"],
        user_id=seed["user_id"],
        account_id=seed["account_id"],
        source_format=ImportSourceFormat.OFX,
        file_name="bank.ofx",
        transaction_ids=[new_tx.id],
    )
    await db_session.commit()

    assert batch.id is not None
    assert batch.row_count == 1
    # Decision 3: confirm rows land ACCEPTED, so the batch opens with
    # accepted_count == row_count, pending_count == 0.
    assert batch.accepted_count == 1
    assert batch.pending_count == 0

    refreshed = await db_session.scalar(
        select(Transaction).where(Transaction.id == new_tx.id)
    )
    assert refreshed.import_batch_id == batch.id


@pytest.mark.asyncio
async def test_create_import_batch_rejects_empty_ids(db_session):
    """An empty ID list raises ``ValidationError`` rather than create
    an empty batch (cleans up the inbox)."""
    seed = await _seed(db_session)
    with pytest.raises(ValidationError):
        await reconciliation_service.create_import_batch(
            db_session,
            org_id=seed["org_id"],
            user_id=seed["user_id"],
            account_id=seed["account_id"],
            source_format=ImportSourceFormat.CSV,
            file_name="empty.csv",
            transaction_ids=[],
        )


# ── Auto-close idempotency ──────────────────────────────────────────────────


# ── PR #247 P1: Edit integrity (balance + category ownership) ───────────────


@pytest.mark.asyncio
async def test_edit_amount_recomputes_account_balance(db_session):
    """Editing the amount of a SETTLED row reverts the old delta and
    applies the new one so ``accounts.balance`` cannot drift from the
    ledger. We pre-apply the seed's three 12.50 expenses to the account
    balance so the starting state is known, then edit row 0 from 12.50
    to 22.50 -- the balance must drop another 10.00."""
    seed = await _seed(db_session)

    # Pre-apply the seed's row 0 expense to the account so the test
    # starts from a known cached balance. (The seed inserts the rows
    # but does not run the balance bookkeeping the create path would.)
    acct = await db_session.scalar(
        select(Account).where(Account.id == seed["account_id"])
    )
    acct.balance = acct.balance - Decimal("12.50")
    await db_session.commit()
    starting_balance = acct.balance

    body = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=seed["tx_ids"][0],
                to_state=ReconciliationState.EDITED,
                edits=ReconciliationEdits(amount=Decimal("22.50")),
            )
        ]
    )
    await reconciliation_service.reconcile_request(
        db_session,
        org_id=seed["org_id"],
        batch_id=seed["batch_id"],
        request=body,
    )

    refreshed_acct = await db_session.scalar(
        select(Account).where(Account.id == seed["account_id"])
    )
    # Expense amount went UP by 10.00, so balance went DOWN by 10.00.
    assert refreshed_acct.balance == starting_balance - Decimal("10.00")


@pytest.mark.asyncio
async def test_edit_with_cross_org_category_is_rejected(db_session):
    """A category ID from another org is refused with ``ValidationError``
    (-> 422). The transaction must not mutate."""
    seed = await _seed(db_session)

    # Spin up another org with its own category.
    other_org = Organization(name="Other", billing_cycle_day=1)
    db_session.add(other_org)
    await db_session.flush()
    other_cat = Category(
        org_id=other_org.id,
        name="Other Cat",
        slug="other-cat",
        type=CategoryType.EXPENSE,
    )
    db_session.add(other_cat)
    await db_session.commit()

    body = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=seed["tx_ids"][0],
                to_state=ReconciliationState.EDITED,
                edits=ReconciliationEdits(category_id=other_cat.id),
            )
        ]
    )
    with pytest.raises(ValidationError):
        await reconciliation_service.reconcile_request(
            db_session,
            org_id=seed["org_id"],
            batch_id=seed["batch_id"],
            request=body,
        )

    refreshed = await db_session.scalar(
        select(Transaction).where(Transaction.id == seed["tx_ids"][0])
    )
    assert refreshed.category_id == seed["category_id"]
    assert refreshed.reconciliation_state == "pending_review"


@pytest.mark.asyncio
async def test_edit_with_incompatible_category_type_is_rejected(db_session):
    """An INCOME category on an EXPENSE transaction is refused (the
    same compatibility check ``transaction_service`` uses on CRUD)."""
    seed = await _seed(db_session)

    income_cat = Category(
        org_id=seed["org_id"],
        name="Salary",
        slug="salary",
        type=CategoryType.INCOME,
    )
    db_session.add(income_cat)
    await db_session.commit()

    body = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=seed["tx_ids"][0],
                to_state=ReconciliationState.EDITED,
                edits=ReconciliationEdits(category_id=income_cat.id),
            )
        ]
    )
    with pytest.raises(ValidationError):
        await reconciliation_service.reconcile_request(
            db_session,
            org_id=seed["org_id"],
            batch_id=seed["batch_id"],
            request=body,
        )

    refreshed = await db_session.scalar(
        select(Transaction).where(Transaction.id == seed["tx_ids"][0])
    )
    assert refreshed.category_id == seed["category_id"]


# ── PR #247 P2: FITID dedup scope + NULL-batch coverage ─────────────────────


@pytest.mark.asyncio
async def test_fitid_dup_warning_scoped_to_account(db_session):
    """FITID uniqueness is **per account** (OFX spec §11.4.4). A row in
    this batch should NOT light the duplicate-warning when the matching
    FITID lives on a DIFFERENT account in the same org (two different
    banks can legitimately share a FITID string)."""
    seed = await _seed(db_session)

    # Make a second account in the same org.
    at = await db_session.scalar(
        select(AccountType).where(
            AccountType.org_id == seed["org_id"]
        )
    )
    other_acct = Account(
        org_id=seed["org_id"],
        name="Other Account",
        account_type_id=at.id,
        balance=Decimal("0"),
        currency="EUR",
    )
    db_session.add(other_acct)
    await db_session.flush()

    # Existing transaction on the OTHER account with a FITID we'll
    # reuse on the batch row.
    existing_other = Transaction(
        org_id=seed["org_id"],
        account_id=other_acct.id,
        category_id=seed["category_id"],
        description="Bank A",
        amount=Decimal("1.00"),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1),
        settled_date=date(2026, 5, 1),
        is_imported=True,
        fitid="SHARED-FITID",
        reconciliation_state="accepted",
    )
    # Tag the batch row 0 with the same FITID -- but on the original
    # (batch-owning) account.
    batch_row = await db_session.scalar(
        select(Transaction).where(Transaction.id == seed["tx_ids"][0])
    )
    batch_row.fitid = "SHARED-FITID"
    db_session.add(existing_other)
    await db_session.commit()

    detail = await reconciliation_service.get_batch_detail(
        db_session, org_id=seed["org_id"], batch_id=seed["batch_id"]
    )
    target_row = next(
        r for r in detail.rows if r.transaction_id == seed["tx_ids"][0]
    )
    # Different account -> no warning despite same FITID.
    assert target_row.duplicate_warning is False
    assert target_row.duplicate_warning_target is None


@pytest.mark.asyncio
async def test_fitid_dup_warning_covers_null_batch_legacy_rows(db_session):
    """Legacy transactions imported pre-batches feature have
    ``import_batch_id IS NULL``. The cross-batch FITID warning MUST
    include them (SQL three-valued logic would otherwise exclude
    NULL-batch rows from ``import_batch_id != batch_id``)."""
    seed = await _seed(db_session)

    # Insert a pre-batches transaction with a known FITID on the SAME
    # account as the batch. import_batch_id stays NULL.
    legacy = Transaction(
        org_id=seed["org_id"],
        account_id=seed["account_id"],
        category_id=seed["category_id"],
        description="Legacy import",
        amount=Decimal("5.00"),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=date(2026, 4, 1),
        settled_date=date(2026, 4, 1),
        is_imported=True,
        import_batch_id=None,
        fitid="LEGACY-FITID",
        reconciliation_state="accepted",
    )
    batch_row = await db_session.scalar(
        select(Transaction).where(Transaction.id == seed["tx_ids"][0])
    )
    batch_row.fitid = "LEGACY-FITID"
    db_session.add(legacy)
    await db_session.commit()

    detail = await reconciliation_service.get_batch_detail(
        db_session, org_id=seed["org_id"], batch_id=seed["batch_id"]
    )
    target_row = next(
        r for r in detail.rows if r.transaction_id == seed["tx_ids"][0]
    )
    # NULL-batch legacy row IS considered -> warning fires.
    assert target_row.duplicate_warning is True
    assert target_row.duplicate_warning_target == legacy.id


@pytest.mark.asyncio
async def test_close_batch_is_idempotent(db_session):
    """Calling ``close_batch_if_complete`` on an already-CLOSED batch
    is a no-op and returns False."""
    seed = await _seed(db_session)

    # Drive the batch to CLOSED.
    body = ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=tid,
                to_state=ReconciliationState.ACCEPTED,
            )
            for tid in seed["tx_ids"]
        ]
    )
    await reconciliation_service.reconcile_request(
        db_session,
        org_id=seed["org_id"],
        batch_id=seed["batch_id"],
        request=body,
    )

    batch = await db_session.scalar(
        select(ImportBatch).where(ImportBatch.id == seed["batch_id"])
    )
    assert batch.status == ImportBatchStatus.CLOSED

    # Re-call: no-op.
    again = await reconciliation_service.close_batch_if_complete(
        db_session, batch=batch
    )
    assert again is False
