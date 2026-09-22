"""TBD-471 — transfer-ness gets its own axis on the transactions report source.

`TransactionType.TRANSFER` has ZERO write sites: `create_transfer` types the
legs EXPENSE (source) and INCOME (destination), `apply_balance`/`revert_balance`
raise on it, and the model says so in invariant 8. Yet the Reports UI has
offered a "Transfer" checkbox on the Type control since it shipped, and the
compiler binds it -- so `Type = Transfer` has never matched a row, for any org,
and returns an empty chart with no error.

⚠ TRANSFER-NESS CANNOT BE A `txn_type` VALUE. `txn_type` publishes ops
``("eq","in")`` and renders as an OR checkbox group, so there is no AND across
values of one field: `[transfer, income]` WIDENS rather than narrowing, and
"is a transfer leg AND inbound" -- which is the actual question -- becomes
inexpressible. Hence a separate `FilterField.TRANSFER`, which composes with
`txn_type` because AST filters AND at the list level.

⚠ THE €700 PROBLEM. `Transaction.amount` is `gt=0` (magnitude only; direction
lives in `type`) and the measure is a bare `func.sum` with no CASE on type. Both
legs of a savings<->checking transfer sit on the savings account. So a savings
account with a 500 inbound and a 200 outbound transfer sums to **700**, which is
neither the gross-in (500) nor the net (300). This ticket makes 500 expressible
(`transfer=true` + `txn_type=income`); it does NOT make 700 unreachable, because
"total transfer traffic on this account" is a legitimate question. The signed
measure that would give 300 directly is TBD-553.

⚠ THE BASE CLAUSE MUST STAND DOWN. `transfer=true` conjoined with the default
`reportable_transaction_filter()` (`linked_transaction_id IS NULL`) is
UNSATISFIABLE -- zero rows, silently, which is the exact defect class this
ticket exists to remove. `_reportability_base()` returns one named clause for
the whole query, mirroring `_currency_mode` (TBD-507): an explicit request
stands the defensive default down, decided in ONE place, rather than by an
inline boolean nobody can find.

⚠⚠ F1 COMPILES THE CLAUSE ALONE, AND THE REASON IS NARROWER THAN IT FIRST
LOOKS. An earlier draft of this docstring claimed the composed mutant is
"identical" to the correct clause and that an end-to-end fence therefore proves
nothing. That is FALSE, and the injection gate disproves it: replacing the
clause with bare non-nullness turns F2, F3 and F5 red. The reduction is
``link NOT NULL AND EXISTS_bcf``, and ``balance_contribution_filter``'s EXISTS
carries only TWO conjuncts where this clause carries four. Per cell:
  * SELF-LINK -- diverges AND is visible composed (``bcf`` keeps self-links),
    so F2/F3/F5/G2 already kill that half;
  * ONE-WAY reconcile match -- diverges but is INVISIBLE composed, because
    ``bcf`` dropped the row before this clause ran;
  * CROSS-ORG -- diverges in principle; fenced directly in F1, unreachable in
    practice.
So F1's unique contribution is the ONE-WAY cell (and nominally cross-org). That
is still `reference_downstream_guard_masks_the_mutant` -- just for one cell, not
for the whole clause. ⚠ Do not delete F1 as redundant, and do not delete the
``!= id`` conjunct believing only F1 covers it.

⚠ And the honest statement of what the clause buys: on REACHABLE data it adds
nothing over `linked_transaction_id IS NOT NULL`. It diverges only on
self-links (which `balance_contribution_filter` deliberately KEEPS) and
cross-org links, and no writer produces either. Its justification is the
standing rule -- anything meaning "these two rows are ONE transfer pair" must
test mutuality, never non-nullness -- because the next caller will not have
`balance_contribution_filter()` conjoined.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.transaction import (
    Transaction,
    TransactionStatus,
    TransactionType,
)
from app.reports import sources as registry
from app.schemas.reports_enums import Aggregation, Dataset, Dimension, MeasureField
from app.schemas.reports_query import (
    Filter,
    FilterField,
    FilterOp,
    Measure,
    ReportsQuery,
)
from app.services import reports_query_service
from app.services.transaction_filters import reciprocal_transfer_filter

P_START = datetime.date(2026, 6, 1)
P_END = datetime.date(2026, 6, 30)
D = datetime.date(2026, 6, 5)

IN_AMT = Decimal("500")    # checking -> savings, lands on savings as INCOME
OUT_AMT = Decimal("200")   # savings -> checking, lands on savings as EXPENSE
GROSS_TRAFFIC = IN_AMT + OUT_AMT          # 700 -- the indefensible number
PLAIN_EXPENSE = Decimal("33")             # an ordinary non-transfer row
# ⚠ TWO savings accounts share one type -- and NOT for the reason it is tempting
# to write down. A missing ``AccountType`` join multiplies by the number of
# account_type ROWS (2 here), not by accounts-per-type, so the cross-join mutant
# is caught with one account per type too. What the second Savings account
# actually buys is that "Savings" is a MULTI-ACCOUNT bucket, which is the only
# thing that distinguishes "group by AccountType.name" from "group by
# Account.name and label the column account_type" -- with one account per type
# those two mutants are indistinguishable.
IN_AMT_2 = Decimal("11")
# ⚠ G1 claims the default path excludes manual adjustments; without a manual
# row that clause of its docstring was covering nothing. It also gives G2 and
# F7 a second discriminating cell, because ``balance_contribution_filter``
# (unlike ``reportable_transaction_filter``) does NOT exclude manual rows.
MANUAL_ADJ = Decimal("5")


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine_and_factory() -> AsyncIterator[tuple]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield engine, factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db(engine_and_factory) -> AsyncIterator[AsyncSession]:
    _engine, factory = engine_and_factory
    async with factory() as session:
        yield session


async def _org(db, name: str = "Transfer Org") -> dict:
    org = Organization(name=name, billing_cycle_day=1, primary_currency="EUR")
    db.add(org)
    await db.flush()
    # ⚠ No slug on either type. The account_type DIMENSION groups by
    # ``AccountType.name``; nothing here may depend on ``slug``, which
    # ``routers/account_types.py`` never assigns for user-created types.
    checking = AccountType(org_id=org.id, name="Checking", slug=None)
    savings = AccountType(org_id=org.id, name="Savings", slug=None)
    db.add_all([checking, savings])
    await db.flush()
    cat = Category(org_id=org.id, name="Transfer", slug="transfer",
                   type=CategoryType.EXPENSE)
    db.add(cat)
    db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
    await db.flush()
    return {"org": org, "checking_t": checking, "savings_t": savings, "cat": cat}


async def _acct(db, o, name: str, type_key: str) -> Account:
    a = Account(org_id=o["org"].id, account_type_id=o[type_key].id, name=name,
                balance=Decimal("0"), currency="EUR")
    db.add(a)
    await db.flush()
    return a


async def _txn(db, o, acct, amount: Decimal, ttype: TransactionType, *,
               manual: bool = False) -> Transaction:
    t = Transaction(
        org_id=o["org"].id, account_id=acct.id, category_id=o["cat"].id,
        description="row", amount=amount, type=ttype,
        status=TransactionStatus.SETTLED, date=D, settled_date=D,
        is_manual_adjustment=manual,
    )
    db.add(t)
    await db.flush()
    return t


async def _pair(db, o, src, dst, amount: Decimal) -> tuple[Transaction, Transaction]:
    """A REAL transfer: two legs, typed by direction, linked BOTH ways."""
    out = await _txn(db, o, src, amount, TransactionType.EXPENSE)
    inn = await _txn(db, o, dst, amount, TransactionType.INCOME)
    out.linked_transaction_id = inn.id
    inn.linked_transaction_id = out.id
    await db.flush()
    return out, inn


@pytest_asyncio.fixture
async def world(db) -> dict:
    """One org: two savings accounts of one type, a checking account, two real
    transfer pairs, a plain expense, a ONE-WAY matched row and a SELF-LINKED row.

    The last two exist so F1 can prove the clause tests mutuality rather than
    non-nullness. They are unreachable through the product -- no writer creates
    a self-link, and a one-way link is a reconcile match -- which is exactly why
    the clause must refuse them structurally rather than by luck.
    """
    o = await _org(db)
    checking = await _acct(db, o, "Checking", "checking_t")
    sav1 = await _acct(db, o, "Savings One", "savings_t")
    sav2 = await _acct(db, o, "Savings Two", "savings_t")

    await _pair(db, o, checking, sav1, IN_AMT)     # 500 into savings
    await _pair(db, o, sav1, checking, OUT_AMT)    # 200 out of savings
    await _pair(db, o, checking, sav2, IN_AMT_2)   # 11 into the OTHER savings

    plain = await _txn(db, o, sav1, PLAIN_EXPENSE, TransactionType.EXPENSE)
    await _txn(db, o, sav1, MANUAL_ADJ, TransactionType.EXPENSE, manual=True)

    # ONE-WAY link: a reconcile match. The canonical row does NOT link back.
    one_way = await _txn(db, o, sav1, Decimal("77"), TransactionType.EXPENSE)
    one_way.linked_transaction_id = plain.id

    # SELF-LINK: corrupt data containing exactly one row.
    selfie = await _txn(db, o, sav1, Decimal("88"), TransactionType.EXPENSE)
    selfie.linked_transaction_id = selfie.id

    # CROSS-ORG link, so F1's ``org_id`` conjunct is exercised rather than
    # merely asserted in a docstring. Unreachable through the product -- which
    # is exactly why nothing else would ever catch its removal.
    # ⚠ Deliberately on CHECKING, not on sav1: it is reciprocal-but-foreign, so
    # ``balance_contribution_filter`` (which does not check org) admits it while
    # this clause refuses it. On sav1 it would inflate every account-scoped
    # figure in the file for a reason unrelated to what those tests measure.
    other = await _org(db, "Other Org")
    other_acct = await _acct(db, other, "Other Checking", "checking_t")
    foreign = await _txn(db, other, other_acct, Decimal("999"),
                         TransactionType.EXPENSE)
    cross = await _txn(db, o, checking, Decimal("999"), TransactionType.EXPENSE)
    cross.linked_transaction_id = foreign.id
    foreign.linked_transaction_id = cross.id

    await db.commit()
    return {**o, "checking": checking, "sav1": sav1, "sav2": sav2,
            "plain": plain, "one_way": one_way, "selfie": selfie,
            "cross": cross, "other_org": other["org"]}


def _q(dimensions=None, filters=None, limit: int = 100,
       include_non_reportable: bool = False) -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=dimensions or [],
        filters=filters or [],
        limit=limit,
        include_non_reportable=include_non_reportable,
    )


def _transfer(value: bool = True) -> Filter:
    return Filter(field=FilterField.TRANSFER, op=FilterOp.EQ, value=value)


# ── F1: the DIRECT mutuality fence — uncomposed, the only form that can fail ──


async def test_f1_reciprocal_clause_tests_mutuality_not_non_nullness(db, world):
    """F1. Kills ``reciprocal_transfer_filter()`` -> ``linked_transaction_id IS NOT NULL``.

    ⚠⚠ COMPILED ALONE, ON PURPOSE. Through the reports path this mutant is
    MASKED: ``transfer=true`` only runs with ``balance_contribution_filter()``
    conjoined, and that clause already drops non-reciprocal links, so
    ``(link NOT NULL) AND (link IS NULL OR reciprocal)`` reduces to
    ``(link NOT NULL) AND reciprocal`` -- the mutant and the correct clause are
    the same statement. An end-to-end assertion here is a manufactured green.

    The one-way row and the self-link are unreachable through the product.
    They are the ONLY cells where this clause differs from non-nullness, which
    is the whole reason it is written the way it is.
    """
    rows = (
        await db.execute(
            select(Transaction.id).where(
                Transaction.org_id == world["org"].id,
                reciprocal_transfer_filter(),
            )
        )
    ).scalars().all()
    got = set(rows)

    assert world["one_way"].id not in got, (
        "a ONE-WAY reconcile-matched row was counted as a transfer leg -- the "
        "clause is testing non-nullness, not mutuality"
    )
    assert world["selfie"].id not in got, (
        "a SELF-LINKED row was counted as a transfer leg; the != id conjunct is "
        "missing. ⚠ balance_contribution_filter deliberately KEEPS self-links, "
        "so borrowing its EXISTS verbatim ships exactly this mutant."
    )
    assert world["plain"].id not in got, "an unlinked row is not a transfer leg"
    assert world["cross"].id not in got, (
        "a CROSS-ORG reciprocal link was counted as a transfer leg; the "
        "org_id conjunct is missing. Nothing else in the suite covers it -- "
        "no writer can produce this row, which is why it must be built by hand."
    )
    # Six real legs: three pairs, all inside this org.
    assert len(got) == 6, f"expected the 6 paired legs, got {len(got)}"


# ── F2: the base clause stands down ─────────────────────────────────────────


async def test_f2_transfer_filter_stands_the_reportability_default_down(db, world):
    """F2. Kills "add the filter, leave the base clause alone".

    The default base is ``reportable_transaction_filter()``, whose first term is
    ``linked_transaction_id IS NULL``. Conjoined with a transfer filter that
    demands a link, it is UNSATISFIABLE -- zero rows, no error, no warning,
    which is the same silent-empty defect the dead Transfer checkbox already
    ships. The caller did not pass ``include_non_reportable``; asking for
    transfers IS the request.
    """
    rows, _meta = await reports_query_service.execute_query(
        db, _q(filters=[_transfer(True)]), org_id=world["org"].id
    )
    # ⚠ NOT ``assert rows``: with no dimensions the measure is
    # ``coalesce(sum(...), 0)``, so an unsatisfiable WHERE returns
    # ``[{"value": 0.0}]``, never ``[]``. The VALUE is the assertion.
    # ⚠ BOTH legs of every pair. A transfer writes two rows, so the org-wide
    # total is twice the transferred value -- which is precisely why a figure
    # like this needs a direction term (F3) before it means anything.
    assert Decimal(str(rows[0]["value"])) == (IN_AMT + OUT_AMT + IN_AMT_2) * 2, (
        "expected every transfer leg in the org, both sides of each pair"
    )


# ── F3: direction composes — the €700 question ──────────────────────────────


async def test_f3_transfer_composes_with_txn_type_for_gross_in(db, world):
    """F3. Kills transfer-as-a-fourth-`txn_type`-value, and any design with no
    direction axis.

    ⚠ The assertion that matters is the PAIR of numbers, not either alone. A
    design that cannot express "transfer AND inbound" returns 700 for both, and
    700 is neither defensible answer. Asserting only the 500 would also pass
    against an implementation that silently dropped the outbound leg.
    """
    def _for(extra):
        return _q(filters=[_transfer(True),
                           Filter(field=FilterField.ACCOUNT_ID, op=FilterOp.EQ,
                                  value=world["sav1"].id)] + extra)

    gross, _ = await reports_query_service.execute_query(
        db, _for([]), org_id=world["org"].id)
    inbound, _ = await reports_query_service.execute_query(
        db, _for([Filter(field=FilterField.TXN_TYPE, op=FilterOp.IN,
                         value=["income"])]), org_id=world["org"].id)

    assert Decimal(str(gross[0]["value"])) == GROSS_TRAFFIC, (
        "unfiltered transfer traffic on the savings account should be the gross "
        "sum of both legs"
    )
    assert Decimal(str(inbound[0]["value"])) == IN_AMT, (
        f"transfer+income should be gross-IN ({IN_AMT}), not the gross traffic "
        f"({GROSS_TRAFFIC}) -- direction is inexpressible"
    )


# ── F4: the false branch ────────────────────────────────────────────────────


async def test_f4_transfer_false_excludes_transfer_legs(db, world):
    """F4. Kills "drop the ``~``", NOT "only implement the true branch".

    ⚠ READ THIS BEFORE CITING F4 AS COVERAGE OF THE FALSE BRANCH. Both
    reviewers caught the original docstring overclaiming here. With
    ``include_non_reportable`` defaulted off, ``_reportability_base`` returns
    ``reportable_transaction_filter()``, whose first term is
    ``linked_transaction_id IS NULL`` -- so every surviving row is already
    unlinked and ``~reciprocal_transfer_filter()`` is trivially true for all of
    them. Deleting the ``else`` arm entirely leaves this test green.

    What it DOES kill is replacing ``~clause`` with ``clause`` (the filter then
    contradicts the base and the figure collapses). F7 covers the cell where
    the negation is actually load-bearing.
    """
    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(filters=[_transfer(False),
                    Filter(field=FilterField.ACCOUNT_ID, op=FilterOp.EQ,
                           value=world["sav1"].id)]),
        org_id=world["org"].id,
    )
    assert Decimal(str(rows[0]["value"])) == PLAIN_EXPENSE, (
        "transfer=false should leave only the ordinary row on this account"
    )


# ── F7: the cell where the NEGATION is load-bearing ─────────────────────────


async def test_f7_transfer_false_with_non_reportable_actually_subtracts(db, world):
    """F7. Kills "only implement the true branch" -- the mutant F4 cannot see.

    This is the ONE configuration where ``~clause`` removes anything. With
    ``include_non_reportable=True`` the base is
    ``balance_contribution_filter()``, which ADMITS linked rows, so the real
    transfer legs are present and the negation has to cut them out. Drop the
    ``else`` arm and this returns the full 826 instead of 126.

    The expected figure is G2's 826 minus the two reciprocal legs:
      plain 33 + self-link 88 + manual adjustment 5 = 126.

    ⚠ The self-link survives, deliberately. ``reciprocal_transfer_filter``
    fails CLOSED, so a self-linked row is NOT a transfer -- and "not a
    transfer" is exactly what this query asked for. That is a consequence of
    the declared polarity, not an accident, and it is the only assertion that
    pins it.
    """
    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(filters=[_transfer(False),
                    Filter(field=FilterField.ACCOUNT_ID, op=FilterOp.EQ,
                           value=world["sav1"].id)],
           include_non_reportable=True),
        org_id=world["org"].id,
    )
    assert Decimal(str(rows[0]["value"])) == (
        PLAIN_EXPENSE + Decimal("88") + MANUAL_ADJ
    ), "the ~clause did not remove the real transfer legs"


# ── F5: the account_type dimension and its join ─────────────────────────────


async def test_f5_account_type_dimension_partitions_without_cross_joining(db, world):
    """F5. Kills a missing or duplicated ``AccountType`` join.

    Referencing ``AccountType.name`` without a join renders a CROSS JOIN, which
    does not raise -- it multiplies by the account_type row count, so each
    bucket reads the org-wide total instead of its own.

    ⚠ The second assertion breaks the symmetry deliberately. Every pair has one
    Checking leg and one Savings leg of equal size, so with the transfer filter
    on, both buckets sum to the SAME number -- which cannot distinguish "each
    bucket got its own rows" from "both buckets got everything". Dropping the
    filter gives Savings the plain/manual/self-link rows that Checking does not
    have, and that asymmetry is what proves attribution.
    """
    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(dimensions=[Dimension.ACCOUNT_TYPE], filters=[_transfer(True)]),
        org_id=world["org"].id,
    )
    by_type = {r["account_type"]: Decimal(str(r["value"])) for r in rows}
    assert by_type == {
        "Savings": IN_AMT + OUT_AMT + IN_AMT_2,
        "Checking": IN_AMT + OUT_AMT + IN_AMT_2,
    }, f"expected one row per account type with its own legs; got {by_type}"

    # Asymmetric control: no transfer filter, so the default base leaves only
    # the ordinary rows -- which live on Savings alone.
    rows2, _ = await reports_query_service.execute_query(
        db, _q(dimensions=[Dimension.ACCOUNT_TYPE]), org_id=world["org"].id
    )
    by_type2 = {r["account_type"]: Decimal(str(r["value"])) for r in rows2}
    assert by_type2 == {"Savings": PLAIN_EXPENSE}, (
        f"expected the ordinary rows attributed to Savings only; got {by_type2}"
    )


async def test_f5b_account_type_dimension_composes_with_account_dimension(db, world):
    """F5b. Kills "join AccountType in its own `if`, so two dimensions double-join".

    ``MAX_DIMENSIONS`` is 2 and both are published, so this is an ordinary pick.
    Joining ``Account`` twice raises at compile time; every other test here asks
    for at most one of the two.
    """
    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(dimensions=[Dimension.ACCOUNT, Dimension.ACCOUNT_TYPE],
           filters=[_transfer(True)]),
        org_id=world["org"].id,
    )
    pairs = {(r["account"], r["account_type"]) for r in rows}
    assert ("Savings One", "Savings") in pairs
    assert ("Checking", "Checking") in pairs


# ── F6: the catalog publishes what the compiler handles ─────────────────────


async def test_f6_catalog_publishes_the_transfer_filter_and_account_type_dim():
    """F6. Kills "wire the compiler, forget the catalog".

    Without the catalog rows ``validate_against_catalog`` rejects the AST before
    it reaches the compiler, and the editor never offers the dimension.

    ⚠ The account_type FILTER is deliberately NOT published -- it would be the
    second consecutive backend-only filter with no control, after TBD-507's
    currency. The DIMENSION is reachable with zero frontend code because the
    picker is catalog-driven. Asserting its absence keeps that ruling honest.
    """
    src = registry.get_source("transactions")
    assert "transfer" in {f.field for f in src.filters()}
    assert "account_type" in {d.key for d in src.dimensions()}
    assert "account_type" not in {f.field for f in src.filters()}, (
        "the account_type FILTER was published; it was cut deliberately"
    )
    src.validate(_q(dimensions=[Dimension.ACCOUNT_TYPE], filters=[_transfer(True)]))


# ── F8: the op guard ────────────────────────────────────────────────────────


async def test_f8_transfer_rejects_an_op_the_catalog_does_not_publish(db, world):
    """F8. Kills "treat any op as eq" in the TRANSFER branch.

    Unreachable through the router today -- ``validate_against_catalog``
    enforces the ``("eq",)`` tuple and 422s anything else. The guard exists for
    the day that tuple is widened, and the failure it prevents is this ticket's
    own defect class rather than a merely wrong answer: with ``op=in`` the value
    becomes a LIST, which is truthy (so the clause applies) but is not ``True``
    (so ``_asks_for_transfers`` returns False and the base does NOT stand down)
    -- giving an unsatisfiable conjunction and a silent empty chart.

    ⚠ Driven at the COMPILER, bypassing the catalog on purpose: going through
    ``source.validate()`` would be stopped one layer earlier and would fence the
    catalog rather than this branch.
    """
    import pytest

    ast = _q(filters=[Filter(field=FilterField.TRANSFER, op=FilterOp.IN,
                             value=[True])])
    with pytest.raises(ValueError, match="unsupported op"):
        reports_query_service.compile_ast_to_query(ast, org_id=world["org"].id)


# ── G1 / G2: OVER-REACH GUARDS ──────────────────────────────────────────────


async def test_g1_a_query_that_says_nothing_about_transfers_is_unchanged(db, world):
    """OVER-REACH GUARD. Kills "stand the base down unconditionally".

    The default must still exclude transfer legs and manual adjustments -- both
    are in the fixture and both are excluded here. ⚠ It does NOT cover the
    third term (reverted reconciliation rows): no skipped/rejected row is built,
    so that clause of ``reportable_transaction_filter`` is untouched by this
    test. It is covered elsewhere (TBD-470); do not read G1 as proving it.

    ⚠ Read the scope of the claim: against THIS fixture it is a genuine fence
    (an unconditional stand-down returns the transfer legs and the number
    changes). It is not coverage of the transfer axis itself.
    """
    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(filters=[Filter(field=FilterField.ACCOUNT_ID, op=FilterOp.EQ,
                           value=world["sav1"].id)]),
        org_id=world["org"].id,
    )
    # ⚠ ONLY the unlinked row. ``reportable_transaction_filter`` excludes every
    # row with a non-NULL ``linked_transaction_id`` -- it does not ask about
    # mutuality -- so the one-way matched row and the self-link are excluded
    # here too, for a different reason than F1 excludes them.
    assert Decimal(str(rows[0]["value"])) == PLAIN_EXPENSE, (
        "the default path stopped excluding linked rows or manual adjustments"
    )


async def test_g2_include_non_reportable_still_works_on_its_own(db, world):
    """OVER-REACH GUARD. Kills "make the base depend ONLY on the transfer filter".

    ``include_non_reportable`` is an independent request and must keep working
    with no transfer filter present -- the two inputs pick a base clause
    together, and a refactor that reads only one of them breaks the shipped
    toggle.
    """
    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(filters=[Filter(field=FilterField.ACCOUNT_ID, op=FilterOp.EQ,
                           value=world["sav1"].id)],
           include_non_reportable=True),
        org_id=world["org"].id,
    )
    # transfer legs (500 + 200) + plain 33 + self-link 88 + manual 5.
    # ⚠ The one-way 77 is dropped by balance_contribution_filter (TBD-470), and
    # the manual adjustment IS included -- ``balance_contribution_filter`` does
    # not exclude manual rows, only ``reportable_transaction_filter`` does.
    # That difference is the whole point of the opt-in toggle's label.
    assert Decimal(str(rows[0]["value"])) == (
        IN_AMT + OUT_AMT + PLAIN_EXPENSE + Decimal("88") + MANUAL_ADJ
    )
