"""Occurrence identity is ``(recurring_id, scheduled date)``, derived (TBD-271).

The definition lives in ``date_utils.occurrences_in_window``'s docstring. In
short: the scheduled date is where the iterated ``advance_date`` walk lands from
the template's CURRENT ``next_due_date``, and for a materialised occurrence it
is the row's ``Transaction.date``. **The frontier consumes; the key
identifies.** ``_advance_frontier`` spends an occurrence; the
``(recurring_id, date)`` probe (generation, ``forecast_service``,
``account_balance_forecast_service``; no status term in any of them) only
decides occurrences at or after the frontier.

Every assertion here is over SETS OF KEYS wherever a public surface exposes
per-occurrence dates. Two surfaces were checked, not assumed:

  * ``compute_account_balance_forecast`` emits ``recurring_lines`` per ACCOUNT,
    each ``{"amount", "date"}`` -- a date but NO ``recurring_id``. Fixtures
    therefore put one template on each account, or give templates sharing an
    account distinct amounts, and ``_projected`` rebuilds the key from that;
  * ``compute_forecast`` emits totals and a per-CATEGORY breakdown only. No
    dates at all, so it is used for ``forecast_net`` and nothing else.

Resume (F2c in the ruling) is deliberately NOT duplicated here. It is already
fenced on both axes, by name:

  * ``test_recurring_stop_resume_no_duplicates.py::
    test_resume_lands_on_generations_own_path_dependent_grid`` -- the re-anchor
    lands on generation's own iterated grid, so a resumed template's keys are
    the keys generation would have produced;
  * ``test_recurring_occurrence_count.py::
    test_ff_resume_reanchors_frontier_without_spending_instalments`` --
    ``occurrences_elapsed`` does not move on the re-anchor, which is exactly
    why an ORDINAL key was rejected: ordinals shift under a resume.

Each test is labelled ``fence`` (goes RED under a named wrong implementation,
injected and confirmed) or ``guard``. ``X1`` is a strict xfail reproducing
TBD-509 and turns into an XPASS failure the day that ticket lands.

Clocks are injected everywhere. Fixtures anchor to ``date.today() - n`` via
``_safe_month_anchor`` (``reference_wall_clock_date_bomb_tests``); F4 uses
fixed literals because the month-end path is its whole subject, and every
clock it hands the services is injected, so no wall-clock read can reach it.
"""
from __future__ import annotations

import datetime
from collections import Counter
from decimal import Decimal

import pytest
import pytest_asyncio
from dateutil.relativedelta import relativedelta
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.recurring import RecurringUpdate
from app.schemas.transaction import PromoteToRecurringRequest
from app.services import forecast_service, recurring_service, transaction_service
from app.services.account_balance_forecast_service import (
    compute_account_balance_forecast,
)
from app.services.billing_service import current_cycle_window
from app.services.date_utils import occurrences_in_window

DAY = datetime.timedelta(days=1)
WEEK = datetime.timedelta(weeks=1)

Key = tuple[int, datetime.date]


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


# ─── fixture scaffolding ─────────────────────────────────────────────────────

def _safe_month_anchor(d: datetime.date) -> datetime.date:
    """Nudge ``d`` back to a day-of-month that exists in every month (<= 28),
    so ``p_start + 1 month`` is a clean period boundary. See the identically
    named helper in ``test_forecast_overdue_recurring``."""
    while d.day > 28:
        d -= DAY
    return d


def _month_window(start: datetime.date) -> tuple[datetime.date, datetime.date]:
    return start, start + relativedelta(months=1) - DAY


async def _seed(
    db: AsyncSession, *, open_start: datetime.date, successor: bool = True,
) -> dict:
    """One org, two checking accounts, one expense category, a period roster.

    Roster: an OPEN period at ``open_start`` (P1) and, when ``successor``, a
    CLOSED successor (P2). P2 is what lets a fixture see UNMATERIALISED keys
    after a generation run: generation materialises through P1's end only, so
    P1's projection is empty afterwards and an "after" set of ``{}`` would make
    every disjointness claim vacuous.
    """
    org = Organization(name="T", billing_cycle_day=open_start.day)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    acct_a = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("1000.00"), currency="EUR", is_default=True,
    )
    acct_b = Account(
        org_id=org.id, name="Savings", account_type_id=at.id,
        balance=Decimal("1000.00"), currency="EUR", is_default=False,
    )
    db.add_all([acct_a, acct_b])
    await db.flush()
    cat = Category(org_id=org.id, name="Rent", slug="rent", type=CategoryType.EXPENSE)
    db.add(cat)
    await db.flush()

    p1 = _month_window(open_start)
    db.add(BillingPeriod(org_id=org.id, start_date=open_start))
    p2 = None
    if successor:
        p2 = _month_window(p1[1] + DAY)
        db.add(BillingPeriod(org_id=org.id, start_date=p2[0], end_date=p2[1]))
    await db.commit()
    return {
        "org_id": org.id,
        "account_a": acct_a.id,
        "account_b": acct_b.id,
        "cat_id": cat.id,
        "p1": p1,
        "p2": p2,
    }


async def _seed_on_grid(db: AsyncSession, today: datetime.date) -> dict:
    """P1 started 10..13 days before ``today``; P1 == generation's window.

    Asserted, not assumed: every conservation claim below is a claim about the
    projection window and ``current_cycle_window`` being the same interval.
    """
    p_start = _safe_month_anchor(today - datetime.timedelta(days=10))
    seed = await _seed(db, open_start=p_start)
    # ``pytest.fail``, not ``assert``: X1 is ``xfail(raises=AssertionError)``,
    # and a broken fixture must FAIL it, never satisfy it.
    if current_cycle_window(p_start.day, today) != seed["p1"]:
        pytest.fail(f"cycle window drifted from P1 {seed['p1']}")
    return seed


def _template(seed: dict, **overrides) -> RecurringTransaction:
    defaults = dict(
        org_id=seed["org_id"],
        account_id=seed["account_a"],
        category_id=seed["cat_id"],
        description="rent",
        amount=Decimal("10.00"),
        type="expense",
        frequency="weekly",
        auto_settle=False,
        is_active=True,
        occurrences_elapsed=0,
    )
    defaults.update(overrides)
    return RecurringTransaction(**defaults)


async def _add(db: AsyncSession, t: RecurringTransaction) -> RecurringTransaction:
    db.add(t)
    await db.commit()
    return t


async def _reload(db: AsyncSession, template_id: int) -> RecurringTransaction:
    res = await db.execute(
        select(RecurringTransaction)
        .where(RecurringTransaction.id == template_id)
        .execution_options(populate_existing=True)
    )
    return res.scalar_one()


def _no_dupes(keys: list[Key]) -> set[Key]:
    """A set would silently collapse a DOUBLE count onto one key, which is the
    very defect several fences exist to see. Refuse duplicates first.

    ``pytest.fail`` rather than ``assert`` for the reason in ``_seed_on_grid``.
    """
    dupes = [k for k, n in Counter(keys).items() if n > 1]
    if dupes:
        pytest.fail(f"duplicated occurrence keys: {dupes}")
    return set(keys)


async def _row_keys(db: AsyncSession, org_id: int) -> set[Key]:
    res = await db.execute(
        select(Transaction.recurring_id, Transaction.date).where(
            Transaction.org_id == org_id
        )
    )
    return _no_dupes([(rid, d) for rid, d in res.all()])


async def _projected(
    db: AsyncSession, seed: dict, today: datetime.date,
    owners: dict[int, int | dict[Decimal, int]],
    periods: tuple[str, ...] = ("p1", "p2"),
) -> set[Key]:
    """Unmaterialised keys, read off ``recurring_lines`` over the given periods.

    ``owners`` maps account_id -> the ONE template on it, or, where two
    templates share an account (F5), -> ``{amount: template_id}`` with distinct
    amounts. A line nobody owns is a KeyError, not a silent skip.
    """
    keys: list[Key] = []
    for name in periods:
        fc = await compute_account_balance_forecast(
            db, seed["org_id"], period_start=seed[name][0], today=today
        )
        for acct in fc["accounts"]:
            for line in acct["recurring_lines"]:
                owner = owners[acct["account_id"]]
                if isinstance(owner, dict):
                    owner = owner[abs(Decimal(line["amount"]))]
                keys.append((owner, datetime.date.fromisoformat(line["date"])))
    return _no_dupes(keys)


async def _net(db: AsyncSession, org_id: int, today: datetime.date) -> Decimal:
    fc = await forecast_service.compute_forecast(db, org_id, today=today)
    return Decimal(fc["forecast_net"])


# ─────────────────────────────────────────────────────────────────────────────
# F1 — fence. Generation PARTITIONS the key set; it never re-keys.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f1_generation_partitions_keys_for_overdue_monthly_and_weekly(db_session):
    """FENCE. keys projected before == keys created ∪ keys projected after.

    Two OVERDUE templates, one per account: monthly (frontier a full period
    before P1, not auto-settled) and weekly (frontier 11 days before P1,
    auto-settled, so its in-window rows are a MIX of SETTLED and PENDING).
    Projection is read over P1 ∪ P2; P2 keeps an unmaterialised remainder so
    "after" is non-empty and disjointness is not ``x & {} == {}``.

    Wrong implementations killed:
      * generation stamping catch-up rows with ``today`` instead of ``due`` --
        the created keys are no longer the projected keys (injected: RED);
      * a positional ordinal key -- "occurrence #0" is a different date before
        and after the run, so the partition cannot hold on a surface that
        exposes dates (argued, not injectable without a rewrite).

    ⚠ NOT killed here, and it cannot be: a removed or status-filtered PROBE.
    After this run the frontier sits past every created row, so the template
    is dropped by ``next_due_date <= window_end`` before the probe is ever
    consulted -- "the frontier consumes; the key identifies" is exactly why.
    Both probe mutants were injected and stayed GREEN on this test. F5, where a
    row sits AT the frontier, is their fence.
    """
    today = datetime.date.today()
    seed = await _seed_on_grid(db_session, today)
    p_start, window_end = seed["p1"]
    monthly_frontier = p_start - relativedelta(months=1)
    weekly_frontier = p_start - datetime.timedelta(days=11)

    monthly = await _add(db_session, _template(
        seed, frequency="monthly", amount=Decimal("100.00"),
        next_due_date=monthly_frontier,
    ))
    weekly = await _add(db_session, _template(
        seed, account_id=seed["account_b"], auto_settle=True,
        next_due_date=weekly_frontier,
    ))
    owners = {seed["account_a"]: monthly.id, seed["account_b"]: weekly.id}

    before = await _projected(db_session, seed, today, owners)
    net_before = await _net(db_session, seed["org_id"], today)

    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    created = await _row_keys(db_session, seed["org_id"])
    after = await _projected(db_session, seed, today, owners)

    in_window = {k for k in created if k[1] >= p_start}
    assert before == in_window | after
    assert not in_window & after
    assert not created & after

    # The created keys ARE the iterated walk from the original frontier.
    for tid, frontier, freq in (
        (monthly.id, monthly_frontier, Frequency.MONTHLY),
        (weekly.id, weekly_frontier, Frequency.WEEKLY),
    ):
        assert {d for t, d in created if t == tid} == set(
            occurrences_in_window(frontier, freq, frontier, window_end)
        )

    # Anti-vacuity.
    assert {t for t, _ in in_window} == {monthly.id, weekly.id}
    assert {t for t, _ in after} == {monthly.id, weekly.id}
    assert any(d < p_start for _, d in created)
    statuses = set((await db_session.execute(
        select(Transaction.status).where(
            Transaction.recurring_id == weekly.id, Transaction.date >= p_start
        )
    )).scalars().all())
    assert statuses == {TransactionStatus.SETTLED, TransactionStatus.PENDING}

    assert await _net(db_session, seed["org_id"], today) == net_before


# ─────────────────────────────────────────────────────────────────────────────
# F2a — fence. An amount edit does not touch identity.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f2a_amount_edit_leaves_every_key_unchanged(db_session):
    """FENCE. Amount is not part of the key: a row AT the frontier still owns it.

    The source row sits ON the frontier (promote with ``next_due_date ==
    tx.date == p_start``) and carries the OLD amount. The amount is edited
    BEFORE anything is projected or generated after it, so both probes are
    genuinely consulted with a row whose amount no longer matches the template.

    Wrong implementations killed:
      * an amount-in-the-key probe in generation (``Transaction.amount ==
        r.amount``) -- the source row no longer matches, and ``p_start`` is
        created a second time (``_row_keys`` refuses duplicates);
      * the same probe in the balance forecast -- the source row's key is
        projected on top of the row;
      * an edit path that re-anchors the frontier on a non-schedule edit
        (``next_due_date = today``) -- ``today`` is provably OFF the weekly
        grid, so the projected keys move.
    """
    today = datetime.date.today()
    seed = await _seed_on_grid(db_session, today)
    p_start, _ = seed["p1"]
    assert (today - p_start).days % 7 != 0   # today is off-grid: discriminating
    src = Transaction(
        org_id=seed["org_id"], account_id=seed["account_a"],
        category_id=seed["cat_id"], description="rent",
        amount=Decimal("10.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=p_start, settled_date=p_start,
    )
    db_session.add(src)
    await db_session.commit()
    promoted = await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], src.id,
        PromoteToRecurringRequest(frequency="weekly", next_due_date=p_start),
        today=today,
    )
    tid = promoted.recurring_id
    assert (await _reload(db_session, tid)).next_due_date == p_start   # AT the frontier
    owners = {seed["account_a"]: tid}
    src_key = (tid, p_start)

    rows_before = await _row_keys(db_session, seed["org_id"])
    assert rows_before == {src_key}
    proj_before = await _projected(db_session, seed, today, owners)
    assert src_key not in proj_before and proj_before

    await recurring_service.update_recurring(
        db_session, seed["org_id"], tid,
        RecurringUpdate(amount=Decimal("25.00")), today=today,
    )
    assert (await _reload(db_session, tid)).next_due_date == p_start
    proj_edited = await _projected(db_session, seed, today, owners)
    assert proj_edited == proj_before

    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    rows = await _row_keys(db_session, seed["org_id"])
    after = await _projected(db_session, seed, today, owners)
    assert rows_before <= rows
    assert proj_before == (rows - rows_before) | after
    assert not rows & after


# ─────────────────────────────────────────────────────────────────────────────
# F2b — fence. Moving the frontier FORWARD discards keys; nothing brings them back.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f2b_frontier_moved_forward_skips_keys_for_good(db_session):
    """FENCE. The skipped keys are neither projected nor created.

    Weekly, frontier on ``p_start``, moved forward two steps before anything is
    generated. The two skipped occurrences are gone: the frontier consumed them
    without a row, and the key only identifies what the walk still reaches.

    ⚠ This does NOT distinguish key designs. The ruling says a frontier edit
    invalidates unmaterialised keys under ANY key choice, so an ordinal or a
    stored key would behave identically here. It fences the edit's
    propagation, nothing more.

    Wrong implementation killed: the ``next_due_date`` write not reaching the
    frontier -- the skipped keys are still projected and then created.
    """
    today = datetime.date.today()
    seed = await _seed_on_grid(db_session, today)
    p_start, _ = seed["p1"]
    t = await _add(db_session, _template(seed, next_due_date=p_start))
    owners = {seed["account_a"]: t.id}

    proj_before = await _projected(db_session, seed, today, owners)
    skipped = {(t.id, p_start), (t.id, p_start + WEEK)}
    assert skipped <= proj_before

    await recurring_service.update_recurring(
        db_session, seed["org_id"], t.id,
        RecurringUpdate(next_due_date=p_start + 2 * WEEK), today=today,
    )
    proj_after = await _projected(db_session, seed, today, owners)
    assert proj_after == proj_before - skipped

    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    created = await _row_keys(db_session, seed["org_id"])
    assert created
    assert not created & skipped
    assert not (await _projected(db_session, seed, today, owners)) & skipped


# ─────────────────────────────────────────────────────────────────────────────
# F2d — fence. A frequency edit invalidates unmaterialised keys, and ONLY those.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f2d_frequency_change_drops_old_unmaterialised_keys(db_session):
    """FENCE. Rows keep their keys; the old grid's future keys vanish, unremapped.

    Weekly, generated through P1, then switched to monthly. P2 held four or
    five weekly keys; afterwards it holds exactly the monthly walk from the
    unchanged frontier.

    ⚠ This does NOT distinguish key designs: the ruling says a frequency edit
    invalidates unmaterialised keys under ANY key choice. It fences that the
    edit propagates to the walk and leaves materialised rows alone.

    Wrong implementation killed: the frequency write not reaching the walk --
    the weekly keys are still projected.
    """
    today = datetime.date.today()
    seed = await _seed_on_grid(db_session, today)
    p_start, _ = seed["p1"]
    p2_start, p2_end = seed["p2"]
    t = await _add(db_session, _template(seed, next_due_date=p_start))
    owners = {seed["account_a"]: t.id}

    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    rows_before = await _row_keys(db_session, seed["org_id"])
    proj_before = await _projected(db_session, seed, today, owners)
    frontier = (await _reload(db_session, t.id)).next_due_date

    await recurring_service.update_recurring(
        db_session, seed["org_id"], t.id,
        RecurringUpdate(frequency="monthly"), today=today,
    )

    assert await _row_keys(db_session, seed["org_id"]) == rows_before
    assert (await _reload(db_session, t.id)).next_due_date == frontier
    proj_after = await _projected(db_session, seed, today, owners)
    assert proj_after == {
        (t.id, d)
        for d in occurrences_in_window(frontier, Frequency.MONTHLY, p2_start, p2_end)
    }
    # Anti-vacuity: the weekly tail in P2 was real and is gone.
    assert len(proj_before - proj_after) >= 3


# ─────────────────────────────────────────────────────────────────────────────
# F3 — fence. Consumed does NOT mean "a row exists".
# ─────────────────────────────────────────────────────────────────────────────

async def test_f3_deleted_row_is_not_resurrected(db_session):
    """FENCE. Materialise, delete one row, generate again: the key stays spent.

    Wrong implementations killed:
      * "consumed means a row exists" in any form -- ``delete_transaction``
        rewinding the frontier onto the deleted date re-projects the key and
        the second run re-creates it;
      * ``delete_transaction`` decrementing ``occurrences_elapsed`` -- the
        counter moves;
      * ``_advance_frontier`` not spending on the CREATE branch -- the counter
        after the first run is not the number of occurrences walked;
      * ``forecast_service`` walking from the period start instead of the
        frontier (so "not a row" reads as "still owed") -- the deleted expense
        is re-projected and ``forecast_net`` does not rise by its amount.
    """
    today = datetime.date.today()
    seed = await _seed_on_grid(db_session, today)
    p_start, _ = seed["p1"]
    t = await _add(db_session, _template(seed, next_due_date=p_start))
    owners = {seed["account_a"]: t.id}

    res = await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    rows_before = await _row_keys(db_session, seed["org_id"])
    assert res["generated"] == len(rows_before) >= 4
    assert (await _reload(db_session, t.id)).occurrences_elapsed == len(rows_before)

    victim_key = (t.id, p_start + WEEK)
    assert victim_key in rows_before
    victim = (await db_session.execute(
        select(Transaction).where(
            Transaction.recurring_id == t.id, Transaction.date == victim_key[1]
        )
    )).scalar_one()
    assert victim.type == TransactionType.EXPENSE
    victim_id, victim_amount = victim.id, victim.amount
    net_pre_delete = await _net(db_session, seed["org_id"], today)
    await transaction_service.delete_transaction(db_session, seed["org_id"], victim_id)

    elapsed = (await _reload(db_session, t.id)).occurrences_elapsed
    assert elapsed == len(rows_before)
    proj_1 = await _projected(db_session, seed, today, owners)
    assert victim_key not in proj_1
    net_1 = await _net(db_session, seed["org_id"], today)
    # The deleted expense LEAVES the forecast. A walk that re-projects it keeps
    # the net where it was before the delete.
    assert net_1 == net_pre_delete + victim_amount

    res = await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    assert res["generated"] == 0
    assert await _row_keys(db_session, seed["org_id"]) == rows_before - {victim_key}
    assert (await _reload(db_session, t.id)).occurrences_elapsed == elapsed
    assert await _projected(db_session, seed, today, owners) == proj_1
    assert await _net(db_session, seed["org_id"], today) == net_1


# ─────────────────────────────────────────────────────────────────────────────
# F4 — fence. Month-end: the key is the ITERATED walk, not a closed form.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f4_month_end_keys_follow_the_iterated_walk(db_session):
    """FENCE. Jan 31 monthly -> rows Feb 28 and Mar 28; Mar 31 is never a key.

    Fixed literals, on purpose: the month-end path IS the subject. Every clock
    handed to a service is injected, so nothing reads the wall.

    The projection is taken BEFORE generation, while the frontier is still
    Jan 31: that is the only moment a closed form and the iterated walk
    disagree about March. Once generation has advanced the frontier to Mar 28,
    any walk from there agrees.

    Wrong implementation killed: ``occurrences_in_window`` jumping
    ``next_due + relativedelta(months=n)`` -- projects Mar 31, which generation
    never creates, so the partition and the Mar-31 exclusion both break.
    """
    today = datetime.date(2027, 3, 10)
    seed = await _seed(db_session, open_start=datetime.date(2027, 3, 1), successor=False)
    assert current_cycle_window(1, today) == seed["p1"]
    t = await _add(db_session, _template(
        seed, frequency="monthly", next_due_date=datetime.date(2027, 1, 31),
    ))
    owners = {seed["account_a"]: t.id}
    mar_31 = (t.id, datetime.date(2027, 3, 31))

    before = await _projected(db_session, seed, today, owners, periods=("p1",))
    assert before == {(t.id, datetime.date(2027, 3, 28))}

    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    created = await _row_keys(db_session, seed["org_id"])
    assert created == {
        (t.id, datetime.date(2027, 1, 31)),
        (t.id, datetime.date(2027, 2, 28)),
        (t.id, datetime.date(2027, 3, 28)),
    }
    after = await _projected(db_session, seed, today, owners, periods=("p1",))
    assert after == set()
    assert mar_31 not in before | created | after


# ─────────────────────────────────────────────────────────────────────────────
# F5 — fence. The probe on the two copies that had no key-at-frontier test.
# ─────────────────────────────────────────────────────────────────────────────

async def test_f5_linked_row_at_the_frontier_is_neither_duplicated_nor_projected(
    db_session,
):
    """FENCE. A row already dated ON the frontier owns that key -- and ONLY that key.

    The promote path produces it: ``next_due_date == tx.date``, the source row
    SETTLED and linked. A SECOND template ("gym", distinct amount) sits on the
    SAME account with its frontier on the SAME date and no row, which is what
    separates ``(recurring_id, date)`` from a coarser key (the sibling of
    ``test_forecast_overdue_recurring.py::test_f21_...`` for the other two
    probe copies). Weekly so both grids stay inside P1 ∪ P2. ``today`` is
    injected into promote like everything else.

    Wrong implementations killed:
      * generation's probe removed, or status-filtered to PENDING -- the source
        row's key is created a second time (``_row_keys`` refuses duplicates);
      * generation's probe keyed on date (or account + date) alone -- gym's
        occurrence on that date is never created;
      * the balance forecast's probe removed, or status-filtered to PENDING --
        the source row's key is projected on top of the row;
      * the balance forecast's probe keyed on date alone -- gym's owed
        occurrence is suppressed from the projection.

    No AST fence: it is dodged by ``and_``/``tuple_`` and was rejected.
    """
    today = datetime.date.today()
    seed = await _seed_on_grid(db_session, today)
    src = Transaction(
        org_id=seed["org_id"], account_id=seed["account_a"],
        category_id=seed["cat_id"], description="rent",
        amount=Decimal("10.00"), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=today, settled_date=today,
    )
    db_session.add(src)
    await db_session.commit()

    promoted = await transaction_service.promote_to_recurring(
        db_session, seed["org_id"], src.id,
        PromoteToRecurringRequest(frequency="weekly", next_due_date=today),
        today=today,
    )
    tid = promoted.recurring_id
    assert (await _reload(db_session, tid)).next_due_date == today   # AT the frontier
    gym = await _add(db_session, _template(
        seed, description="gym", amount=Decimal("7.00"), next_due_date=today,
    ))
    owners = {seed["account_a"]: {Decimal("10"): tid, Decimal("7"): gym.id}}
    src_key = (tid, today)
    gym_key = (gym.id, today)

    rows_before = await _row_keys(db_session, seed["org_id"])
    assert rows_before == {src_key}
    before = await _projected(db_session, seed, today, owners)
    assert src_key not in before
    assert gym_key in before                  # same account, same date, still owed
    assert (tid, today + WEEK) in before      # the rest of the grid IS projected
    net_before = await _net(db_session, seed["org_id"], today)

    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    rows = await _row_keys(db_session, seed["org_id"])
    after = await _projected(db_session, seed, today, owners)
    created = rows - rows_before

    assert src_key in rows
    assert gym_key in created
    assert before == created | after
    assert not rows & after
    assert await _net(db_session, seed["org_id"], today) == net_before


# ─────────────────────────────────────────────────────────────────────────────
# X1 — TBD-509, reproduced. Known exception #2 of the definition.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.xfail(
    strict=True,
    # Only the assertion may xfail. Without ``raises`` a fixture or setup
    # error is swallowed as an "expected failure" too.
    raises=AssertionError,
    reason="TBD-509: rewinding the frontier onto an already-generated date "
    "re-spends occurrences_elapsed through the exists branch",
)
async def test_x1_rewind_onto_generated_date_double_spends_elapsed(db_session):
    """GUARD (strict xfail). One occurrence, one row, one instalment spent.

    Monthly, 12 instalments, frontier on ``p_start``. Generate (1 row, elapsed
    1), rewind ``next_due_date`` back onto ``p_start`` through the real update
    path (legal: ``validate_frontier`` floors at ``p_start``), generate again.
    The ``exists`` branch finds the row and ``_advance_frontier`` spends a
    second instalment for the same key. Flips to XPASS -> FAIL when TBD-509
    lands, which is the prompt to delete this marker.
    """
    today = datetime.date.today()
    seed = await _seed_on_grid(db_session, today)
    p_start, _ = seed["p1"]
    t = await _add(db_session, _template(
        seed, frequency="monthly", next_due_date=p_start, occurrence_count=12,
    ))

    # Every precondition is ``pytest.fail``, never ``assert``: under
    # ``raises=AssertionError`` a broken precondition would otherwise XFAIL
    # for the wrong reason. The LAST line is the only ``assert`` in this test.
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    if (await _reload(db_session, t.id)).occurrences_elapsed != 1:
        pytest.fail("precondition: first run must spend exactly one instalment")

    await recurring_service.update_recurring(
        db_session, seed["org_id"], t.id,
        RecurringUpdate(next_due_date=p_start), today=today,
    )
    if (await _reload(db_session, t.id)).next_due_date != p_start:
        pytest.fail("precondition: the rewind must land on p_start")
    await recurring_service.generate_due_transactions(
        db_session, seed["org_id"], today=today
    )
    if await _row_keys(db_session, seed["org_id"]) != {(t.id, p_start)}:
        pytest.fail("precondition: still exactly one row for the one key")
    assert (await _reload(db_session, t.id)).occurrences_elapsed == 1
