"""TBD-308 -- skipping a transfer leg must revert its contribution, and the
surviving partner's edits must not move an account that does not hold the
skipped leg's amount.

ROOT CAUSE (one, two halves): balance bookkeeping was keyed on a *reports*
predicate.

* ``_apply_balance_for_transition`` derived its revert/apply action from
  ``is_reportable_transaction``, which ANDs ``linked_transaction_id is None``
  and is therefore False for EVERY linked row. A reciprocal transfer leg
  transitioning to SKIPPED gave a ``False -> False`` diff and reverted nothing,
  while ``balance_contribution_filter``'s state clause dropped that row from the
  reconstruction the instant it read ``skipped``. Permanent drift.
* Arms 4b / 4f in ``update_transaction`` were ungated, so editing the surviving
  partner of a leg whose amount is NOT inside ``accounts.balance`` reverted an
  amount that was never there and applied the new one.

REACHABILITY of the second half is via **skip-then-pair**, not pair-then-skip:
``find_match_candidates`` and ``_link_pair`` carry no ``reconciliation_state``
term, so a SKIPPED row (contribution already correctly reverted) is an ordinary
"Mark as transfer" candidate. SKIPPED is terminal, so that row can never be
reconciled again -- which is why no guard on the reconcile path can reach it.

EVERY fence asserts the INVARIANT via the production primitive
``transaction_service.reconcile_account`` -- ``stored == computed`` under
``balance_contribution_filter()`` -- never a hand-computed number. A fence that
asserts arithmetic records the item; one that asserts the invariant records the
path.

Fence roster (see specs/tbd-308-skip-transfer-leg-balance-revert.md):

* **F1** was INVERTED by TBD-385 and no longer kills what this line used to
  advertise. It now kills a missing, narrowed or mis-keyed REFUSAL.

  ⚠⚠ MEASURED COVERAGE LOSS, recorded rather than left to be discovered.
  F1 used to kill ``main``'s ``is_reportable_transaction`` derivation in
  ``_apply_balance_for_transition``. Re-injecting that derivation at BOTH sites
  now passes this file (9 passed) AND the whole of ``backend/tests/services/``
  (1908 passed). It is no longer exercised by any test in the services suite.

  SAFE, because unreachable rather than merely untested: post-guard a reciprocal
  leg can only move ACCEPTED<->PENDING_REVIEW, where the old and new predicates
  are both no-ops (False->False vs True->True), and
  ``_settle_batch_counters_and_demote_orphans`` excludes reciprocal referrers,
  so no path puts a reciprocal row into a reverted state.

  NOT fully re-pinned. ``test_link_reciprocity_predicates.py`` still pins the
  predicate FUNCTIONS' parity, but ``reconciliation_service``'s USE of
  ``contributes_to_cached_balance`` is unfenced for reciprocal rows: a refactor
  swapping it back goes green. Do not cite F1 as covering that any more.
* **F2** kills ungated 4b/4f, and pins the skip-then-pair ROUTE by building it
  through the real service functions rather than hand-writing state.
* **F3** is the over-reach pin: without it, hard-coding either gate to False
  passes F1 and F2.
* **F4** pins the DELIBERATE ABSENCE of a ``_link_pair`` state guard. SKIPPED is
  terminal, so refusing to pair a reverted row would strand a mis-skipped row
  with delete as its only exit (the TBD-295 closed loop). A future hygiene PR
  adding that guard goes RED here and must argue with the dead end.
* **F5** pins the stale ONE-WAY link case: it must keep behaving as today, and
  it is what goes red if the partner is passed as ``None`` (the predicate fails
  OPEN on an unresolvable partner, which would silently disable the match
  revert).
"""
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
from app.schemas.transaction import TransactionCreate, TransactionUpdate
from app.services import reconciliation_service, transaction_service
from app.services.exceptions import ValidationError

ACCT_A_OPENING = Decimal("1000.00")
ACCT_B_OPENING = Decimal("400.00")
ACCT_C_OPENING = Decimal("250.00")

TX_DATE = date(2026, 5, 10)


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


async def _seed(db: AsyncSession, *, anchor_id: int = 8000) -> dict:
    org = Organization(name="Org 308", billing_cycle_day=1)
    db.add(org)
    await db.flush()

    user = User(
        username="seed_308", email="u-308@example.com", password_hash="x",
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

    cat = Category(org_id=org.id, name="Shared", slug="shared", type=CategoryType.BOTH)
    db.add(cat)
    await db.flush()

    batch = ImportBatch(
        org_id=org.id, account_id=accts["a"].id,
        source_format=ImportSourceFormat.CSV, file_name="seed.csv",
        created_by_user_id=user.id, status=ImportBatchStatus.OPEN,
        row_count=0, accepted_count=0, pending_count=0,
    )
    db.add(batch)

    # ID ANCHOR: keep every fixture id away from ``1``, where a wrong lookup and
    # a right one are indistinguishable. PENDING and on a third account, so it
    # contributes to no reconstruction.
    db.add(
        Transaction(
            id=anchor_id, org_id=org.id, account_id=accts["c"].id, category_id=cat.id,
            description="id-anchor", amount=Decimal("0.01"),
            type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
            date=TX_DATE,
        )
    )
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
    in_batch: bool = False,
) -> Transaction:
    """Create through the REAL create path so ``accounts.balance`` is applied by
    production code, and optionally enrol the row in the import batch."""
    tx = await transaction_service.create_transaction(
        db,
        seed["org_id"],
        TransactionCreate(
            account_id=account_id,
            category_id=seed["cat_id"],
            description=f"{label}-{amount}",
            amount=Decimal(amount),
            type=tx_type.value,
            status=TransactionStatus.SETTLED.value,
            date=TX_DATE,
            settled_date=TX_DATE,
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
                transaction_id=tx_id, to_state=state, match_with_transaction_id=match,
            )
        ]
    )


async def _reconcile(db: AsyncSession, seed: dict, request: ReconcileBatchRequest):
    return await reconciliation_service.reconcile_request(
        db, org_id=seed["org_id"], batch_id=seed["batch_id"], request=request
    )


async def _account(db: AsyncSession, account_id: int) -> Account:
    acct = await db.scalar(select(Account).where(Account.id == account_id))
    await db.refresh(acct)
    return acct


async def _reload(db: AsyncSession, tx_id: int) -> Transaction | None:
    tx = await db.scalar(select(Transaction).where(Transaction.id == tx_id))
    if tx is not None:
        await db.refresh(tx)
    return tx


async def assert_invariant(db: AsyncSession, seed: dict) -> None:
    """``stored == computed`` for EVERY account, via the production primitive
    ``reconcile_account`` -- the same one ``/accounts/{id}/reconcile`` serves,
    gated on ``balance_contribution_filter()`` on both subqueries."""
    for key in ("acct_a_id", "acct_b_id", "acct_c_id"):
        acct = await _account(db, seed[key])
        stored, computed, ok = await transaction_service.reconcile_account(
            db, seed["org_id"], acct
        )
        assert ok, (
            f"balance invariant broken on {acct.name}: "
            f"stored={stored} computed={computed}"
        )


async def _make_reciprocal_pair(
    db: AsyncSession, seed: dict, *, amount: str = "100.00", leg_in_batch: bool = False
) -> tuple[Transaction, Transaction]:
    """Build a REAL bidirectional transfer pair through the production pairing
    path: expense on A, income on B, linked by ``pair_existing_transactions``.

    ``leg_in_batch`` enrols the expense leg in the import batch BEFORE pairing,
    reproducing what ``import_service`` does when a CSV row is paired with an
    existing transaction -- the shape that reaches the inbox as a reciprocal
    leg.
    """
    expense = await _create(
        db, seed, account_id=seed["acct_a_id"], amount=amount, label="leg-exp",
        tx_type=TransactionType.EXPENSE, in_batch=leg_in_batch,
    )
    income = await _create(
        db, seed, account_id=seed["acct_b_id"], amount=amount, label="leg-inc",
        tx_type=TransactionType.INCOME,
    )
    await transaction_service.pair_existing_transactions(
        db, seed["org_id"], expense_tx_id=expense.id, income_tx_id=income.id,
    )
    await db.commit()
    return await _reload(db, expense.id), await _reload(db, income.id)


# ══ F1 -- the inbox refuses to revert a reciprocal leg ══════════════════


@pytest.mark.parametrize(
    "reverted_state",
    [ReconciliationState.SKIPPED, ReconciliationState.REJECTED],
    ids=["skipped", "rejected"],
)
@pytest.mark.asyncio
async def test_reverting_a_reciprocal_transfer_leg_is_refused(
    db_session, reverted_state
):
    """F1 (fence). Moving a genuine, bidirectionally-linked transfer leg into a
    REVERTED state through the inbox must be REFUSED, and must move no money.

    ⚠ THIS FENCE WAS INVERTED BY TBD-385, deliberately. The assertion it now
    makes is strictly STRONGER than the one it replaces, on the same path.

    Until TBD-385 this test asserted the revert HAPPENED correctly -- the leg's
    amount left ``accounts.balance``. That was the right assertion while the
    transition was permitted: TBD-308 had just fixed a ``False -> False`` no-op
    that left permanent drift. TBD-385 ruled the transition itself
    illegitimate, so the behaviour this fence guarded is no longer REACHABLE,
    and a fence asserting an unreachable behaviour is decoration.

    ⚠ "Reachability removed, not coverage lost" is only HALF true, and the other
    half is measured: the old derivation is now unfenced across all 1908 tests
    in ``tests/services/``. The module docstring's F1 entry states exactly what
    that does and does not leave pinned.

    WHY THE RULING: a half-skipped pair moves ``accounts.balance`` by the full
    leg amount while BOTH legs sit outside ``reportable_transaction_filter``
    (it requires ``linked_transaction_id IS NULL``). Net worth steps with no
    reportable row anywhere to explain it. The ticket's claim that
    ``unpair``-then-skip reaches the same state is FALSE:
    ``unpair_transactions`` NULLs both links, so the survivor becomes
    reportable and the movement is visible.

    KILLS:

    * A guard keyed on the literal ``"skipped"``. REJECTED is parametrized here
      for exactly that reason -- it is a first-class inbox transition, and a
      SKIPPED-only guard passes every other fence in this module.
    * A guard keyed on ``linked_transaction_id is not None`` instead of on
      mutuality. That shape ALSO refuses the stale one-way link F5 pins as
      legitimately skippable, so F1 and F5 together are the discriminator and
      NEITHER ALONE IS. Deleting or weakening F5 silently un-fences this.

    ⚠ NOT in the kill list, deliberately: "re-resolves the partner rather than
    reusing the resolved instance". That WAS claimed here and is FALSE --
    measured, the re-query runs pre-mutation with identical org scoping and so
    returns the same row; the mutant passes all 9 tests in this file. Reusing
    ``source_partner`` is a COST argument (one query) plus a
    guard-vs-bookkeeping consistency argument, not a behaviour any test can
    see. It is kept as defence in depth and is NOT test-killable -- the same
    footing on which ``transaction_service`` documents its own unkillable
    clause.
    * A refusal that fires but leaks a partial write. Both balances AND both
      states are asserted unchanged, so a guard placed after the state flip or
      after the balance bookkeeping goes red here.

    ⚠ NOT an over-reach control: it FAILS against ``main``, where the
    transition is permitted and the balance moves.
    """
    seed = await _seed(db_session)
    expense, income = await _make_reciprocal_pair(
        db_session, seed, amount="100.00", leg_in_batch=True
    )
    # Precondition: a real transfer pair, mutual in BOTH directions.
    assert expense.linked_transaction_id == income.id
    assert income.linked_transaction_id == expense.id
    await assert_invariant(db_session, seed)

    a_before = (await _account(db_session, seed["acct_a_id"])).balance
    b_before = (await _account(db_session, seed["acct_b_id"])).balance
    state_before = (await _reload(db_session, expense.id)).reconciliation_state

    with pytest.raises(ValidationError) as exc:
        await _reconcile(db_session, seed, _transition(expense.id, reverted_state))

    # The refusal must name the REMEDY, not merely say no. A bare "not allowed"
    # strands the user on a screen that offers no way forward.
    assert "unlink" in str(exc.value).lower()

    # Nothing moved: not the state, not either side's balance.
    assert (
        await _reload(db_session, expense.id)
    ).reconciliation_state == state_before
    assert (await _account(db_session, seed["acct_a_id"])).balance == a_before
    assert (await _account(db_session, seed["acct_b_id"])).balance == b_before
    await assert_invariant(db_session, seed)


# ══ F2 -- the compounding half, via the skip-then-pair route ════════════════


@pytest.mark.asyncio
async def test_editing_partner_of_a_skipped_leg_moves_no_money(db_session):
    """F2 (fence). Editing the surviving partner of a leg whose amount is NOT
    inside ``accounts.balance`` must not move that leg's account.

    KILLS: ungated arms 4b / 4f. Ungated, 4b reverts the partner's OLD amount
    from an account that never held it and 4f applies the NEW one, moving the
    account by the edit delta on every edit.

    THE ROUTE IS THE FINDING, so it is built through the real service functions:
    skip an UNLINKED imported row (its revert fires correctly), then pair it via
    the ordinary transfer path. ``find_match_candidates`` and ``_link_pair``
    carry no ``reconciliation_state`` term, so this is an ordinary user action.
    Hand-writing ``reconciliation_state`` onto a paired row would still go RED
    here, but would pin the ITEM rather than the PATH.
    """
    seed = await _seed(db_session)
    skipped = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount="100.00",
        label="dup", tx_type=TransactionType.EXPENSE, in_batch=True,
    )
    partner = await _create(
        db_session, seed, account_id=seed["acct_b_id"], amount="100.00",
        label="live", tx_type=TransactionType.INCOME,
    )

    # Step 1: skip it while UNLINKED. This revert is correct on main and must
    # stay correct -- it is what puts the row's amount outside accounts.balance.
    await _reconcile(
        db_session, seed, _transition(skipped.id, ReconciliationState.SKIPPED)
    )
    await assert_invariant(db_session, seed)

    # Step 2: pair it. Ordinary "Mark as transfer"; no state guard refuses it.
    await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"], expense_tx_id=skipped.id, income_tx_id=partner.id,
    )
    await db_session.commit()
    reloaded = await _reload(db_session, skipped.id)
    assert reloaded.reconciliation_state == "skipped"
    assert reloaded.linked_transaction_id == partner.id, "route precondition"
    await assert_invariant(db_session, seed)

    a_before = (await _account(db_session, seed["acct_a_id"])).balance

    # Step 3: edit the SURVIVING partner's amount. Arms 4b/4f fire on the
    # skipped leg's account unless gated.
    await transaction_service.update_transaction(
        db_session, seed["org_id"], partner.id,
        TransactionUpdate(amount=Decimal("150.00")),
    )

    assert (await _account(db_session, seed["acct_a_id"])).balance == a_before
    await assert_invariant(db_session, seed)


# ══ F3 -- the over-reach pin ════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_real_transfer_pair_still_mirrors_and_moves_both_balances(db_session):
    """F3 (guard). THE OVER-REACH FENCE. A genuine, unskipped transfer pair must
    still mirror the amount to its partner and still move BOTH accounts.

    Without this, hard-coding either new gate to ``False`` -- or widening the
    revert to "any linked row moves no money" -- passes F1 and F2 while
    freezing every legitimate transfer edit.
    """
    seed = await _seed(db_session)
    expense, income = await _make_reciprocal_pair(db_session, seed, amount="80.00")
    a_before = (await _account(db_session, seed["acct_a_id"])).balance
    b_before = (await _account(db_session, seed["acct_b_id"])).balance

    await transaction_service.update_transaction(
        db_session, seed["org_id"], expense.id,
        TransactionUpdate(amount=Decimal("120.00")),
    )

    assert (await _reload(db_session, income.id)).amount == Decimal("120.00")
    assert (await _account(db_session, seed["acct_a_id"])).balance == (
        a_before - Decimal("40.00")
    )
    assert (await _account(db_session, seed["acct_b_id"])).balance == (
        b_before + Decimal("40.00")
    )
    await assert_invariant(db_session, seed)


# ══ F4 -- the decision fence ════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_pairing_a_skipped_row_still_succeeds(db_session):
    """F4 (guard). Pairing a SKIPPED row is PERMITTED ON PURPOSE, and this fence
    exists to pin that decision.

    A ``reconciliation_state`` guard on ``_link_pair`` / ``find_match_candidates``
    was proposed to make reciprocal+reverted unreachable by construction, and
    was REJECTED: SKIPPED and REJECTED are terminal
    (``ALLOWED_TRANSITIONS[SKIPPED] = frozenset()``), so a row skipped by
    mistake could then never be paired and never un-skipped -- delete would be
    its only exit, which is exactly the closed loop TBD-295 documents.
    ``_settle_batch_counters_and_demote_orphans`` already refused a guard on that same ground.

    The state is arithmetically SAFE because arms 4b/4f are gated (F2) and the
    state clause keeps the leg out of the reconstruction. A future hygiene PR
    that adds the guard goes RED here and has to argue with the dead end.
    """
    seed = await _seed(db_session)
    skipped = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount="60.00",
        label="mis-skipped", tx_type=TransactionType.EXPENSE, in_batch=True,
    )
    partner = await _create(
        db_session, seed, account_id=seed["acct_b_id"], amount="60.00",
        label="live", tx_type=TransactionType.INCOME,
    )
    await _reconcile(
        db_session, seed, _transition(skipped.id, ReconciliationState.SKIPPED)
    )

    # The row is still OFFERED as a transfer candidate. This half of the fence
    # pins the REACHABILITY claim the whole ticket rests on: the skip-then-pair
    # route exists because ``find_match_candidates`` carries no
    # ``reconciliation_state`` term. Without this assertion, a future PR adding
    # such a filter would make the route unreachable from the UI while every
    # other fence here stayed green, and the "the route is the finding"
    # reasoning would rot silently.
    candidates = await transaction_service.find_match_candidates(
        db_session, seed["org_id"],
        source_type=TransactionType.INCOME,
        amount=Decimal("60.00"),
        account_id_excluded=seed["acct_b_id"],
        date=TX_DATE,
        currency="EUR",
    )
    assert skipped.id in {c.id for c in candidates}, (
        "a SKIPPED row must still surface as a transfer-pair candidate; "
        "if this fails, the skip-then-pair route is closed and the 4b/4f gate "
        "may no longer be reachable -- re-read the dead-end argument first"
    )

    await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"], expense_tx_id=skipped.id, income_tx_id=partner.id,
    )
    await db_session.commit()

    reloaded = await _reload(db_session, skipped.id)
    assert reloaded.linked_transaction_id == partner.id
    assert (await _reload(db_session, partner.id)).linked_transaction_id == skipped.id
    await assert_invariant(db_session, seed)


# ══ F6 -- the other side of the boundary ═══════════════════════════════════


@pytest.mark.asyncio
async def test_accepting_a_reciprocal_transfer_leg_moves_no_money(db_session):
    """F6 (guard). A reciprocal transfer leg transitioned to ACCEPTED -- a
    non-reverting state -- must move NO balance. Its amount was inside
    ``accounts.balance`` before and is still inside it after.

    KILLS: the SOURCE-ONLY asymmetric swap, i.e. computing the source snapshot
    with ``contributes_to_cached_balance`` while leaving the target on
    ``is_reportable_transaction``. That mutant answers ``True -> False`` here
    (True because the leg is reciprocal and unreverted; False because
    ``is_reportable`` is False for any linked row) and fires a SPURIOUS revert
    on a perfectly healthy transfer.

    F1 cannot catch it -- F1's transition ends in a reverted state, where both
    predicates agree on False, so the source-only mutant produces the right
    answer there by luck. A boundary pinned from one side is not pinned.
    """
    seed = await _seed(db_session)
    expense, income = await _make_reciprocal_pair(
        db_session, seed, amount="70.00", leg_in_batch=True
    )
    assert expense.linked_transaction_id == income.id
    assert income.linked_transaction_id == expense.id

    a_before = (await _account(db_session, seed["acct_a_id"])).balance
    b_before = (await _account(db_session, seed["acct_b_id"])).balance

    await _reconcile(
        db_session, seed, _transition(expense.id, ReconciliationState.ACCEPTED)
    )

    assert (await _account(db_session, seed["acct_a_id"])).balance == a_before
    assert (await _account(db_session, seed["acct_b_id"])).balance == b_before
    await assert_invariant(db_session, seed)


# ══ F5 -- the stale one-way link pin ════════════════════════════════════════


@pytest.mark.asyncio
async def test_stale_one_way_link_after_reopen_still_skips_and_moves_no_money(
    db_session,
):
    """F5 (guard). A row carrying a STALE ONE-WAY link after
    ``MATCHED -> ACCEPTED -> PENDING_REVIEW`` must keep behaving exactly as
    today: it stays skippable, and its skip moves NO balance, because its
    contribution was already reverted at match time. Nothing clears the link on
    reopen, which is documented and deliberate.

    KILLS: passing ``None`` as the partner into the new predicate.
    ``contributes_to_cached_balance`` FAILS OPEN on an unresolvable partner, so
    a ``None`` would answer True here and drive a SECOND revert on a row whose
    amount is already out of the balance. It equally kills any blanket
    ``linked_transaction_id is not None`` treatment, which would refuse or
    re-revert this legitimately reopened row.

    ⚠⚠ SINCE TBD-385 THIS FENCE IS HALF OF A PAIR -- do not weaken or delete it
    without reading F1. F1 proves the inbox REFUSES a reciprocal leg; this one
    proves it still PERMITS a one-way link. A guard written as
    ``linked_transaction_id is not None`` satisfies F1 and is caught ONLY here.
    Measured: injecting that mutant turns this test, and no other in the
    module, red. F1 alone cannot see it; neither can F8.
    """
    seed = await _seed(db_session)
    duplicate = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount="45.00",
        label="dup", tx_type=TransactionType.EXPENSE, in_batch=True,
    )
    canonical = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount="45.00",
        label="canonical", tx_type=TransactionType.EXPENSE,
    )

    # Match reverts the duplicate's contribution (it is a duplicate of a charge
    # already recorded by the canonical row).
    await _reconcile(
        db_session, seed,
        _transition(duplicate.id, ReconciliationState.MATCHED, match=canonical.id),
    )
    await assert_invariant(db_session, seed)

    # Reopen. The one-way link SURVIVES -- that is the documented behaviour.
    await _reconcile(
        db_session, seed, _transition(duplicate.id, ReconciliationState.ACCEPTED)
    )
    await _reconcile(
        db_session, seed, _transition(duplicate.id, ReconciliationState.PENDING_REVIEW)
    )
    reopened = await _reload(db_session, duplicate.id)
    assert reopened.linked_transaction_id == canonical.id, "route precondition"
    assert (
        await _reload(db_session, canonical.id)
    ).linked_transaction_id is None, "link must be ONE-WAY, not reciprocal"

    a_before = (await _account(db_session, seed["acct_a_id"])).balance

    await _reconcile(
        db_session, seed, _transition(duplicate.id, ReconciliationState.SKIPPED)
    )

    # Already reverted at match time; skipping must NOT revert a second time.
    assert (await _account(db_session, seed["acct_a_id"])).balance == a_before
    await assert_invariant(db_session, seed)


# ── TBD-363: the ticket's literal public-endpoint route, driven end to end ──


async def _create_in_batch_accepted(
    db: AsyncSession,
    seed: dict,
    *,
    account_id: int,
    amount: str,
    label: str,
    tx_type: TransactionType,
) -> Transaction:
    """Enrol a row in the import batch WITHOUT touching ``reconciliation_state``.

    Imported rows land at the model default ``accepted`` -- ``reconciliation_service``
    states it in terms: "committed rows land ACCEPTED, so the batch opens fully
    accepted with zero pending".

    ⚠ This helper exists because ``_create(in_batch=True)`` FORCES
    ``pending_review`` by direct assignment, so a test using it cannot cross
    the ``ACCEPTED -> PENDING_REVIEW`` reopen edge at all.

    ⚠ CORRECTED CLAIM. An earlier revision said "every fence in this file skips
    the reopen edge". That is FALSE and was caught in review: F5
    (``test_stale_one_way_link_after_reopen_still_skips_and_moves_no_money``)
    drives MATCHED -> ACCEPTED -> PENDING_REVIEW -> SKIPPED through
    ``_reconcile``, and repo-wide
    ``test_reconciliation_service.py::test_reopen_from_skipped_reapplies_balance``
    has crossed it since before TBD-308.

    The true and narrower claim: no fence had crossed the reopen edge **with a
    RECIPROCAL row**. F5 crosses it with a ONE-WAY link, whose source
    membership is already False, so a mutant at that edge is a False -> False
    no-op there; the other crosses it with an UNLINKED row. A reciprocal row is
    the only shape with a contribution to lose at that edge, which is what
    makes the cell below worth a test.
    """
    tx = await _create(
        db, seed, account_id=account_id, amount=amount, label=label, tx_type=tx_type
    )
    tx.import_batch_id = seed["batch_id"]
    batch = await db.scalar(
        select(ImportBatch).where(ImportBatch.id == seed["batch_id"])
    )
    batch.row_count += 1
    batch.accepted_count += 1
    await db.commit()
    return tx


async def test_f7_import_pair_reopen_skip_moves_no_money(db_session):
    """F7 -- TBD-363's repro, through the PUBLIC path, all three legs.

    TBD-363 claimed a residual defect: a manually-paired transfer leg inside an
    import batch, then skipped, would leave its amount in ``accounts.balance``
    while dropping out of ``computed`` -- reporting drift the other way round.

    It was filed 2026-08-10 inside TBD-303's commit body as a known residual
    and closed in substance by TBD-308 two days later, which switched
    ``_apply_balance_for_transition`` from ``is_reportable_transaction`` to
    ``contributes_to_cached_balance``. The ticket's cited line numbers all
    resolve against the pre-TBD-308 tree.

    ⚠ WHY THIS FENCE EXISTS ANYWAY, stated at the width it actually holds.
    F1 covers the reciprocal-leg revert but reaches ``pending_review`` by
    DIRECT ASSIGNMENT, so "the reopen edge is membership-neutral, therefore F1
    covers TBD-363's route" was an ARGUMENT rather than a measurement -- and
    this repo's rule is that a fence drives the path the app takes, not the
    shortest path in.

    ⚠ It is NOT true that no fence crosses the reopen edge (F5 does, with a
    one-way link; ``test_reopen_from_skipped_reapplies_balance`` does, with an
    unlinked row). What no fence crossed is that edge with a RECIPROCAL row --
    the only link shape whose contribution is inside the cached balance at that
    point, and therefore the only one with anything to lose.

    MEASURED, and this is the fence's justification: a mutant that makes the
    reopen non-neutral for LINKED rows only --

        if target_state == PENDING_REVIEW and tx.linked_transaction_id is not None:
            target_in_cached_balance = False

    -- is caught by this test and by NOTHING ELSE across the whole reconcile
    surface (10 files, 175 tests, F1/F5/F6 and
    test_reopen_from_skipped_reapplies_balance included). F5 cannot see it
    because a one-way link is already False -> False there. This
    test drives all three legs through their real entrypoints:

      1. two settled rows enrolled in batch B at the model default ``accepted``
      2. ``pair_existing_transactions`` -- the service behind
         ``POST /api/v1/transactions/pair``
      3. ``reconcile_request`` twice: ACCEPTED -> PENDING_REVIEW, then
         PENDING_REVIEW -> SKIPPED

    Kills: a future edit that makes the reopen edge NON-neutral for a
    reciprocal row -- e.g. re-deriving the source snapshot after the state flip
    instead of before it. No other fence anywhere sees that, for the reason
    above: every other reopen-crossing test uses a one-way or unlinked row.

    ⚠ TBD-363's DoD item 3 ("a control proving TBD-303's fences still hold: an
    account with a settled matched row still reports is_consistent = True") is
    NOT delivered here and does not need to be -- it is already satisfied by
    F5, which drives a real MATCHED transition then asserts the invariant, and
    by ``test_matched_row_actions.py``, which asserts the equation directly.
    Recorded rather than left silent, per the standing rule to check every DoD
    item before closing.
    """
    seed = await _seed(db_session)

    exp = await _create_in_batch_accepted(
        db_session, seed, account_id=seed["acct_a_id"], amount="300.00",
        label="imported-out", tx_type=TransactionType.EXPENSE,
    )
    inc = await _create_in_batch_accepted(
        db_session, seed, account_id=seed["acct_b_id"], amount="300.00",
        label="imported-in", tx_type=TransactionType.INCOME,
    )
    await assert_invariant(db_session, seed)

    # Preconditions, asserted rather than assumed. If the rows do not actually
    # land ACCEPTED the reopen below is not the edge under test and the fence
    # silently becomes a duplicate of F1.
    assert exp.reconciliation_state == "accepted"
    assert inc.reconciliation_state == "accepted"
    assert exp.import_batch_id == seed["batch_id"]

    a_before = (await _account(db_session, seed["acct_a_id"])).balance

    # Leg 2: pair them. No `reconciliation_state` or `import_batch_id` guard
    # exists here, deliberately (TBD-308 spec) -- so this succeeds and the pair
    # becomes bidirectional while still inside the open batch.
    await transaction_service.pair_existing_transactions(
        db_session, seed["org_id"], expense_tx_id=exp.id, income_tx_id=inc.id
    )
    exp = await _reload(db_session, exp.id)
    inc = await _reload(db_session, inc.id)
    assert exp.linked_transaction_id == inc.id
    assert inc.linked_transaction_id == exp.id
    await assert_invariant(db_session, seed)

    # Leg 3a: the REOPEN edge -- the one no other fence in this file crosses.
    await _reconcile(
        db_session, seed, _transition(exp.id, ReconciliationState.PENDING_REVIEW)
    )
    exp = await _reload(db_session, exp.id)
    assert exp.reconciliation_state == "pending_review"
    # Membership-neutral for a reciprocal row: nothing may have moved yet.
    assert (await _account(db_session, seed["acct_a_id"])).balance == a_before
    await assert_invariant(db_session, seed)

    # Leg 3b: the skip -- REFUSED since TBD-385, because ``exp`` is now one leg
    # of a reciprocal pair. Before TBD-385 this reverted the expense out of
    # ``accounts.balance``; that transition is no longer legitimate, because it
    # would move the balance while BOTH legs stay outside
    # ``reportable_transaction_filter``, stepping net worth with nothing to
    # explain it.
    #
    # ⚠ THE FENCE'S ORIGINAL PURPOSE IS UNCHANGED AND STILL LIVE. What makes
    # F7 unique is leg 3a -- the REOPEN edge crossed with a RECIPROCAL row,
    # the only link shape with a contribution to lose there. That assertion is
    # untouched above and still kills the mutant nothing else sees:
    #
    #     if target_state == PENDING_REVIEW and tx.linked_transaction_id is not None:
    #         target_in_cached_balance = False
    #
    # Only leg 3b was inverted; leg 3a is untouched and still runs.
    #
    # ⚠ An earlier draft of this comment claimed the guard's POSITION inside
    # ``_reconcile_one`` was what kept leg 3a executing. That was wrong and is
    # recorded here so it is not re-derived: the guard's first conjunct is
    # ``target_state in REVERTED_RECONCILIATION_STATES``, and leg 3a targets
    # PENDING_REVIEW, so leg 3a runs wherever the guard sits. The actual
    # constraint on the guard's position is different and narrower --
    # ``source_partner`` is not resolved until just above it, so the guard
    # cannot move to the top of the function without re-resolving the partner,
    # which is the documented fail-open trap.
    a_after_reopen = (await _account(db_session, seed["acct_a_id"])).balance

    with pytest.raises(ValidationError) as exc:
        await _reconcile(
            db_session, seed, _transition(exp.id, ReconciliationState.SKIPPED)
        )
    assert "unlink" in str(exc.value).lower()

    exp = await _reload(db_session, exp.id)
    assert exp.reconciliation_state == "pending_review"
    assert (await _account(db_session, seed["acct_a_id"])).balance == a_after_reopen

    # The claim TBD-363 said would fail. Asserted through the production
    # primitive, for every account, not by hand-computing a number.
    await assert_invariant(db_session, seed)


# ══ F8 -- the wire signal discriminates mutuality, not linkedness ═══════════


@pytest.mark.asyncio
async def test_batch_detail_flags_only_reciprocal_legs(db_session):
    """F8 (fence). ``ReconciliationRow.is_reciprocal_transfer_leg`` must be True
    for a REAL transfer leg and False for a STALE ONE-WAY link, in the same
    batch, in the same response.

    WHY THE CLIENT CANNOT COMPUTE THIS: the partner is usually outside the
    batch and therefore absent from the payload, and the row's own
    ``linked_transaction_id`` -- which IS on the DTO -- is not an answer, since
    it has three writers and only one of them makes a transfer.

    ⚠ "usually", not "always". A batch row's ``account_id`` is not constrained
    to the batch header's, so an in-batch reciprocal pair is constructible --
    F7 in this very module builds one. Mutuality is a server fact regardless.

    KILLS, and this is the whole point of building shapes 1 and 2 into ONE batch
    (shape 3, the plain unlinked row, is a redundant CONTROL -- shape 2 already
    kills everything it kills): an implementation that sets the flag from
    ``linked_transaction_id is not None``. That mutant returns True for BOTH
    the transfer leg and the reopened match, so the client would hide Skip on a
    row the server happily skips -- offering the user no legal action at all on
    a row that has one. A fence containing only the transfer leg cannot see it,
    because the mutant agrees with the truth on that row.

    ⚠ This is the client-side half of the F1/F5 discriminator. F1 proves the
    server refuses the reciprocal leg; F5 proves it permits the one-way row;
    F8 proves the WIRE tells them apart, which is what keeps the button set
    honest. Deleting any one of the three un-fences the other two.
    """
    seed = await _seed(db_session)

    # Shape 1: a genuine transfer pair, expense leg enrolled in the batch.
    expense, income = await _make_reciprocal_pair(
        db_session, seed, amount="100.00", leg_in_batch=True
    )

    # Shape 2: a STALE ONE-WAY link -- matched, then reopened. The link
    # survives the reopen; that is documented and deliberate.
    duplicate = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount="45.00",
        label="dup-f8", tx_type=TransactionType.EXPENSE, in_batch=True,
    )
    canonical = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount="45.00",
        label="canonical-f8", tx_type=TransactionType.EXPENSE,
    )
    await _reconcile(
        db_session, seed,
        _transition(duplicate.id, ReconciliationState.MATCHED, match=canonical.id),
    )
    await _reconcile(
        db_session, seed, _transition(duplicate.id, ReconciliationState.ACCEPTED)
    )
    # ⚠ Drive the REOPEN too. The docstring above calls this shape "matched,
    # then reopened", and until this line it was only "matched, then accepted".
    # The flag happens to be state-independent, so the assertion held either
    # way -- but a fence whose fixture does not match its own description is
    # how the next reader ends up trusting coverage that was never built.
    await _reconcile(
        db_session, seed,
        _transition(duplicate.id, ReconciliationState.PENDING_REVIEW),
    )

    # Shape 3: an ordinary unlinked row.
    plain = await _create(
        db_session, seed, account_id=seed["acct_a_id"], amount="12.00",
        label="plain-f8", tx_type=TransactionType.EXPENSE, in_batch=True,
    )
    await db_session.commit()

    detail = await reconciliation_service.get_batch_detail(
        db_session, org_id=seed["org_id"], batch_id=seed["batch_id"]
    )
    by_id = {r.transaction_id: r for r in detail.rows}

    # Preconditions, asserted rather than assumed: if shape 2 lost its link the
    # discriminator collapses and this fence silently becomes a duplicate of a
    # single-shape test.
    assert by_id[duplicate.id].linked_transaction_id == canonical.id
    assert by_id[expense.id].linked_transaction_id == income.id
    assert by_id[plain.id].linked_transaction_id is None

    assert by_id[expense.id].is_reciprocal_transfer_leg is True
    assert by_id[duplicate.id].is_reciprocal_transfer_leg is False
    assert by_id[plain.id].is_reciprocal_transfer_leg is False
