"""F2 edit policy on linked transfer pairs (PR-B.5)."""
import pytest
import pytest_asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.transaction import TransactionUpdate
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


async def _seed_pair_with_balances(session: AsyncSession, src_balance, dst_balance, amount, currency="EUR"):
    """Seed a linked transfer pair with the given account balances reflecting the transfer."""
    org = Organization(name="T", billing_cycle_day=1)
    session.add(org)
    await session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    session.add(at)
    await session.flush()
    src = Account(org_id=org.id, name="Src", account_type_id=at.id, balance=src_balance, currency=currency)
    dst = Account(org_id=org.id, name="Dst", account_type_id=at.id, balance=dst_balance, currency=currency)
    session.add_all([src, dst])
    cat = Category(org_id=org.id, name="Transfer", slug="transfer", type=CategoryType.BOTH, is_system=True)
    session.add(cat)
    await session.flush()
    expense = Transaction(
        org_id=org.id, account_id=src.id, category_id=cat.id,
        description="t", amount=amount, type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    income = Transaction(
        org_id=org.id, account_id=dst.id, category_id=cat.id,
        description="t", amount=amount, type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    session.add_all([expense, income])
    await session.flush()
    expense.linked_transaction_id = income.id
    income.linked_transaction_id = expense.id
    await session.commit()
    return org, src, dst, expense, income


async def test_edit_linked_row_amount_mirrors_to_partner(db_session):
    """PATCH amount on EXPENSE leg → partner's amount mirrors atomically; both balances re-applied."""
    org, src, dst, exp, inc = await _seed_pair_with_balances(
        db_session, src_balance=Decimal("400"), dst_balance=Decimal("100"), amount=Decimal("100"),
    )
    # Increase amount from 100 to 150
    body = TransactionUpdate(amount=Decimal("150"))
    result = await transaction_service.update_transaction(db_session, org.id, exp.id, body)

    # Partner amount mirrored
    partner = await db_session.scalar(select(Transaction).where(Transaction.id == inc.id))
    assert partner.amount == Decimal("150")
    assert result.amount == Decimal("150")

    # Balances re-applied: src goes from 400 (post-100-expense) to 350 (post-150-expense),
    # dst goes from 100 (post-100-income) to 150 (post-150-income).
    await db_session.refresh(src)
    await db_session.refresh(dst)
    assert src.balance == Decimal("350")
    assert dst.balance == Decimal("150")


async def test_edit_linked_row_type_rejected(db_session):
    """PATCH type on linked row → ValidationError."""
    from app.services.exceptions import ValidationError
    org, src, dst, exp, inc = await _seed_pair_with_balances(
        db_session, src_balance=Decimal("400"), dst_balance=Decimal("100"), amount=Decimal("100"),
    )
    body = TransactionUpdate(type="income")  # try to flip the expense leg to income
    with pytest.raises(ValidationError):
        await transaction_service.update_transaction(db_session, org.id, exp.id, body)


async def test_edit_linked_row_account_id_validates_currency(db_session):
    """PATCH account_id on linked row to a different-currency account → ValidationError."""
    from app.services.exceptions import ValidationError
    org, src, dst, exp, inc = await _seed_pair_with_balances(
        db_session, src_balance=Decimal("400"), dst_balance=Decimal("100"), amount=Decimal("100"),
    )
    # Add a USD account
    at = await db_session.scalar(select(AccountType).where(AccountType.org_id == org.id))
    usd_acct = Account(org_id=org.id, name="USD", account_type_id=at.id, balance=Decimal("0"), currency="USD")
    db_session.add(usd_acct)
    await db_session.commit()

    body = TransactionUpdate(account_id=usd_acct.id)
    with pytest.raises(ValidationError):
        await transaction_service.update_transaction(db_session, org.id, exp.id, body)


async def test_edit_linked_row_account_id_rejects_partner_account(db_session):
    """PATCH expense leg's account_id to the partner's (income leg's) account → ValidationError."""
    from app.services.exceptions import ValidationError
    org, src, dst, exp, inc = await _seed_pair_with_balances(
        db_session, src_balance=Decimal("400"), dst_balance=Decimal("100"), amount=Decimal("100"),
    )
    body = TransactionUpdate(account_id=inc.account_id)
    with pytest.raises(ValidationError):
        await transaction_service.update_transaction(db_session, org.id, exp.id, body)


async def test_edit_linked_row_per_leg_status_independent(db_session):
    """PATCH status on one leg leaves partner status untouched."""
    org, src, dst, exp, inc = await _seed_pair_with_balances(
        db_session, src_balance=Decimal("400"), dst_balance=Decimal("100"), amount=Decimal("100"),
    )
    body = TransactionUpdate(status="pending")
    result = await transaction_service.update_transaction(db_session, org.id, exp.id, body)
    assert result.status == TransactionStatus.PENDING
    assert result.settled_date is None  # cleared on pending

    # Partner unchanged
    partner = await db_session.scalar(select(Transaction).where(Transaction.id == inc.id))
    assert partner.status == TransactionStatus.SETTLED


async def test_edit_linked_row_settled_date_with_provided_value(db_session):
    """status=settled with provided settled_date uses provided date."""
    org, src, dst, exp, inc = await _seed_pair_with_balances(
        db_session, src_balance=Decimal("400"), dst_balance=Decimal("100"), amount=Decimal("100"),
    )
    # Move expense leg to pending first
    await transaction_service.update_transaction(db_session, org.id, exp.id, TransactionUpdate(status="pending"))
    # Now back to settled with a specific settled_date
    body = TransactionUpdate(status="settled", settled_date=date(2026, 5, 5))
    result = await transaction_service.update_transaction(db_session, org.id, exp.id, body)
    assert result.status == TransactionStatus.SETTLED
    assert result.settled_date == date(2026, 5, 5)


async def test_edit_linked_row_settled_date_defaults_to_today_on_transition(db_session):
    """Transition to settled with NO settled_date provided sets today."""
    import datetime as dt
    org, src, dst, exp, inc = await _seed_pair_with_balances(
        db_session, src_balance=Decimal("400"), dst_balance=Decimal("100"), amount=Decimal("100"),
    )
    await transaction_service.update_transaction(db_session, org.id, exp.id, TransactionUpdate(status="pending"))
    body = TransactionUpdate(status="settled")
    result = await transaction_service.update_transaction(db_session, org.id, exp.id, body)
    assert result.status == TransactionStatus.SETTLED
    assert result.settled_date == dt.date.today()


async def test_edit_unlinked_row_still_works(db_session):
    """Plain (non-transfer) row edits still work as before — no regression."""
    org = Organization(name="T", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    acct = Account(org_id=org.id, name="A", account_type_id=at.id, balance=Decimal("500"), currency="EUR")
    db_session.add(acct)
    cat = Category(org_id=org.id, name="C", slug="c", type=CategoryType.BOTH, is_system=True)
    db_session.add(cat)
    await db_session.flush()
    plain = Transaction(
        org_id=org.id, account_id=acct.id, category_id=cat.id,
        description="orig", amount=Decimal("50"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    db_session.add(plain)
    await db_session.commit()
    body = TransactionUpdate(description="updated", amount=Decimal("75"))
    result = await transaction_service.update_transaction(db_session, org.id, plain.id, body)
    assert result.description == "updated"
    assert result.amount == Decimal("75")


async def test_edit_raises_conflict_when_link_appears_after_preview(db_session):
    """Synthetic race: row's linked_transaction_id points at a partner not in the
    locked set. Simulates a concurrent pair landing between unlocked preview
    and locked SELECT. Must raise ConflictError, not silently bypass guards.
    """
    from app.services.exceptions import ConflictError
    org = Organization(name="T", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    acct = Account(org_id=org.id, name="A", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    db_session.add(acct)
    cat = Category(org_id=org.id, name="C", slug="c", type=CategoryType.BOTH, is_system=True)
    db_session.add(cat)
    await db_session.flush()

    tx = Transaction(
        org_id=org.id, account_id=acct.id, category_id=cat.id,
        description="x", amount=Decimal("10"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    db_session.add(tx)
    await db_session.commit()

    # Patch the unlocked preview path so it returns an unlinked snapshot,
    # while the actual DB row gets a stale linked_transaction_id pointing at
    # a non-existent partner. The locked SELECT then returns a tx with a
    # linked_transaction_id that isn't in the lock set.
    from sqlalchemy import text as _sql_text, update as _sql_update
    # Set a stale link to a non-existent partner id. Disable FK enforcement
    # for the synthetic write since prod rows would never satisfy FK either
    # in this race window — the partner row exists but isn't in our lock set.
    fake_partner_id = tx.id + 9999
    await db_session.execute(_sql_text("PRAGMA foreign_keys=OFF"))
    await db_session.execute(
        _sql_update(Transaction).where(Transaction.id == tx.id).values(linked_transaction_id=fake_partner_id)
    )
    await db_session.commit()
    await db_session.execute(_sql_text("PRAGMA foreign_keys=ON"))

    with pytest.raises(ConflictError):
        await transaction_service.update_transaction(
            db_session, org.id, tx.id, TransactionUpdate(amount=Decimal("20"))
        )


async def test_edit_raises_conflict_on_bidirectional_link_violation(db_session):
    """If the partner's linked_transaction_id doesn't point back to tx, the
    pair is corrupted. Edits must raise ConflictError, not proceed.
    """
    from app.services.exceptions import ConflictError
    org = Organization(name="T", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    src = Account(org_id=org.id, name="Src", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    dst = Account(org_id=org.id, name="Dst", account_type_id=at.id, balance=Decimal("0"), currency="EUR")
    db_session.add_all([src, dst])
    cat = Category(org_id=org.id, name="C", slug="c", type=CategoryType.BOTH, is_system=True)
    db_session.add(cat)
    await db_session.flush()

    expense = Transaction(
        org_id=org.id, account_id=src.id, category_id=cat.id,
        description="x", amount=Decimal("10"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    income = Transaction(
        org_id=org.id, account_id=dst.id, category_id=cat.id,
        description="x", amount=Decimal("10"),
        type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    )
    db_session.add_all([expense, income])
    await db_session.flush()
    # Asymmetric link: expense thinks income is its partner, but income's link
    # points elsewhere (None to simulate a corrupted half-pair).
    expense.linked_transaction_id = income.id
    income.linked_transaction_id = None
    await db_session.commit()

    with pytest.raises(ConflictError):
        await transaction_service.update_transaction(
            db_session, org.id, expense.id, TransactionUpdate(amount=Decimal("20"))
        )
