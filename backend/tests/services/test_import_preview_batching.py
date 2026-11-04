"""TBD-736: the import preview's detectors run as set-based queries.

``build_preview`` used to call ``infer_category``,
``find_duplicate_of_linked_leg`` and ``find_match_candidates`` once per parsed
row. It now resolves the whole file with ``infer_categories``,
``find_duplicate_of_linked_leg_batch`` and ``find_match_candidates_batch``.

Two kinds of fence:

* Equivalence. The per-row helpers are kept (other callers use them), so they
  serve as the reference: over a varied seeded dataset, every batch answer
  must equal the per-row answer for the same inputs, ids and order included,
  and a whole preview must carry exactly the per-row results.
* Query count. The number of statements a preview executes must not grow
  with the number of rows in the file.
"""
import datetime
import random
from contextlib import contextmanager
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.category_rule import CategoryRule, RuleSource
from app.models.merchant_dictionary import MerchantDictionaryEntry
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.services import import_service
from app.services.category_rules_service import infer_categories, infer_category
from app.services.import_parser import ParsedRow
from app.services.transaction_service import (
    find_duplicate_of_linked_leg,
    find_duplicate_of_linked_leg_batch,
    find_match_candidates,
    find_match_candidates_batch,
)

BASE = datetime.date(2026, 5, 1)
AMOUNTS = [Decimal("10.00"), Decimal("12.50"), Decimal("20.00"), Decimal("99.99")]


@pytest_asyncio.fixture
async def engine_and_session():
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
        yield engine, session
    await engine.dispose()


@contextmanager
def capture_sql(engine):
    seen: list[str] = []

    def _record(_conn, _cursor, statement, _params, _ctx, _many):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


async def _seed(db: AsyncSession) -> dict:
    """A deterministic, deliberately messy ledger around ``BASE``.

    Mixes every attribute a detector filters on: account (import account,
    two same-currency peers, one other-currency account), type, amount,
    status, linked or not, recurring or not, manual adjustment or not, and
    dates inside and outside the ±3-day windows.
    """
    rnd = random.Random(736)
    org = Organization(name="Batch Org", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    atype = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(atype)
    await db.flush()
    accounts = [
        Account(org_id=org.id, account_type_id=atype.id, name=name,
                balance=Decimal("0"), currency=cur)
        for name, cur in [("Src", "EUR"), ("Peer A", "EUR"), ("Peer B", "EUR"),
                          ("Dollars", "USD")]
    ]
    db.add_all(accounts)
    groceries = Category(org_id=org.id, name="Groceries", slug="groceries",
                         is_system=True, type=CategoryType.EXPENSE)
    salary = Category(org_id=org.id, name="Salary", slug="paycheck",
                      is_system=True, type=CategoryType.INCOME)
    custom = Category(org_id=org.id, name="Coffee", slug=None,
                      is_system=False, type=CategoryType.EXPENSE)
    db.add_all([groceries, salary, custom])
    await db.flush()
    src, peer_a, peer_b, usd = accounts

    recurring = RecurringTransaction(
        org_id=org.id, account_id=peer_a.id, category_id=groceries.id,
        description="rent", amount=Decimal("20.00"), type="expense",
        frequency=Frequency.MONTHLY, next_due_date=BASE,
    )
    db.add(recurring)
    await db.flush()

    def tx(account, ttype, amount, day, **kw):
        status = kw.pop("status", TransactionStatus.SETTLED)
        when = BASE + datetime.timedelta(days=day)
        return Transaction(
            org_id=org.id, account_id=account.id, category_id=groceries.id,
            description=kw.pop("description", "seed"), amount=amount, type=ttype,
            status=status, date=when,
            settled_date=when if status == TransactionStatus.SETTLED else None,
            **kw,
        )

    rows: list[Transaction] = []
    for _ in range(160):
        account = rnd.choice(accounts)
        ttype = rnd.choice([TransactionType.EXPENSE, TransactionType.INCOME])
        amount = rnd.choice(AMOUNTS)
        day = rnd.randint(-8, 26)
        extra = {}
        roll = rnd.random()
        if roll < 0.15:
            extra["status"] = TransactionStatus.PENDING
        elif roll < 0.22:
            extra["recurring_id"] = recurring.id
        elif roll < 0.28:
            extra["is_manual_adjustment"] = True
        rows.append(tx(account, ttype, amount, day, **extra))
    db.add_all(rows)
    await db.flush()

    # Linked pairs, some with a leg on the import account (Detector 1 hits).
    for _ in range(40):
        leg_account = rnd.choice([src, src, peer_a, usd])
        other = rnd.choice([peer_a, peer_b, usd])
        amount = rnd.choice(AMOUNTS)
        day = rnd.randint(-6, 24)
        expense = tx(leg_account, TransactionType.EXPENSE, amount, day)
        income = tx(other, TransactionType.INCOME, amount, day)
        if rnd.random() < 0.5:
            expense, income = (
                tx(other, TransactionType.EXPENSE, amount, day),
                tx(leg_account, TransactionType.INCOME, amount, day),
            )
        db.add_all([expense, income])
        await db.flush()
        expense.linked_transaction_id = income.id
        income.linked_transaction_id = expense.id

    # Smart rules: an org rule, a dictionary entry the org has a system
    # category for, and one it does not (falls through to default).
    db.add(CategoryRule(
        org_id=org.id, normalized_token="CORNER CAFE", raw_description_seen="x",
        category_id=custom.id, match_count=1, source=RuleSource.USER_PICK,
    ))
    db.add_all([
        MerchantDictionaryEntry(normalized_token="LIDL", category_slug="groceries",
                                is_seed=True, vote_count=0),
        MerchantDictionaryEntry(normalized_token="ACME PAYROLL", category_slug="paycheck",
                                is_seed=True, vote_count=0),
        MerchantDictionaryEntry(normalized_token="NOWHERE", category_slug="no_such_slug",
                                is_seed=True, vote_count=0),
        # Same token as the org rule: tier 1 must win.
        MerchantDictionaryEntry(normalized_token="CORNER CAFE", category_slug="groceries",
                                is_seed=True, vote_count=0),
    ])
    await db.commit()
    return {"org_id": org.id, "src": src, "usd": usd}


DESCRIPTIONS = [
    "POS LIDL *9999", "LIDL 2026-05-01", "Corner Café", "CORNER CAFE",
    "ACME Payroll B.V.", "nowhere", "", "   ", "***", "Unknown shop",
]


def _keys() -> set:
    amounts = AMOUNTS + [Decimal("12.5"), Decimal("12.500"), Decimal("55.55")]
    return {
        (ttype, amount, BASE + datetime.timedelta(days=day))
        for ttype in (TransactionType.EXPENSE, TransactionType.INCOME)
        for amount in amounts
        for day in range(-12, 30, 3)
    }


async def test_duplicate_of_linked_leg_batch_equals_per_row(engine_and_session) -> None:
    _engine, db = engine_and_session
    seed = await _seed(db)
    hits = 0
    for account in (seed["src"], seed["usd"]):
        keys = _keys()
        batch = await find_duplicate_of_linked_leg_batch(
            db, seed["org_id"], account_id=account.id,
            currency=account.currency, keys=keys,
        )
        for key in keys:
            ttype, amount, when = key
            per_row = await find_duplicate_of_linked_leg(
                db, seed["org_id"], account_id=account.id, amount=amount,
                type=ttype, date=when, currency=account.currency,
            )
            assert [t.id for t in batch[key]] == [t.id for t in per_row], key
            hits += bool(per_row)
    assert hits, "dataset must exercise Detector 1"


async def test_match_candidates_batch_equals_per_row(engine_and_session) -> None:
    _engine, db = engine_and_session
    seed = await _seed(db)
    multi = 0
    for account in (seed["src"], seed["usd"]):
        keys = _keys()
        batch = await find_match_candidates_batch(
            db, seed["org_id"], account_id_excluded=account.id,
            currency=account.currency, keys=keys,
        )
        for key in keys:
            ttype, amount, when = key
            per_row = await find_match_candidates(
                db, seed["org_id"], source_type=ttype, amount=amount,
                account_id_excluded=account.id, date=when,
                currency=account.currency,
            )
            assert [t.id for t in batch[key]] == [t.id for t in per_row], key
            multi += len(per_row) >= 2
    assert multi, "dataset must exercise multi-candidate matches"


async def test_batch_detectors_empty_keys(engine_and_session) -> None:
    _engine, db = engine_and_session
    seed = await _seed(db)
    src = seed["src"]
    assert await find_match_candidates_batch(
        db, seed["org_id"], account_id_excluded=src.id, currency="EUR", keys=set(),
    ) == {}
    assert await find_duplicate_of_linked_leg_batch(
        db, seed["org_id"], account_id=src.id, currency="EUR", keys=set(),
    ) == {}


async def test_infer_categories_equals_per_row(engine_and_session) -> None:
    _engine, db = engine_and_session
    seed = await _seed(db)
    batch = await infer_categories(db, org_id=seed["org_id"], descriptions=DESCRIPTIONS)
    assert set(batch) == set(DESCRIPTIONS)
    sources = set()
    for description in DESCRIPTIONS:
        expected = await infer_category(db, org_id=seed["org_id"], description=description)
        assert batch[description] == expected, description
        sources.add(expected[1])
    assert sources == {"org_rule", "shared_dictionary", "default"}


async def test_infer_categories_other_org_sees_no_org_rules(engine_and_session) -> None:
    _engine, db = engine_and_session
    await _seed(db)
    other = Organization(name="Other", billing_cycle_day=1)
    db.add(other)
    await db.commit()
    batch = await infer_categories(db, org_id=other.id, descriptions=DESCRIPTIONS)
    for description in DESCRIPTIONS:
        assert batch[description] == await infer_category(
            db, org_id=other.id, description=description
        )
    assert all(v == (None, "default") for v in batch.values())


def _parsed_rows(n: int) -> list[ParsedRow]:
    rnd = random.Random(n)
    return [
        ParsedRow(
            row_number=i + 1,
            date=BASE + datetime.timedelta(days=rnd.randint(-4, 24)),
            description=rnd.choice(DESCRIPTIONS[:6] + ["Shop %d" % i]),
            amount=rnd.choice(AMOUNTS + [Decimal("55.55")]),
            type=rnd.choice(["expense", "income"]),
            counterparty=None,
            transaction_type=None,
        )
        for i in range(n)
    ]


async def test_preview_matches_per_row_reference(engine_and_session) -> None:
    """A whole preview carries exactly what the per-row helpers return."""
    _engine, db = engine_and_session
    seed = await _seed(db)
    src = seed["src"]
    rows = _parsed_rows(120)
    preview = await import_service.build_preview(
        db, org_id=seed["org_id"], account_id=src.id, file_name="t.csv",
        parsed_rows=rows,
    )
    assert len(preview.rows) == len(rows)
    actions = set()
    for row, out in zip(rows, preview.rows):
        ttype = TransactionType.EXPENSE if row.type == "expense" else TransactionType.INCOME
        assert (out.suggested_category_id, out.suggestion_source) == await infer_category(
            db, org_id=seed["org_id"], description=row.description,
        )
        dups = await find_duplicate_of_linked_leg(
            db, seed["org_id"], account_id=src.id, amount=row.amount, type=ttype,
            date=row.date, currency=src.currency,
        )
        assert out.is_duplicate_of_linked_leg == bool(dups)
        assert out.default_action_drop == bool(dups)
        if dups:
            assert out.duplicate_candidate.id == dups[0].id
            assert out.transfer_match_action == "none"
            actions.add("dup")
            continue
        assert out.duplicate_candidate is None
        matches = await find_match_candidates(
            db, seed["org_id"], source_type=ttype, amount=row.amount,
            account_id_excluded=src.id, date=row.date, currency=src.currency,
        )
        actions.add(out.transfer_match_action)
        if not matches:
            assert out.transfer_match_action == "none"
            assert out.pair_with_transaction_id is None
        elif len(matches) >= 2:
            assert out.transfer_match_action == "choose_candidate"
            assert [c.id for c in out.transfer_candidates] == [m.id for m in matches]
        else:
            assert out.pair_with_transaction_id == matches[0].id
            assert out.transfer_match_action == (
                "pair_with" if matches[0].date == row.date else "suggest_pair"
            )
    assert {"dup", "none", "choose_candidate"} <= actions


async def test_preview_query_count_does_not_grow_with_rows(engine_and_session) -> None:
    """The preview's statement count is the same for a small and a large file."""
    engine, db = engine_and_session
    seed = await _seed(db)
    counts = []
    for n in (3, 150):
        rows = _parsed_rows(n)
        with capture_sql(engine) as seen:
            await import_service.build_preview(
                db, org_id=seed["org_id"], account_id=seed["src"].id,
                file_name="t.csv", parsed_rows=rows,
            )
        counts.append(len(seen))
        db.expunge_all()
    small, large = counts
    assert large == small, counts
    # Account, category preflight, duplicate + FITID lookups, three smart-rules
    # tiers and two detector queries, each detector with its eager loads.
    assert large <= 20, counts
