"""TBD-798: a legacy ``'eur'`` / ``'EUR '`` account is the same currency as an
``'EUR'`` one at every transfer surface, not only in the scoped figures.

``accounts.currency`` was free text before TBD-325 PR 1 and cannot be repaired
through the product. Each fence below names the site it kills. The SQL
detector fence is red on SQLite and vacuous on MySQL (case-insensitive
collation); every other fence here is an in-Python comparison and is red on
both engines.

Not fenced, on purpose: the currency clause in ``find_duplicate_of_linked_leg``
and its batch twin. Both callers pass the currency they read from the SAME
account the query is pinned to (``account_id == ...``), so the clause compares
an account with itself and no stored value can make a raw and a folded compare
disagree. Reverting either is an equivalent mutant.

Scoped figures and ``find_match_candidates`` are fenced in
``test_currency_scope_aggregates.py``.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agent.registry import ToolContext
from app.agent.sensitive_tools import _transfer_preview
from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.schemas.import_schemas import ImportConfirmRequest, ImportConfirmRow
from app.schemas.recurring import RecurringCreate
from app.schemas.transaction import TransactionUpdate, TransferCreate
from app.services import import_service, recurring_service, transaction_service
from app.services.import_parser import ParsedRow
from tests.routers.test_transactions_pair_routes import (  # noqa: F401  (fixture)
    _add_tx,
    _seed_base,
    make_app,
    session_factory,
)
from tests.services import test_plan_projection_service as plan_t

DAY = datetime.date(2026, 5, 1)
AMOUNT = Decimal(25)


@pytest_asyncio.fixture
async def db() -> AsyncSession:
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


@pytest_asyncio.fixture
async def books(db) -> dict:
    """One org: ``'EUR'``, legacy ``'eur'`` and padded ``'EUR '`` accounts, a
    Transfer category, and an expense and an income category."""
    org = Organization(name="Spellings", billing_cycle_day=1, primary_currency="EUR")
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    accts = {
        code: Account(org_id=org.id, account_type_id=at.id, name=f"Acct {code!r}",
                      balance=Decimal(0), currency=code)
        for code in ("EUR", "eur", "EUR ")
    }
    cats = {
        "transfer": Category(org_id=org.id, name="Transfer", slug="transfer",
                             type=CategoryType.BOTH, is_system=True),
        "food": Category(org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE),
        "pay": Category(org_id=org.id, name="Pay", slug="pay", type=CategoryType.INCOME),
    }
    db.add_all([*accts.values(), *cats.values()])
    await db.commit()
    return {"org_id": org.id, "acct": {k: a.id for k, a in accts.items()},
            "cat": {k: c.id for k, c in cats.items()}}


async def _tx(db, books, code, tx_type, *, category="transfer") -> Transaction:
    tx = Transaction(
        org_id=books["org_id"], account_id=books["acct"][code],
        category_id=books["cat"][category], description="row", amount=AMOUNT,
        type=tx_type, status=TransactionStatus.SETTLED, date=DAY, settled_date=DAY,
    )
    db.add(tx)
    await db.commit()
    return tx


async def _pair(db, books, expense_code, income_code) -> tuple[Transaction, Transaction]:
    e = await _tx(db, books, expense_code, TransactionType.EXPENSE)
    i = await _tx(db, books, income_code, TransactionType.INCOME)
    return await transaction_service.pair_existing_transactions(
        db, books["org_id"], e.id, i.id, recategorize=False)


# ── import ──────────────────────────────────────────────────────────────────


async def test_import_preview_offers_the_lowercase_partner_and_confirm_pairs_it(db, books):
    """Kills the bare compare in ``find_match_candidates_batch`` (the preview
    offers nothing) and the raw ``!=`` in the ``pair_with_existing`` confirm
    (a 'Currency mismatch detected at confirm; re-preview' 409 loop)."""
    partner = await _tx(db, books, "eur", TransactionType.INCOME)
    preview = await import_service.build_preview(
        db, org_id=books["org_id"], account_id=books["acct"]["EUR"], file_name="t.csv",
        parsed_rows=[ParsedRow(row_number=1, date=DAY, description="TO SAVINGS",
                               amount=AMOUNT, type="expense", counterparty=None,
                               transaction_type=None)],
    )
    assert preview.rows[0].pair_with_transaction_id == partner.id

    result = await import_service.execute_import(db, org_id=books["org_id"], body=ImportConfirmRequest(
        account_id=books["acct"]["EUR"], default_category_id=books["cat"]["transfer"],
        file_name="t.csv", source_format="csv",
        rows=[ImportConfirmRow(
            row_number=1, date=DAY, description="TO SAVINGS", amount=AMOUNT, type="expense",
            category_id=books["cat"]["transfer"], action="pair_with_existing",
            pair_with_transaction_id=partner.id, recategorize=False,
        )],
    ))
    assert (result.paired_count, result.error_count) == (1, 0), result


async def test_import_create_transfer_pair_onto_a_lowercase_account(db, books):
    """Kills the raw ``!=`` in the ``create_transfer_pair`` partner check."""
    result = await import_service.execute_import(db, org_id=books["org_id"], body=ImportConfirmRequest(
        account_id=books["acct"]["EUR"], default_category_id=books["cat"]["transfer"],
        file_name="t.csv", source_format="csv",
        rows=[ImportConfirmRow(
            row_number=1, date=DAY, description="TO SAVINGS", amount=AMOUNT, type="expense",
            category_id=books["cat"]["transfer"], action="create_transfer_pair",
            partner_account_id=books["acct"]["eur"], recategorize=False,
        )],
    ))
    assert (result.paired_count, result.error_count) == (1, 0), result


# ── transaction_service ─────────────────────────────────────────────────────


async def test_convert_and_create_leg_into_a_lowercase_account(db, books):
    """Kills the raw ``!=`` in ``convert_and_create_leg``'s pre-check AND in
    its locked re-check (either one alone refuses)."""
    source = await _tx(db, books, "EUR", TransactionType.EXPENSE, category="food")
    expense, income = await transaction_service.convert_and_create_leg(
        db, books["org_id"], source.id, destination_account_id=books["acct"]["eur"])
    assert income.account_id == books["acct"]["eur"]
    assert expense.linked_transaction_id == income.id


async def test_editing_a_mixed_spelling_pair_passes_the_post_edit_check(db, books):
    """Kills the raw ``!=`` in ``update_transaction``'s post-edit invariant:
    any edit of an 'EUR'/'eur' pair raised 'Pair currencies differ after edit'."""
    expense, _income = await _pair(db, books, "EUR", "eur")
    out = await transaction_service.update_transaction(
        db, books["org_id"], expense.id, TransactionUpdate(description="renamed"))
    assert out.description == "renamed"


async def test_moving_a_leg_onto_a_padded_account_keeps_the_pair(db, books):
    """Kills the raw ``!=`` in ``update_transaction``'s new-account check: the
    leg moves from 'EUR' to 'EUR ' while its partner sits on 'eur'."""
    expense, _income = await _pair(db, books, "EUR", "eur")
    out = await transaction_service.update_transaction(
        db, books["org_id"], expense.id, TransactionUpdate(account_id=books["acct"]["EUR "]))
    assert out.account_id == books["acct"]["EUR "]


# ── recurring ───────────────────────────────────────────────────────────────


async def test_a_recurring_transfer_between_spellings_is_accepted(db, books):
    """Kills the raw ``!=`` in ``recurring_service._validate_transfer_accounts``."""
    r = await recurring_service.create_recurring(db, books["org_id"], RecurringCreate(
        account_id=books["acct"]["EUR"], to_account_id=books["acct"]["eur"],
        category_id=books["cat"]["transfer"], description="To savings", amount=AMOUNT,
        type="transfer", frequency="monthly", next_due_date=DAY + datetime.timedelta(days=20),
        auto_settle=False,
    ), today=DAY)
    assert r.to_account_id == books["acct"]["eur"]


# ── agent ───────────────────────────────────────────────────────────────────


async def test_the_agent_transfer_preview_accepts_mixed_spellings(db, books):
    """Kills the raw ``!=`` in ``sensitive_tools._transfer_preview``."""
    ctx = ToolContext(db=db, user=None, org_id=books["org_id"], channel="in_app",
                      api_token_id=None)
    preview = await _transfer_preview(ctx, TransferCreate(
        from_account_id=books["acct"]["EUR"], to_account_id=books["acct"]["eur"],
        amount=AMOUNT, date=DAY,
    ))
    assert preview.summary == "Move money between two accounts"


# ── router ──────────────────────────────────────────────────────────────────


async def test_the_match_endpoint_accepts_a_lowercase_destination(session_factory):  # noqa: F811
    """Kills the raw ``!=`` guard in ``GET /transactions/{id}/transfer-candidates``
    (a 400 before the detector runs)."""
    seed = await _seed_base(session_factory)
    async with session_factory() as s:
        await s.execute(update(Account).where(Account.id == seed["a2_id"]).values(currency="eur"))
        await s.commit()
    source_id = await _add_tx(
        session_factory, org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"], type=TransactionType.EXPENSE,
        amount=Decimal("50.00"), on_date=DAY,
    )
    partner_id = await _add_tx(
        session_factory, org_id=seed["org_id"], account_id=seed["a2_id"],
        category_id=seed["cat_salary_id"], type=TransactionType.INCOME,
        amount=Decimal("50.00"), on_date=DAY,
    )
    with TestClient(make_app(session_factory)) as client:
        res = client.get(f"/api/v1/transactions/{source_id}/transfer-candidates",
                         params={"destination_account_id": seed["a2_id"]})
    assert res.status_code == 200, res.text
    assert [c["id"] for c in res.json()["candidates"]] == [partner_id]


# ── plans ───────────────────────────────────────────────────────────────────


async def test_a_lowercase_account_is_in_the_plan_pool(db):
    """Kills the raw ``==`` in ``plan_projection_service.project``'s
    ``in_currency``: the legacy account fell out of an EUR plan's pool."""
    b = await plan_t._books(db)
    await db.execute(update(Account).where(Account.id == b["savings"]).values(currency="eur"))
    await db.commit()
    result = await plan_t._project(db, b, [plan_t._plan(b["org_id"])], [])
    assert b["savings"] in result.pool
    assert b["usd"] not in result.pool


async def test_fixture_spellings_are_as_stored(db, books):
    """Control: the fixture really holds three spellings (no ORM folding)."""
    rows = (await db.execute(select(Account.currency).order_by(Account.id))).scalars().all()
    assert rows == ["EUR", "eur", "EUR "]
