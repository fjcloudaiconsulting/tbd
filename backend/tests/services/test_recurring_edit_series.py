"""TBD-762: editing a whole series from the recurring page.

The "Edit series" dialog sends ``PUT /recurring/{id}`` with only the fields the
user changed. Three server rules this file fences:

- **A next due date cannot land in a period the series already has a row for.**
  Rent on the 1st, the Nov 1 occurrence exists, next due Dec 1. Moving the next
  due date to Nov 3 would make generation write a SECOND November rent: the
  ``(recurring_id, date)`` probes key on the exact date and cannot see Nov 1.
  Every row of the series counts, whatever its status (skipped included).
- **An explicit ``occurrence_count: null`` means "never ends".** It is read off
  ``model_fields_set`` (an omitted key still means "not supplied"), and is
  written before the un-exhaustion check, so a finished series turned open-ended
  is re-anchored on the current cycle instead of back-filling the finished
  stretch.
- **Any request carrying more than ``is_active`` locks the template.** Resume
  (``{"is_active": true}``) alone stays as it was.
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

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.recurring import RecurringUpdate
from app.services import recurring_service
from app.services.exceptions import ConflictError

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
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("1000.00"), currency="EUR",
    )
    db.add(acct)
    await db.flush()
    cat = Category(org_id=org.id, name="Rent", slug="rent", type=CategoryType.EXPENSE)
    db.add(cat)
    await db.commit()
    return {"org_id": org.id, "account_id": acct.id, "cat_id": cat.id}


async def _add_template(db: AsyncSession, seed: dict, **over) -> int:
    fields = dict(
        org_id=seed["org_id"], account_id=seed["account_id"],
        category_id=seed["cat_id"], description="Rent", amount=Decimal("900.00"),
        type="expense", frequency=Frequency.MONTHLY, next_due_date=date(Y, 12, 1),
        auto_settle=False, is_active=True, occurrences_elapsed=0,
    )
    fields.update(over)
    r = RecurringTransaction(**fields)
    db.add(r)
    await db.commit()
    return r.id


async def _add_row(
    db: AsyncSession, seed: dict, rid: int, *, dt: date,
    status: TransactionStatus = TransactionStatus.PENDING,
    reconciliation_state: str = "accepted",
) -> int:
    tx = Transaction(
        org_id=seed["org_id"], account_id=seed["account_id"], category_id=seed["cat_id"],
        description="Rent", amount=Decimal("900.00"), type=TransactionType.EXPENSE,
        status=status, date=dt,
        settled_date=(dt if status == TransactionStatus.SETTLED else None),
        recurring_id=rid, reconciliation_state=reconciliation_state,
    )
    db.add(tx)
    await db.commit()
    return tx.id


async def _get_tmpl(db: AsyncSession, rid: int) -> RecurringTransaction:
    db.expire_all()
    return await db.get(RecurringTransaction, rid)


async def _update(db: AsyncSession, seed: dict, rid: int, payload: dict, **kw):
    # ``model_validate`` of a dict, as the router parses the JSON body: an
    # explicit ``None`` lands in ``model_fields_set`` exactly as a JSON null.
    return await recurring_service.update_recurring(
        db, seed["org_id"], rid, RecurringUpdate.model_validate(payload), **kw,
    )


# ── Next due date guard ──────────────────────────────────────────────────────

async def test_next_due_date_inside_a_period_that_has_its_row_is_refused(db_session):
    """Rent on the 1st: Nov 1 exists, next due Dec 1. Nov 3 would be a second
    November rent that the ``(recurring_id, date)`` probes cannot see."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    await _add_row(db_session, seed, rid, dt=date(Y, 11, 1))

    with pytest.raises(ConflictError) as exc:
        await _update(db_session, seed, rid, {"next_due_date": date(Y, 11, 3).isoformat()})

    assert f"the series has a transaction on {Y}-11-01" in exc.value.detail.lower()
    assert f"{Y}-12-01 or later" in exc.value.detail
    # The request's session is rolled back on the error, as the router's is.
    await db_session.rollback()
    assert (await _get_tmpl(db_session, rid)).next_due_date == date(Y, 12, 1)


async def test_next_due_date_a_full_period_after_the_latest_row_is_accepted(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    await _add_row(db_session, seed, rid, dt=date(Y, 11, 1))

    updated = await _update(db_session, seed, rid, {"next_due_date": date(Y, 12, 3).isoformat()})

    assert updated.next_due_date == date(Y, 12, 3)
    assert (await _get_tmpl(db_session, rid)).next_due_date == date(Y, 12, 3)


@pytest.mark.parametrize(
    ("status", "state"),
    [
        (TransactionStatus.PENDING, "skipped"),
        (TransactionStatus.PENDING, "rejected"),
        (TransactionStatus.SETTLED, "accepted"),
    ],
    ids=["skipped", "rejected", "settled"],
)
async def test_every_row_of_the_series_counts_whatever_its_status(db_session, status, state):
    """A skipped occurrence was still that period's occurrence: generation's
    ``exists`` probe keys on the date, not on the state."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    await _add_row(db_session, seed, rid, dt=date(Y, 11, 1), status=status, reconciliation_state=state)

    with pytest.raises(ConflictError, match=f"{Y}-11-01"):
        await _update(db_session, seed, rid, {"next_due_date": date(Y, 11, 3).isoformat()})


async def test_the_latest_row_decides_not_the_first(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, next_due_date=date(Y, 12, 1))
    await _add_row(db_session, seed, rid, dt=date(Y, 9, 1))
    await _add_row(db_session, seed, rid, dt=date(Y, 11, 1))
    await _add_row(db_session, seed, rid, dt=date(Y, 10, 1))

    with pytest.raises(ConflictError, match=f"{Y}-11-01"):
        await _update(db_session, seed, rid, {"next_due_date": date(Y, 11, 20).isoformat()})


async def test_a_later_move_from_the_current_frontier_is_accepted(db_session):
    """Nov's row was moved by hand to Nov 5; the frontier stays Dec 1. Moving
    it LATER to Dec 2 adds no occurrence to any period, so it stands even
    though Dec 2 is less than a month after Nov 5."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    await _add_row(db_session, seed, rid, dt=date(Y, 11, 5))

    updated = await _update(db_session, seed, rid, {"next_due_date": date(Y, 12, 2).isoformat()})

    assert updated.next_due_date == date(Y, 12, 2)


async def test_a_date_on_or_before_a_row_is_refused_even_when_later_than_the_frontier(db_session):
    """Strictly after EVERY row: a row already past the frontier still blocks."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    await _add_row(db_session, seed, rid, dt=date(Y, 12, 10))

    with pytest.raises(ConflictError, match=f"{Y}-12-10"):
        await _update(db_session, seed, rid, {"next_due_date": date(Y, 12, 10).isoformat()})


async def test_the_new_frequency_sets_the_period(db_session):
    """Monthly -> weekly with Nov 1 materialised: the next weekly slot is Nov 8."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    await _add_row(db_session, seed, rid, dt=date(Y, 11, 1))

    updated = await _update(
        db_session, seed, rid,
        {"frequency": "weekly", "next_due_date": date(Y, 11, 8).isoformat()},
    )

    assert updated.next_due_date == date(Y, 11, 8)
    assert updated.frequency == Frequency.WEEKLY


async def test_an_unchanged_next_due_date_is_not_checked(db_session):
    """The dialog re-sends nothing it did not change, but an API client may:
    re-sending the stored date must not refuse an unrelated edit."""
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    await _add_row(db_session, seed, rid, dt=date(Y, 11, 5))

    updated = await _update(
        db_session, seed, rid,
        {"next_due_date": date(Y, 12, 1).isoformat(), "description": "Flat rent"},
    )

    assert updated.description == "Flat rent"


async def test_a_series_with_no_rows_takes_any_legal_date(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)

    updated = await _update(db_session, seed, rid, {"next_due_date": date(Y, 11, 3).isoformat()})

    assert updated.next_due_date == date(Y, 11, 3)


async def test_another_series_rows_do_not_count(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    other = await _add_template(db_session, seed, description="Gym")
    await _add_row(db_session, seed, other, dt=date(Y, 11, 1))

    updated = await _update(db_session, seed, rid, {"next_due_date": date(Y, 11, 3).isoformat()})

    assert updated.next_due_date == date(Y, 11, 3)


# ── occurrence_count: explicit null = never ends ─────────────────────────────

async def test_explicit_null_occurrence_count_makes_the_series_open_ended(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, occurrence_count=12, occurrences_elapsed=3)

    updated = await _update(db_session, seed, rid, {"occurrence_count": None})

    assert updated.occurrence_count is None
    assert updated.occurrences_elapsed == 3


async def test_an_omitted_occurrence_count_leaves_it_alone(db_session):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed, occurrence_count=12, occurrences_elapsed=3)

    updated = await _update(db_session, seed, rid, {"description": "Sofa"})

    assert updated.occurrence_count == 12


async def test_a_finished_auto_settle_series_set_to_never_ends_back_fills_nothing(db_session):
    """FENCE. A 3-instalment auto-settle plan finished in February, frozen
    frontier Mar 1. Made open-ended in June: the un-exhaustion re-anchors the
    frontier onto the current cycle (Jun 1), so generation writes June's row and
    nothing for March, April or May. Writing the null after the
    ``has_remaining`` comparison (or not at all) leaves either a back-fill of
    three SETTLED rows into closed periods or a series that never resumes; both
    fail here."""
    seed = await _seed(db_session)
    today = date(Y, 6, 17)
    rid = await _add_template(
        db_session, seed, next_due_date=date(Y, 3, 1), auto_settle=True,
        occurrence_count=3, occurrences_elapsed=3,
    )
    for m in (12, 1, 2):
        await _add_row(
            db_session, seed, rid, dt=date(Y - 1 if m == 12 else Y, m, 1),
            status=TransactionStatus.SETTLED,
        )
    balance = (await db_session.get(Account, seed["account_id"])).balance

    updated = await _update(db_session, seed, rid, {"occurrence_count": None}, today=today)

    assert updated.occurrence_count is None
    assert updated.next_due_date == date(Y, 6, 1)

    await recurring_service.generate_due_transactions(db_session, seed["org_id"], today=today)

    rows = (await db_session.execute(
        select(Transaction.date, Transaction.status)
        .where(Transaction.recurring_id == rid, Transaction.date > date(Y, 2, 1))
        .order_by(Transaction.date)
    )).all()
    assert rows == [(date(Y, 6, 1), TransactionStatus.SETTLED)]
    db_session.expire_all()
    assert (await db_session.get(Account, seed["account_id"])).balance == balance - Decimal("900.00")


# ── The template lock ────────────────────────────────────────────────────────

def _capture_template_selects(db: AsyncSession) -> list:
    """Record each ORM SELECT of a recurring template, with its lock and
    ``populate_existing`` flags. SQLite drops ``FOR UPDATE`` from the SQL, so
    the statement object is the only place the lock is visible; this proves the
    code path, not the lock itself."""
    seen: list = []

    @event.listens_for(db.sync_session, "do_orm_execute")
    def _rec(state):
        if not state.is_select:
            return
        if not any(
            d.get("entity") is RecurringTransaction
            for d in state.statement.column_descriptions
        ):
            return
        seen.append((
            state.statement._for_update_arg is not None,
            bool(state.execution_options.get("populate_existing")),
        ))

    return seen


@pytest.mark.parametrize(
    "payload",
    [
        {"auto_settle": True},
        {"frequency": "weekly", "next_due_date": f"{Y}-12-01"},
        {"next_due_date": f"{Y}-12-08"},
        {"occurrence_count": None},
        {"occurrence_count": 6},
        {"is_active": True, "auto_settle": True},
    ],
    ids=["auto_settle", "frequency", "next_due_date", "never_ends", "count", "resume_plus"],
)
async def test_any_request_beyond_is_active_locks_the_template(db_session, payload):
    seed = await _seed(db_session)
    rid = await _add_template(db_session, seed)
    seen = _capture_template_selects(db_session)

    await _update(db_session, seed, rid, payload)

    # The first template SELECT is the one the edit reads its old values from.
    assert seen and seen[0] == (True, True)


async def test_resume_alone_does_not_lock_and_behaves_as_before(db_session):
    """``{"is_active": true}`` stays a plain read, and the next-due guard does
    not run on it: the re-anchor moves the frontier, the user did not. A row
    on May 15 would refuse a user-chosen Jun 1 (less than a month later); the
    re-anchored Jun 1 must stand."""
    seed = await _seed(db_session)
    today = date(Y, 6, 17)
    rid = await _add_template(
        db_session, seed, next_due_date=date(Y, 2, 1), is_active=False,
    )
    await _add_row(db_session, seed, rid, dt=date(Y, 5, 15))
    seen = _capture_template_selects(db_session)

    updated = await _update(db_session, seed, rid, {"is_active": True}, today=today)

    assert seen and seen[0] == (False, False)
    assert updated.is_active is True
    assert updated.next_due_date == date(Y, 6, 1)
