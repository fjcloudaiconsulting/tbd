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

* **F1** kills ``main``'s ``is_reportable_transaction`` derivation, and any
  ``not is_reciprocal_pair(...)`` lookalike substituted for the predicate.
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


# ══ F1 -- the root fix ══════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "reverted_state",
    [ReconciliationState.SKIPPED, ReconciliationState.REJECTED],
    ids=["skipped", "rejected"],
)
@pytest.mark.asyncio
async def test_reverting_a_reciprocal_transfer_leg_reverts_its_contribution(
    db_session, reverted_state
):
    """F1 (fence). Moving a genuine, bidirectionally-linked transfer leg into a
    REVERTED state through the inbox must revert that leg's amount out of
    ``accounts.balance``, because ``balance_contribution_filter`` drops it from
    the reconstruction the moment its state enters
    ``_RECON_EXCLUDED_STATES``.

    KILLS: ``main``'s ``is_reportable_transaction`` derivation in
    ``_apply_balance_for_transition``. That predicate ANDs
    ``linked_transaction_id is None``, so it is False for a linked row BOTH
    before and after the flip -- a ``False -> False`` no-op that reverts nothing
    while the row leaves the reconstruction. Also kills any ``not
    is_reciprocal_pair(...)`` lookalike, which answers about link shape rather
    than about cached-balance membership.

    ⚠ PARAMETRIZED OVER BOTH MEMBERS of ``_RECON_EXCLUDED_STATES``, deliberately
    and not for symmetry's sake: an implementation that special-cases the
    literal ``"skipped"`` INSIDE this code path -- rather than deferring to the
    shared tuple -- passes every SKIPPED-only fence in this module. The parity
    fences on the shared predicate cannot see that mutant either, because it
    never touches the shared predicate. REJECTED is not a hypothetical state:
    it is a first-class inbox transition AND what ``_demote_match_orphans``
    writes in production.
    """
    seed = await _seed(db_session)
    expense, income = await _make_reciprocal_pair(
        db_session, seed, amount="100.00", leg_in_batch=True
    )
    # Precondition: a real transfer pair, mutual in both directions.
    assert expense.linked_transaction_id == income.id
    assert income.linked_transaction_id == expense.id
    await assert_invariant(db_session, seed)

    a_before = (await _account(db_session, seed["acct_a_id"])).balance

    await _reconcile(db_session, seed, _transition(expense.id, reverted_state))

    assert (
        await _reload(db_session, expense.id)
    ).reconciliation_state == reverted_state.value
    # The leg's amount must leave the cached balance: it is an EXPENSE, so
    # reverting it raises the account.
    assert (await _account(db_session, seed["acct_a_id"])).balance == (
        a_before + Decimal("100.00")
    )
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
    ``_demote_match_orphans`` already refused a guard on that same ground.

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
