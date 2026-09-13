"""Editing a recurring-linked transaction syncs name/category forward.

- name/category edit on any linked instance updates the template AND all
  PENDING sibling instances
- SETTLED instances are never touched
- amount-only edits do not propagate
- editing a non-origin (settled) instance still propagates to the series
- TBD-315: account propagates too (template + ALL pending siblings), with no
  balance move on the propagated rows; amount and date still do not
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.transaction import TransactionUpdate
from app.services import recurring_service, transaction_service
from app.services.exceptions import ValidationError

pytestmark = pytest.mark.asyncio


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
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db.add(acct)
    await db.flush()
    exp = Category(org_id=org.id, name="Gym", slug="gym", type=CategoryType.EXPENSE)
    exp2 = Category(org_id=org.id, name="Health", slug="health", type=CategoryType.EXPENSE)
    db.add_all([exp, exp2])
    await db.commit()
    return {
        "org_id": org.id, "account_id": acct.id,
        "exp_cat": exp.id, "exp_cat2": exp2.id,
    }


async def _add_template(
    db: AsyncSession, seed: dict, *, account_id: int | None = None,
    next_due: date | None = None, auto_settle: bool = False,
) -> int:
    r = RecurringTransaction(
        org_id=seed["org_id"], account_id=account_id or seed["account_id"],
        category_id=seed["exp_cat"], description="Gym", amount=Decimal("30.00"),
        type="expense", frequency=Frequency.MONTHLY,
        next_due_date=next_due or date.today(),
        auto_settle=auto_settle, is_active=True,
    )
    db.add(r)
    await db.commit()
    return r.id


async def _add_instance(
    db: AsyncSession, seed: dict, recurring_id: int, *, status: TransactionStatus,
    description: str = "Gym", category_id: int | None = None, dt: date | None = None,
    account_id: int | None = None, amount: Decimal = Decimal("30.00"),
) -> int:
    when = dt or date.today()
    tx = Transaction(
        org_id=seed["org_id"], account_id=account_id or seed["account_id"],
        category_id=category_id or seed["exp_cat"], description=description,
        amount=amount, type=TransactionType.EXPENSE, status=status,
        date=when,
        settled_date=when if status == TransactionStatus.SETTLED else None,
        recurring_id=recurring_id,
    )
    db.add(tx)
    await db.commit()
    return tx.id


async def test_edit_name_propagates_to_template_and_pending(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    p1 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    p2 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    settled = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.SETTLED,
        dt=date.today() - timedelta(days=10),
    )

    await transaction_service.update_transaction(
        db_session, seed["org_id"], p1, TransactionUpdate(description="Gym Membership"),
    )

    db_session.expire_all()
    assert (await db_session.get(RecurringTransaction, rid)).description == "Gym Membership"
    assert (await db_session.get(Transaction, p1)).description == "Gym Membership"
    assert (await db_session.get(Transaction, p2)).description == "Gym Membership"
    assert (await db_session.get(Transaction, settled)).description == "Gym"


async def test_edit_category_propagates_to_template_and_pending(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    p1 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    settled = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.SETTLED,
        dt=date.today() - timedelta(days=10),
    )

    await transaction_service.update_transaction(
        db_session, seed["org_id"], p1, TransactionUpdate(category_id=seed["exp_cat2"]),
    )

    db_session.expire_all()
    assert (await db_session.get(RecurringTransaction, rid)).category_id == seed["exp_cat2"]
    assert (await db_session.get(Transaction, p1)).category_id == seed["exp_cat2"]
    assert (await db_session.get(Transaction, settled)).category_id == seed["exp_cat"]


async def test_amount_only_edit_does_not_propagate(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    p1 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)

    await transaction_service.update_transaction(
        db_session, seed["org_id"], p1, TransactionUpdate(amount=Decimal("99.00")),
    )

    db_session.expire_all()
    tmpl = await db_session.get(RecurringTransaction, rid)
    assert tmpl.description == "Gym"
    assert tmpl.amount == Decimal("30.00")


async def test_edit_from_settled_instance_still_propagates(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    pending = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    settled = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.SETTLED,
        dt=date.today() - timedelta(days=10),
    )

    await transaction_service.update_transaction(
        db_session, seed["org_id"], settled, TransactionUpdate(description="Renamed"),
    )

    db_session.expire_all()
    assert (await db_session.get(RecurringTransaction, rid)).description == "Renamed"
    assert (await db_session.get(Transaction, pending)).description == "Renamed"
    assert (await db_session.get(Transaction, settled)).description == "Renamed"


async def test_edit_name_and_category_together_propagate(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    p1 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)

    await transaction_service.update_transaction(
        db_session,
        seed["org_id"],
        p1,
        TransactionUpdate(description="Gym Plus", category_id=seed["exp_cat2"]),
    )

    db_session.expire_all()
    tmpl = await db_session.get(RecurringTransaction, rid)
    assert tmpl.description == "Gym Plus"
    assert tmpl.category_id == seed["exp_cat2"]
    inst = await db_session.get(Transaction, p1)
    assert inst.description == "Gym Plus"
    assert inst.category_id == seed["exp_cat2"]


async def test_category_not_propagated_when_type_also_changed(db_session):
    from app.models.category import Category, CategoryType
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    p1 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)

    inc = Category(org_id=seed["org_id"], name="Bonus", slug="bonus", type=CategoryType.INCOME)
    db_session.add(inc)
    await db_session.commit()
    inc_id = inc.id

    await transaction_service.update_transaction(
        db_session, seed["org_id"], p1,
        TransactionUpdate(type="income", category_id=inc_id),
    )

    db_session.expire_all()
    tmpl = await db_session.get(RecurringTransaction, rid)
    # Template keeps its original expense category and type; no corrupting cross-type write.
    assert tmpl.category_id == seed["exp_cat"]
    assert tmpl.type == "expense"
    # The edited row itself did change (its own type/category), but that's local.
    edited = await db_session.get(Transaction, p1)
    assert edited.category_id == inc_id


async def test_category_not_propagated_from_type_diverged_instance(db_session):
    from app.models.category import Category, CategoryType
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)  # expense template, exp_cat
    diverged = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    sibling = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    inc1 = Category(org_id=seed["org_id"], name="Salary", slug="salary", type=CategoryType.INCOME)
    inc2 = Category(org_id=seed["org_id"], name="Bonus", slug="bonus", type=CategoryType.INCOME)
    db_session.add_all([inc1, inc2])
    await db_session.commit()
    inc1_id, inc2_id = inc1.id, inc2.id

    # Diverge `diverged` to income (type + compatible income category together).
    await transaction_service.update_transaction(
        db_session, seed["org_id"], diverged,
        TransactionUpdate(type="income", category_id=inc1_id),
    )
    # Now edit ONLY its category to another income category (no type change).
    await transaction_service.update_transaction(
        db_session, seed["org_id"], diverged, TransactionUpdate(category_id=inc2_id),
    )

    db_session.expire_all()
    # Expense template + expense sibling must NOT receive the income category.
    assert (await db_session.get(RecurringTransaction, rid)).category_id == seed["exp_cat"]
    assert (await db_session.get(Transaction, sibling)).category_id == seed["exp_cat"]
    # The edited (income) row itself changes.
    assert (await db_session.get(Transaction, diverged)).category_id == inc2_id


async def test_category_propagation_skips_type_diverged_sibling(db_session):
    from app.models.category import Category, CategoryType
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)  # expense template, exp_cat
    normal = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    diverged_sib = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    inc = Category(org_id=seed["org_id"], name="Salary", slug="salary", type=CategoryType.INCOME)
    db_session.add(inc)
    await db_session.commit()
    inc_id = inc.id

    # Diverge the sibling to income.
    await transaction_service.update_transaction(
        db_session, seed["org_id"], diverged_sib,
        TransactionUpdate(type="income", category_id=inc_id),
    )
    # Edit the NORMAL expense instance's category.
    await transaction_service.update_transaction(
        db_session, seed["org_id"], normal, TransactionUpdate(category_id=seed["exp_cat2"]),
    )

    db_session.expire_all()
    # Template (expense) + the normal expense row get the new expense category.
    assert (await db_session.get(RecurringTransaction, rid)).category_id == seed["exp_cat2"]
    assert (await db_session.get(Transaction, normal)).category_id == seed["exp_cat2"]
    # The income-diverged sibling is NOT overwritten with an expense category.
    assert (await db_session.get(Transaction, diverged_sib)).category_id == inc_id


async def test_unchanged_resave_does_not_propagate(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)  # description "Gym"
    p1 = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING, description="Gym")
    # Drift the template so that, IF propagation wrongly fired, it would overwrite this.
    tmpl = await db_session.get(RecurringTransaction, rid)
    tmpl.description = "Drifted"
    await db_session.commit()
    # Re-save with the SAME description (no real change) plus an amount edit.
    await transaction_service.update_transaction(
        db_session, seed["org_id"], p1,
        TransactionUpdate(description="Gym", amount=Decimal("12.00")),
    )
    db_session.expire_all()
    # description didn't change → propagation must not have fired → drift preserved.
    assert (await db_session.get(RecurringTransaction, rid)).description == "Drifted"


async def test_stop_clears_recurring_link_on_survivors(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    settled = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.SETTLED,
        dt=date.today() - timedelta(days=10),
    )
    future_pending = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.PENDING,
        dt=date.today() + timedelta(days=10),
    )

    await recurring_service.stop_recurring(db_session, seed["org_id"], rid)

    db_session.expire_all()
    survivor = await db_session.get(Transaction, settled)
    assert survivor is not None
    assert survivor.recurring_id is None
    assert (await db_session.get(Transaction, future_pending)) is None


async def test_delete_clears_recurring_link_on_survivors(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    settled = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.SETTLED,
        dt=date.today() - timedelta(days=10),
    )

    await recurring_service.delete_recurring(db_session, seed["org_id"], rid)

    db_session.expire_all()
    survivor = await db_session.get(Transaction, settled)
    assert survivor is not None
    assert survivor.recurring_id is None


# ── TBD-315: account propagation ─────────────────────────────────────────────
#
# Fixed clock so generation windows are deterministic (billing_cycle_day=1, so
# the cycle containing TODAY is 2026-03-01..2026-03-31).
TODAY = date(2026, 3, 15)


async def _add_account(
    db: AsyncSession, org_id: int, name: str, balance: str, *, at_id: int | None = None,
) -> int:
    if at_id is None:
        at = AccountType(org_id=org_id, name=f"T-{name}", slug=f"t-{name.lower()}")
        db.add(at)
        await db.flush()
        at_id = at.id
    acct = Account(
        org_id=org_id, name=name, account_type_id=at_id,
        balance=Decimal(balance), currency="EUR",
    )
    db.add(acct)
    await db.commit()
    return acct.id


async def _balance(db: AsyncSession, account_id: int) -> Decimal:
    return (await db.get(Account, account_id)).balance


async def test_account_edit_reaches_next_generated_occurrence(db_session):
    """fence F1 (DoD). Kills: moving pending rows but not the template, and
    moving only the edited row -- either way generation copies A again."""
    seed = await _seed(db_session)
    acct_a = seed["account_id"]
    acct_b = await _add_account(db_session, seed["org_id"], "B", "0")
    rid = await _add_template(db_session, seed, next_due=date(2026, 3, 20))

    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=TODAY)
    first = (await db_session.execute(
        select(Transaction).where(Transaction.recurring_id == rid)
    )).scalar_one()
    assert first.status == TransactionStatus.PENDING and first.account_id == acct_a

    await transaction_service.update_transaction(
        db_session, seed["org_id"], first.id, TransactionUpdate(account_id=acct_b),
    )
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=date(2026, 4, 5),
    )

    db_session.expire_all()
    new_row = (await db_session.execute(
        select(Transaction).where(
            Transaction.recurring_id == rid, Transaction.date == date(2026, 4, 20),
        )
    )).scalar_one()
    assert new_row.account_id == acct_b
    assert (await db_session.get(RecurringTransaction, rid)).account_id == acct_b


async def test_account_edit_on_settled_row_moves_only_its_own_balance(db_session):
    """fence F2. Rows are inserted by the helper WITHOUT touching balances, so
    stored balances are SEEDED to what the service would have produced:
    A = 1000 - 11 (settled sibling) - 50 (edited settled row) = 939, B = 1000.
    Kills: applying a balance move to the propagated pending row (B -> 927),
    and a bulk UPDATE missing the PENDING filter (no balance moves, so only the
    settled sibling's account_id check sees it)."""
    seed = await _seed(db_session)
    org_id = seed["org_id"]
    acct_a = seed["account_id"]
    a = await db_session.get(Account, acct_a)
    a.balance = Decimal("939.00")
    await db_session.commit()
    acct_b = await _add_account(db_session, org_id, "B", "1000.00")
    rid = await _add_template(db_session, seed)
    settled_sib = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.SETTLED,
        dt=TODAY - timedelta(days=40), amount=Decimal("11.00"),
    )
    pending_sib = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.PENDING,
        dt=TODAY + timedelta(days=10), amount=Decimal("23.00"),
    )
    edited = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.SETTLED,
        dt=TODAY - timedelta(days=5), amount=Decimal("50.00"),
    )

    await transaction_service.update_transaction(
        db_session, org_id, edited, TransactionUpdate(account_id=acct_b),
    )

    db_session.expire_all()
    assert await _balance(db_session, acct_a) == Decimal("989.00")
    assert await _balance(db_session, acct_b) == Decimal("950.00")
    assert (await db_session.get(Transaction, edited)).account_id == acct_b
    assert (await db_session.get(Transaction, settled_sib)).account_id == acct_a
    assert (await db_session.get(Transaction, pending_sib)).account_id == acct_b
    assert (await db_session.get(RecurringTransaction, rid)).account_id == acct_b


async def test_propagated_pending_row_auto_settles_on_new_account(db_session):
    """fence F3. Kills: writing only the template -- the due pending sibling
    would settle on its own (old) account A. Balances seeded at 1000 each;
    the helper inserts pending rows without balance effect (correctly: pending
    amounts are never in accounts.balance)."""
    seed = await _seed(db_session)
    org_id = seed["org_id"]
    acct_a = seed["account_id"]
    a = await db_session.get(Account, acct_a)
    a.balance = Decimal("1000.00")
    await db_session.commit()
    acct_b = await _add_account(db_session, org_id, "B", "1000.00")
    # next_due beyond the cycle end, so the run generates nothing new and the
    # only balance move is the auto-settle of the due sibling.
    rid = await _add_template(
        db_session, seed, next_due=date(2026, 4, 20), auto_settle=True,
    )
    due_sib = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.PENDING,
        dt=TODAY - timedelta(days=5), amount=Decimal("40.00"),
    )
    edited = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.PENDING,
        dt=TODAY + timedelta(days=10), amount=Decimal("40.00"),
    )

    await transaction_service.update_transaction(
        db_session, org_id, edited, TransactionUpdate(account_id=acct_b),
    )
    await recurring_service.generate_due_transactions(db_session, org_id, today=TODAY)

    db_session.expire_all()
    sib = await db_session.get(Transaction, due_sib)
    assert sib.status == TransactionStatus.SETTLED
    assert sib.account_id == acct_b
    assert await _balance(db_session, acct_a) == Decimal("1000.00")
    assert await _balance(db_session, acct_b) == Decimal("960.00")


async def test_resave_with_same_account_does_not_propagate(db_session):
    """fence F4. Kills: a `body.account_id is not None` trigger without the
    `!= old_account_id` comparison -- it would drag the individually-moved
    sibling (C) and the drifted template (C) back to A."""
    seed = await _seed(db_session)
    org_id = seed["org_id"]
    acct_a = seed["account_id"]
    acct_c = await _add_account(db_session, org_id, "C", "0")
    rid = await _add_template(db_session, seed, account_id=acct_c)
    sib = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.PENDING, account_id=acct_c,
    )
    x = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)

    await transaction_service.update_transaction(
        db_session, org_id, x, TransactionUpdate(account_id=acct_a, amount=Decimal("99.00")),
    )

    db_session.expire_all()
    assert (await db_session.get(Transaction, sib)).account_id == acct_c
    assert (await db_session.get(RecurringTransaction, rid)).account_id == acct_c


async def test_cross_org_account_is_rejected_before_any_propagation(db_session):
    """fence F5. Kills: propagating before validation / writing the raw body
    value. Reads happen in the SAME, uncommitted session transaction on
    purpose: a rollback would also discard a premature propagation write and
    hide exactly the ordering this fence exists to pin."""
    seed = await _seed(db_session)
    acct_a = seed["account_id"]
    other = Organization(name="Other", billing_cycle_day=1)
    db_session.add(other)
    await db_session.commit()
    foreign = await _add_account(db_session, other.id, "Foreign", "0")
    rid = await _add_template(db_session, seed)
    sib = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)
    x = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING)

    with pytest.raises(ValidationError):
        await transaction_service.update_transaction(
            db_session, seed["org_id"], x, TransactionUpdate(account_id=foreign),
        )

    db_session.expire_all()
    assert (await db_session.get(RecurringTransaction, rid)).account_id == acct_a
    assert (await db_session.get(Transaction, sib)).account_id == acct_a
    assert (await db_session.get(Transaction, x)).account_id == acct_a


async def test_account_and_amount_edit_propagates_only_the_account(db_session):
    """guard F6. Kills: adding amount (or date) to the propagation. TBD-273
    owns per-occurrence overrides."""
    seed = await _seed(db_session)
    acct_b = await _add_account(db_session, seed["org_id"], "B", "0")
    rid = await _add_template(db_session, seed)
    sib = await _add_instance(
        db_session, seed, rid, status=TransactionStatus.PENDING, dt=TODAY + timedelta(days=30),
    )
    x = await _add_instance(db_session, seed, rid, status=TransactionStatus.PENDING, dt=TODAY)

    await transaction_service.update_transaction(
        db_session, seed["org_id"], x,
        TransactionUpdate(account_id=acct_b, amount=Decimal("77.00"), date=TODAY + timedelta(days=1)),
    )

    db_session.expire_all()
    tmpl = await db_session.get(RecurringTransaction, rid)
    assert tmpl.account_id == acct_b
    assert tmpl.amount == Decimal("30.00")
    s = await db_session.get(Transaction, sib)
    assert s.account_id == acct_b
    assert s.amount == Decimal("30.00")
    assert s.date == TODAY + timedelta(days=30)
