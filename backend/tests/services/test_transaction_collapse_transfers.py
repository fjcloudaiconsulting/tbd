"""TBD-268: server-side transfer collapse before pagination.

The defect: the server paginated and the CLIENT then hid one leg of each
transfer pair, so the hide ate rows out of an already-truncated page. Because
``create_transfer`` adds the expense leg before the income leg and flushes both
together, the income leg reliably takes the HIGHER id -- so a client rule keyed
on ``id > linked_transaction_id`` hid EVERY row returned by ``?type=income`` or
by ``?account_id=<account holding the higher-id leg>``. The list rendered its
empty state while Pagination said "N total".

The fix moves the collapse into the query, BEFORE the LIMIT, behind an opt-in
``collapse_transfers`` flag. Each fence below names the wrong implementation it
kills; every one of them was verified RED against that injected implementation
before being accepted (see specs/2026-07-30-transaction-transfer-collapse.md).

The load-bearing subtlety: ``linked_transaction_id`` is NOT a transfer marker.
``_link_pair`` writes it bidirectionally; ``reconciliation_service._apply_match``
writes it ONE-WAY, and that direction is a discriminator
``transaction_filters.balance_contribution_filter`` depends on. So the collapse
predicate must test MUTUALITY (B4), or it suppresses reconcile-matched rows.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.tag import Tag, TransactionTag
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services import transaction_service

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


# ── fixture helpers ────────────────────────────────────────────────────────


async def _org(db, name):
    org = Organization(name=name, billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    return org.id, at.id


async def _acct(db, org_id, at_id, name):
    a = Account(
        org_id=org_id, name=name, account_type_id=at_id,
        balance=Decimal("0"), currency="EUR",
    )
    db.add(a)
    await db.flush()
    return a.id


async def _cat(db, org_id, name, slug):
    c = Category(org_id=org_id, name=name, slug=slug, type=CategoryType.BOTH)
    db.add(c)
    await db.flush()
    return c.id


async def _tx(
    db, org_id, acct_id, cat_id, *,
    desc, amount="10.00", when=None, tx_type=TransactionType.EXPENSE,
    status=TransactionStatus.SETTLED,
):
    when = when or date(2026, 5, 1)
    t = Transaction(
        org_id=org_id, account_id=acct_id, category_id=cat_id, description=desc,
        amount=Decimal(amount), type=tx_type, status=status,
        date=when, settled_date=when if status == TransactionStatus.SETTLED else None,
    )
    db.add(t)
    await db.flush()
    return t.id


async def _pair(db, org_id, cat_id, *, acct_from, acct_to, desc, amount="50.00", when=None):
    """Create a reciprocally-linked transfer pair, expense leg FIRST.

    Mirrors ``create_transfer``: the expense (source) leg is added before the
    income (destination) leg, so the INCOME leg takes the higher id. Several
    fences below depend on that asymmetry to stay non-vacuous.

    Returns ``(expense_id, income_id)`` with ``expense_id < income_id``.
    """
    exp = await _tx(
        db, org_id, acct_from, cat_id, desc=f"{desc} out", amount=amount,
        when=when, tx_type=TransactionType.EXPENSE,
    )
    inc = await _tx(
        db, org_id, acct_to, cat_id, desc=f"{desc} in", amount=amount,
        when=when, tx_type=TransactionType.INCOME,
    )
    assert exp < inc, "fixture invariant: income leg must take the higher id"
    await db.execute(
        text("UPDATE transactions SET linked_transaction_id=:p WHERE id=:i"),
        [{"p": inc, "i": exp}, {"p": exp, "i": inc}],
    )
    return exp, inc


async def _fk_off(db):
    """Disable SQLite FK enforcement on the shared StaticPool connection.

    SQLite refuses this pragma inside a transaction, so commit first. Only used
    by the fences that must write states no sanctioned code path can produce
    (a dangling link, a transaction whose Account row is absent).
    """
    await db.commit()
    await db.execute(text("PRAGMA foreign_keys=OFF"))


async def _fk_on(db):
    await db.commit()
    await db.execute(text("PRAGMA foreign_keys=ON"))


async def _seed_two_pairs_and_six_singles(db):
    """10 rows: 6 unlinked + 2 reciprocal pairs. Collapses to 8."""
    org_id, at = await _org(db, "A")
    a = await _acct(db, org_id, at, "Account A")
    b = await _acct(db, org_id, at, "Account B")
    cat = await _cat(db, org_id, "General", "general")
    for i in range(6):
        await _tx(db, org_id, a, cat, desc=f"single-{i}", when=date(2026, 5, 1) + timedelta(days=i))
    p1 = await _pair(db, org_id, cat, acct_from=a, acct_to=b, desc="t1")
    p2 = await _pair(db, org_id, cat, acct_from=a, acct_to=b, desc="t2")
    await db.commit()
    return {"org_id": org_id, "a": a, "b": b, "cat": cat, "p1": p1, "p2": p2}


# ── B1: collapse actually collapses, on BOTH queries ───────────────────────


async def test_b1_collapse_folds_pairs_in_items_and_total(db_session):
    """B1 — 10 rows, 2 reciprocal pairs, limit=10 => 8 items AND total 8.

    Kills: a server-side no-op, and a collapse applied to ``page_q`` only
    (which would return 8 items against a total of 10 and leave Pagination
    lying about how many rows exist).
    """
    f = await _seed_two_pairs_and_six_singles(db_session)

    items, total = await transaction_service.list_transactions(
        db_session, f["org_id"], limit=10, offset=0, collapse_transfers=True,
    )

    assert len(items) == 8
    assert total == 8
    # The surviving leg of each pair is the LOWER-id (expense) one.
    ids = {t.id for t in items}
    assert f["p1"][0] in ids and f["p1"][1] not in ids
    assert f["p2"][0] in ids and f["p2"][1] not in ids


# ── B2: default off ────────────────────────────────────────────────────────


async def test_b2_without_flag_both_legs_survive(db_session):
    """B2 — same fixture, collapse_transfers=False => 10 items AND total 10.

    Kills: an unconditional collapse. The flag is opt-in because aggregate
    callers that sum per account need BOTH legs (each sits on a different
    account), so a default-on collapse would zero an account's column.
    """
    f = await _seed_two_pairs_and_six_singles(db_session)

    items, total = await transaction_service.list_transactions(
        db_session, f["org_id"], limit=10, offset=0, collapse_transfers=False,
    )

    assert len(items) == 10
    assert total == 10


# ── B3: the production blackout ────────────────────────────────────────────


async def test_b3_partner_filtered_out_keeps_the_higher_id_leg(db_session):
    """B3 (highest value) — pair expense@A <-> income@B, filtered to account B.

    The income leg has the HIGHER id, so a predicate that only keeps the
    lower-id leg returns ZERO rows against a non-zero total: exactly the
    production blackout TBD-268 reports.

    Kills: branch 5 omitted (``partner not in the filtered set``). Asserts the
    returned id explicitly, not just the count -- the count alone would pass
    against a predicate that kept the wrong leg.
    """
    org_id, at = await _org(db_session, "A")
    a = await _acct(db_session, org_id, at, "Account A")
    b = await _acct(db_session, org_id, at, "Account B")
    cat = await _cat(db_session, org_id, "General", "general")
    exp, inc = await _pair(db_session, org_id, cat, acct_from=a, acct_to=b, desc="t")
    await db_session.commit()

    assert inc > exp, "non-vacuity: the surviving leg must be the HIGHER-id one"

    items, total = await transaction_service.list_transactions(
        db_session, org_id, account_id=b, limit=25, offset=0, collapse_transfers=True,
    )

    assert [t.id for t in items] == [inc]
    assert total == 1


# ── B4: mutuality — the reconciliation-match guard ─────────────────────────


async def test_b4_one_way_link_is_never_collapsed(db_session):
    """B4 — a ONE-WAY link (``a.linked = b.id``, ``b.linked = None``) is a
    reconciliation match, not a transfer pair. Both rows must be returned.

    Kills: any predicate without the mutuality EXISTS -- i.e. the ticket's own
    proposed SQL, and the rule the client shipped on ``main``. Same org and
    deliberately the SAME type, because ``_apply_match`` does not require
    opposite types.
    """
    org_id, at = await _org(db_session, "A")
    a = await _acct(db_session, org_id, at, "Account A")
    cat = await _cat(db_session, org_id, "General", "general")
    lo = await _tx(db_session, org_id, a, cat, desc="canonical")
    hi = await _tx(db_session, org_id, a, cat, desc="imported duplicate")
    assert hi > lo
    # _apply_match points the imported (higher-id) row at its canonical target.
    await db_session.execute(
        text("UPDATE transactions SET linked_transaction_id=:p WHERE id=:i"),
        {"p": lo, "i": hi},
    )
    await db_session.commit()

    items, total = await transaction_service.list_transactions(
        db_session, org_id, limit=25, offset=0, collapse_transfers=True,
    )

    assert {t.id for t in items} == {lo, hi}
    assert total == 2


# ── B5: dangling link — fail open ──────────────────────────────────────────


async def test_b5_dangling_link_row_still_returned(db_session):
    """B5 — ``a.linked_transaction_id`` points at a row that does not exist.

    Kills: a fail-CLOSED predicate. A row whose partner is missing has no other
    representative, so dropping it makes it unreachable from the UI entirely.

    NON-VACUITY (this fence was originally written wrong and caught by the
    injection gate): the dangling target must have a LOWER id than the row.
    With a HIGHER target (e.g. 999999) the lower-id branch is trivially true
    and rescues the row on its own, so the fence passes against predicates it
    is supposed to kill -- including the naive ``id > linked_transaction_id``
    rule that shipped on the client.
    """
    org_id, at = await _org(db_session, "A")
    a = await _acct(db_session, org_id, at, "Account A")
    cat = await _cat(db_session, org_id, "General", "general")
    ghost = await _tx(db_session, org_id, a, cat, desc="about to vanish")
    lone = await _tx(db_session, org_id, a, cat, desc="dangling")
    assert lone > ghost
    await _fk_off(db_session)
    # Delete the partner OUT from under the link. FK enforcement is off, so
    # the ondelete=SET NULL never fires and the link is left dangling at an
    # id that is lower than the surviving row's.
    await db_session.execute(text("DELETE FROM transactions WHERE id=:i"), {"i": ghost})
    await db_session.execute(
        text("UPDATE transactions SET linked_transaction_id=:g WHERE id=:i"),
        {"g": ghost, "i": lone},
    )
    await _fk_on(db_session)

    items, total = await transaction_service.list_transactions(
        db_session, org_id, limit=25, offset=0, collapse_transfers=True,
    )

    assert [t.id for t in items] == [lone]
    assert total == 1


# ── B6: self-link — never drop a row we cannot pair ────────────────────────


async def test_b6_self_linked_row_still_returned(db_session):
    """B6 — ``a.linked_transaction_id == a.id``, written by direct SQL.

    Trace a self-link under a predicate lacking branch 2: IS NULL false,
    ``id < id`` false, and the partner (itself) IS in the filtered set so the
    mutuality EXISTS succeeds -- the row VANISHES. Unreachable via sanctioned
    paths (``_link_pair`` invariant 7; ``_apply_match`` rejects
    ``match_id == tx.id``) but reachable by direct SQL, and a vanishing row is
    the worst possible failure mode for a ticket about vanishing rows.

    Kills: branch 2 omitted.
    """
    org_id, at = await _org(db_session, "A")
    a = await _acct(db_session, org_id, at, "Account A")
    cat = await _cat(db_session, org_id, "General", "general")
    me = await _tx(db_session, org_id, a, cat, desc="self-linked")
    await db_session.execute(
        text("UPDATE transactions SET linked_transaction_id=:i WHERE id=:i"),
        {"i": me},
    )
    await db_session.commit()

    items, total = await transaction_service.list_transactions(
        db_session, org_id, limit=25, offset=0, collapse_transfers=True,
    )

    assert [t.id for t in items] == [me]
    assert total == 1

    # ...and the RESPONSE must not dress a self-link up as a transfer. The
    # eager load resolves ``linked_transaction`` to the row itself, so without
    # ``partner.id != tx.id`` in ``to_response`` this row renders
    # "Real -> Real" with an Unlink button that would hand
    # ``unpair_transactions`` a row pointing at itself. Measured with the
    # self-check removed: ``linked_account_name='Real'``.
    assert transaction_service.to_response(items[0]).linked_account_name is None


# ── B7: cross-org link — fail open, and never leak ─────────────────────────


async def test_b7_cross_org_reciprocal_link_is_not_collapsed(db_session):
    """B7 — two rows in DIFFERENT orgs link to each other reciprocally.

    Org A's row is returned (fail-open: from org A's perspective the partner
    does not exist), and org B's row never appears in org A's items or total.
    Org A's row is created SECOND so it takes the HIGHER id, making the
    lower-id branch inapplicable.

    Kills: org scoping being dropped from the collapse. MEASURED during the
    injection gate, and worth writing down because it is not what the design
    brief assumed: the two org clauses are MUTUALLY REDUNDANT here. Removing
    only the ``filtered_ids`` org clause leaves the EXISTS org clause to
    fail-open the row; removing only the EXISTS org clause leaves branch 5 to
    do it (a cross-org partner can never be inside an org-scoped filtered
    set). This fence goes red only when BOTH are removed. Both are kept
    anyway: ``_transfer_collapse_clause`` is a reusable clause builder, and
    the EXISTS must stand on its own if it is ever paired with a differently
    scoped subquery.
    """
    org_b, at_b = await _org(db_session, "B")
    acct_b = await _acct(db_session, org_b, at_b, "B acct")
    cat_b = await _cat(db_session, org_b, "General", "general")
    row_b = await _tx(db_session, org_b, acct_b, cat_b, desc="other org")

    org_a, at_a = await _org(db_session, "A")
    acct_a = await _acct(db_session, org_a, at_a, "A acct")
    cat_a = await _cat(db_session, org_a, "General", "general")
    row_a = await _tx(db_session, org_a, acct_a, cat_a, desc="my org")

    assert row_a > row_b, "non-vacuity: org A's row must be the HIGHER-id one"
    await db_session.execute(
        text("UPDATE transactions SET linked_transaction_id=:p WHERE id=:i"),
        [{"p": row_b, "i": row_a}, {"p": row_a, "i": row_b}],
    )
    await db_session.commit()

    items, total = await transaction_service.list_transactions(
        db_session, org_a, limit=25, offset=0, collapse_transfers=True,
    )

    assert [t.id for t in items] == [row_a]
    assert total == 1
    assert row_b not in {t.id for t in items}

    # ...and org B's ACCOUNT NAME must not leak through the response either.
    # ``selectinload(Transaction.linked_transaction)`` follows the raw FK with
    # NO org predicate, so the partner object IS loaded here and carries the
    # other tenant's account. ``partner.org_id == tx.org_id`` in
    # ``to_response`` is the only thing stopping it: measured with that clause
    # removed, ``linked_account_name='B acct'``. This is a tenant-isolation
    # clause, not a cosmetic one, and it is not covered by the predicate
    # assertions above -- they never call ``to_response``.
    assert transaction_service.to_response(items[0]).linked_account_name is None


# ── B8: paging is a partition, under every sort key ────────────────────────


@pytest.mark.parametrize(
    "sort_by",
    ["date", "amount", "description", "status", "account_name", "category_name"],
)
@pytest.mark.parametrize("sort_dir", ["asc", "desc"])
async def test_b8_paging_partitions_the_collapsed_set(db_session, sort_by, sort_dir):
    """B8 — walk every page at limit=3 and assert the concatenation is exactly
    the collapsed set: no duplicates, no missing ids, length == total.

    Kills: any sort-dependent survivor rule (which flips a pair's survivor
    between windows, producing both gaps and duplicates), and the
    account_name / category_name INNER join asymmetry.
    """
    org_id, at = await _org(db_session, "A")
    a = await _acct(db_session, org_id, at, "Zeta")
    b = await _acct(db_session, org_id, at, "Alpha")
    cat1 = await _cat(db_session, org_id, "General", "general")
    cat2 = await _cat(db_session, org_id, "Transfer", "transfer")
    expected = set()
    for i in range(7):
        expected.add(await _tx(
            db_session, org_id, a if i % 2 else b, cat1 if i % 2 else cat2,
            desc=f"single-{i:02d}", amount=f"{10 + i}.00",
            when=date(2026, 5, 1) + timedelta(days=i),
            status=TransactionStatus.PENDING if i % 3 == 0 else TransactionStatus.SETTLED,
        ))
    for j in range(3):
        exp, _inc = await _pair(
            db_session, org_id, cat2, acct_from=a, acct_to=b,
            desc=f"pair-{j}", amount=f"{100 + j}.00",
            when=date(2026, 5, 10) + timedelta(days=j),
        )
        expected.add(exp)
    await db_session.commit()

    seen: list[int] = []
    total = None
    for page in range(10):
        items, t = await transaction_service.list_transactions(
            db_session, org_id, sort_by=sort_by, sort_dir=sort_dir,
            limit=3, offset=page * 3, collapse_transfers=True,
        )
        total = t
        if not items:
            break
        seen.extend(x.id for x in items)

    assert len(seen) == len(set(seen)), f"duplicate ids across pages: {seen}"
    assert set(seen) == expected
    assert len(seen) == total


# ── B9: the join asymmetry ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sort_by", "fk_column"),
    [("account_name", "account_id"), ("category_name", "category_id")],
)
async def test_b9_missing_parent_row_survives_name_sort(db_session, sort_by, fk_column):
    """B9 — a name sort with the joined parent row absent.

    Kills: the INNER join that ``page_q`` (and only ``page_q``) attaches for
    the account_name / category_name sorts. An inner join silently drops the
    row from ``items`` while ``count_q`` still counts it, so ``total`` stops
    equalling the number of rows the client renders -- the very guarantee this
    ticket exists to establish. Unreachable while the FK holds (both columns
    are ``nullable=False``, and the fixture must turn FK enforcement OFF to
    reach the state at all); the guarantee must not depend on referential
    integrity.

    PARAMETRIZED over both sorts on purpose. The two joins were changed
    together under one comment, and with only the account_name case covered,
    reverting JUST the category_name branch to an inner join was invisible to
    the entire suite: measured ``items=[1] / total=2``, exactly the
    items/total divergence B9 exists to forbid.
    """
    org_id, at = await _org(db_session, "A")
    a = await _acct(db_session, org_id, at, "Real")
    cat = await _cat(db_session, org_id, "General", "general")
    keeper = await _tx(db_session, org_id, a, cat, desc="has parent")
    await _fk_off(db_session)
    orphan = await _tx(db_session, org_id, a, cat, desc="orphan")
    await db_session.execute(
        text(f"UPDATE transactions SET {fk_column}=888888 WHERE id=:i"), {"i": orphan},
    )
    await _fk_on(db_session)

    items, total = await transaction_service.list_transactions(
        db_session, org_id, sort_by=sort_by, sort_dir="asc",
        limit=25, offset=0, collapse_transfers=True,
    )

    assert {t.id for t in items} == {keeper, orphan}
    assert total == 2
    assert len(items) == total


# ── B10: a full page really is a full page ─────────────────────────────────


async def test_b10_full_page_of_collapsed_rows(db_session):
    """B10 — 30 raw rows (6 pairs + 18 singles) at limit=25.

    Page 0 returns all 24 collapsed rows, page 1 returns none, total == 24.
    Kills a collapse that runs after the LIMIT (which would return 25 raw rows
    folded down to fewer than 24 on page 0 and leave rows stranded on page 1).
    """
    org_id, at = await _org(db_session, "A")
    a = await _acct(db_session, org_id, at, "Account A")
    b = await _acct(db_session, org_id, at, "Account B")
    cat = await _cat(db_session, org_id, "General", "general")
    for i in range(18):
        await _tx(db_session, org_id, a, cat, desc=f"single-{i:02d}",
                  when=date(2026, 5, 1) + timedelta(days=i))
    for j in range(6):
        await _pair(db_session, org_id, cat, acct_from=a, acct_to=b, desc=f"p{j}")
    await db_session.commit()

    page0, total = await transaction_service.list_transactions(
        db_session, org_id, limit=25, offset=0, collapse_transfers=True,
    )
    page1, _ = await transaction_service.list_transactions(
        db_session, org_id, limit=25, offset=25, collapse_transfers=True,
    )

    assert total == 24
    assert len(page0) == 24
    assert len(page1) == 0


# ── B11 / B12: linked_account_name ─────────────────────────────────────────


async def test_b11_linked_account_name_populated_on_collapsed_rows(db_session):
    """B11 — the collapsed transfer row carries its partner's account name;
    an unlinked row carries None.

    After the server collapse the partner is NEVER in the page, so the client
    cannot resolve it from the returned array. Kills the ``__dict__.get``
    probe in ``to_response`` regressing to plain attribute access, which would
    raise MissingGreenlet from the async context on every non-eager-loaded
    call site.
    """
    org_id, at = await _org(db_session, "A")
    a = await _acct(db_session, org_id, at, "Checking")
    b = await _acct(db_session, org_id, at, "Savings")
    cat = await _cat(db_session, org_id, "General", "general")
    solo = await _tx(db_session, org_id, a, cat, desc="solo")
    exp, _inc = await _pair(db_session, org_id, cat, acct_from=a, acct_to=b, desc="tr")
    await db_session.commit()

    items, _ = await transaction_service.list_transactions(
        db_session, org_id, limit=25, offset=0, collapse_transfers=True,
    )
    by_id = {t.id: transaction_service.to_response(t) for t in items}

    assert by_id[exp].linked_account_name == "Savings"
    assert by_id[exp].account_name == "Checking"
    assert by_id[solo].linked_account_name is None


async def test_b12_linked_account_name_none_without_the_flag(db_session):
    """B12 — with collapse_transfers=False the eager load is not attached, so
    ``linked_account_name`` is None on every row.

    Kills an UNGATED eager load, which would tax every default caller (and
    every other ``to_response`` call site) with two extra queries per page for
    a field they never read.
    """
    org_id, at = await _org(db_session, "A")
    a = await _acct(db_session, org_id, at, "Checking")
    b = await _acct(db_session, org_id, at, "Savings")
    cat = await _cat(db_session, org_id, "General", "general")
    await _pair(db_session, org_id, cat, acct_from=a, acct_to=b, desc="tr")
    await db_session.commit()

    items, _ = await transaction_service.list_transactions(
        db_session, org_id, limit=25, offset=0, collapse_transfers=False,
    )

    assert len(items) == 2
    assert all(
        transaction_service.to_response(t).linked_account_name is None for t in items
    )


async def _tag(db, org_id, name):
    t = Tag(org_id=org_id, name=name, name_normalized=name.lower())
    db.add(t)
    await db.flush()
    return t.id


async def _attach(db, tx_id, tag_id):
    db.add(TransactionTag(transaction_id=tx_id, tag_id=tag_id))
    await db.flush()


@pytest.mark.parametrize("shape", ["search", "tags", "tags_exclude"])
async def test_b14_branch5_under_subquery_filters(db_session, shape):
    """B14 — branch 5 exercised through filters that are NOT ``account_id``.

    Every other branch-5 fence reaches it via ``account_id``, a plain column
    predicate. ``search`` / ``tags`` / ``tags_exclude`` compile
    ``filtered_ids`` into a NESTED derived table
    (``NOT IN (SELECT id FROM (... IN (SELECT transaction_id ...)))``), a
    different SQL shape that no test exercised. The exactly-one property is
    safe by construction here -- tag filters are IN-subqueries, not joins, so
    they cannot duplicate a row -- but the shape itself was untested.

    Fixture excludes the LOWER-id (expense) leg in every case, so the income
    leg must survive on branch 5 alone: branch 4 cannot rescue it.
    """
    org_id, at = await _org(db_session, "A")
    a = await _acct(db_session, org_id, at, "Account A")
    b = await _acct(db_session, org_id, at, "Account B")
    cat = await _cat(db_session, org_id, "General", "general")
    exp, inc = await _pair(db_session, org_id, cat, acct_from=a, acct_to=b, desc="t")
    assert inc > exp, "non-vacuity: the surviving leg must be the HIGHER-id one"

    kwargs: dict = {}
    if shape == "search":
        # `_pair` names the legs "t out" / "t in"; "t in" matches only the
        # income leg ("t out" contains no "in").
        kwargs["search"] = "t in"
    elif shape == "tags":
        keep = await _tag(db_session, org_id, "keepme")
        await _attach(db_session, inc, keep)
        kwargs["tags"] = ["keepme"]
    else:
        drop = await _tag(db_session, org_id, "dropme")
        await _attach(db_session, exp, drop)
        kwargs["tags_exclude"] = ["dropme"]
    await db_session.commit()

    items, total = await transaction_service.list_transactions(
        db_session, org_id, limit=25, offset=0, collapse_transfers=True, **kwargs,
    )

    assert [t.id for t in items] == [inc]
    assert total == 1


async def test_b14b_both_legs_inside_a_tag_filtered_set_collapse_to_one(db_session):
    """B14b — the same nested-derived-table shape with BOTH legs inside the
    filtered set: exactly one row survives, and it is the lower-id leg.

    Pairs with B14: that fence proves the shape fails OPEN when the partner is
    excluded, this one proves it still collapses when it is not.
    """
    org_id, at = await _org(db_session, "A")
    a = await _acct(db_session, org_id, at, "Account A")
    b = await _acct(db_session, org_id, at, "Account B")
    cat = await _cat(db_session, org_id, "General", "general")
    exp, inc = await _pair(db_session, org_id, cat, acct_from=a, acct_to=b, desc="t")
    both = await _tag(db_session, org_id, "both")
    await _attach(db_session, exp, both)
    await _attach(db_session, inc, both)
    await db_session.commit()

    items, total = await transaction_service.list_transactions(
        db_session, org_id, tags=["both"], limit=25, offset=0, collapse_transfers=True,
    )

    assert [t.id for t in items] == [exp]
    assert total == 1


async def test_b11b_one_way_link_gets_no_linked_account_name(db_session):
    """B11b — mutuality also gates the RESPONSE field, not just the predicate.

    A reconcile-matched row survives the collapse (B4); it must not then be
    dressed up as a transfer by carrying a partner account name, because the
    client gates its "Unlink transfer" affordance on exactly this field.
    """
    org_id, at = await _org(db_session, "A")
    a = await _acct(db_session, org_id, at, "Checking")
    cat = await _cat(db_session, org_id, "General", "general")
    lo = await _tx(db_session, org_id, a, cat, desc="canonical")
    hi = await _tx(db_session, org_id, a, cat, desc="imported")
    await db_session.execute(
        text("UPDATE transactions SET linked_transaction_id=:p WHERE id=:i"),
        {"p": lo, "i": hi},
    )
    await db_session.commit()

    items, _ = await transaction_service.list_transactions(
        db_session, org_id, limit=25, offset=0, collapse_transfers=True,
    )
    by_id = {t.id: transaction_service.to_response(t) for t in items}

    assert by_id[hi].linked_transaction_id == lo
    assert by_id[hi].linked_account_name is None


# ── TBD-386: the survivor is chosen by (liveness, id), not by id alone ──────
#
# Branch 4 was a pure lower-id tiebreak and therefore STATE-BLIND. A reciprocal
# pair with one leg in `skipped` or `rejected` -- reverted out of
# `accounts.balance` and outside every reportable aggregate -- rendered the
# REVERTED leg as the pair's representative whenever it held the lower id, and
# hid the live one. That is not cosmetic: the surviving leg is the pair's only
# editable handle (`_link_pair` mirrors only `category_id`; description, date,
# status and tags are per-leg), so a rename lands on a row that counts toward
# nothing while the leg that IS in the ledger keeps a stale description.
#
# ⚠⚠ THE VACUITY TRAP, and it is specific. `_pair` adds the EXPENSE leg first
# and asserts `exp < inc`, so `main` already keeps the expense leg. A fence
# that reverts the INCOME (higher-id) leg is therefore GREEN AGAINST BROKEN
# CODE -- both the broken and the fixed predicate keep the expense leg, same id,
# same count. The defect fence MUST revert the LOWER-id expense leg, and it
# MUST assert the surviving ID: `len(items) == 1` is true in both directions.


async def _set_state(db, tx_id, state):
    """Force a reconciliation_state that no service path produces for this
    shape (reciprocal + rejected has no writer -- see B15's docstring)."""
    await db.execute(
        text("UPDATE transactions SET reconciliation_state=:s WHERE id=:i"),
        {"s": state, "i": tx_id},
    )
    await db.commit()


async def _seed_one_pair(db):
    org_id, at = await _org(db, "P")
    a = await _acct(db, org_id, at, "Account A")
    b = await _acct(db, org_id, at, "Account B")
    cat = await _cat(db, org_id, "General", "general")
    exp, inc = await _pair(db, org_id, cat, acct_from=a, acct_to=b, desc="tr")
    await db.commit()
    return {"org_id": org_id, "a": a, "b": b, "cat": cat, "exp": exp, "inc": inc}


async def _collapse(db, org_id, **kw):
    return await transaction_service.list_transactions(
        db, org_id, limit=50, offset=0, collapse_transfers=True, **kw
    )


@pytest.mark.parametrize("reverted_state", ["skipped", "rejected"])
async def test_b15_reverted_lower_id_leg_yields_to_the_live_leg(db_session, reverted_state):
    """B15 -- THE defect fence. The reverted leg holds the LOWER id; the LIVE
    leg must render, and it is the higher-id one.

    Kills: the shipped pure-id tiebreak `Transaction.id <
    Transaction.linked_transaction_id`, which renders the reverted leg here.

    Parametrized over the whole roster because an implementation that
    special-cases the literal "skipped" passes every skipped-only fence --
    the precedent is test_skipped_transfer_leg_balance.py's own note. Note
    `rejected` on a RECIPROCAL leg has no service writer
    (`_settle_batch_counters_and_demote_orphans` writes REJECTED only to
    NON-reciprocal referrers), so it is set by direct SQL on purpose.
    """
    f = await _seed_one_pair(db_session)
    await _set_state(db_session, f["exp"], reverted_state)

    # Non-vacuity, asserted rather than assumed: the reverted leg really is the
    # one `main` would have kept. Without this line the fence silently rots the
    # day `_pair`'s insertion order changes.
    assert f["exp"] < f["inc"]

    items, total = await _collapse(db_session, f["org_id"])

    assert [t.id for t in items] == [f["inc"]]
    assert total == 1


async def test_b16_reverted_higher_id_leg_still_yields_to_the_live_leg(db_session):
    """B16 -- the mirror. The reverted leg holds the HIGHER id.

    ⚠ NON-REGRESSION FENCE, NOT A DEFECT FENCE. It is GREEN against `main`,
    because `main` keeps the lower-id expense leg by id and that leg happens to
    be the live one here. Do not cite its green as evidence the bug is fixed --
    B15 is the fence that moves.

    Kills: an over-correction that FLIPS THE TIEBREAK DIRECTION whenever a leg
    is reverted (`and_(self_live, partner_live, tiebreak)` plus
    `and_(or_(~self_live, ~partner_live), ~tiebreak)`). B15 stays green against
    that mutant; this is the fence that moves, which is why it exists.

    ⚠ It does NOT kill "a preference written without the `self_live`
    conjunct" -- an earlier revision claimed that and MEASUREMENT REFUTED IT,
    twice over. Both `or_(~partner_live, tiebreak)` and a bare `~partner_live`
    leave this fence GREEN, because in this shape the reverted leg holds the
    higher id so those mutants reach the right answer by accident. B15, B17,
    B18 and B20 are what kill them.
    """
    f = await _seed_one_pair(db_session)
    await _set_state(db_session, f["inc"], "skipped")

    items, total = await _collapse(db_session, f["org_id"])

    assert [t.id for t in items] == [f["exp"]]
    assert total == 1


async def test_b17_both_legs_reverted_still_collapses_to_exactly_one(db_session):
    """B17 -- both legs reverted. Exactly one row, deterministically the lower id.

    Kills: the naive "prefer the live leg" clause written as
    `and_(self_live, ...)` or `and_(self_live, ~partner_live)` alone. Under
    those, NEITHER leg is kept: the transfer vanishes from the list entirely
    and `total` under-counts. That is strictly WORSE than the defect being
    fixed, and B15 and B16 are both green against it -- this is the highest-
    value fence in the group after B15.
    """
    f = await _seed_one_pair(db_session)
    await _set_state(db_session, f["exp"], "skipped")
    await _set_state(db_session, f["inc"], "rejected")

    items, total = await _collapse(db_session, f["org_id"])

    assert [t.id for t in items] == [f["exp"]]
    assert total == 1


@pytest.mark.parametrize(
    "exp_state,inc_state,expect_key",
    [
        ("accepted", "accepted", "exp"),   # both live  -> lower id
        ("skipped", "accepted", "inc"),    # lower reverted -> live wins
        ("accepted", "skipped", "exp"),    # higher reverted -> live wins
        ("skipped", "skipped", "exp"),     # both reverted -> lower id
    ],
)
async def test_b18_items_and_total_agree_in_every_liveness_case(
    db_session, exp_state, inc_state, expect_key
):
    """B18 -- exactly-one holds across all four cases, on BOTH queries.

    Kills: a fix applied to `page_q` only. `reconcile_account`'s own note
    records why that half-fix class is worth pinning even when the clause is
    shared: "gating only one is a half-fix that swaps a permanent false
    negative for a smaller one".

    ⚠ That kill is NOT unique to this fence -- measured, a page_q-only mutant
    also reddens B1, B8, B15, B16 and B17. This fence earns its place by
    covering all four liveness cases in ONE table, which is what makes a gap
    visible at a glance; it does not earn it by uniqueness.
    """
    f = await _seed_one_pair(db_session)
    await _set_state(db_session, f["exp"], exp_state)
    await _set_state(db_session, f["inc"], inc_state)

    items, total = await _collapse(db_session, f["org_id"])

    assert len(items) == 1
    assert total == len(items)
    assert items[0].id == f[expect_key]


async def test_b19_reverted_rows_outside_a_reciprocal_pair_are_untouched(db_session):
    """B19 -- a reverted row that is NOT one leg of a reciprocal pair still renders.

    Three shapes: unlinked, the target of a ONE-WAY match, and self-linked.

    Kills TWO distinct wrongs:
      * conjoining the state term with the whole clause, or hoisting it above
        the `or_()` -- which silently deletes every reverted row from the list.
        That is the transactions list's role as the ONLY recovery surface for a
        mis-skipped row, and SKIPPED/REJECTED are terminal, so a row nobody can
        see has no way back. TBD-268's blackout, re-entered through a different
        door.
    ⚠ An earlier revision ALSO claimed this kills "a liveness EXISTS missing
    `linked_transaction_id == Transaction.id`". That claim was WITHDRAWN after
    measurement: the mutant survives the whole file, and it is not a weak fence
    but an IMPOSSIBLE one. `partner_live` matches on `_cps.id ==
    Transaction.linked_transaction_id`, a PK equality, so at most one row; and
    branch 4 only decides a row's fate when branch 3 is FALSE, which by
    definition means that same unique row already satisfies both the backlink
    and the org conjunct. Both are logically implied, so NO behavioural test
    can ever kill either. They stay as defence in depth if branch 3 is ever
    narrowed -- but a docstring naming a kill that cannot exist is precisely
    the "fence asserts existence, not behaviour" class this repo keeps
    re-learning.
    """
    org_id, at = await _org(db_session, "S")
    a = await _acct(db_session, org_id, at, "A")
    cat = await _cat(db_session, org_id, "G", "g")

    lone = await _tx(db_session, org_id, a, cat, desc="lone skipped")
    src = await _tx(db_session, org_id, a, cat, desc="one-way source")
    dst = await _tx(db_session, org_id, a, cat, desc="one-way target")
    selfie = await _tx(db_session, org_id, a, cat, desc="self linked")
    await db_session.commit()

    # one-way: src -> dst, dst does NOT link back (reconciliation match shape)
    await db_session.execute(
        text("UPDATE transactions SET linked_transaction_id=:p WHERE id=:i"),
        {"p": dst, "i": src},
    )
    await db_session.execute(
        text("UPDATE transactions SET linked_transaction_id=:p WHERE id=:i"),
        {"p": selfie, "i": selfie},
    )
    await db_session.commit()
    for t in (lone, src, selfie):
        await _set_state(db_session, t, "skipped")

    items, _ = await _collapse(db_session, org_id)
    ids = {t.id for t in items}

    assert lone in ids
    assert src in ids
    assert selfie in ids


async def test_b4b_reverted_one_way_higher_id_row_still_renders(db_session):
    """B4b -- branch 3's mutant, which B4 stops killing once branch 4 is
    liveness-aware.

    B4's rows are both live, so under the new predicate the live half of
    branch 4 fires and keeps the higher-id one-way row EVEN WITH BRANCH 3
    DELETED. Branch 3 only stays load-bearing for a REVERTED higher-id one-way
    row, which is what this builds.

    Kills: deleting the mutuality EXISTS (branch 3).
    """
    org_id, at = await _org(db_session, "OW")
    a = await _acct(db_session, org_id, at, "A")
    cat = await _cat(db_session, org_id, "G", "g")
    lo = await _tx(db_session, org_id, a, cat, desc="canonical")
    hi = await _tx(db_session, org_id, a, cat, desc="imported duplicate")
    await db_session.commit()
    assert lo < hi
    # hi -> lo one-way; lo does NOT link back.
    await db_session.execute(
        text("UPDATE transactions SET linked_transaction_id=:p WHERE id=:i"),
        {"p": lo, "i": hi},
    )
    await db_session.commit()
    await _set_state(db_session, hi, "skipped")

    items, _ = await _collapse(db_session, org_id)

    assert hi in {t.id for t in items}


async def test_b20_the_reverted_roster_can_grow(db_session, monkeypatch):
    """B20 -- both sides of the comparison read ONE roster, resolved per call.

    Kills the silent-drift mode the roster's own comment predicts: one side
    reading `REVERTED_RECONCILIATION_STATES` while the other hardcodes
    ("skipped", "rejected"). Every other fence here is green against that
    mutant, because today's roster has exactly two members and both sides agree
    on them.

    ⚠ TWO vacuity guards, both mandatory:
      1. The NEGATIVE CONTROL below. Without it a no-op monkeypatch (wrong
         target, or an expression captured at import time) produces a fake
         green -- the roster grows, nothing changes, and the assertion that
         "the live leg renders" is satisfied by the UNPATCHED behaviour.
      2. The patch target is `transaction_service`, NOT `transaction_filters`.
         `transaction_service.py` does `from ... import
         REVERTED_RECONCILIATION_STATES`, so it holds its own binding and
         patching the source module is a no-op. This is also why the clause
         builds its expressions INSIDE the function: an import-time-captured
         expression makes this fence worthless by construction.
    """
    f = await _seed_one_pair(db_session)
    # `edited` is a real enum value and is NOT in today's roster.
    # ⚠ Not because "SQLite's CHECK accepts it" -- an earlier revision said
    # that and it is wrong: SQLAlchemy 2.0 defaults `Enum.create_constraint`
    # to False, so the emitted SQLite DDL is a bare VARCHAR(14) NOT NULL with
    # NO constraint at all, and would accept `'totally_bogus'` just as
    # happily. A real enum value is used because it keeps the fixture honest
    # on MySQL, where the ENUM would reject anything else.
    await _set_state(db_session, f["exp"], "edited")

    # NEGATIVE CONTROL, unpatched: `edited` counts as live, so the lower-id
    # expense leg wins on the ordinary tiebreak.
    items, _ = await _collapse(db_session, f["org_id"])
    assert [t.id for t in items] == [f["exp"]], (
        "negative control: with `edited` OUTSIDE the roster the lower-id leg "
        "must win; if this already returns the income leg the fence below "
        "proves nothing"
    )

    monkeypatch.setattr(
        transaction_service,
        "REVERTED_RECONCILIATION_STATES",
        ("skipped", "rejected", "edited"),
    )
    items, total = await _collapse(db_session, f["org_id"])

    assert [t.id for t in items] == [f["inc"]]
    assert total == 1


async def test_b21_rows_outside_the_pair_region_are_unchanged(db_session):
    """B21 -- the blast-radius fence.

    Branch 4 decides a row's fate ONLY when branches 1/2/3/5 are all false,
    i.e. only for legs of reciprocal, same-org, non-self pairs with BOTH legs
    inside the filtered set. Everywhere else the `or_()` is already true. So
    making branch 4 liveness-aware must change nothing outside that region --
    including for singles carrying assorted reconciliation states.

    Kills: any fix that makes branch 4 load-bearing outside the pair region.

    ⚠ Measured across 27 mutants, this fence dies only where B19 also dies, so
    it is currently SUBSUMED and adds no unique kill. Kept deliberately: it
    states the blast-radius invariant directly and over a much wider fixture
    (two pairs plus six singles carrying assorted states), so it is the fence
    that will still be meaningful if branch 3 or branch 5 is ever narrowed and
    the pair region widens.
    """
    f = await _seed_two_pairs_and_six_singles(db_session)
    baseline_items, baseline_total = await _collapse(db_session, f["org_id"])
    baseline = {t.id for t in baseline_items}

    singles = sorted(baseline - {f["p1"][0], f["p2"][0]})
    for tx_id, state in zip(singles, ["skipped", "rejected", "edited", "matched"]):
        await _set_state(db_session, tx_id, state)

    items, total = await _collapse(db_session, f["org_id"])

    assert {t.id for t in items} == baseline
    assert total == baseline_total


@pytest.mark.parametrize("reverted_leg,acct_key", [("exp", "a"), ("inc", "b")])
async def test_b22_branch5_still_wins_over_the_liveness_preference(
    db_session, reverted_leg, acct_key
):
    """B22 -- a REVERTED leg whose live partner is filtered OUT must still render.

    It is the transfer's only representative in the filtered set, so hiding it
    makes the row reachable from nowhere.

    Kills TWO wrong fixes:
      * AND-ing `non_reverted_transaction_filter()` (or a bare `~self_live`)
        into the clause -- zero rows against a non-zero total, TBD-268's
        blackout re-introduced by the fix for a different bug;
      * gating branch 5 itself on liveness, e.g.
        `and_(or_(self_live, tiebreak), linked_transaction_id.notin_(...))`.
        That reads as a natural extension of the liveness preference and is
        the shape a careless widening produces.

    ⚠⚠ PARAMETRIZED OVER WHICH LEG IS REVERTED, AND THAT IS THE WHOLE POINT.
    An earlier revision reverted only the EXPENSE (lower-id) leg -- and was
    MEASURED GREEN against the branch-5-gating mutant above, because `tiebreak`
    is true for the lower-id leg and rescued the row for entirely the wrong
    reason. The mutant survived all 43 fences in this file.

    That is the identical fixture-polarity trap B15's own ⚠⚠ block warns
    about, walked into a hundred lines further down. The high-id case is the
    one that discriminates: with the INCOME leg reverted, `tiebreak` is FALSE
    and only a genuinely ungated branch 5 keeps the row.
    """
    f = await _seed_one_pair(db_session)
    await _set_state(db_session, f[reverted_leg], "skipped")

    # Filter to the account holding the REVERTED leg. Its live partner sits in
    # the other account and is excluded, so branch 5 fires.
    items, total = await _collapse(db_session, f["org_id"], account_id=f[acct_key])

    assert [t.id for t in items] == [f[reverted_leg]]
    assert total == 1


async def test_b23_the_two_aliases_correlate_and_do_not_leak_into_the_outer_from():
    """B23 -- a STRUCTURAL check on the compiled SQL, for BOTH dialects.

    TBD-386 adds a second ``aliased(Transaction)`` and a second correlated
    EXISTS to a clause that already had one. If either subquery fails to
    correlate, its alias is lifted into the OUTER ``FROM`` and the statement
    becomes a cartesian product -- which DOES NOT ERROR. ``total`` inflates to
    a multiple of the true count and the page repeats rows.

    ⚠ Why this is a compiled-SQL assertion and not a behavioural one. B1 would
    catch the cartesian product (its ``total == 8`` would go to 80), but ONLY
    on aiosqlite and only for the shapes B1 seeds. Every backend shard runs on
    in-process aiosqlite; ``Migration Checks`` is the one CI job
    with a MySQL service and it runs migrations, not list queries. So NO job
    anywhere executes this predicate against MySQL. A correlation difference
    between the two dialects is exactly the class the shards structurally
    cannot see, which is why this compiles both.

    Also asserts the two EXISTS use DISTINCT aliases. ⚠ Measured, so the claim
    is calibrated: sharing one alias does NOT change behaviour -- two sibling
    EXISTS built from one alias render two independent subqueries that merely
    share an alias NAME, and 42 of the 43 tests here stay green. This fence is
    the ONLY thing that notices. It is hygiene, not semantics: an alias name
    appearing in two independent FROM scopes is indistinguishable in an
    EXPLAIN and breaks the day either subquery is lifted into the outer FROM.

    ⚠ The `== 3` below is deliberately strict and WILL go red on a
    behaviourally identical refactor -- specifically the "Design B" fold the
    production docstring itself sanctions, which restores the count to 1. That
    is the expected cause of a red here. Confirm the behavioural fences are
    green, then update the number; do not go hunting for a defect.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects import mysql, sqlite

    ids = select(Transaction.id).where(Transaction.org_id == 1).subquery("f")
    stmt = select(Transaction.id).where(
        transaction_service._transfer_collapse_clause(ids)
    )

    for dialect in (sqlite.dialect(), mysql.dialect()):
        sql = str(stmt.compile(dialect=dialect))
        outer = sql[: sql.index("WHERE")]

        # The outer FROM names the real table once and no alias of it.
        assert "transactions_1" not in outer, (dialect.name, outer)
        assert "transactions_2" not in outer, (dialect.name, outer)

        # Both aliases appear, and they are DIFFERENT ones.
        assert "transactions_1" in sql, (dialect.name, sql)
        assert "transactions_2" in sql, (dialect.name, sql)

        # THREE correlated EXISTS renders, and the number is the point.
        # Branch 3's reciprocity EXISTS renders once; `partner_live` renders
        # TWICE, because it appears in both halves of branch 4 and SQLAlchemy
        # does no common-subexpression elimination for an `Exists` object used
        # at two sites in a boolean tree. `main` rendered ONE.
        #
        # That is the measured, accepted cost of keeping the numbered branches
        # rather than folding 2+3+4 into a single EXISTS (which would restore
        # the count to 1). Each is an `eq_ref` PK point lookup on
        # `transactions.PRIMARY`, and the clause runs only when
        # `collapse_transfers=true`. Pinned here so the cost cannot drift
        # upward unnoticed: if a later edit renders `partner_live` a third
        # time, this fence says so.
        assert sql.count("EXISTS (SELECT") == 3, (dialect.name, sql)
