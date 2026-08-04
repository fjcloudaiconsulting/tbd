"""Delete paths vs link reciprocity (TBD-280 + TBD-293).

``delete_transaction`` and ``bulk_delete_transactions`` cascaded the
delete to the partner on ANY non-null ``linked_transaction_id``. That
column has a ONE-WAY writer (``reconciliation_service._apply_match``),
so deleting a reconcile-inbox row destroyed the unrelated canonical row
it had been matched against, and reverted that row's amount from an
account that still owed it.

TBD-293 is the second half: the balance revert was gated on ``tx``'s
status alone and then applied to BOTH legs, so a mixed-status transfer
pair reverted the wrong leg's money -- in both directions.

Fixture rules this file follows (design §6.0):

* A symmetric transfer pair nets to ZERO across accounts, so no
  assertion here is ever a cross-account or org-wide total. Every
  balance assertion is PER ACCOUNT, and the accounts start at DIFFERENT
  opening balances so a swapped attribution cannot hide.
* Amounts are distinct powers of two, so every subset sum is unique and
  a double revert is distinguishable from a single one.
* Assertions compare ID SETS, never counts: a count of 2 is satisfied by
  the wrong 2.
* No fixture relies on id ``1``; transaction ids are explicit, from 6001.
* The adversarial fixture is a one-way link between two rows satisfying
  EVERY transfer invariant except mutuality: opposite types, equal
  amounts, different accounts, same currency, same org.
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

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.services import transaction_service


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


# Distinct opening balances -- a revert attributed to the wrong account is
# visible because no two accounts start equal.
OPENING = {"A": Decimal("1000.00"), "B": Decimal("250.00"), "C": Decimal("77.00")}


async def _seed(db: AsyncSession) -> dict:
    org = Organization(name="Primary", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    accts = {}
    for name, opening in OPENING.items():
        acct = Account(
            org_id=org.id, name=f"Acct {name}", account_type_id=at.id,
            balance=opening, currency="EUR",
        )
        db.add(acct)
        accts[name] = acct
    cat = Category(
        org_id=org.id, name="Transfer", slug="transfer",
        type=CategoryType.BOTH, is_system=True,
    )
    db.add(cat)
    await db.commit()
    return {
        "org_id": org.id,
        "cat_id": cat.id,
        "acct": {k: v.id for k, v in accts.items()},
    }


async def _add(
    db: AsyncSession,
    seed: dict,
    *,
    tx_id: int,
    acct: str,
    amount: str,
    tx_type: TransactionType = TransactionType.EXPENSE,
    status: TransactionStatus = TransactionStatus.SETTLED,
    recon: str = "accepted",
) -> Transaction:
    tx = Transaction(
        id=tx_id,
        org_id=seed["org_id"],
        account_id=seed["acct"][acct],
        category_id=seed["cat_id"],
        description=f"row-{tx_id}",
        amount=Decimal(amount),
        type=tx_type,
        status=status,
        date=date(2026, 5, 1),
        settled_date=date(2026, 5, 1) if status == TransactionStatus.SETTLED else None,
        reconciliation_state=recon,
    )
    db.add(tx)
    await db.commit()
    return tx


async def _link(db: AsyncSession, src: Transaction, dst_id: int | None) -> None:
    src.linked_transaction_id = dst_id
    await db.commit()


async def _balance(db: AsyncSession, seed: dict, acct: str) -> Decimal:
    return await db.scalar(
        select(Account.balance).where(Account.id == seed["acct"][acct])
    )


async def _surviving(db: AsyncSession, ids: list[int]) -> set[int]:
    rows = await db.scalars(select(Transaction.id).where(Transaction.id.in_(ids)))
    return set(rows.all())


# ── F1 / F2: the one-way cascade ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_of_a_one_way_matched_row_spares_the_canonical_partner(db_session):
    """F1. Kills: ``linked_tx = rows.get(...)`` with no reciprocity test.

    The adversarial fixture: ``M -> T`` satisfies every transfer
    invariant except mutuality -- opposite types, equal amounts,
    different accounts, same currency, same org. Only the missing
    back-pointer distinguishes it from a real transfer, and that is
    exactly what ``_apply_match`` produces.
    """
    seed = await _seed(db_session)
    m = await _add(db_session, seed, tx_id=6001, acct="A", amount="8.00",
                   tx_type=TransactionType.EXPENSE, recon="matched")
    t = await _add(db_session, seed, tx_id=6002, acct="B", amount="8.00",
                   tx_type=TransactionType.INCOME)
    await _link(db_session, m, t.id)

    await transaction_service.delete_transaction(db_session, seed["org_id"], m.id)

    assert await _surviving(db_session, [m.id, t.id]) == {t.id}


@pytest.mark.asyncio
async def test_delete_of_a_one_way_matched_row_leaves_the_partner_balance_alone(
    db_session,
):
    """F2. Kills: the cascade fixed but the two-account revert restored.

    ``T`` is on a DIFFERENT account from ``M`` on purpose: a symmetric
    pair nets to zero across accounts, so only a per-account assertion
    can see a revert applied to the wrong side.
    """
    seed = await _seed(db_session)
    m = await _add(db_session, seed, tx_id=6011, acct="A", amount="8.00",
                   tx_type=TransactionType.EXPENSE, recon="matched")
    t = await _add(db_session, seed, tx_id=6012, acct="B", amount="8.00",
                   tx_type=TransactionType.INCOME)
    await _link(db_session, m, t.id)

    await transaction_service.delete_transaction(db_session, seed["org_id"], m.id)

    assert await _balance(db_session, seed, "B") == OPENING["B"]


# ── F3: the self-link ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_of_a_self_linked_row_moves_the_balance_exactly_once(db_session):
    """F3. A PROPERTY fence, not a single-mutant fence. Read the honest
    accounting below before trusting the "Kills:" line this used to carry.

    What it pins: a self-linked row -- corrupt data containing exactly ONE
    row -- is deleted once and moves the account by 4.00, never 8.00.

    *(measured)* What it does NOT kill, each on its own:

    * ``partner.id != tx.id`` dropped from ``is_reciprocal_pair``: GREEN.
      ``to_delete`` is id-keyed, so the row lands under its own key twice
      and the second write is a no-op. Covered instead by
      ``tests/services/test_link_reciprocity_predicates.py::
      test_is_reciprocal_pair_shapes[self_link-False]``.
    * ``to_delete`` changed from ``dict[int, Transaction]`` to a list:
      GREEN. With the not-self conjunct present, ``pair_partner`` is never
      ``tx``, so the id-keying in ``delete_transaction`` is unkillable by
      any test in this repo. It is kept for symmetry with the bulk path,
      where the same keying IS killable (see F7c), and as second-line
      defence if the predicate ever loosens. Do not read it as fenced.

    It DOES go red against the two applied TOGETHER *(measured)*, which is
    the state this test exists to make unreachable: the composition is the
    property, and no smaller mutant reaches it.
    """
    seed = await _seed(db_session)
    s = await _add(db_session, seed, tx_id=6021, acct="A", amount="4.00",
                   tx_type=TransactionType.EXPENSE)
    await _link(db_session, s, s.id)

    await transaction_service.delete_transaction(db_session, seed["org_id"], s.id)

    assert await _surviving(db_session, [s.id]) == set()
    # EXPENSE revert adds the amount back. Exactly one 4.00, never 8.00.
    assert await _balance(db_session, seed, "A") == OPENING["A"] + Decimal("4.00")


# ── F4 / F4b: TBD-293, the per-row status gate ──────────────────────────────


@pytest.mark.asyncio
async def test_delete_of_a_settled_leg_whose_partner_is_pending(db_session):
    """F4. Kills: the per-pair status gate restored (TBD-293), forward case.

    A PENDING row's amount was never applied to the cached balance. The
    old code gated BOTH legs' reverts on ``tx``'s status, so deleting a
    SETTLED leg reverted its PENDING partner's amount too -- money that
    account never held.
    """
    seed = await _seed(db_session)
    e = await _add(db_session, seed, tx_id=6031, acct="A", amount="16.00",
                   tx_type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED)
    i = await _add(db_session, seed, tx_id=6032, acct="B", amount="16.00",
                   tx_type=TransactionType.INCOME, status=TransactionStatus.PENDING)
    await _link(db_session, e, i.id)
    await _link(db_session, i, e.id)

    await transaction_service.delete_transaction(db_session, seed["org_id"], e.id)

    assert await _surviving(db_session, [e.id, i.id]) == set()
    assert await _balance(db_session, seed, "A") == OPENING["A"] + Decimal("16.00")
    assert await _balance(db_session, seed, "B") == OPENING["B"]


@pytest.mark.asyncio
async def test_delete_of_a_pending_leg_whose_partner_is_settled(db_session):
    """F4b. Kills: the same gate, REVERSE case.

    Today BOTH branches miss: ``tx`` is PENDING so neither the paired
    branch nor the single-row branch fires, the SETTLED partner is
    deleted anyway, and account B keeps a deleted row's money forever.
    A boundary pinned from one side is not pinned.
    """
    seed = await _seed(db_session)
    e = await _add(db_session, seed, tx_id=6041, acct="A", amount="16.00",
                   tx_type=TransactionType.EXPENSE, status=TransactionStatus.PENDING)
    i = await _add(db_session, seed, tx_id=6042, acct="B", amount="16.00",
                   tx_type=TransactionType.INCOME, status=TransactionStatus.SETTLED)
    await _link(db_session, e, i.id)
    await _link(db_session, i, e.id)

    await transaction_service.delete_transaction(db_session, seed["org_id"], e.id)

    assert await _surviving(db_session, [e.id, i.id]) == set()
    assert await _balance(db_session, seed, "A") == OPENING["A"]
    # INCOME revert subtracts.
    assert await _balance(db_session, seed, "B") == OPENING["B"] - Decimal("16.00")


# ── F5: reverted reconciliation states ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["skipped", "rejected"])
async def test_delete_of_a_reverted_reconciliation_row_leaves_the_balance_alone(
    db_session, state
):
    """F5 (first half). Kills: ``contributes_to_cached_balance`` dropped.

    A SKIPPED / REJECTED row's amount was already reverted from
    ``accounts.balance`` at the state transition. Reverting again on
    delete drifts the account by the full amount.
    """
    seed = await _seed(db_session)
    r = await _add(db_session, seed, tx_id=6051, acct="A", amount="32.00",
                   tx_type=TransactionType.EXPENSE, recon=state)

    await transaction_service.delete_transaction(db_session, seed["org_id"], r.id)

    assert await _surviving(db_session, [r.id]) == set()
    assert await _balance(db_session, seed, "A") == OPENING["A"]


@pytest.mark.asyncio
async def test_delete_of_a_matched_row_leaves_the_balance_alone(db_session):
    """F5 (second half, the ``matched`` repeat).

    ``matched`` is NOT in ``_RECON_EXCLUDED_STATES`` -- this row is
    excluded from the cached balance by the LINK arm of the predicate,
    not the state arm. It exercises a different branch than the
    skipped/rejected case above.
    """
    seed = await _seed(db_session)
    m = await _add(db_session, seed, tx_id=6061, acct="A", amount="32.00",
                   tx_type=TransactionType.EXPENSE, recon="matched")
    t = await _add(db_session, seed, tx_id=6062, acct="B", amount="32.00",
                   tx_type=TransactionType.INCOME)
    await _link(db_session, m, t.id)

    await transaction_service.delete_transaction(db_session, seed["org_id"], m.id)

    assert await _surviving(db_session, [m.id, t.id]) == {t.id}
    assert await _balance(db_session, seed, "A") == OPENING["A"]


# ── F6 / F6b: the two faces of `not is_reciprocal_pair` ─────────────────────


@pytest.mark.asyncio
async def test_delete_of_one_leg_of_a_real_pair_reverts_both_legs(db_session):
    """F6. Kills: ``contributes_to_cached_balance`` derived as
    ``not is_reciprocal_pair(...)``.

    ⚠ The revision-1 version of this fence used a SELF-LINK and was
    VACUOUS: the mutant returns the correct answer there. A real
    reciprocal pair, both legs SETTLED, on two accounts with different
    opening balances, is the shape that kills it -- under the mutant
    both legs report "does not contribute" and NEITHER account moves.
    """
    seed = await _seed(db_session)
    e = await _add(db_session, seed, tx_id=6071, acct="A", amount="16.00",
                   tx_type=TransactionType.EXPENSE)
    i = await _add(db_session, seed, tx_id=6072, acct="B", amount="16.00",
                   tx_type=TransactionType.INCOME)
    await _link(db_session, e, i.id)
    await _link(db_session, i, e.id)

    await transaction_service.delete_transaction(db_session, seed["org_id"], e.id)

    assert await _surviving(db_session, [e.id, i.id]) == set()
    assert await _balance(db_session, seed, "A") == OPENING["A"] + Decimal("16.00")
    assert await _balance(db_session, seed, "B") == OPENING["B"] - Decimal("16.00")


@pytest.mark.asyncio
async def test_delete_of_a_one_way_matched_row_does_not_revert_its_own_amount(
    db_session,
):
    """F6b. Kills: the SAME mutant, other face.

    A one-way matched row's amount was already reverted at match time.
    Under the ``not is_reciprocal_pair`` mutant this row reports
    "contributes", gets reverted a second time, and account A drifts.
    """
    seed = await _seed(db_session)
    m = await _add(db_session, seed, tx_id=6081, acct="A", amount="64.00",
                   tx_type=TransactionType.EXPENSE, recon="matched")
    t = await _add(db_session, seed, tx_id=6082, acct="B", amount="64.00",
                   tx_type=TransactionType.INCOME)
    await _link(db_session, m, t.id)

    await transaction_service.delete_transaction(db_session, seed["org_id"], m.id)

    assert await _balance(db_session, seed, "A") == OPENING["A"]


# ── F18: baseline delete semantics (zero coverage before this PR) ───────────


@pytest.mark.asyncio
async def test_delete_of_an_unlinked_settled_row_reverts_the_balance(db_session):
    """F18 (first half). Kills: the ``status == SETTLED`` conjunction
    dropped in the OTHER direction -- an unlinked settled row must still
    give its money back."""
    seed = await _seed(db_session)
    r = await _add(db_session, seed, tx_id=6091, acct="A", amount="2.00",
                   tx_type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED)

    await transaction_service.delete_transaction(db_session, seed["org_id"], r.id)

    assert await _balance(db_session, seed, "A") == OPENING["A"] + Decimal("2.00")


@pytest.mark.asyncio
async def test_delete_of_an_unlinked_pending_row_leaves_the_balance_alone(db_session):
    """F18 (second half). Kills: the missing ``status == SETTLED``
    conjunction.

    ``contributes_to_cached_balance`` has NO status term, so an
    implementation that gates the revert on the predicate alone reverts
    an amount that was never applied. This is the most-travelled delete
    path in the app and had ZERO coverage before this PR.
    """
    seed = await _seed(db_session)
    r = await _add(db_session, seed, tx_id=6092, acct="A", amount="2.00",
                   tx_type=TransactionType.EXPENSE, status=TransactionStatus.PENDING)

    await transaction_service.delete_transaction(db_session, seed["org_id"], r.id)

    assert await _surviving(db_session, [r.id]) == set()
    assert await _balance(db_session, seed, "A") == OPENING["A"]


# ── F7 / F7b / F7c: bulk delete ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_delete_of_a_one_way_matched_row_spares_the_partner(db_session):
    """F7. TWO named mutants, both killed here:

    (a) ``for tx in found: db.delete(tx)`` restored -- the delete set
        collapsed back onto the LOCK set, which deliberately includes
        every non-null link.
    (b) ``return (len(found), skipped_ids)`` restored -- the count taken
        from the lock set, so collateral rows inflate ``deleted_count``.

    Revision 1 of this fence named only (a); the count clause stayed
    green against it. Assert BOTH.
    """
    seed = await _seed(db_session)
    m = await _add(db_session, seed, tx_id=6101, acct="A", amount="8.00",
                   tx_type=TransactionType.EXPENSE, recon="matched")
    t = await _add(db_session, seed, tx_id=6102, acct="B", amount="8.00",
                   tx_type=TransactionType.INCOME)
    await _link(db_session, m, t.id)

    deleted, skipped, _demoted = await transaction_service.bulk_delete_transactions(
        db_session, seed["org_id"], [m.id]
    )

    assert await _surviving(db_session, [m.id, t.id]) == {t.id}   # kills (a)
    assert await _balance(db_session, seed, "B") == OPENING["B"]  # kills (a)
    assert deleted == 1                                            # kills (b)
    assert skipped == []


@pytest.mark.asyncio
async def test_bulk_delete_of_one_leg_still_cascades_to_the_real_partner(db_session):
    """F7b. Kills: the delete set narrowed to the requested ids only.

    The documented cascade must survive: passing ONE leg of a real
    transfer deletes both. The only pre-existing bulk test passes BOTH
    ids, so it stays green against this mutant.
    """
    seed = await _seed(db_session)
    e = await _add(db_session, seed, tx_id=6111, acct="A", amount="16.00",
                   tx_type=TransactionType.EXPENSE)
    i = await _add(db_session, seed, tx_id=6112, acct="B", amount="16.00",
                   tx_type=TransactionType.INCOME)
    await _link(db_session, e, i.id)
    await _link(db_session, i, e.id)

    deleted, skipped, _demoted = await transaction_service.bulk_delete_transactions(
        db_session, seed["org_id"], [e.id]
    )

    assert await _surviving(db_session, [e.id, i.id]) == set()
    assert deleted == 2
    assert skipped == []
    assert await _balance(db_session, seed, "A") == OPENING["A"] + Decimal("16.00")
    assert await _balance(db_session, seed, "B") == OPENING["B"] - Decimal("16.00")


@pytest.mark.asyncio
async def test_bulk_delete_of_a_self_linked_row_counts_and_reverts_once(db_session):
    """F7c. Same honesty note as F3: a PROPERTY fence, not a mutant fence.

    What it pins: a self-linked row goes through ``bulk_delete`` once --
    deleted once, reverted once (4.00, never 8.00), counted once.

    *(measured)* What it does NOT kill: its own named mutant, the bulk
    ``delete_set`` changed from ``dict[int, Transaction]`` to a list. This
    test stays GREEN, because ``is_reciprocal_pair`` already refuses the
    self-link, so the row is appended exactly once either way.

    ⚠ RECORD THE COVER, because it is neither obvious nor stable. That
    mutant is killed by exactly one test in the repo:

        tests/services/test_transaction_service_delete_linked.py::
        test_bulk_delete_transactions_on_transfer_pair_no_circular_dependency

    It requests BOTH ids of a real reciprocal pair, so a list collects four
    entries and ``deleted_count`` reports 4 instead of 2. Its stated
    purpose -- in its own docstring and in its file's module header -- is
    ``CircularDependencyError``; nothing there says it is also the only
    thing pinning the bulk delete set's id-keying. If that file is tidied,
    or that assertion relaxed to "both rows are gone", the coverage
    evaporates silently and F7c will not notice.
    """
    seed = await _seed(db_session)
    s = await _add(db_session, seed, tx_id=6121, acct="A", amount="4.00",
                   tx_type=TransactionType.EXPENSE)
    await _link(db_session, s, s.id)

    deleted, skipped, _demoted = await transaction_service.bulk_delete_transactions(
        db_session, seed["org_id"], [s.id]
    )

    assert await _surviving(db_session, [s.id]) == set()
    assert deleted == 1
    assert skipped == []
    assert await _balance(db_session, seed, "A") == OPENING["A"] + Decimal("4.00")


@pytest.mark.asyncio
async def test_bulk_delete_reverts_per_row_across_mixed_statuses(db_session):
    """The bulk twin of F4 / F4b / F18: the ``status == SETTLED``
    conjunction and the per-row predicate both apply to the bulk path.

    Amounts are distinct powers of two so the surviving balance
    identifies exactly which subset was reverted.
    """
    seed = await _seed(db_session)
    settled = await _add(db_session, seed, tx_id=6131, acct="C", amount="1.00",
                         status=TransactionStatus.SETTLED)
    pending = await _add(db_session, seed, tx_id=6132, acct="C", amount="2.00",
                         status=TransactionStatus.PENDING)
    skipped_row = await _add(db_session, seed, tx_id=6133, acct="C", amount="4.00",
                             status=TransactionStatus.SETTLED, recon="skipped")

    deleted, skipped, _demoted = await transaction_service.bulk_delete_transactions(
        db_session, seed["org_id"], [settled.id, pending.id, skipped_row.id]
    )

    assert deleted == 3
    assert skipped == []
    assert await _surviving(
        db_session, [settled.id, pending.id, skipped_row.id]
    ) == set()
    # ONLY the settled, contributing 1.00 comes back.
    assert await _balance(db_session, seed, "C") == OPENING["C"] + Decimal("1.00")
