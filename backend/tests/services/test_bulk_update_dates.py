"""TBD-763: batch edit of the transaction date and the settlement date.

Both fields SET a date (no shift), through the same ``TransactionUpdate`` as
category/status/account, so a row stays all-or-nothing on a failure. The
fences here:

- date is applied to plain rows, and skipped (with a note, the rest of the
  row still applying) on recurring occurrences and transfer legs. Manual
  adjustments stay skipped whole, as before.
- a PENDING row keeps its expected-settlement gap when its date moves; a
  SETTLED row whose new date would pass its settled_date is never auto-shifted:
  its date is skipped with a note.
- settled_date applies to rows that are, or are being, SETTLED; with
  status=settled the rows settle on the chosen date instead of today. A row
  being set to pending gets a note. (Rows that stay pending take it as their
  expected settlement since TBD-791: test_pending_settlement_move.py.)
- a row failing the ordering check is skipped with its reason; the rest of the
  batch proceeds.
- ``accounts.balance`` does not move on a date or settled_date change.

Runs on SQLite in-memory; no MySQL.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.models.transaction import TransactionStatus, TransactionType
from app.models.user import Role, User
from app.schemas.transaction import TransferCreate
from app.security import hash_password
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


async def _seed(db: AsyncSession) -> dict:
    org = Organization(name="T", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    user = User(
        org_id=org.id, username="u-t", email="u@t.example",
        password_hash=hash_password("pw-1234567"), role=Role.OWNER,
        is_active=True, email_verified=True,
    )
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add_all([user, at])
    await db.flush()
    src = Account(
        org_id=org.id, name="Src", account_type_id=at.id,
        balance=Decimal("1000"), currency="EUR",
    )
    dst = Account(
        org_id=org.id, name="Dst", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    third = Account(
        org_id=org.id, name="Third", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db.add_all([src, dst, third])
    await db.flush()
    transfer_cat = Category(
        org_id=org.id, name="Transfer", slug="transfer",
        type=CategoryType.BOTH, is_system=True,
    )
    groceries = Category(
        org_id=org.id, name="Groceries", slug="groceries", type=CategoryType.EXPENSE,
    )
    dining = Category(
        org_id=org.id, name="Dining", slug="dining", type=CategoryType.EXPENSE,
    )
    internal = Category(
        org_id=org.id, name="Internal", slug="internal", type=CategoryType.BOTH,
    )
    salary = Category(
        org_id=org.id, name="Salary", slug="salary", type=CategoryType.INCOME,
    )
    db.add_all([transfer_cat, groceries, dining, internal, salary])
    await db.commit()
    return {
        "org_id": org.id, "user_id": user.id,
        "src_id": src.id, "dst_id": dst.id, "third_id": third.id,
        "transfer_cat_id": transfer_cat.id, "groceries_id": groceries.id,
        "dining_id": dining.id, "internal_id": internal.id,
        "salary_id": salary.id,
    }


async def _row(
    db: AsyncSession, seed: dict, *, status: TransactionStatus,
    tx_date: date, settled: date | None, **extra,
) -> int:
    tx = Transaction(
        org_id=seed["org_id"], account_id=seed["src_id"],
        category_id=seed["groceries_id"], description="lunch",
        amount=Decimal("10"), type=TransactionType.EXPENSE,
        status=status, date=tx_date, settled_date=settled, **extra,
    )
    db.add(tx)
    await db.commit()
    return tx.id


async def _settled(db, seed, tx_date=date(2026, 5, 1), settled=None, **extra) -> int:
    return await _row(
        db, seed, status=TransactionStatus.SETTLED, tx_date=tx_date,
        settled=settled or tx_date, **extra,
    )


async def _pending(db, seed, tx_date=date(2026, 5, 1), settled=None, **extra) -> int:
    return await _row(
        db, seed, status=TransactionStatus.PENDING, tx_date=tx_date,
        settled=settled, **extra,
    )


async def _transfer(db: AsyncSession, seed: dict) -> tuple[int, int]:
    exp, inc = await transaction_service.create_transfer(
        db, seed["org_id"],
        TransferCreate(
            from_account_id=seed["src_id"], to_account_id=seed["dst_id"],
            category_id=seed["transfer_cat_id"], amount=Decimal("50"),
            date=date(2026, 5, 1), status="settled",
        ),
    )
    return exp.id, inc.id


async def _recurring_row(db: AsyncSession, seed: dict, *, settled: bool = False, **extra) -> int:
    r = RecurringTransaction(
        org_id=seed["org_id"], account_id=seed["src_id"],
        category_id=seed["groceries_id"], description="rent",
        amount=Decimal("10"), type="expense", frequency="monthly",
        next_due_date=date(2026, 6, 1), auto_settle=False, is_active=True,
        occurrences_elapsed=1,
    )
    db.add(r)
    await db.commit()
    if settled:
        return await _settled(db, seed, recurring_id=r.id, **extra)
    return await _pending(db, seed, recurring_id=r.id, **extra)


async def _get(db: AsyncSession, tx_id: int) -> Transaction:
    return (await db.execute(
        select(Transaction).where(Transaction.id == tx_id)
        .execution_options(populate_existing=True)
    )).scalar_one()


async def _balance(db: AsyncSession, account_id: int) -> Decimal:
    return (await db.execute(
        select(Account).where(Account.id == account_id)
        .execution_options(populate_existing=True)
    )).scalar_one().balance


async def _bulk(db, seed, ids, **fields):
    return await transaction_service.bulk_update_transactions(
        db, seed["org_id"], ids, actor_user_id=seed["user_id"], **fields,
    )


# -- date ------------------------------------------------------------------


async def test_date_is_set_on_plain_rows(db_session):
    seed = await _seed(db_session)
    pending = await _pending(db_session, seed)
    settled = await _settled(db_session, seed, settled=date(2026, 5, 20))
    new = date(2026, 5, 10)

    updated, skipped, notes = await _bulk(db_session, seed, [pending, settled], date=new)

    assert (updated, skipped, notes) == (2, [], [])
    assert (await _get(db_session, pending)).date == new
    row = await _get(db_session, settled)
    assert row.date == new
    assert row.settled_date == date(2026, 5, 20)  # untouched


async def test_recurring_row_skips_date_with_a_note_and_category_still_applies(db_session):
    seed = await _seed(db_session)
    rec = await _recurring_row(db_session, seed)

    updated, skipped, notes = await _bulk(
        db_session, seed, [rec], date=date(2026, 5, 10), category_id=seed["dining_id"],
    )

    assert updated == 1 and skipped == []
    assert notes == [(rec, "date not applied to recurring occurrences")]
    row = await _get(db_session, rec)
    assert row.date == date(2026, 5, 1)
    assert row.category_id == seed["dining_id"]


async def test_recurring_row_with_only_a_date_is_skipped(db_session):
    seed = await _seed(db_session)
    rec = await _recurring_row(db_session, seed)

    updated, skipped, notes = await _bulk(db_session, seed, [rec], date=date(2026, 5, 10))

    assert updated == 0 and notes == []
    assert skipped == [(rec, "date not applied to recurring occurrences")]


async def test_transfer_leg_skips_both_dates_with_notes_and_category_still_applies(db_session):
    seed = await _seed(db_session)
    exp_id, inc_id = await _transfer(db_session, seed)

    updated, skipped, notes = await _bulk(
        db_session, seed, [exp_id], date=date(2026, 5, 10),
        settled_date=date(2026, 5, 12), category_id=seed["internal_id"],
    )

    assert updated == 1 and skipped == []
    assert notes == [(
        exp_id,
        "date not applied to transfers; settlement date not applied to transfers",
    )]
    for leg in (exp_id, inc_id):
        row = await _get(db_session, leg)
        assert row.date == date(2026, 5, 1)
        assert row.settled_date == date(2026, 5, 1)
        assert row.category_id == seed["internal_id"]


async def test_manual_adjustment_is_skipped_whole(db_session):
    seed = await _seed(db_session)
    adj = await _settled(db_session, seed, is_manual_adjustment=True)
    plain = await _pending(db_session, seed)

    updated, skipped, notes = await _bulk(
        db_session, seed, [adj, plain], date=date(2026, 5, 10),
        category_id=seed["dining_id"],
    )

    assert updated == 1 and notes == []
    assert skipped == [(adj, "Manual balance adjustments cannot be edited")]
    assert (await _get(db_session, adj)).date == date(2026, 5, 1)


async def test_pending_row_keeps_its_settlement_gap(db_session):
    seed = await _seed(db_session)
    tx = await _pending(db_session, seed, settled=date(2026, 5, 4))

    updated, skipped, notes = await _bulk(db_session, seed, [tx], date=date(2026, 5, 10))

    assert (updated, skipped, notes) == (1, [], [])
    row = await _get(db_session, tx)
    assert row.date == date(2026, 5, 10)
    assert row.settled_date == date(2026, 5, 13)


async def test_settled_row_whose_new_date_passes_its_settlement_is_skipped(db_session):
    seed = await _seed(db_session)
    tx = await _settled(db_session, seed)

    updated, skipped, notes = await _bulk(db_session, seed, [tx], date=date(2026, 5, 10))

    note = "date not applied: settled on 2026-05-01; set the settlement date too"
    assert updated == 0 and notes == []
    assert skipped == [(tx, note)]
    row = await _get(db_session, tx)
    assert (row.date, row.settled_date) == (date(2026, 5, 1), date(2026, 5, 1))


async def test_settled_row_past_settlement_keeps_the_rest_of_the_batch(db_session):
    seed = await _seed(db_session)
    tx = await _settled(db_session, seed)

    updated, skipped, notes = await _bulk(
        db_session, seed, [tx], date=date(2026, 5, 10), category_id=seed["dining_id"],
    )

    assert updated == 1 and skipped == []
    assert notes == [
        (tx, "date not applied: settled on 2026-05-01; set the settlement date too"),
    ]
    row = await _get(db_session, tx)
    assert row.date == date(2026, 5, 1)
    assert row.category_id == seed["dining_id"]


async def test_settled_row_moves_past_its_settlement_when_both_are_set(db_session):
    seed = await _seed(db_session)
    tx = await _settled(db_session, seed)

    updated, skipped, notes = await _bulk(
        db_session, seed, [tx], date=date(2026, 5, 10), settled_date=date(2026, 5, 11),
    )

    assert (updated, skipped, notes) == (1, [], [])
    row = await _get(db_session, tx)
    assert (row.date, row.settled_date) == (date(2026, 5, 10), date(2026, 5, 11))


# -- settled_date ------------------------------------------------------------


async def test_settled_date_is_applied_to_settled_rows(db_session):
    seed = await _seed(db_session)
    a = await _settled(db_session, seed)
    b = await _settled(db_session, seed, tx_date=date(2026, 5, 2))

    updated, skipped, notes = await _bulk(
        db_session, seed, [a, b], settled_date=date(2026, 5, 6),
    )

    assert (updated, skipped, notes) == (2, [], [])
    assert (await _get(db_session, a)).settled_date == date(2026, 5, 6)
    assert (await _get(db_session, b)).settled_date == date(2026, 5, 6)


async def test_status_settled_settles_on_the_chosen_date(db_session):
    seed = await _seed(db_session)
    tx = await _pending(db_session, seed, settled=date(2026, 5, 3))
    before = await _balance(db_session, seed["src_id"])

    updated, skipped, notes = await _bulk(
        db_session, seed, [tx], status="settled", settled_date=date(2026, 5, 7),
    )

    assert (updated, skipped, notes) == (1, [], [])
    row = await _get(db_session, tx)
    assert row.status == TransactionStatus.SETTLED
    assert row.settled_date == date(2026, 5, 7)  # not today
    # Settling is the one thing in this batch that moves money.
    assert await _balance(db_session, seed["src_id"]) == before - Decimal("10")


async def test_settled_row_set_to_pending_does_not_take_the_settled_date(db_session):
    seed = await _seed(db_session)
    tx = await _settled(db_session, seed)

    updated, skipped, notes = await _bulk(
        db_session, seed, [tx], status="pending", settled_date=date(2026, 5, 9),
    )

    assert updated == 1 and skipped == []
    assert notes == [
        (tx, "settlement date not applied to rows being set to pending"),
    ]
    row = await _get(db_session, tx)
    assert row.status == TransactionStatus.PENDING
    assert row.settled_date is None


async def test_ordering_violation_is_skipped_and_the_rest_proceed(db_session):
    seed = await _seed(db_session)
    early = await _settled(db_session, seed, tx_date=date(2026, 5, 1))
    late = await _settled(db_session, seed, tx_date=date(2026, 5, 10))

    updated, skipped, notes = await _bulk(
        db_session, seed, [early, late], settled_date=date(2026, 5, 5),
        category_id=seed["dining_id"],
    )

    assert updated == 1 and notes == []
    assert skipped == [(late, "settled_date must be on or after date")]
    assert (await _get(db_session, early)).settled_date == date(2026, 5, 5)
    # All-or-nothing per row: the category did not land on the skipped row.
    row = await _get(db_session, late)
    assert row.settled_date == date(2026, 5, 10)
    assert row.category_id == seed["groceries_id"]


# -- balance -------------------------------------------------------------------


async def test_balance_does_not_move_on_date_and_settled_date_changes(db_session):
    seed = await _seed(db_session)
    a = await _settled(db_session, seed)
    b = await _settled(db_session, seed, tx_date=date(2026, 5, 2))
    p = await _pending(db_session, seed, settled=date(2026, 5, 3))
    before = await _balance(db_session, seed["src_id"])

    await _bulk(db_session, seed, [a, b, p], date=date(2026, 4, 20))
    await _bulk(db_session, seed, [a, b], settled_date=date(2026, 4, 25))
    await _bulk(
        db_session, seed, [a, b], date=date(2026, 6, 1), settled_date=date(2026, 6, 2),
    )

    assert await _balance(db_session, seed["src_id"]) == before
    row = await _get(db_session, a)
    assert (row.date, row.settled_date) == (date(2026, 6, 1), date(2026, 6, 2))


async def test_balance_moves_only_by_the_account_change_with_settled_date(db_session):
    seed = await _seed(db_session)
    tx = await _settled(db_session, seed)
    src_before = await _balance(db_session, seed["src_id"])
    third_before = await _balance(db_session, seed["third_id"])

    updated, skipped, notes = await _bulk(
        db_session, seed, [tx], account_id=seed["third_id"],
        settled_date=date(2026, 5, 8),
    )

    assert (updated, skipped, notes) == (1, [], [])
    row = await _get(db_session, tx)
    assert row.account_id == seed["third_id"]
    assert row.settled_date == date(2026, 5, 8)
    # The 10 expense leaves Src (reverted) and lands on Third (applied) once.
    assert await _balance(db_session, seed["src_id"]) == src_before + Decimal("10")
    assert await _balance(db_session, seed["third_id"]) == third_before - Decimal("10")


# -- review follow-ups ---------------------------------------------------------

RECURRING_NOTE = "date not applied to recurring occurrences"
EARLIER_NOTE = "settlement date not applied: before the unchanged date 2026-05-01"


async def test_recurring_settled_row_keeps_its_date_and_drops_an_earlier_settled_date(db_session):
    """The date is skipped by policy, so a settled_date chosen for the NEW
    date lands before the unchanged one: it is dropped with a note instead of
    failing the row and losing the category."""
    seed = await _seed(db_session)
    rec = await _recurring_row(db_session, seed, settled=True)

    updated, skipped, notes = await _bulk(
        db_session, seed, [rec], category_id=seed["dining_id"],
        date=date(2026, 4, 20), settled_date=date(2026, 4, 20),
    )

    assert (updated, skipped) == (1, [])
    assert notes == [(rec, f"{RECURRING_NOTE}; {EARLIER_NOTE}")]
    row = await _get(db_session, rec)
    assert row.category_id == seed["dining_id"]
    assert (row.date, row.settled_date) == (date(2026, 5, 1), date(2026, 5, 1))


async def test_recurring_pending_row_settled_in_the_batch_with_both_dates(db_session):
    seed = await _seed(db_session)
    rec = await _recurring_row(db_session, seed)
    before = await _balance(db_session, seed["src_id"])

    updated, skipped, notes = await _bulk(
        db_session, seed, [rec], category_id=seed["dining_id"], status="settled",
        date=date(2026, 4, 20), settled_date=date(2026, 4, 20),
    )

    assert (updated, skipped) == (1, [])
    assert notes == [(rec, f"{RECURRING_NOTE}; {EARLIER_NOTE}")]
    row = await _get(db_session, rec)
    assert row.status == TransactionStatus.SETTLED
    assert row.category_id == seed["dining_id"]
    assert row.date == date(2026, 5, 1)
    assert row.settled_date == date.today()  # the settle stamp, not 04-20
    assert await _balance(db_session, seed["src_id"]) == before - Decimal("10")


async def test_settling_a_pending_row_onto_a_future_date_says_why(db_session):
    """Settling stamps today; a new date after today would fail the ordering
    check with a reason that names neither field the user touched."""
    seed = await _seed(db_session)
    tx = await _pending(db_session, seed)

    updated, skipped, notes = await _bulk(
        db_session, seed, [tx], status="settled", date=date(2099, 1, 1),
        category_id=seed["dining_id"],
    )

    assert (updated, notes) == (0, [])
    assert skipped == [
        (tx, "date is after today's settlement; set the settlement date too"),
    ]
    row = await _get(db_session, tx)
    assert row.status == TransactionStatus.PENDING
    assert row.category_id == seed["groceries_id"]
    assert row.date == date(2026, 5, 1)


async def test_settled_row_set_to_pending_with_a_date(db_session):
    seed = await _seed(db_session)
    tx = await _settled(db_session, seed)
    before = await _balance(db_session, seed["src_id"])

    updated, skipped, notes = await _bulk(
        db_session, seed, [tx], status="pending", date=date(2026, 5, 10),
    )

    assert (updated, skipped, notes) == (1, [], [])
    row = await _get(db_session, tx)
    assert row.status == TransactionStatus.PENDING
    assert row.date == date(2026, 5, 10)
    assert row.settled_date is None
    # Unsettling reverts the 10 expense out of the balance.
    assert await _balance(db_session, seed["src_id"]) == before + Decimal("10")


async def test_reconcile_matched_one_way_row_takes_a_date_without_moving_money(db_session):
    seed = await _seed(db_session)
    canonical = await _settled(db_session, seed)
    dup = await _settled(
        db_session, seed, linked_transaction_id=canonical,
        reconciliation_state="matched",
    )
    before = await _balance(db_session, seed["src_id"])

    updated, skipped, notes = await _bulk(db_session, seed, [dup], date=date(2026, 4, 20))

    assert (updated, skipped, notes) == (1, [], [])
    assert (await _get(db_session, dup)).date == date(2026, 4, 20)
    assert (await _get(db_session, canonical)).date == date(2026, 5, 1)
    assert await _balance(db_session, seed["src_id"]) == before


async def test_reverted_recurring_row_takes_only_a_settled_date(db_session):
    seed = await _seed(db_session)
    rec = await _recurring_row(
        db_session, seed, settled=True, reconciliation_state="skipped",
    )
    before = await _balance(db_session, seed["src_id"])

    updated, skipped, notes = await _bulk(
        db_session, seed, [rec], settled_date=date(2026, 5, 6),
    )

    assert (updated, skipped, notes) == (1, [], [])
    assert (await _get(db_session, rec)).settled_date == date(2026, 5, 6)
    assert await _balance(db_session, seed["src_id"]) == before


async def test_settled_row_past_settlement_note_with_status_settled(db_session):
    seed = await _seed(db_session)
    tx = await _settled(db_session, seed)

    updated, skipped, notes = await _bulk(
        db_session, seed, [tx], status="settled", date=date(2026, 5, 10),
    )

    assert (updated, skipped) == (1, [])
    assert notes == [
        (tx, "date not applied: settled on 2026-05-01; set the settlement date too"),
    ]
    row = await _get(db_session, tx)
    assert row.status == TransactionStatus.SETTLED
    assert (row.date, row.settled_date) == (date(2026, 5, 1), date(2026, 5, 1))


async def test_a_failed_row_keeps_its_notes_in_the_skip_reason(db_session):
    seed = await _seed(db_session)
    rec = await _recurring_row(db_session, seed)

    updated, skipped, notes = await _bulk(
        db_session, seed, [rec], date=date(2026, 5, 10),
        category_id=seed["salary_id"],  # income-only on an expense row
    )

    assert (updated, notes) == (0, [])
    assert len(skipped) == 1 and skipped[0][0] == rec
    reason = skipped[0][1]
    assert reason.endswith(f"; {RECURRING_NOTE}")
    assert len(reason) > len(f"; {RECURRING_NOTE}")
