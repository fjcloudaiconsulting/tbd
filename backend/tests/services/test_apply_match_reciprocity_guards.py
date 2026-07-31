"""``_apply_match`` reciprocity guards (TBD-282).

``_apply_match`` writes ``linked_transaction_id`` ONE-WAY, on purpose:
that direction is the discriminator ``balance_contribution_filter`` uses
to tell a reconcile match apart from a real transfer leg. But nothing
stopped a second match from being applied in the OPPOSITE direction,
which manufactures a mutual link out of two reconcile matches -- a pair
of rows that every downstream predicate then reads as a real transfer.

Two guards, both BEFORE the write:

* target guard -- refuse when the target already links back at ``tx``.
  Narrow on purpose (``== tx.id``, never ``is not None``): matching an
  imported row against a leg of a real transfer is a supported flow.
* tx-side guard -- refuse when ``tx`` is already one leg of a MUTUAL
  link. Narrower than ``_apply_edits``' blanket refusal, so re-matching
  a previously one-way-matched row after a reopen keeps working.

Fixture notes (design §6.0): distinct powers of two for every amount so
each subset sum is unique; different opening balances per account so a
swapped attribution is visible; no id ``1`` anywhere -- transaction ids
are explicit and start at 8001.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import (
    Account,
    AccountType,
    Category,
    ImportBatch,
    ImportBatchStatus,
    ImportSourceFormat,
    Organization,
    Transaction,
    User,
)
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.import_reconciliation import (
    ReconcileBatchRequest,
    ReconciliationState,
    ReconciliationTransition,
)
from app.services import reconciliation_service
from app.services.exceptions import ValidationError


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


# ── Fixture ─────────────────────────────────────────────────────────────────

# Opening balances differ per account so a revert attributed to the wrong
# account cannot hide behind an equal starting value.
ACCT_A_OPENING = Decimal("1000.00")
ACCT_B_OPENING = Decimal("400.00")


async def _seed(db: AsyncSession) -> dict:
    org = Organization(name="Primary", billing_cycle_day=1)
    db.add(org)
    await db.flush()

    user = User(
        username="seed_user", email="u@example.com", password_hash="x",
        org_id=org.id, is_superadmin=False,
    )
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add_all([user, at])
    await db.flush()

    acct_a = Account(
        org_id=org.id, name="Acct A", account_type_id=at.id,
        balance=ACCT_A_OPENING, currency="EUR",
    )
    acct_b = Account(
        org_id=org.id, name="Acct B", account_type_id=at.id,
        balance=ACCT_B_OPENING, currency="EUR",
    )
    db.add_all([acct_a, acct_b])
    await db.flush()

    cat = Category(
        org_id=org.id, name="Groceries", slug="groceries", type=CategoryType.EXPENSE,
    )
    db.add(cat)
    await db.flush()

    batch = ImportBatch(
        org_id=org.id, account_id=acct_a.id,
        source_format=ImportSourceFormat.CSV, file_name="seed.csv",
        created_by_user_id=user.id, status=ImportBatchStatus.OPEN,
        row_count=1, accepted_count=0, pending_count=1,
    )
    db.add(batch)
    await db.flush()

    # A filler row keeps ``pending_count`` above zero for the whole test, so
    # the batch never auto-closes mid-scenario.
    filler = Transaction(
        id=8099, org_id=org.id, account_id=acct_a.id, category_id=cat.id,
        description="filler", amount=Decimal("1.00"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 10), settled_date=date(2026, 5, 10),
        is_imported=True, import_batch_id=batch.id,
        reconciliation_state="pending_review",
    )
    db.add(filler)
    await db.commit()

    return {
        "org_id": org.id,
        "batch_id": batch.id,
        "acct_a_id": acct_a.id,
        "acct_b_id": acct_b.id,
        "cat_id": cat.id,
    }


async def _add_row(
    db: AsyncSession,
    seed: dict,
    *,
    tx_id: int,
    account_id: int,
    amount: str,
    in_batch: bool,
    tx_type: TransactionType = TransactionType.EXPENSE,
) -> Transaction:
    tx = Transaction(
        id=tx_id,
        org_id=seed["org_id"],
        account_id=account_id,
        category_id=seed["cat_id"],
        description=f"row-{tx_id}",
        amount=Decimal(amount),
        type=tx_type,
        status=TransactionStatus.SETTLED,
        date=date(2026, 5, 10),
        settled_date=date(2026, 5, 10),
        is_imported=in_batch,
        import_batch_id=seed["batch_id"] if in_batch else None,
        reconciliation_state="pending_review" if in_batch else "accepted",
    )
    db.add(tx)
    if in_batch:
        # Keep the batch counters honest, otherwise pending_count hits zero
        # early, the batch auto-closes mid-scenario, and the service logs a
        # drift warning that has nothing to do with what is under test.
        batch = await db.scalar(
            select(ImportBatch).where(ImportBatch.id == seed["batch_id"])
        )
        batch.row_count += 1
        batch.pending_count += 1
    await db.commit()
    return tx


def _match(tx_id: int, target_id: int) -> ReconcileBatchRequest:
    return ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=tx_id,
                to_state=ReconciliationState.MATCHED,
                match_with_transaction_id=target_id,
            )
        ]
    )


def _to(tx_id: int, state: ReconciliationState) -> ReconcileBatchRequest:
    return ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(transaction_id=tx_id, to_state=state)
        ]
    )


async def _balances(db: AsyncSession, seed: dict) -> dict[int, Decimal]:
    rows = (
        await db.scalars(
            select(Account).where(Account.org_id == seed["org_id"]).execution_options(
                populate_existing=True
            )
        )
    ).all()
    return {a.id: a.balance for a in rows}


async def _reload(db: AsyncSession, tx_id: int) -> Transaction:
    return await db.scalar(
        select(Transaction)
        .where(Transaction.id == tx_id)
        .execution_options(populate_existing=True)
    )


# ── F11 / F12 ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_matching_back_onto_an_already_matched_partner_is_refused(db_session):
    """F11. Kills: the ``_apply_match`` target guard deleted.

    F12. Kills: the guard written in the WRONG direction
    (``tx.linked_transaction_id == target.id``).

    ⚠ This fixture discriminates the two directions ONLY because
    ``A -> B`` is matched FIRST. At the moment of the refused call the
    subject is ``B`` (link still NULL) and the target is ``A`` (already
    linking at ``B``). A reversed guard reads ``B.linked_transaction_id
    == A.id`` -> ``None == A.id`` -> False, lets the write through, and
    this test goes red. Do not "simplify" the ordering.
    """
    seed = await _seed(db_session)
    a = await _add_row(db_session, seed, tx_id=8001,
                       account_id=seed["acct_a_id"], amount="8.00", in_batch=True)
    b = await _add_row(db_session, seed, tx_id=8002,
                       account_id=seed["acct_b_id"], amount="16.00", in_batch=True)

    await reconciliation_service.reconcile_request(
        db_session, org_id=seed["org_id"], batch_id=seed["batch_id"],
        request=_match(a.id, b.id),
    )
    assert (await _reload(db_session, a.id)).linked_transaction_id == b.id
    before = await _balances(db_session, seed)

    with pytest.raises(ValidationError) as exc:
        await reconciliation_service.reconcile_request(
            db_session, org_id=seed["org_id"], batch_id=seed["batch_id"],
            request=_match(b.id, a.id),
        )
    # ValidationError is mapped to HTTP 400 by app.main.validation_handler;
    # ConflictError (409) would tell a client to refresh and retry, which
    # can never help here.
    assert str(a.id) in exc.value.detail and str(b.id) in exc.value.detail

    db_session.expunge_all()
    assert (await _reload(db_session, b.id)).linked_transaction_id is None
    assert (await _reload(db_session, a.id)).linked_transaction_id == b.id
    assert await _balances(db_session, seed) == before


# ── F13 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_matching_an_imported_row_against_a_real_transfer_leg_succeeds(db_session):
    """F13. Kills: the target guard over-tightened to ``is not None``.

    ``B <-> C`` is a genuine transfer pair. Matching an imported duplicate
    ``A`` against leg ``B`` is a supported flow -- it is exactly what
    ``find_duplicate_of_linked_leg`` exists to surface. The target guard
    must refuse only a link that points back at ``A``.
    """
    seed = await _seed(db_session)
    a = await _add_row(db_session, seed, tx_id=8011,
                       account_id=seed["acct_a_id"], amount="32.00", in_batch=True)
    b = await _add_row(db_session, seed, tx_id=8012,
                       account_id=seed["acct_a_id"], amount="64.00", in_batch=False)
    c = await _add_row(db_session, seed, tx_id=8013,
                       account_id=seed["acct_b_id"], amount="64.00", in_batch=False,
                       tx_type=TransactionType.INCOME)
    b.linked_transaction_id = c.id
    c.linked_transaction_id = b.id
    await db_session.commit()

    await reconciliation_service.reconcile_request(
        db_session, org_id=seed["org_id"], batch_id=seed["batch_id"],
        request=_match(a.id, b.id),
    )

    db_session.expunge_all()
    assert (await _reload(db_session, a.id)).linked_transaction_id == b.id
    # The real pair is untouched.
    assert (await _reload(db_session, b.id)).linked_transaction_id == c.id
    assert (await _reload(db_session, c.id)).linked_transaction_id == b.id


# ── F14 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_target_guard_fires_before_the_write(db_session):
    """F14. Kills: the guard placed AFTER ``tx.linked_transaction_id = ...``.

    ⚠ CORRECTED. This used to claim flatly that "an API-level test cannot
    kill it", because ``reconcile_request`` wraps the loop in a savepoint
    that rolls the write back either way, leaving the PERSISTED state
    identical. The persisted-state argument is sound; the flat claim was
    not. DO NOT DELETE F14 AS REDUNDANT on the strength of that.

    *(measured)* Hoisting the write above both guards turns THREE tests
    red, and only one of the three reds is about this mutant:

    * ``test_target_guard_fires_before_the_write`` (this test) -- red on
      its own assertion, ``b.linked_transaction_id is None``. The only
      SEMANTIC red for the guard-1-after-write mutant.
    * ``test_matching_back_onto_an_already_matched_partner_is_refused``
      (F11, API-level) -- red, but with ``sqlalchemy.exc.MissingGreenlet``
      raised from ``str(a.id)`` at the "detail names both ids" assertion.
      The extra write expires the instance and re-loading it wants IO in a
      sync context. That is an ORM accident of this session fixture, not a
      statement about the guard: it would not survive a refactor that
      reloads or expunges differently, and it reports the same red for a
      dozen unrelated mutations.
    * ``test_a_real_transfer_leg_cannot_be_matched`` -- red with DID NOT
      RAISE. That one IS semantic, but it is about GUARD 2: with the write
      hoisted, ``tx.linked_transaction_id`` already equals ``match_id``,
      so guard 2 resolves the match target as ``current``, finds no
      back-link, and declines to fire. It says nothing about guard 1.

    So the IN-MEMORY assertion below is still the only fence that pins
    guard 1's POSITION on purpose rather than by side effect. It has to be
    a DIRECT unit test on ``_apply_match`` for exactly that reason.
    """
    seed = await _seed(db_session)
    a = await _add_row(db_session, seed, tx_id=8021,
                       account_id=seed["acct_a_id"], amount="8.00", in_batch=True)
    b = await _add_row(db_session, seed, tx_id=8022,
                       account_id=seed["acct_b_id"], amount="16.00", in_batch=True)
    a.linked_transaction_id = b.id   # a one-way reconcile match already applied
    await db_session.commit()

    transition = ReconciliationTransition(
        transaction_id=b.id,
        to_state=ReconciliationState.MATCHED,
        match_with_transaction_id=a.id,
    )
    with pytest.raises(ValidationError):
        await reconciliation_service._apply_match(
            db_session, org_id=seed["org_id"], tx=b, transition=transition
        )

    assert b.linked_transaction_id is None


# ── F15 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rematching_after_a_reopen_still_works(db_session):
    """F15. Kills: the tx-side guard over-tightened to ``is not None``.

    ``A`` carries a stale ONE-WAY link at ``B`` after MATCHED -> ACCEPTED
    -> PENDING_REVIEW (nothing clears it on reopen). Re-matching it at a
    different row must still work: the tx-side guard refuses only a
    MUTUAL link, i.e. an actual transfer leg.
    """
    seed = await _seed(db_session)
    a = await _add_row(db_session, seed, tx_id=8031,
                       account_id=seed["acct_a_id"], amount="8.00", in_batch=True)
    b = await _add_row(db_session, seed, tx_id=8032,
                       account_id=seed["acct_b_id"], amount="16.00", in_batch=False)
    d = await _add_row(db_session, seed, tx_id=8033,
                       account_id=seed["acct_b_id"], amount="32.00", in_batch=False)

    for request in (
        _match(a.id, b.id),
        _to(a.id, ReconciliationState.ACCEPTED),
        _to(a.id, ReconciliationState.PENDING_REVIEW),
    ):
        await reconciliation_service.reconcile_request(
            db_session, org_id=seed["org_id"], batch_id=seed["batch_id"],
            request=request,
        )
    reopened = await _reload(db_session, a.id)
    assert reopened.linked_transaction_id == b.id   # stale one-way link survives
    assert reopened.reconciliation_state == "pending_review"

    await reconciliation_service.reconcile_request(
        db_session, org_id=seed["org_id"], batch_id=seed["batch_id"],
        request=_match(a.id, d.id),
    )

    db_session.expunge_all()
    assert (await _reload(db_session, a.id)).linked_transaction_id == d.id


@pytest.mark.asyncio
async def test_a_real_transfer_leg_cannot_be_matched(db_session):
    """The tx-side guard's positive face: a row that IS one leg of a
    mutual link is refused, with both ids in the message."""
    seed = await _seed(db_session)
    a = await _add_row(db_session, seed, tx_id=8041,
                       account_id=seed["acct_a_id"], amount="8.00", in_batch=True)
    b = await _add_row(db_session, seed, tx_id=8042,
                       account_id=seed["acct_b_id"], amount="8.00", in_batch=False,
                       tx_type=TransactionType.INCOME)
    d = await _add_row(db_session, seed, tx_id=8043,
                       account_id=seed["acct_b_id"], amount="64.00", in_batch=False)
    a.linked_transaction_id = b.id
    b.linked_transaction_id = a.id      # a REAL transfer pair
    await db_session.commit()

    with pytest.raises(ValidationError) as exc:
        await reconciliation_service.reconcile_request(
            db_session, org_id=seed["org_id"], batch_id=seed["batch_id"],
            request=_match(a.id, d.id),
        )
    assert "transfer leg" in exc.value.detail

    db_session.expunge_all()
    assert (await _reload(db_session, a.id)).linked_transaction_id == b.id
