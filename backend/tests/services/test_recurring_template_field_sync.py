"""TBD-760: a template name, category or account edit reaches its PENDING occurrences.

``update_recurring`` passes the changed fields to ``_propagate_fields_to_series``,
the same writer an occurrence edit uses, with the pre-edit template values as
``*_was`` guards (the TBD-756 amount rule):

- an occurrence still holding the OLD template value follows; one whose value
  differs (changed by hand, or left behind by an earlier edit) keeps it;
- the targets are ``_series_sync_targets``': SETTLED rows, transfer legs,
  skipped/rejected rows and rows a bank row was reconcile-matched against are
  never touched;
- category reaches only rows of the template's type; name and account are
  type-independent and also reach a type-diverged row;
- no balance moves (PENDING amounts are never inside ``accounts.balance``);
- a ``next_due_date``/``auto_settle``-only edit, or a re-send of the same
  values, moves no row.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.recurring import RecurringUpdate
from app.services import recurring_service

pytestmark = pytest.mark.asyncio

# A fixed year well past the current billing cycle, so every date below clears
# the frontier lower bound (TBD-283) whatever day the suite runs on.
Y = max(date.today().year + 1, 2027)


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


async def _seed(db: AsyncSession) -> dict:
    org = Organization(name="T", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    # Non-zero balances, so "unchanged" is a real assertion.
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("1000.00"), currency="EUR",
    )
    acct_b = Account(
        org_id=org.id, name="Savings", account_type_id=at.id,
        balance=Decimal("250.00"), currency="EUR",
    )
    acct_c = Account(
        org_id=org.id, name="Card", account_type_id=at.id,
        balance=Decimal("-40.00"), currency="EUR",
    )
    db.add_all([acct, acct_b, acct_c])
    await db.flush()
    exp = Category(org_id=org.id, name="Lease", slug="lease", type=CategoryType.EXPENSE)
    exp_b = Category(org_id=org.id, name="Housing", slug="housing", type=CategoryType.EXPENSE)
    exp_c = Category(org_id=org.id, name="Misc", slug="misc", type=CategoryType.EXPENSE)
    inc = Category(org_id=org.id, name="Salary", slug="salary", type=CategoryType.INCOME)
    inc_b = Category(org_id=org.id, name="Refunds", slug="refunds", type=CategoryType.INCOME)
    db.add_all([exp, exp_b, exp_c, inc, inc_b])
    await db.commit()
    return {
        "org_id": org.id, "account_id": acct.id, "account_b": acct_b.id,
        "account_c": acct_c.id, "exp_cat": exp.id, "exp_cat_b": exp_b.id,
        "exp_cat_c": exp_c.id, "inc_cat": inc.id, "inc_cat_b": inc_b.id,
    }


async def _add_template(db: AsyncSession, seed: dict, *, next_due: date) -> int:
    r = RecurringTransaction(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["exp_cat"], description="Lease", amount=Decimal("695.59"),
        type="expense", frequency=Frequency.MONTHLY, next_due_date=next_due,
        auto_settle=False, is_active=True,
    )
    db.add(r)
    await db.commit()
    return r.id


async def _add_row(
    db: AsyncSession, seed: dict, recurring_id: int | None, *, dt: date,
    status: TransactionStatus = TransactionStatus.PENDING,
    tx_type: TransactionType = TransactionType.EXPENSE, account_id: int | None = None,
    category_id: int | None = None, description: str = "Lease",
    reconciliation_state: str = "accepted",
) -> int:
    default_cat = seed["exp_cat"] if tx_type == TransactionType.EXPENSE else seed["inc_cat"]
    tx = Transaction(
        org_id=seed["org_id"], account_id=account_id or seed["account_id"],
        category_id=category_id or default_cat,
        description=description, amount=Decimal("695.59"), type=tx_type, status=status,
        date=dt, settled_date=(dt if status == TransactionStatus.SETTLED else None),
        recurring_id=recurring_id, reconciliation_state=reconciliation_state,
    )
    db.add(tx)
    await db.commit()
    return tx.id


async def _get_tx(db: AsyncSession, tx_id: int) -> Transaction:
    db.expire_all()
    return await db.get(Transaction, tx_id)


async def _get_tmpl(db: AsyncSession, rid: int) -> RecurringTransaction:
    db.expire_all()
    return await db.get(RecurringTransaction, rid)


async def _balances(db: AsyncSession, seed: dict) -> dict[int, Decimal]:
    db.expire_all()
    ids = (seed["account_id"], seed["account_b"], seed["account_c"])
    return {i: (await db.get(Account, i)).balance for i in ids}


async def _snapshot(db: AsyncSession, tx_id: int) -> tuple:
    t = await _get_tx(db, tx_id)
    return (t.description, t.category_id, t.account_id, t.amount, t.date, t.type, t.status)


async def _update(db: AsyncSession, seed: dict, rid: int, **fields) -> RecurringTransaction:
    return await recurring_service.update_recurring(
        db, seed["org_id"], rid, RecurringUpdate(**fields),
    )


async def _excluded_rows(db: AsyncSession, seed: dict, rid: int) -> dict[str, int]:
    """One of each row the sync must never touch, all still on the template's
    old name/category/account so only the exclusion can keep them there."""
    settled = await _add_row(db, seed, rid, dt=date(Y, 2, 5), status=TransactionStatus.SETTLED)
    skipped = await _add_row(db, seed, rid, dt=date(Y, 3, 10), reconciliation_state="skipped")
    rejected = await _add_row(db, seed, rid, dt=date(Y, 3, 15), reconciliation_state="rejected")
    leg = await _add_row(db, seed, rid, dt=date(Y, 3, 20))
    partner = await _add_row(
        db, seed, None, dt=date(Y, 3, 20), tx_type=TransactionType.INCOME,
        account_id=seed["account_b"],
    )
    matched = await _add_row(db, seed, rid, dt=date(Y, 3, 25))
    bank_row = await _add_row(
        db, seed, None, dt=date(Y, 3, 26), status=TransactionStatus.SETTLED,
    )
    leg_row = await db.get(Transaction, leg)
    partner_row = await db.get(Transaction, partner)
    bank = await db.get(Transaction, bank_row)
    leg_row.linked_transaction_id = partner
    partner_row.linked_transaction_id = leg
    bank.linked_transaction_id = matched  # one-way reconcile match
    await db.commit()
    return {
        "settled": settled, "skipped": skipped, "rejected": rejected,
        "leg": leg, "partner": partner, "matched": matched, "bank": bank_row,
    }


# ── Each field reaches the untouched pending rows, nothing else ──────────────

async def test_template_description_reaches_untouched_pending_occurrences(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    pending = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5))
    later = await _add_row(db_session, seed, rid, dt=date(Y, 5, 5))
    excluded = await _excluded_rows(db_session, seed, rid)
    before = {k: await _snapshot(db_session, v) for k, v in excluded.items()}
    balances = await _balances(db_session, seed)

    await _update(db_session, seed, rid, description="Lease (new flat)")

    assert (await _get_tmpl(db_session, rid)).description == "Lease (new flat)"
    assert (await _get_tx(db_session, pending)).description == "Lease (new flat)"
    assert (await _get_tx(db_session, later)).description == "Lease (new flat)"
    for name, tx_id in excluded.items():
        assert await _snapshot(db_session, tx_id) == before[name], name
    assert await _balances(db_session, seed) == balances


async def test_template_category_reaches_untouched_pending_occurrences(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    pending = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5))
    excluded = await _excluded_rows(db_session, seed, rid)
    before = {k: await _snapshot(db_session, v) for k, v in excluded.items()}
    balances = await _balances(db_session, seed)

    await _update(db_session, seed, rid, category_id=seed["exp_cat_b"])

    assert (await _get_tmpl(db_session, rid)).category_id == seed["exp_cat_b"]
    assert (await _get_tx(db_session, pending)).category_id == seed["exp_cat_b"]
    for name, tx_id in excluded.items():
        assert await _snapshot(db_session, tx_id) == before[name], name
    assert await _balances(db_session, seed) == balances


async def test_template_account_reaches_untouched_pending_occurrences(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    pending = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5))
    excluded = await _excluded_rows(db_session, seed, rid)
    before = {k: await _snapshot(db_session, v) for k, v in excluded.items()}
    balances = await _balances(db_session, seed)

    await _update(db_session, seed, rid, account_id=seed["account_c"])

    assert (await _get_tmpl(db_session, rid)).account_id == seed["account_c"]
    assert (await _get_tx(db_session, pending)).account_id == seed["account_c"]
    for name, tx_id in excluded.items():
        assert await _snapshot(db_session, tx_id) == before[name], name
    # Only SETTLED rows are inside accounts.balance: moving pending rows
    # between accounts moves no money.
    assert await _balances(db_session, seed) == balances


async def test_all_three_fields_and_amount_in_one_request(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    pending = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5))
    settled = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5), status=TransactionStatus.SETTLED)
    balances = await _balances(db_session, seed)

    await _update(
        db_session, seed, rid, description="Rent", category_id=seed["exp_cat_b"],
        account_id=seed["account_c"], amount=Decimal("752.09"),
    )

    tmpl = await _get_tmpl(db_session, rid)
    assert (tmpl.description, tmpl.category_id, tmpl.account_id, tmpl.amount) == (
        "Rent", seed["exp_cat_b"], seed["account_c"], Decimal("752.09"),
    )
    row = await _get_tx(db_session, pending)
    assert (row.description, row.category_id, row.account_id, row.amount) == (
        "Rent", seed["exp_cat_b"], seed["account_c"], Decimal("752.09"),
    )
    old = await _get_tx(db_session, settled)
    assert (old.description, old.category_id, old.account_id, old.amount) == (
        "Lease", seed["exp_cat"], seed["account_id"], Decimal("695.59"),
    )
    assert await _balances(db_session, seed) == balances


# ── Type divergence ──────────────────────────────────────────────────────────

async def test_template_category_skips_a_type_diverged_row(db_session):
    """An expense category on an income row would be an invalid pair."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    refund = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5), tx_type=TransactionType.INCOME)
    # Legacy shape: an income row still carrying the template's old EXPENSE
    # category. The old-value guard alone would let it through; the type
    # filter is what keeps it.
    legacy = await _add_row(
        db_session, seed, rid, dt=date(Y, 4, 10), tx_type=TransactionType.INCOME,
        category_id=seed["exp_cat"],
    )

    await _update(db_session, seed, rid, category_id=seed["exp_cat_b"])

    assert (await _get_tx(db_session, refund)).category_id == seed["inc_cat"]
    assert (await _get_tx(db_session, legacy)).category_id == seed["exp_cat"]


async def test_template_name_and_account_reach_a_type_diverged_row(db_session):
    """Name and account are type-independent (the helper's own rule)."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    refund = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5), tx_type=TransactionType.INCOME)

    await _update(db_session, seed, rid, description="Rent", account_id=seed["account_c"])

    row = await _get_tx(db_session, refund)
    assert (row.description, row.account_id) == ("Rent", seed["account_c"])
    assert row.category_id == seed["inc_cat"]


async def test_type_and_category_change_together_push_no_category(db_session):
    """Type changes are out of scope (rows keep their type), so the new
    category, validated for the NEW type, must not land on old-type rows."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    pending = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5))

    await _update(db_session, seed, rid, type="income", category_id=seed["inc_cat_b"])

    tmpl = await _get_tmpl(db_session, rid)
    assert (tmpl.type, tmpl.category_id) == ("income", seed["inc_cat_b"])
    row = await _get_tx(db_session, pending)
    assert (row.type, row.category_id) == (TransactionType.EXPENSE, seed["exp_cat"])


# ── Hand-overridden rows keep their value (TBD-756 amount rule) ──────────────

async def test_a_row_with_a_different_value_keeps_it(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    untouched = await _add_row(db_session, seed, rid, dt=date(Y, 3, 5))
    own_name = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5), description="Lease + parking")
    own_cat = await _add_row(db_session, seed, rid, dt=date(Y, 4, 10), category_id=seed["exp_cat_c"])
    own_acct = await _add_row(db_session, seed, rid, dt=date(Y, 4, 15), account_id=seed["account_b"])

    await _update(
        db_session, seed, rid, description="Rent", category_id=seed["exp_cat_b"],
        account_id=seed["account_c"],
    )

    row = await _get_tx(db_session, untouched)
    assert (row.description, row.category_id, row.account_id) == (
        "Rent", seed["exp_cat_b"], seed["account_c"],
    )
    # Each override is kept; the row's OTHER fields still follow.
    row = await _get_tx(db_session, own_name)
    assert (row.description, row.category_id, row.account_id) == (
        "Lease + parking", seed["exp_cat_b"], seed["account_c"],
    )
    row = await _get_tx(db_session, own_cat)
    assert (row.description, row.category_id, row.account_id) == (
        "Rent", seed["exp_cat_c"], seed["account_c"],
    )
    row = await _get_tx(db_session, own_acct)
    assert (row.description, row.category_id, row.account_id) == (
        "Rent", seed["exp_cat_b"], seed["account_b"],
    )


# ── Edits that must move nothing ─────────────────────────────────────────────

@pytest.mark.parametrize("fields", [
    {"next_due_date": date(Y, 6, 9)},
    {"auto_settle": True},
])
async def test_schedule_only_edit_moves_no_row(db_session, fields):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    # Rows that differ from the template, so a stray sync would show.
    a = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5), description="Old name")
    b = await _add_row(
        db_session, seed, rid, dt=date(Y, 5, 5), category_id=seed["exp_cat_c"],
        account_id=seed["account_b"],
    )
    before = {a: await _snapshot(db_session, a), b: await _snapshot(db_session, b)}

    await _update(db_session, seed, rid, **fields)

    for tx_id, snap in before.items():
        assert await _snapshot(db_session, tx_id) == snap


async def test_resending_the_same_values_does_not_call_the_sync(db_session, monkeypatch):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due=date(Y, 6, 5))
    pending = await _add_row(db_session, seed, rid, dt=date(Y, 4, 5))
    before = await _snapshot(db_session, pending)
    calls: list = []
    real = recurring_service._propagate_fields_to_series

    async def _spy(*args, **kwargs):
        calls.append(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(recurring_service, "_propagate_fields_to_series", _spy)

    await _update(
        db_session, seed, rid, description="Lease", category_id=seed["exp_cat"],
        account_id=seed["account_id"], amount=Decimal("695.59"),
    )

    assert calls == []
    assert await _snapshot(db_session, pending) == before
