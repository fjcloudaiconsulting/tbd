"""reconcile_account must reconcile against the SAME invariant the live
balance is built on: ``balance == opening_balance + Σ settled(income − expense)``.

Before the fix, ``computed`` omitted ``opening_balance``, so every account
with a non-zero opening balance was falsely reported inconsistent (by exactly
the opening balance). Regression guard for the 2026-06-14 investigation.
"""
import pytest
import pytest_asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.transaction import TransactionCreate
from app.services import reconciliation_service as rs
from app.services import transaction_service as ts


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor(); cur.execute("PRAGMA foreign_keys=ON"); cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db, *, opening: str):
    org = Organization(name="T", billing_cycle_day=1)
    db.add(org); await db.flush()
    at = AccountType(org_id=org.id, name="Bank", slug="bank", is_system=True)
    db.add(at); await db.flush()
    acct = Account(org_id=org.id, name="A", account_type_id=at.id,
                   balance=Decimal(opening), currency="EUR",
                   opening_balance=Decimal(opening), opening_balance_date=date(2026, 1, 1))
    db.add(acct)
    db.add(Category(org_id=org.id, name="G", slug="g", type=CategoryType.BOTH, is_system=True))
    await db.flush(); await db.commit()
    return org, acct


@pytest.mark.asyncio
async def test_reconcile_consistent_with_nonzero_opening_and_no_txns(db_session):
    db = db_session
    org, acct = await _seed(db, opening="1000.00")
    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    assert stored == Decimal("1000.00")
    assert computed == Decimal("1000.00")   # opening included
    assert consistent is True


@pytest.mark.asyncio
async def test_reconcile_consistent_opening_plus_settled_txns(db_session):
    db = db_session
    org, acct = await _seed(db, opening="1000.00")
    from sqlalchemy import select
    gid = await db.scalar(select(Category.id).where(Category.org_id == org.id))
    await ts.create_transaction(db, org.id, TransactionCreate(
        account_id=acct.id, category_id=gid, description="pay",
        amount=Decimal("200.00"), type="income", status="settled", date=date(2026, 6, 1)))
    await ts.create_transaction(db, org.id, TransactionCreate(
        account_id=acct.id, category_id=gid, description="buy",
        amount=Decimal("50.00"), type="expense", status="settled", date=date(2026, 6, 2)))
    await db.refresh(acct)
    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    # balance = 1000 + 200 - 50 = 1150; computed must equal it.
    assert stored == Decimal("1150.00")
    assert computed == Decimal("1150.00")
    assert consistent is True


async def _seed_two(db, *, opening_a: str, opening_b: str):
    """Seed an org with two accounts (nonzero openings) + a Transfer category."""
    org = Organization(name="T", billing_cycle_day=1)
    db.add(org); await db.flush()
    at = AccountType(org_id=org.id, name="Bank", slug="bank", is_system=True)
    db.add(at); await db.flush()
    acct_a = Account(org_id=org.id, name="A", account_type_id=at.id,
                     balance=Decimal(opening_a), currency="EUR",
                     opening_balance=Decimal(opening_a),
                     opening_balance_date=date(2026, 1, 1))
    acct_b = Account(org_id=org.id, name="B", account_type_id=at.id,
                     balance=Decimal(opening_b), currency="EUR",
                     opening_balance=Decimal(opening_b),
                     opening_balance_date=date(2026, 1, 1))
    db.add(acct_a); db.add(acct_b)
    db.add(Category(org_id=org.id, name="Transfer", slug="transfer",
                    type=CategoryType.BOTH, is_system=True))
    await db.flush(); await db.commit()
    return org, acct_a, acct_b


@pytest.mark.asyncio
async def test_reconcile_consistent_with_transfer_legs(db_session):
    """OVER-REACH FENCE (TBD-303). Kills a fix that drops too much.

    A transfer creates settled EXPENSE+INCOME legs counted by reconcile.
    Both source and destination accounts must stay consistent: each balance
    moved by the transfer amount, and computed includes the matching leg.

    TBD-303 extension: acct_a also carries a MANUAL ADJUSTMENT row. Both
    shapes -- a RECIPROCAL transfer pair and ``is_manual_adjustment=True``
    -- are inside ``accounts.balance`` and must stay inside ``computed``.

    Wrong implementations this kills:
      * ``reportable_transaction_filter()`` in reconcile_account -- it drops
        BOTH transfer legs (``linked_transaction_id IS NOT NULL``) and manual
        adjustments (``is_manual_adjustment IS TRUE``).
      * any flat ``linked_transaction_id IS NULL`` clause -- drops reciprocal
        legs, which link back and DO contribute.
    """
    db = db_session
    org, acct_a, acct_b = await _seed_two(db, opening_a="1000.00", opening_b="400.00")

    from app.schemas.transaction import TransferCreate
    await ts.create_transfer(db, org.id, TransferCreate(
        from_account_id=acct_a.id, to_account_id=acct_b.id,
        amount=Decimal("250.00"), status="settled", date=date(2026, 6, 1)))

    # Manual balance adjustment on acct_a -- the escape hatch reconcile
    # DELIBERATELY counts (see transaction_filters module docstring).
    from sqlalchemy import select as _select
    cat_id = await db.scalar(_select(Category.id).where(Category.org_id == org.id))
    adj = Transaction(
        org_id=org.id, account_id=acct_a.id, category_id=cat_id,
        description="adjust", amount=Decimal("30.00"),
        type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
        date=date(2026, 6, 2), settled_date=date(2026, 6, 2),
        is_manual_adjustment=True, reconciliation_state="accepted",
    )
    db.add(adj)
    await db.flush()
    ts.apply_balance(acct_a, adj.amount, adj.type)   # adjustment IS in balance
    await db.commit()
    await db.refresh(acct_a)
    await db.refresh(acct_b)

    # Source: 1000 - 250 (expense leg) + 30 (adjustment) = 780.
    # Dest:    400 + 250 (income leg) = 650.
    s_a, c_a, ok_a = await ts.reconcile_account(db, org.id, acct_a)
    assert s_a == Decimal("780.00")
    assert c_a == Decimal("780.00")
    assert ok_a is True

    s_b, c_b, ok_b = await ts.reconcile_account(db, org.id, acct_b)
    assert s_b == Decimal("650.00")
    assert c_b == Decimal("650.00")
    assert ok_b is True


@pytest.mark.asyncio
async def test_reconcile_consistent_after_negative_opening_delta(db_session):
    """Lowering opening_balance (1000 -> 300) must shift balance down by 700 and
    keep reconcile consistent. Mirrors the router's opening-shift path applied
    to a freshly-seeded account (no txns: balance tracks opening directly)."""
    db = db_session
    org, acct = await _seed(db, opening="1000.00")
    assert acct.balance == Decimal("1000.00")

    # Apply the same shift the router's _apply_non_type_fields performs.
    new_opening = Decimal("300.00")
    acct.balance += new_opening - acct.opening_balance
    acct.opening_balance = new_opening
    await db.commit()
    await db.refresh(acct)

    assert acct.balance == Decimal("300.00")  # shifted down by 700
    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    assert stored == Decimal("300.00")
    assert computed == Decimal("300.00")
    assert consistent is True


# ── TBD-303: balance-reverted rows must leave `computed` ─────────────────────
#
# ``reconcile_account`` summed EVERY settled row with no link/state clause,
# while ``reconciliation_service._apply_balance_for_transition`` REVERTS a
# row's contribution from ``accounts.balance`` the moment it stops being
# reportable (matched / skipped / rejected). Any account holding such a row
# therefore reported ``is_consistent=False`` forever.
#
# ⚠ MECHANISM NOTE (why a naive fence here is vacuous): MATCHED is NOT
# excluded by any state clause -- ``balance_contribution_filter()`` only
# names ``skipped`` and ``rejected``. A matched row is dropped by the ONE-WAY
# LINK test. So a fixture that sets ``reconciliation_state='matched'`` and
# leaves ``linked_transaction_id`` NULL passes the filter, correctly, and the
# test is green with or without the fix. Every matched fixture below therefore
# reproduces ``_apply_match``'s one-way shape: the duplicate points at the
# canonical row, the canonical row does NOT point back.


async def _cat_id(db, org):
    from sqlalchemy import select
    return await db.scalar(select(Category.id).where(Category.org_id == org.id))


async def _settled(db, org, acct, *, amount, tx_type="expense", d=date(2026, 6, 1)):
    """Create a settled row through the real create path (balance applied)."""
    return await ts.create_transaction(db, org.id, TransactionCreate(
        account_id=acct.id, category_id=await _cat_id(db, org),
        description=f"row-{amount}", amount=Decimal(amount),
        type=tx_type, status="settled", date=d))


async def _pending(db, org, acct, *, amount, tx_type="expense", d=date(2026, 6, 1)):
    """Create a PENDING row through the real create path.

    ``_create_transaction_no_commit`` calls ``apply_balance`` only for SETTLED
    rows, so a row created here is deliberately NOT inside ``accounts.balance``.
    The caller asserts that rather than trusting it.
    """
    return await ts.create_transaction(db, org.id, TransactionCreate(
        account_id=acct.id, category_id=await _cat_id(db, org),
        description=f"pending-{amount}", amount=Decimal(amount),
        type=tx_type, status="pending", date=d))


async def _transition(db, org, tx, *, target_state, link_to=None):
    """Drive the REAL balance bookkeeping for a reconcile state transition.

    Uses ``reconciliation_service._apply_balance_for_transition`` rather than
    hand-writing the new balance, so the fixture cannot disagree with the
    production revert it is supposed to model.
    """
    source_reportable = True          # row was an ordinary accepted row
    tx.reconciliation_state = target_state
    if link_to is not None:
        tx.linked_transaction_id = link_to      # ONE-WAY, as _apply_match writes
    await rs._apply_balance_for_transition(
        db, org_id=org.id, tx=tx,
        source_state="accepted", target_state=target_state,
        source_reportable=source_reportable,
    )
    await db.commit()


# Every fence below seeds a reverted row on BOTH sides of the query -- one
# EXPENSE and one INCOME. ``reconcile_account`` runs TWO subqueries; a
# "fix" that gates only one of them leaves the other side's reverted rows
# over-counted. A fence built from expense rows alone is green against an
# expense-only half-fix, and vice versa. Amounts are all distinct so any
# subset sum identifies exactly which rows leaked in.


@pytest.mark.asyncio
async def test_reconcile_consistent_with_matched_duplicate(db_session):
    """FENCE (TBD-303). Kills:
      * the unfixed query (settled rows summed with no balance-contribution
        clause) -- the matched duplicates land in ``computed`` but not in
        ``accounts.balance``;
      * ``non_reverted_transaction_filter()`` as the fix -- MATCHED is NOT in
        the excluded-state roster, so a state-only clause still counts these;
      * the HALF-FIX: the clause on only the income or only the expense
        subquery (one matched duplicate per side).

    Shape: canonical settled rows plus their reconcile-MATCHED duplicates,
    each duplicate carrying a ONE-WAY link to its canonical row exactly as
    ``_apply_match`` writes it. The canonical rows do NOT link back -- that
    asymmetry, not the state string, is what drops the duplicates.
    """
    db = db_session
    org, acct = await _seed(db, opening="1000.00")

    canon_exp = await _settled(db, org, acct, amount="100.00")
    dup_exp = await _settled(db, org, acct, amount="77.00", d=date(2026, 6, 2))
    canon_inc = await _settled(db, org, acct, amount="200.00",
                               tx_type="income", d=date(2026, 6, 3))
    dup_inc = await _settled(db, org, acct, amount="13.00",
                             tx_type="income", d=date(2026, 6, 4))
    await db.refresh(acct)
    assert acct.balance == Decimal("1036.00")       # 1000 -100 -77 +200 +13

    await _transition(db, org, dup_exp, target_state="matched",
                      link_to=canon_exp.id)
    await _transition(db, org, dup_inc, target_state="matched",
                      link_to=canon_inc.id)
    await db.refresh(acct)
    await db.refresh(canon_exp)
    await db.refresh(canon_inc)
    # The reverts really fired: 77 and 13 are back out of the cached balance.
    assert acct.balance == Decimal("1100.00")       # 1000 - 100 + 200
    # ONE-WAY, not transfer pairs: the canonical rows do not link back.
    assert dup_exp.linked_transaction_id == canon_exp.id
    assert dup_inc.linked_transaction_id == canon_inc.id
    assert canon_exp.linked_transaction_id is None
    assert canon_inc.linked_transaction_id is None

    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    assert stored == Decimal("1100.00")
    # CONTROL inside the fence: the canonical 100 / 200 are STILL counted. A
    # filter that also dropped them would give 1000 and fail here.
    assert computed == Decimal("1100.00")
    assert consistent is True


@pytest.mark.asyncio
async def test_reconcile_consistent_with_skipped_row(db_session):
    """FENCE (TBD-303). Kills the unfixed query, and the half-fix, via
    SKIPPED rows on both sides. A SKIPPED row's amount was reverted from
    ``accounts.balance``; counting it in ``computed`` drifts by its amount.
    These rows carry NO link -- they are dropped by the STATE clause alone,
    so this fence also kills a "fix" that only tests link reciprocity.
    """
    db = db_session
    org, acct = await _seed(db, opening="1000.00")

    await _settled(db, org, acct, amount="100.00")
    skip_exp = await _settled(db, org, acct, amount="64.00", d=date(2026, 6, 2))
    await _settled(db, org, acct, amount="200.00", tx_type="income",
                   d=date(2026, 6, 3))
    skip_inc = await _settled(db, org, acct, amount="21.00", tx_type="income",
                              d=date(2026, 6, 4))
    await db.refresh(acct)
    assert acct.balance == Decimal("1057.00")       # 1000 -100 -64 +200 +21

    await _transition(db, org, skip_exp, target_state="skipped")
    await _transition(db, org, skip_inc, target_state="skipped")
    await db.refresh(acct)
    assert acct.balance == Decimal("1100.00")       # 64 and 21 reverted back
    assert skip_exp.linked_transaction_id is None
    assert skip_inc.linked_transaction_id is None

    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    assert stored == Decimal("1100.00")
    assert computed == Decimal("1100.00")
    assert consistent is True


@pytest.mark.asyncio
async def test_reconcile_consistent_with_rejected_row(db_session):
    """FENCE (TBD-303), sibling state. Kills the same wrong implementations via
    REJECTED -- the state ``_demote_match_orphans`` writes. Both sides again.
    """
    db = db_session
    org, acct = await _seed(db, opening="1000.00")

    await _settled(db, org, acct, amount="100.00")
    rej_exp = await _settled(db, org, acct, amount="32.00", d=date(2026, 6, 2))
    await _settled(db, org, acct, amount="200.00", tx_type="income",
                   d=date(2026, 6, 3))
    rej_inc = await _settled(db, org, acct, amount="9.00", tx_type="income",
                             d=date(2026, 6, 4))
    await _transition(db, org, rej_exp, target_state="rejected")
    await _transition(db, org, rej_inc, target_state="rejected")
    await db.refresh(acct)
    assert acct.balance == Decimal("1100.00")

    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    assert stored == Decimal("1100.00")
    assert computed == Decimal("1100.00")
    assert consistent is True


@pytest.mark.asyncio
async def test_reconcile_reports_drift_for_orphaned_duplicate(db_session):
    """GUARD / boundary (TBD-303). The fix must not become a rubber stamp.

    An ORPHANED duplicate -- a matched row whose canonical partner was deleted
    by a path that did NOT route through ``_demote_match_orphans``, so the
    ``ON DELETE SET NULL`` FK erased its link and left it byte-identical to an
    ordinary ACCEPTED row -- is genuinely NOT inside ``accounts.balance``.
    ``balance_contribution_filter()`` keeps it (that is its documented
    KEEP-on-uncertainty polarity), so reconcile must still report the drift
    rather than silently absorb it.

    Kills: a fix that excludes rows on ``is_imported`` / ``import_batch_id``,
    or on "was ever matched", instead of on the live link + state columns --
    such a fix would hide this real drift and report consistent.

    ⚠ Requires ``PRAGMA foreign_keys=ON`` (set in the db_session fixture).
    Without it the FK never fires, the link stays dangling, and the row would
    be dropped by the reciprocity EXISTS -- passing for the wrong reason.
    """
    from sqlalchemy import delete, select

    db = db_session
    org, acct = await _seed(db, opening="1000.00")

    canon = await _settled(db, org, acct, amount="100.00")
    dup = await _settled(db, org, acct, amount="77.00", d=date(2026, 6, 2))
    dup.is_imported = True
    await _transition(db, org, dup, target_state="matched", link_to=canon.id)
    await db.refresh(acct)
    assert acct.balance == Decimal("900.00")

    # A delete path that bypasses _demote_match_orphans: raw DELETE + the
    # balance revert the canonical row's removal owes the account.
    canon_id, dup_id = canon.id, dup.id
    await db.execute(
        delete(Transaction).where(Transaction.id == canon_id)
        .execution_options(synchronize_session=False)
    )
    ts.revert_balance(acct, Decimal("100.00"), TransactionType.EXPENSE)
    dup.reconciliation_state = "accepted"
    await db.commit()

    # Re-read from the DB (not the identity map): the FK really fired, which
    # is what makes the fixture the orphan shape rather than a dangling link.
    assert (await db.scalar(
        select(Transaction.id).where(Transaction.id == canon_id))) is None
    await db.refresh(dup)
    assert dup.id == dup_id
    assert dup.linked_transaction_id is None
    assert dup.reconciliation_state == "accepted"

    await db.refresh(acct)
    assert acct.balance == Decimal("1000.00")           # only opening remains

    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    assert stored == Decimal("1000.00")
    assert computed == Decimal("923.00")                # 1000 - 77 (orphan)
    assert stored - computed == Decimal("77.00")        # drift == signed amount
    assert consistent is False


@pytest.mark.asyncio
async def test_reconcile_still_detects_real_drift(db_session):
    """CONTROL (TBD-303). Ordinary settled rows are unaffected by the filter,
    and a genuinely wrong stored balance is STILL reported inconsistent.

    Kills: any fix broad enough to drop ordinary accepted, unlinked rows --
    that would make reconcile report consistent on a corrupt account.
    """
    db = db_session
    org, acct = await _seed(db, opening="1000.00")

    await _settled(db, org, acct, amount="100.00")
    await _settled(db, org, acct, amount="40.00", tx_type="income",
                   d=date(2026, 6, 2))
    await db.refresh(acct)
    assert acct.balance == Decimal("940.00")

    # Sanity: consistent while untouched (ordinary rows counted, both types).
    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    assert (stored, computed, consistent) == (
        Decimal("940.00"), Decimal("940.00"), True)

    # Corrupt the cached balance by hand: reconcile must catch it.
    acct.balance = Decimal("999.00")
    await db.commit()
    await db.refresh(acct)
    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    assert stored == Decimal("999.00")
    assert computed == Decimal("940.00")
    assert consistent is False


@pytest.mark.asyncio
async def test_reconcile_excludes_pending_rows(db_session):
    """FENCE (TBD-303 follow-up). Kills: dropping
    ``Transaction.status == TransactionStatus.SETTLED`` from either
    ``reconcile_account`` subquery.

    The docstring has always claimed "Only settled transactions are included
    in the computation", but nothing pinned it -- the whole reconcile-relevant
    suite stayed green with the SETTLED gate deleted, because no fixture
    anywhere seeded a PENDING row and called ``reconcile_account``. The
    TBD-303 ``contributes`` clause sits directly under that gate, so it is
    fenced here.

    A PENDING row's amount is virtual: ``_create_transaction_no_commit``
    calls ``apply_balance`` only for SETTLED rows. So the pending amounts are
    NOT in ``accounts.balance`` and must not be in ``computed``.

    ⚠ NON-VACUITY: the two pending amounts (500 expense / 40 income) are
    distinct and do NOT net to zero. A symmetric pair would cancel inside
    ``income - expense`` and the gated and un-gated implementations would
    agree on the fixture, pinning nothing. Here the three leak shapes are all
    distinguishable from the correct 1100.00:
        both sides leak  -> 640.00   (1100 + 40 - 500)
        income only      -> 1140.00
        expense only     ->  600.00
    """
    db = db_session
    org, acct = await _seed(db, opening="1000.00")

    await _settled(db, org, acct, amount="100.00")
    await _settled(db, org, acct, amount="200.00", tx_type="income",
                   d=date(2026, 6, 2))
    await db.refresh(acct)
    assert acct.balance == Decimal("1100.00")       # 1000 - 100 + 200

    pend_exp = await _pending(db, org, acct, amount="500.00", d=date(2026, 6, 3))
    pend_inc = await _pending(db, org, acct, amount="40.00", tx_type="income",
                              d=date(2026, 6, 4))
    await db.refresh(acct)
    # The invariant under test: pending amounts never entered the balance.
    assert acct.balance == Decimal("1100.00")
    assert pend_exp.status == TransactionStatus.PENDING
    assert pend_inc.status == TransactionStatus.PENDING
    # CONTROL: the pending rows are ordinary in every other respect, so they
    # are NOT dropped by the TBD-303 clause -- only by the SETTLED gate.
    assert pend_exp.linked_transaction_id is None
    assert pend_inc.linked_transaction_id is None
    assert pend_exp.reconciliation_state == "accepted"
    assert pend_inc.reconciliation_state == "accepted"

    stored, computed, consistent = await ts.reconcile_account(db, org.id, acct)
    assert stored == Decimal("1100.00")
    assert computed == Decimal("1100.00")
    assert consistent is True
