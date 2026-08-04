"""A matched row is editable, its balance arms are gated, and deleting its
canonical twin demotes it instead of resurrecting it (TBD-292 / 294 / 295).

``reconciliation_service._apply_match`` writes ``linked_transaction_id``
ONE-WAY. Three consequences this file fences:

* **TBD-292** -- ``update_transaction`` raised
  ``ConflictError("Transfer pair link integrity violated")`` on any
  asymmetric link, i.e. on EVERY matched row, forever. Deleting that guard
  is only half the fix: the balance revert/apply arms must ALSO learn that a
  matched row's amount is no longer inside ``accounts.balance``.

  ⚠ THE TRAP the ticket's own prescription walks into:
  ``contributes_to_cached_balance(tx, None)`` returns **True** -- it fails
  OPEN by design. So "reassign ``partner = None`` when the link is not
  reciprocal, then call the predicate" produces a vacuously always-true
  gate: the 409 goes away and the balance drifts instead, with every
  "returned 200" test still green. ``test_matched_row_amount_edit_preserves_
  balance_invariant`` is the only fence here that catches it, because it is
  the only one that asserts the ledger, not the status code.

* **TBD-294** -- the FK is ``ON DELETE SET NULL``. Deleting the canonical
  twin erased the discriminator and the matched duplicate silently re-entered
  BOTH ``balance_contribution_filter`` and ``reportable_transaction_filter``.

* **TBD-295** -- promote-to-recurring still refuses a linked row (the guard
  is not asking "is this a transfer leg", it is asking "may this row seed a
  repeating series"), and now also refuses a reverted-state row.

Fixture rules (design §6.0, inherited from test_delete_link_reciprocity):

* **Every matched row is built through the real ``reconcile_request`` path.**
  A hand-written ``linked_transaction_id`` never ran
  ``_apply_balance_for_transition``, so the premise under test -- that the
  contribution WAS reverted -- would be absent and every assertion untethered.
* Every balance assertion is PER ACCOUNT, against the RECONSTRUCTION through
  the **SQL** ``balance_contribution_filter``, never the Python sibling: the
  two intentionally disagree on an unresolvable partner (an ``xfail(strict)``
  divergence cell in ``test_link_reciprocity_predicates``).
* Accounts open at DIFFERENT balances so a swapped attribution cannot hide.
* Amounts are distinct powers of two, so every subset sum is unique.
* No fixture id is ``1``; transaction ids are explicit, from 7001.
* ``PRAGMA foreign_keys=ON`` -- without it ``SET NULL`` never fires and the
  TBD-294 fences pass for the wrong reason.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import case, event, func, select
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
from app.schemas.transaction import (
    PromoteToRecurringRequest,
    TransactionCreate,
    TransactionUpdate,
)
from app.services import reconciliation_service, transaction_service
from app.services.exceptions import ValidationError
from app.services.transaction_filters import balance_contribution_filter


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # LOAD-BEARING for every TBD-294 fence: without it SQLite ignores the
    # ``ON DELETE SET NULL`` on transactions.linked_transaction_id, the
    # orphan is never produced, and the test passes for the wrong reason.
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

ACCT_A_OPENING = Decimal("1000.00")
ACCT_B_OPENING = Decimal("400.00")
ACCT_C_OPENING = Decimal("250.00")

TX_DATE = date(2026, 5, 10)


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

    accts = {}
    for key, name, opening in (
        ("a", "Acct A", ACCT_A_OPENING),
        ("b", "Acct B", ACCT_B_OPENING),
        ("c", "Acct C", ACCT_C_OPENING),
    ):
        acct = Account(
            org_id=org.id, name=name, account_type_id=at.id,
            balance=opening, opening_balance=opening,
            opening_balance_date=date(2026, 1, 1), currency="EUR",
        )
        db.add(acct)
        accts[key] = acct
    await db.flush()

    # CategoryType.BOTH so a transfer pair can share it and a type flip in an
    # edit is never rejected for an unrelated reason.
    cat = Category(
        org_id=org.id, name="Shared", slug="shared", type=CategoryType.BOTH,
    )
    db.add(cat)
    await db.flush()

    batch = ImportBatch(
        org_id=org.id, account_id=accts["a"].id,
        source_format=ImportSourceFormat.CSV, file_name="seed.csv",
        created_by_user_id=user.id, status=ImportBatchStatus.OPEN,
        row_count=0, accepted_count=0, pending_count=0,
    )
    db.add(batch)

    # ID ANCHOR. ``create_transaction`` owns id assignment, so the only way to
    # keep every fixture id away from ``1`` -- where a wrong lookup and a right
    # one are indistinguishable -- is to plant a high explicit id first and let
    # SQLite's max(rowid)+1 carry it forward. PENDING and on a third account,
    # so it contributes nothing to any balance reconstruction.
    anchor = Transaction(
        id=7000, org_id=org.id, account_id=accts["c"].id, category_id=cat.id,
        description="id-anchor", amount=Decimal("0.01"),
        type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
        date=TX_DATE,
    )
    db.add(anchor)
    await db.commit()

    return {
        "org_id": org.id,
        "batch_id": batch.id,
        "cat_id": cat.id,
        "acct_a_id": accts["a"].id,
        "acct_b_id": accts["b"].id,
        "acct_c_id": accts["c"].id,
    }


async def _create(
    db: AsyncSession,
    seed: dict,
    *,
    account_id: int,
    amount: str,
    label: str = "row",
    tx_type: TransactionType = TransactionType.EXPENSE,
    status: TransactionStatus = TransactionStatus.SETTLED,
    in_batch: bool = False,
) -> Transaction:
    """Create through the REAL create path so ``accounts.balance`` is applied
    by production code, and, when asked, enrol the row in the import batch as
    PENDING_REVIEW.

    Ids come from the DB (the seed's anchor keeps them in the 7000s); callers
    hold the returned objects rather than declaring ids, so no assertion can
    accidentally be satisfied by the wrong row.
    """
    tx = await transaction_service.create_transaction(
        db,
        seed["org_id"],
        TransactionCreate(
            account_id=account_id,
            category_id=seed["cat_id"],
            description=f"{label}-{amount}",
            amount=Decimal(amount),
            type=tx_type.value,
            status=status.value,
            date=TX_DATE,
            settled_date=TX_DATE if status == TransactionStatus.SETTLED else None,
        ),
    )
    if in_batch:
        tx.import_batch_id = seed["batch_id"]
        tx.reconciliation_state = "pending_review"
        batch = await db.scalar(
            select(ImportBatch).where(ImportBatch.id == seed["batch_id"])
        )
        batch.row_count += 1
        batch.pending_count += 1
        await db.commit()
    return tx


def _transition(tx_id: int, state: ReconciliationState, *, match: int | None = None):
    return ReconcileBatchRequest(
        transitions=[
            ReconciliationTransition(
                transaction_id=tx_id,
                to_state=state,
                match_with_transaction_id=match,
            )
        ]
    )


async def _reconcile(db: AsyncSession, seed: dict, request: ReconcileBatchRequest):
    return await reconciliation_service.reconcile_request(
        db, org_id=seed["org_id"], batch_id=seed["batch_id"], request=request
    )


async def _make_matched_pair(
    db: AsyncSession,
    seed: dict,
    *,
    amount: str,
    dup_account: str = "acct_a_id",
    canonical_account: str = "acct_b_id",
    dup_status: TransactionStatus = TransactionStatus.SETTLED,
    canonical: Transaction | None = None,
) -> tuple[Transaction, Transaction]:
    """``dup`` (in the import batch) is MATCHED against ``canonical`` through
    ``reconcile_request``. Afterwards ``dup.linked_transaction_id ==
    canonical.id`` ONE-WAY and ``dup``'s contribution has been reverted by
    ``_apply_balance_for_transition`` -- the actual premise every fence needs.
    """
    fresh_canonical = canonical is None
    if canonical is None:
        canonical = await _create(
            db, seed, account_id=seed[canonical_account], amount=amount,
            label="canonical",
        )
    dup = await _create(
        db, seed, account_id=seed[dup_account], amount=amount, label="dup",
        in_batch=True, status=dup_status,
    )
    await _reconcile(
        db, seed,
        _transition(dup.id, ReconciliationState.MATCHED, match=canonical.id),
    )
    dup = await _reload(db, dup.id)
    assert dup.linked_transaction_id == canonical.id
    canonical = await _reload(db, canonical.id)
    if fresh_canonical:
        assert canonical.linked_transaction_id is None, "match must stay ONE-WAY"
    assert dup.id != canonical.id
    assert min(dup.id, canonical.id) > 7000, "id anchor must be in force"
    return dup, canonical


async def _reload(db: AsyncSession, tx_id: int) -> Transaction | None:
    return await db.scalar(
        select(Transaction)
        .where(Transaction.id == tx_id)
        .execution_options(populate_existing=True)
    )


async def _account(db: AsyncSession, account_id: int) -> Account:
    return await db.scalar(
        select(Account)
        .where(Account.id == account_id)
        .execution_options(populate_existing=True)
    )


_SIGNED = case(
    (Transaction.type == TransactionType.INCOME, Transaction.amount),
    else_=-Transaction.amount,
)


async def _reconstructed_delta(db: AsyncSession, org_id: int, account_id: int) -> Decimal:
    """``Σ signed(rows passing the SQL balance_contribution_filter, SETTLED)``
    for one account. Asserted against the SQL filter, never its Python
    sibling: they intentionally disagree on an unresolvable partner."""
    raw = await db.scalar(
        select(func.coalesce(func.sum(_SIGNED), 0)).where(
            Transaction.org_id == org_id,
            Transaction.account_id == account_id,
            Transaction.status == TransactionStatus.SETTLED,
            balance_contribution_filter(),
        )
    )
    return Decimal(str(raw or 0)).quantize(Decimal("0.01"))


async def assert_invariant(db: AsyncSession, seed: dict) -> None:
    """``balance - opening_balance == reconstruction`` for EVERY account."""
    for key in ("acct_a_id", "acct_b_id", "acct_c_id"):
        acct = await _account(db, seed[key])
        expected = (acct.balance - acct.opening_balance).quantize(Decimal("0.01"))
        actual = await _reconstructed_delta(db, seed["org_id"], acct.id)
        assert expected == actual, (
            f"balance invariant broken on {acct.name}: "
            f"balance-opening={expected} but reconstruction={actual}"
        )


# ══ TBD-292: a matched row is editable ══════════════════════════════════════


@pytest.mark.asyncio
async def test_matched_row_edit_returns_200(db_session):
    """F1. Kills: the ``partner.linked_transaction_id != tx.id`` ConflictError
    guard, which fires on every reconcile match and can never be retried away.
    RED against ``main`` with ConflictError."""
    seed = await _seed(db_session)
    dup, _canonical = await _make_matched_pair(
        db_session, seed, amount="64.00",
    )

    result = await transaction_service.update_transaction(
        db_session, seed["org_id"], dup.id,
        TransactionUpdate(description="renamed by the user"),
    )
    assert result.description == "renamed by the user"
    # The match itself is untouched by an ordinary edit.
    assert result.linked_transaction_id is not None
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_matched_row_amount_edit_preserves_balance_invariant(db_session):
    """⭐ F2. THE fence for §1b's trap.

    Kills, independently:
      * arms 4a / 4e left ungated -- the revert/apply pair moves money that
        was already reverted at match time;
      * ``partner = linked_row if is_reciprocal_pair(...) else None`` followed
        by ``contributes_to_cached_balance(tx, partner)`` -- the predicate
        fails OPEN on ``None``, so the gate is vacuously True and this test
        is the only one that notices;
      * gating on ``linked_row is None or ...`` instead of calling the shared
        predicate (that shape also passes F1 and F3).
    """
    seed = await _seed(db_session)
    dup, _canonical = await _make_matched_pair(
        db_session, seed, amount="100.00",
    )
    await assert_invariant(db_session, seed)
    before = (await _account(db_session, seed["acct_a_id"])).balance

    await transaction_service.update_transaction(
        db_session, seed["org_id"], dup.id,
        TransactionUpdate(amount=Decimal("175.00")),
    )

    after = (await _account(db_session, seed["acct_a_id"])).balance
    # A matched row's amount is not in the cached balance. Editing it must
    # move NOTHING -- not the -75 an ungated pair produces, not anything else.
    assert after == before
    await assert_invariant(db_session, seed)
    assert (await _reload(db_session, dup.id)).amount == Decimal("175.00")


@pytest.mark.asyncio
async def test_matched_row_status_toggle_moves_no_money(db_session):
    """⭐ F3. Both directions, because a boundary pinned from one side is not
    pinned.

    Kills: gating ONLY arm 4a (settled -> pending then drifts by the amount)
    and, separately, gating ONLY arm 4e (pending -> settled drifts the other
    way). Either half alone is worse than gating neither.
    """
    seed = await _seed(db_session)
    dup, _canonical = await _make_matched_pair(
        db_session, seed, amount="32.00",
    )
    start = (await _account(db_session, seed["acct_a_id"])).balance

    # settled -> pending
    await transaction_service.update_transaction(
        db_session, seed["org_id"], dup.id, TransactionUpdate(status="pending"),
    )
    assert (await _account(db_session, seed["acct_a_id"])).balance == start
    await assert_invariant(db_session, seed)

    # pending -> settled
    await transaction_service.update_transaction(
        db_session, seed["org_id"], dup.id,
        TransactionUpdate(status="settled", settled_date=TX_DATE),
    )
    assert (await _account(db_session, seed["acct_a_id"])).balance == start
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_matched_row_account_move_moves_neither_balance(db_session):
    """F4. The two accounts open at DIFFERENT balances, so an attribution
    swapped between them cannot hide behind an equal starting value."""
    seed = await _seed(db_session)
    dup, _canonical = await _make_matched_pair(
        db_session, seed, amount="16.00",
    )
    a_before = (await _account(db_session, seed["acct_a_id"])).balance
    c_before = (await _account(db_session, seed["acct_c_id"])).balance
    assert a_before != c_before

    await transaction_service.update_transaction(
        db_session, seed["org_id"], dup.id,
        TransactionUpdate(account_id=seed["acct_c_id"]),
    )

    assert (await _account(db_session, seed["acct_a_id"])).balance == a_before
    assert (await _account(db_session, seed["acct_c_id"])).balance == c_before
    assert (await _reload(db_session, dup.id)).account_id == seed["acct_c_id"]
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_real_transfer_leg_edit_still_mirrors_and_moves_both(db_session):
    """F5. THE OVER-REACH FENCE. Without it, hard-coding
    ``tx_in_cached_balance = False`` passes F2, F3 and F4."""
    seed = await _seed(db_session)
    expense = await _create(
        db_session, seed, account_id=seed["acct_a_id"],
        amount="80.00", tx_type=TransactionType.EXPENSE,
    )
    income = await _create(
        db_session, seed, account_id=seed["acct_b_id"],
        amount="80.00", tx_type=TransactionType.INCOME,
    )
    await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"], expense_tx_id=expense.id, income_tx_id=income.id,
    )
    a_before = (await _account(db_session, seed["acct_a_id"])).balance
    b_before = (await _account(db_session, seed["acct_b_id"])).balance

    await transaction_service.update_transaction(
        db_session, seed["org_id"], expense.id,
        TransactionUpdate(amount=Decimal("120.00")),
    )

    # Both legs mirror, and BOTH accounts move by the delta in their own
    # direction. A "matched rows never move money" over-reach freezes both.
    assert (await _reload(db_session, income.id)).amount == Decimal("120.00")
    assert (await _account(db_session, seed["acct_a_id"])).balance == a_before - Decimal("40.00")
    assert (await _account(db_session, seed["acct_b_id"])).balance == b_before + Decimal("40.00")
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_skipped_row_amount_edit_moves_no_money(db_session):
    """F6 (TBD-302, fixed for free). A SKIPPED row carries
    ``linked_transaction_id IS NULL`` and had its contribution reverted at the
    state transition.

    Kills: any fix shaped like ``if linked_row is None or not
    is_reciprocal_pair(...)`` -- link-shaped, so it closes TBD-292 and leaves
    TBD-302 entirely in place. ``contributes_to_cached_balance`` excludes this
    row via its RECONCILIATION-STATE branch, not its link branch.
    """
    seed = await _seed(db_session)
    row = await _create(
        db_session, seed, account_id=seed["acct_a_id"],
        amount="8.00", in_batch=True,
    )
    await _reconcile(
        db_session, seed, _transition(row.id, ReconciliationState.SKIPPED),
    )
    assert (await _reload(db_session, row.id)).linked_transaction_id is None
    before = (await _account(db_session, seed["acct_a_id"])).balance

    await transaction_service.update_transaction(
        db_session, seed["org_id"], row.id,
        TransactionUpdate(amount=Decimal("512.00")),
    )

    assert (await _account(db_session, seed["acct_a_id"])).balance == before
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_chain_link_row_edit_succeeds(db_session):
    """F7. ``A -> B -> C``, same org, every link non-null, nothing
    reciprocal. ``_apply_match`` Guard 1 documents this as a SUPPORTED flow.

    Pins that the fix keys on MUTUALITY, not on a reconciliation-shaped
    heuristic ("has an import_batch_id", "state == matched", ...): ``A``'s
    link target ``B`` itself links onward, so any guard that reads "the
    partner must link back at me" still refuses this row.
    """
    seed = await _seed(db_session)
    b, c = await _make_matched_pair(db_session, seed, amount="4.00")
    # A is matched AT B, which already links onward at C: the A -> B -> C
    # chain, every link non-null, no link reciprocal.
    a, _b_again = await _make_matched_pair(
        db_session, seed, amount="2.00", canonical=b,
    )
    a = await _reload(db_session, a.id)
    b = await _reload(db_session, b.id)
    assert a.linked_transaction_id == b.id
    assert b.linked_transaction_id == c.id
    assert c.linked_transaction_id is None

    result = await transaction_service.update_transaction(
        db_session, seed["org_id"], a.id,
        TransactionUpdate(description="chain head renamed"),
    )
    assert result.description == "chain head renamed"
    await assert_invariant(db_session, seed)


# ══ TBD-292 self-link ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_edit_of_a_self_linked_row_moves_the_balance_exactly_once(db_session):
    """F17. A self-linked row is corrupt data containing exactly ONE row.

    ``is_reciprocal_pair`` excludes self-links (fail CLOSED: one row is not a
    pair), but ``contributes_to_cached_balance`` KEEPS them (fail OPEN: the
    amount really is inside ``accounts.balance``). Same column, two questions,
    two polarities.

    Kills: reusing ``is_reciprocal_pair`` for the BALANCE decision -- exactly
    the inversion ``transaction_filters`` warns about in
    ``contributes_to_cached_balance``'s docstring. That mutant freezes this
    row's balance and this test goes red.
    """
    seed = await _seed(db_session)
    row = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount="64.00",
    )
    # No writer produces a self-link, so it has to be forged here. It stays
    # inside the FK (the row exists), so foreign_keys=ON is satisfied.
    row.linked_transaction_id = row.id
    await db_session.commit()

    before = (await _account(db_session, seed["acct_a_id"])).balance
    await transaction_service.update_transaction(
        db_session, seed["org_id"], row.id,
        TransactionUpdate(amount=Decimal("100.00")),
    )
    # EXPENSE: -64 reverted, -100 applied => balance falls by exactly 36.
    assert (await _account(db_session, seed["acct_a_id"])).balance == before - Decimal("36.00")
    await assert_invariant(db_session, seed)


# ══ TBD-294: deleting the canonical twin demotes the duplicate ══════════════


@pytest.mark.asyncio
async def test_deleting_canonical_demotes_matched_duplicate(db_session):
    """⭐ F9. Kills: doing nothing (the orphan re-enters both filters), AND
    a "re-apply the orphan's amount to the balance" implementation -- the
    exact-delta clause below is what separates the two."""
    seed = await _seed(db_session)
    dup, canonical = await _make_matched_pair(
        db_session, seed, amount="256.00",
    )
    b_before = (await _account(db_session, seed["acct_b_id"])).balance

    demoted = await transaction_service.delete_transaction(
        db_session, seed["org_id"], canonical.id,
    )

    assert demoted == [dup.id]
    orphan = await _reload(db_session, dup.id)
    assert orphan is not None, "the duplicate must survive its twin's delete"
    assert orphan.reconciliation_state == "rejected"
    # SET NULL really fired -- the discriminator column IS gone, which is why
    # a state write was the only durable place to put it.
    assert orphan.linked_transaction_id is None
    # The canonical row was an EXPENSE of 256 on Acct B: its delete must move
    # Acct B by EXACTLY +256 and nothing else.
    assert (await _account(db_session, seed["acct_b_id"])).balance == b_before + Decimal("256.00")
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_accepted_state_referrer_is_also_demoted(db_session):
    """⭐⭐ F10. The MAJORITY population: a matched row is normally ACCEPTED
    immediately afterwards.

    Kills: any fix keyed on ``reconciliation_state == 'matched'``. The link
    survives MATCHED -> ACCEPTED untouched, so the hole is wide open on the
    accepted class and a 'matched'-keyed fix looks correct on F9 alone.
    """
    seed = await _seed(db_session)
    dup, canonical = await _make_matched_pair(
        db_session, seed, amount="128.00",
    )
    await _reconcile(
        db_session, seed, _transition(dup.id, ReconciliationState.ACCEPTED),
    )
    assert (await _reload(db_session, dup.id)).reconciliation_state == "accepted"
    batch_before = await db_session.scalar(
        select(ImportBatch.accepted_count).where(ImportBatch.id == seed["batch_id"])
    )

    demoted = await transaction_service.delete_transaction(
        db_session, seed["org_id"], canonical.id,
    )

    assert demoted == [dup.id]
    assert (await _reload(db_session, dup.id)).reconciliation_state == "rejected"
    # Counter bookkeeping: the row left the accepted class.
    after = await db_session.scalar(
        select(ImportBatch.accepted_count).where(ImportBatch.id == seed["batch_id"])
    )
    assert after == batch_before - 1
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_pending_review_referrer_demotion_decrements_pending_count_and_closes_batch(
    db_session,
):
    """F11. The subtle one.

    ``ACCEPTED -> PENDING_REVIEW`` is a legal reopen, and ``_apply_match``
    Guard 2 states outright that nothing clears the link on reopen. So a
    PENDING_REVIEW row carrying a live one-way link is a DOCUMENTED,
    supported state -- and demoting it moves it OUT of ``PENDING_STATES``.

    Kills: decrementing only ``accepted_count``. The batch's
    ``pending_count`` then never reaches zero and the batch is stranded OPEN
    forever with no row left that can move it.
    """
    seed = await _seed(db_session)
    # The filler goes into the batch FIRST so pending_count never touches zero
    # before the reopen -- an auto-close mid-scenario would leave the batch
    # CLOSED with a pending row and test the wrong thing.
    filler = await _create(
        db_session, seed, account_id=seed["acct_a_id"],
        amount="1.00", in_batch=True,
    )
    dup, canonical = await _make_matched_pair(
        db_session, seed, amount="64.00",
    )
    await _reconcile(db_session, seed, _transition(dup.id, ReconciliationState.ACCEPTED))
    await _reconcile(
        db_session, seed, _transition(dup.id, ReconciliationState.PENDING_REVIEW),
    )
    await _reconcile(
        db_session, seed, _transition(filler.id, ReconciliationState.ACCEPTED),
    )
    batch = await db_session.scalar(
        select(ImportBatch)
        .where(ImportBatch.id == seed["batch_id"])
        .execution_options(populate_existing=True)
    )
    assert (await _reload(db_session, dup.id)).reconciliation_state == "pending_review"
    assert batch.pending_count == 1
    assert batch.status == ImportBatchStatus.OPEN

    await transaction_service.delete_transaction(
        db_session, seed["org_id"], canonical.id,
    )

    batch = await db_session.scalar(
        select(ImportBatch)
        .where(ImportBatch.id == seed["batch_id"])
        .execution_options(populate_existing=True)
    )
    assert (await _reload(db_session, dup.id)).reconciliation_state == "rejected"
    assert batch.pending_count == 0
    assert batch.status == ImportBatchStatus.CLOSED
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_pending_matched_orphan_moves_no_money(db_session):
    """F12. A PENDING row's amount was never in ``accounts.balance``.
    Demoting it must not invent money in either direction."""
    seed = await _seed(db_session)
    dup, canonical = await _make_matched_pair(
        db_session, seed, amount="32.00",
        dup_status=TransactionStatus.PENDING,
    )
    a_before = (await _account(db_session, seed["acct_a_id"])).balance
    b_before = (await _account(db_session, seed["acct_b_id"])).balance

    await transaction_service.delete_transaction(
        db_session, seed["org_id"], canonical.id,
    )

    assert (await _reload(db_session, dup.id)).reconciliation_state == "rejected"
    assert (await _account(db_session, seed["acct_a_id"])).balance == a_before
    # Only the canonical EXPENSE of 32 comes back to Acct B.
    assert (await _account(db_session, seed["acct_b_id"])).balance == b_before + Decimal("32.00")
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_reciprocal_partner_delete_still_cascades_and_demotes_nothing(db_session):
    """F13. THE OVER-REACH FENCE for TBD-294. A reciprocal partner IS inside
    the cached balance and is already in the delete set; demoting it would be
    a state write on a row about to cease existing, and (worse) a fix keyed on
    "any inbound referrer" would demote a real transfer leg."""
    seed = await _seed(db_session)
    expense = await _create(
        db_session, seed, account_id=seed["acct_a_id"],
        amount="16.00", tx_type=TransactionType.EXPENSE,
    )
    income = await _create(
        db_session, seed, account_id=seed["acct_b_id"],
        amount="16.00", tx_type=TransactionType.INCOME,
    )
    await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"], expense_tx_id=expense.id, income_tx_id=income.id,
    )

    demoted = await transaction_service.delete_transaction(
        db_session, seed["org_id"], expense.id,
    )

    assert demoted == []
    # The documented transfer cascade is intact: both legs are gone.
    assert await _reload(db_session, expense.id) is None
    assert await _reload(db_session, income.id) is None
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_deleting_one_transfer_leg_demotes_a_referrer_of_the_other_leg(db_session):
    """F13b. Matching an imported row against a leg of a REAL transfer is a
    SUPPORTED flow -- ``_apply_match`` Guard 1 says so in as many words, and
    ``find_duplicate_of_linked_leg`` exists to surface it.

    So deleting either leg orphans a referrer of the OTHER leg, which the
    transfer cascade is about to delete too. Kills: probing inbound referrers
    of the requested id only (the shape this fence was written after a
    surviving mutant exposed it).
    """
    seed = await _seed(db_session)
    expense = await _create(
        db_session, seed, account_id=seed["acct_a_id"],
        amount="16.00", tx_type=TransactionType.EXPENSE, label="leg-e",
    )
    income = await _create(
        db_session, seed, account_id=seed["acct_b_id"],
        amount="16.00", tx_type=TransactionType.INCOME, label="leg-i",
    )
    await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"], expense_tx_id=expense.id, income_tx_id=income.id,
        recategorize=False,
    )
    # An imported duplicate matched against the INCOME leg.
    dup, _canonical = await _make_matched_pair(
        db_session, seed, amount="16.00", canonical=income,
    )

    # Delete via the EXPENSE leg: the cascade removes the income leg too.
    demoted = await transaction_service.delete_transaction(
        db_session, seed["org_id"], expense.id,
    )

    assert demoted == [dup.id]
    orphan = await _reload(db_session, dup.id)
    assert orphan is not None
    assert orphan.reconciliation_state == "rejected"
    assert orphan.linked_transaction_id is None
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_bulk_delete_of_both_rows_demotes_nothing(db_session):
    """F14. When M and T are BOTH in the delete set there is no orphan to
    demote -- M ceases to exist. Kills a helper that demotes before checking
    membership of the delete set."""
    seed = await _seed(db_session)
    dup, canonical = await _make_matched_pair(
        db_session, seed, amount="8.00",
    )

    deleted, skipped, demoted = await transaction_service.bulk_delete_transactions(
        db_session, seed["org_id"], [dup.id, canonical.id],
    )

    assert deleted == 2
    assert skipped == []
    assert demoted == []
    assert await _reload(db_session, dup.id) is None
    assert await _reload(db_session, canonical.id) is None
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_bulk_delete_demotes_too(db_session):
    """F15. This repo has shipped "fixed one, forgot the bulk sibling"
    before. Kills wiring the demotion into ``delete_transaction`` only."""
    seed = await _seed(db_session)
    dup, canonical = await _make_matched_pair(
        db_session, seed, amount="128.00",
    )

    deleted, skipped, demoted = await transaction_service.bulk_delete_transactions(
        db_session, seed["org_id"], [canonical.id],
    )

    assert deleted == 1
    assert demoted == [dup.id]
    orphan = await _reload(db_session, dup.id)
    assert orphan is not None
    assert orphan.reconciliation_state == "rejected"
    assert orphan.linked_transaction_id is None
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_org_wipe_does_not_demote(db_session):
    """F16 (ruling condition 4). The demotion is USER-INITIATED CRUD only.
    ``org_data_service.wipe_org_data`` issues an unbounded
    ``DELETE FROM transactions WHERE org_id = ...`` and never routes through
    ``delete_transaction`` -- if it did, a wipe would strand on rows demoting
    each other. This fence pins that separation so a future "unify the delete
    paths" refactor cannot quietly rejoin them.
    """
    from app.services import org_data_service

    seed = await _seed(db_session)
    dup, canonical = await _make_matched_pair(
        db_session, seed, amount="4.00",
    )

    await org_data_service.wipe_org_data(db_session, org_id=seed["org_id"])
    await db_session.commit()

    # Everything is gone: nothing survived to carry a demotion.
    assert await _reload(db_session, dup.id) is None
    assert await _reload(db_session, canonical.id) is None


# ══ TBD-295: promote-to-recurring ═══════════════════════════════════════════


def _promote_body() -> PromoteToRecurringRequest:
    return PromoteToRecurringRequest(
        frequency="monthly", next_due_date=date(2026, 6, 10), auto_settle=False,
    )


@pytest.mark.asyncio
async def test_promote_still_refuses_a_matched_row(db_session):
    """F18a. The refusal STAYS. This guard is not asking "is this a transfer
    leg" -- it is asking "may this row seed a repeating series", and a row
    asserting *I am the same event as another row* must not. Kills applying
    the mutuality rule reflexively here."""
    seed = await _seed(db_session)
    dup, _canonical = await _make_matched_pair(
        db_session, seed, amount="2.00",
    )
    with pytest.raises(ValidationError) as exc:
        await transaction_service.promote_to_recurring(
            db_session, seed["org_id"], dup.id, _promote_body(), today=TX_DATE,
        )
    # And the MESSAGE stops lying: this row is not a transfer leg.
    assert "transfer leg" not in str(exc.value)


@pytest.mark.asyncio
async def test_promote_refuses_a_rejected_row(db_session):
    """F18b. RED against ``main``: a SKIPPED / REJECTED row's contribution was
    reverted, so seeding a series from it manufactures money the ledger says
    was never there. A TBD-294-demoted row is exactly this shape."""
    seed = await _seed(db_session)
    row = await _create(
        db_session, seed, account_id=seed["acct_a_id"],
        amount="8.00", in_batch=True,
    )
    await _reconcile(
        db_session, seed, _transition(row.id, ReconciliationState.REJECTED),
    )
    assert (await _reload(db_session, row.id)).linked_transaction_id is None

    with pytest.raises(ValidationError):
        await transaction_service.promote_to_recurring(
            db_session, seed["org_id"], row.id, _promote_body(), today=TX_DATE,
        )


@pytest.mark.asyncio
async def test_promote_still_refuses_a_transfer_leg(db_session):
    """F18c. The over-reach fence: a real transfer leg must still be
    refused."""
    seed = await _seed(db_session)
    expense = await _create(
        db_session, seed, account_id=seed["acct_a_id"],
        amount="16.00", tx_type=TransactionType.EXPENSE,
    )
    income = await _create(
        db_session, seed, account_id=seed["acct_b_id"],
        amount="16.00", tx_type=TransactionType.INCOME,
    )
    await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"], expense_tx_id=expense.id, income_tx_id=income.id,
    )
    with pytest.raises(ValidationError):
        await transaction_service.promote_to_recurring(
            db_session, seed["org_id"], expense.id, _promote_body(), today=TX_DATE,
        )


@pytest.mark.asyncio
async def test_promote_still_works_on_an_ordinary_row(db_session):
    """F18d. Over-reach in the other direction: widening the guard must not
    refuse an ordinary ACCEPTED row, which is every manually created one."""
    seed = await _seed(db_session)
    row = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount="8.00",
    )
    result = await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], row.id, _promote_body(), today=TX_DATE,
    )
    assert result.recurring_id is not None


# ══ TBD-292 companion sites ═════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_bulk_edit_applies_status_to_a_matched_row(db_session):
    """F24-backend. ``bulk_update_transactions`` read ``is_transfer`` off
    non-nullness, so a matched row in a batch edit silently dropped
    status / account / tags and reported "not applied to transfers" -- false
    on both counts. RED against ``main`` (the row lands in ``skipped``)."""
    seed = await _seed(db_session)
    dup, _canonical = await _make_matched_pair(
        db_session, seed, amount="16.00",
    )
    before = (await _account(db_session, seed["acct_a_id"])).balance

    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, seed["org_id"], [dup.id], status="pending", actor_user_id=1,
    )

    assert skipped == []
    assert updated == 1
    assert (await _reload(db_session, dup.id)).status == TransactionStatus.PENDING
    assert (await _account(db_session, seed["acct_a_id"])).balance == before
    await assert_invariant(db_session, seed)


@pytest.mark.asyncio
async def test_bulk_edit_still_refuses_status_on_a_real_transfer_leg(db_session):
    """F24-backend over-reach fence: a genuine transfer leg must still be
    skipped for status. Without it, ``is_transfer = False`` passes the fence
    above."""
    seed = await _seed(db_session)
    expense = await _create(
        db_session, seed, account_id=seed["acct_a_id"],
        amount="16.00", tx_type=TransactionType.EXPENSE,
    )
    income = await _create(
        db_session, seed, account_id=seed["acct_b_id"],
        amount="16.00", tx_type=TransactionType.INCOME,
    )
    await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"], expense_tx_id=expense.id, income_tx_id=income.id,
    )

    updated, skipped = await transaction_service.bulk_update_transactions(
        db_session, seed["org_id"], [expense.id], status="pending", actor_user_id=1,
    )

    assert updated == 0
    assert [i for i, _ in skipped] == [expense.id]
    assert "not applied to transfers" in skipped[0][1]
    assert (await _reload(db_session, expense.id)).status == TransactionStatus.SETTLED


@pytest.mark.asyncio
async def test_matched_duplicate_does_not_lock_its_category_type(db_session):
    """F-cat. ``category_service._has_transfer_leg_reference`` counted
    non-nullness, so ONE matched duplicate hard-locked its category at
    ``BOTH`` with the message "referenced by a transfer pair" -- false on both
    counts, and unfixable from the UI.

    RED against ``main`` with that ValidationError.
    """
    from app.services import category_service

    seed = await _seed(db_session)
    await _make_matched_pair(
        db_session, seed, amount="4.00",
    )
    cat = await db_session.scalar(
        select(Category).where(Category.id == seed["cat_id"])
    )
    assert cat.type == CategoryType.BOTH

    await category_service.validate_category_type_change(
        db_session, cat, CategoryType.EXPENSE,
    )


@pytest.mark.asyncio
async def test_real_transfer_pair_still_locks_its_category_type(db_session):
    """F-cat over-reach fence: a genuine transfer pair must still lock the
    category at ``BOTH``. Without it, deleting the check entirely passes the
    fence above."""
    from app.services import category_service

    seed = await _seed(db_session)
    expense = await _create(
        db_session, seed, account_id=seed["acct_a_id"],
        amount="16.00", tx_type=TransactionType.EXPENSE,
    )
    income = await _create(
        db_session, seed, account_id=seed["acct_b_id"],
        amount="16.00", tx_type=TransactionType.INCOME,
    )
    # recategorize=False keeps both legs on the seeded BOTH category, which is
    # the category under test. With the default the pair is moved to the
    # system Transfers category and this fence would assert about the wrong row.
    await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"], expense_tx_id=expense.id, income_tx_id=income.id,
        recategorize=False,
    )
    cat = await db_session.scalar(
        select(Category).where(Category.id == seed["cat_id"])
    )

    with pytest.raises(ValidationError) as exc:
        await category_service.validate_category_type_change(
            db_session, cat, CategoryType.EXPENSE,
        )
    assert "transfer pair" in str(exc.value)


@pytest.mark.asyncio
async def test_self_linked_row_does_not_lock_its_category_type(db_session):
    """F-cat polarity fence. ``_has_transfer_leg_reference`` asks "are these
    two rows ONE transfer pair?", so it must FAIL CLOSED and exclude
    self-links -- matching ``_transfer_collapse_clause``, NOT the frozen
    ``balance_contribution_filter``, which asks a different question and
    deliberately KEEPS self-links.

    Kills copying the frozen SQL filter's EXISTS verbatim (it has no
    not-self clause, so a self-linked row would lock the category).
    """
    from app.services import category_service

    seed = await _seed(db_session)
    row = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount="4.00",
    )
    row.linked_transaction_id = row.id
    await db_session.commit()
    cat = await db_session.scalar(
        select(Category).where(Category.id == seed["cat_id"])
    )

    await category_service.validate_category_type_change(
        db_session, cat, CategoryType.EXPENSE,
    )
