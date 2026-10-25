"""TBD-737: the import confirm commits in chunks and is idempotent by key.

* Each chunk of ``CONFIRM_CHUNK_ROWS`` rows commits on its own, together with
  the confirm's progress on its ``import_batches`` header.
* A repeated confirm with the same ``idempotency_key`` returns the stored
  result (``replayed=True``) and imports nothing.
* A confirm that failed between chunks, repeated with the same key, resumes
  after the last committed chunk: every row lands exactly once and balances
  are applied once.
* The same key with different content is refused.
"""
import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.import_batch import ImportBatch
from app.models.transaction import Transaction
from app.models.user import Organization, User
from app.schemas.import_schemas import ImportConfirmRequest, ImportConfirmRow
from app.services import import_service
from app.services.exceptions import ConflictError

KEY = "c0ffee00-1111-4222-8333-444455556666"


@pytest_asyncio.fixture
async def factory():
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

    # pysqlite opens no transaction before a SAVEPOINT, so releasing a row's
    # savepoint would commit it on its own and hide what a failed chunk rolls
    # back. Emit BEGIN ourselves so SQLite behaves like MySQL here.
    @event.listens_for(engine.sync_engine, "connect")
    def _no_autobegin(dbapi_conn, _record):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def db(factory):
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def seed(db: AsyncSession) -> dict:
    org = Organization(name="Confirm Org", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    user = User(username="importer", email="importer@example.com",
                password_hash="x", org_id=org.id, is_superadmin=False)
    atype = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add_all([user, atype])
    await db.flush()
    account = Account(org_id=org.id, account_type_id=atype.id, name="Checking",
                      balance=Decimal("0"), currency="EUR")
    category = Category(org_id=org.id, name="General", slug="general",
                        is_system=True, type=CategoryType.BOTH)
    db.add_all([account, category])
    await db.commit()
    return {"org_id": org.id, "user_id": user.id, "account_id": account.id,
            "category_id": category.id}


def _rows(n: int, overrides: dict | None = None) -> list[ImportConfirmRow]:
    rows = []
    for i in range(n):
        fields = dict(
            row_number=i + 1,
            date=datetime.date(2026, 5, 1) + datetime.timedelta(days=i),
            description=f"Row {i + 1}",
            amount=Decimal("10.00") + i,
            type="expense" if i % 2 else "income",
        )
        fields.update((overrides or {}).get(i + 1, {}))
        rows.append(ImportConfirmRow(**fields))
    return rows


def _body(seed: dict, rows, *, key: str | None = KEY) -> ImportConfirmRequest:
    return ImportConfirmRequest(
        account_id=seed["account_id"],
        default_category_id=seed["category_id"],
        rows=rows,
        file_name="bank.csv",
        source_format="csv",
        idempotency_key=key,
    )


def _expected_balance(rows) -> Decimal:
    return sum(
        (r.amount if r.type == "income" else -r.amount)
        for r in rows if not r.skip
    )


async def _state(factory, seed: dict) -> dict:
    """Committed state, read through a separate session."""
    async with factory() as s:
        tx_count = await s.scalar(
            select(func.count()).select_from(Transaction)
            .where(Transaction.org_id == seed["org_id"])
        )
        unlinked = await s.scalar(
            select(func.count()).select_from(Transaction)
            .where(Transaction.org_id == seed["org_id"],
                   Transaction.import_batch_id.is_(None))
        )
        balance = await s.scalar(
            select(Account.balance).where(Account.id == seed["account_id"])
        )
        batches = (await s.execute(
            select(ImportBatch).where(ImportBatch.org_id == seed["org_id"])
        )).scalars().all()
    return {"tx": tx_count, "unlinked": unlinked, "balance": balance,
            "batches": batches}


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(import_service, "CONFIRM_CHUNK_ROWS", 2)


async def test_repeat_confirm_replays_the_stored_result(db, factory, seed, small_chunks):
    rows = _rows(5)
    first = await import_service.execute_import(
        db, seed["org_id"], _body(seed, rows), user_id=seed["user_id"]
    )
    assert first.imported_count == 5 and not first.replayed
    after_first = await _state(factory, seed)

    again = await import_service.execute_import(
        db, seed["org_id"], _body(seed, rows), user_id=seed["user_id"]
    )
    assert again.replayed is True
    assert again.model_dump(exclude={"replayed"}) == first.model_dump(exclude={"replayed"})
    after_again = await _state(factory, seed)
    assert after_again["tx"] == after_first["tx"] == 5
    assert after_again["balance"] == after_first["balance"] == _expected_balance(rows)
    assert len(after_again["batches"]) == 1
    batch = after_again["batches"][0]
    assert batch.id == first.import_id
    assert batch.row_count == batch.accepted_count == 5
    assert after_again["unlinked"] == 0


async def test_failure_between_chunks_then_retry_applies_each_row_once(
    db, factory, seed, small_chunks, monkeypatch,
):
    # Row 4 fails as a row-level error, so errors must accumulate across
    # chunks and across the two calls.
    rows = _rows(5, {4: {"action": "pair_with_existing"}})
    original = import_service._apply_confirm_rows
    calls = {"n": 0}

    async def crash_in_second_chunk(db_, ctx, chunk_rows):
        result = await original(db_, ctx, chunk_rows)
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("connection lost")  # after the rows flushed
        return result

    monkeypatch.setattr(import_service, "_apply_confirm_rows", crash_in_second_chunk)
    with pytest.raises(RuntimeError):
        await import_service.execute_import(
            db, seed["org_id"], _body(seed, rows), user_id=seed["user_id"]
        )
    await db.rollback()  # what the request's session does on the way out

    partial = await _state(factory, seed)
    assert partial["tx"] == 2  # only the first chunk committed
    assert partial["balance"] == _expected_balance(rows[:2])
    (batch,) = partial["batches"]
    assert batch.confirm_state["next_row"] == 2
    assert batch.confirm_state["completed"] is False

    monkeypatch.setattr(import_service, "_apply_confirm_rows", original)
    retried = await import_service.execute_import(
        db, seed["org_id"], _body(seed, rows), user_id=seed["user_id"]
    )
    assert retried.replayed is True
    assert retried.imported_count == 4
    assert retried.error_count == 1 and retried.errors[0].row_number == 4
    assert retried.import_id == batch.id

    done = await _state(factory, seed)
    assert done["tx"] == 4
    assert done["unlinked"] == 0
    assert done["balance"] == _expected_balance([r for r in rows if r.row_number != 4])
    (batch,) = done["batches"]
    assert batch.row_count == 4
    assert batch.confirm_state["completed"] is True

    # And a third call is a pure replay of the same final result.
    third = await import_service.execute_import(
        db, seed["org_id"], _body(seed, rows), user_id=seed["user_id"]
    )
    assert third.model_dump() == retried.model_dump()
    assert (await _state(factory, seed))["tx"] == 4


async def test_same_key_with_different_content_is_refused(db, factory, seed):
    await import_service.execute_import(
        db, seed["org_id"], _body(seed, _rows(2)), user_id=seed["user_id"]
    )
    with pytest.raises(ConflictError):
        await import_service.execute_import(
            db, seed["org_id"], _body(seed, _rows(3)), user_id=seed["user_id"]
        )
    await db.rollback()
    assert (await _state(factory, seed))["tx"] == 2


async def test_key_is_scoped_to_the_org(db, factory, seed):
    other = Organization(name="Other Org", billing_cycle_day=1)
    db.add(other)
    await db.flush()
    other_user = User(username="other", email="other@example.com",
                      password_hash="x", org_id=other.id, is_superadmin=False)
    atype = AccountType(org_id=other.id, name="Checking", slug="checking", is_system=True)
    db.add_all([other_user, atype])
    await db.flush()
    other_account = Account(org_id=other.id, account_type_id=atype.id, name="C",
                            balance=Decimal("0"), currency="EUR")
    other_cat = Category(org_id=other.id, name="G", slug="general",
                         is_system=True, type=CategoryType.BOTH)
    db.add_all([other_account, other_cat])
    await db.commit()
    other_seed = {"org_id": other.id, "account_id": other_account.id,
                  "category_id": other_cat.id}

    await import_service.execute_import(
        db, seed["org_id"], _body(seed, _rows(2)), user_id=seed["user_id"]
    )
    theirs = await import_service.execute_import(
        db, other.id, _body(other_seed, _rows(3)), user_id=other_user.id
    )
    assert theirs.imported_count == 3 and not theirs.replayed


async def test_confirm_that_imports_nothing_leaves_no_batch(db, factory, seed, small_chunks):
    rows = _rows(3, {i: {"skip": True} for i in (1, 2, 3)})
    first = await import_service.execute_import(
        db, seed["org_id"], _body(seed, rows), user_id=seed["user_id"]
    )
    assert first.skipped_count == 3 and first.import_id is None
    assert (await _state(factory, seed))["batches"] == []
    again = await import_service.execute_import(
        db, seed["org_id"], _body(seed, rows), user_id=seed["user_id"]
    )
    assert again.skipped_count == 3 and again.import_id is None
    assert (await _state(factory, seed))["batches"] == []


async def test_confirm_without_a_key_still_imports_in_chunks(db, factory, seed, small_chunks):
    rows = _rows(5)
    result = await import_service.execute_import(
        db, seed["org_id"], _body(seed, rows, key=None), user_id=seed["user_id"]
    )
    assert result.imported_count == 5 and not result.replayed
    state = await _state(factory, seed)
    assert state["tx"] == 5 and state["unlinked"] == 0
    assert state["balance"] == _expected_balance(rows)
    (batch,) = state["batches"]
    assert batch.id == result.import_id and batch.row_count == 5


async def test_service_call_without_user_commits_every_chunk(db, factory, seed, small_chunks):
    commits = {"n": 0}
    real_commit = db.commit

    async def counting_commit():
        commits["n"] += 1
        await real_commit()

    db.commit = counting_commit
    result = await import_service.execute_import(db, seed["org_id"], _body(seed, _rows(5)))
    assert result.imported_count == 5 and result.import_id is None
    assert commits["n"] == 3  # 2 + 2 + 1
    assert (await _state(factory, seed))["tx"] == 5


@pytest.mark.parametrize("key", ["short", "x" * 65, "has space in it 123", "semi;colon-key-0001"])
def test_idempotency_key_shape_is_validated(key):
    with pytest.raises(PydanticValidationError):
        ImportConfirmRequest(
            account_id=1, default_category_id=1, rows=[], file_name="f.csv",
            source_format="csv", idempotency_key=key,
        )
