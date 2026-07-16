"""Service-layer tests for the Reports v2 AST compiler.

Covers:

- ``org_id`` is injected on every compiled query and the AST has no
  way to express it (compiler always appends the WHERE clause).
- Bound parameters reach the SQL — no string interpolation.
- Tag-filter semantics (``all`` vs ``any``) mirror the transactions
  list at ``backend/app/services/transaction_service.py:1697``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category
from app.models.tag import Tag, TransactionTag
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.schemas.reports_query import (
    Aggregation,
    Dataset,
    Dimension,
    Filter,
    FilterField,
    FilterOp,
    Measure,
    MeasureField,
    ReportsQuery,
    TagMatch,
)
from app.security import hash_password
from app.services.reports_query_service import (
    QUERY_TIMEOUT_MS,
    _apply_query_timeout,
    compile_ast_to_query,
    execute_query,
)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_world(factory) -> dict:
    """Seed two orgs (A + B) with categories, accounts, transactions,
    and tags so cross-org isolation can be asserted.
    """
    async with factory() as db:
        org_a = Organization(name="Org A", billing_cycle_day=1)
        org_b = Organization(name="Org B", billing_cycle_day=1)
        db.add_all([org_a, org_b])
        await db.commit()

        user_a = User(
            org_id=org_a.id,
            username="user_a",
            email="a@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
        )
        user_b = User(
            org_id=org_b.id,
            username="user_b",
            email="b@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
        )
        db.add_all([user_a, user_b])
        await db.commit()

        at_a = AccountType(org_id=org_a.id, name="Checking")
        at_b = AccountType(org_id=org_b.id, name="Checking")
        db.add_all([at_a, at_b])
        await db.commit()

        acct_a = Account(
            org_id=org_a.id,
            account_type_id=at_a.id,
            name="A Bank",
            currency="EUR",
            balance=Decimal("0"),
        )
        acct_b = Account(
            org_id=org_b.id,
            account_type_id=at_b.id,
            name="B Bank",
            currency="EUR",
            balance=Decimal("0"),
        )
        db.add_all([acct_a, acct_b])
        await db.commit()

        cat_food_a = Category(org_id=org_a.id, name="Food")
        cat_transport_a = Category(org_id=org_a.id, name="Transport")
        cat_food_b = Category(org_id=org_b.id, name="Food")
        db.add_all([cat_food_a, cat_transport_a, cat_food_b])
        await db.commit()

        today = date(2026, 5, 15)
        # Org A — three EXPENSE rows on Food, two on Transport.
        rows = []
        for amt in (Decimal("10"), Decimal("20"), Decimal("30")):
            rows.append(
                Transaction(
                    org_id=org_a.id,
                    account_id=acct_a.id,
                    category_id=cat_food_a.id,
                    description="food",
                    amount=amt,
                    type=TransactionType.EXPENSE,
                    status=TransactionStatus.SETTLED,
                    date=today,
                    settled_date=today,
                )
            )
        for amt in (Decimal("5"), Decimal("15")):
            rows.append(
                Transaction(
                    org_id=org_a.id,
                    account_id=acct_a.id,
                    category_id=cat_transport_a.id,
                    description="transport",
                    amount=amt,
                    type=TransactionType.EXPENSE,
                    status=TransactionStatus.SETTLED,
                    date=today,
                    settled_date=today,
                )
            )
        # Org B — one EXPENSE row on Food, much higher amount. If org
        # isolation leaks, this is the row that shows up in Org A's
        # totals.
        rows.append(
            Transaction(
                org_id=org_b.id,
                account_id=acct_b.id,
                category_id=cat_food_b.id,
                description="food-other-org",
                amount=Decimal("9999"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED,
                date=today,
                settled_date=today,
            )
        )
        db.add_all(rows)
        await db.commit()

        # Tag world for Org A: two named tags. Two of the food rows
        # carry both tags; one carries only the first.
        tag_essentials = Tag(
            org_id=org_a.id,
            name="essentials",
            name_normalized="essentials",
        )
        tag_treat = Tag(
            org_id=org_a.id,
            name="treat",
            name_normalized="treat",
        )
        db.add_all([tag_essentials, tag_treat])
        await db.commit()

        # Re-fetch the food rows in order to attach tags.
        from sqlalchemy import select as _select
        food_rows = (
            await db.execute(
                _select(Transaction)
                .where(Transaction.org_id == org_a.id)
                .where(Transaction.category_id == cat_food_a.id)
                .order_by(Transaction.amount.asc())
            )
        ).scalars().all()
        # Row 10: both tags. Row 20: both tags. Row 30: only essentials.
        db.add_all([
            TransactionTag(transaction_id=food_rows[0].id, tag_id=tag_essentials.id),
            TransactionTag(transaction_id=food_rows[0].id, tag_id=tag_treat.id),
            TransactionTag(transaction_id=food_rows[1].id, tag_id=tag_essentials.id),
            TransactionTag(transaction_id=food_rows[1].id, tag_id=tag_treat.id),
            TransactionTag(transaction_id=food_rows[2].id, tag_id=tag_essentials.id),
        ])
        await db.commit()

        return {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "tag_essentials": "essentials",
            "tag_treat": "treat",
        }


def _sum_by_category_ast() -> ReportsQuery:
    """SUM(amount) GROUP BY category — the canonical "spending breakdown" shape."""
    return ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
        filters=[],
        limit=100,
    )


async def _seed_reportability(factory) -> dict:
    """Seed one org with a mix of reportable and non-reportable rows on a
    single category, so a SUM(amount) proves the reportability filter.

    Rows (all EXPENSE on "Mixed"):
      - R1 reportable                    amount 100
      - T1/T2 transfer pair (both legs)  amount  40 each
      - ADJ manual balance adjustment    amount  25
      - REJ rejected reconciliation row  amount  15
      - SKP skipped reconciliation row   amount   7

    Default (exclude non-reportable)          -> 100
    include_non_reportable (transfers + adj)  -> 100 + 40 + 40 + 25 = 205
    Reverted rows (REJ + SKP) are excluded in BOTH cases.
    """
    async with factory() as db:
        org = Organization(name="Org R", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        at = AccountType(org_id=org.id, name="Checking")
        db.add(at)
        await db.commit()

        acct = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="R Bank",
            currency="EUR",
            balance=Decimal("0"),
        )
        cat = Category(org_id=org.id, name="Mixed")
        db.add_all([acct, cat])
        await db.commit()

        today = date(2026, 5, 15)

        def _row(amount, **kw):
            return Transaction(
                org_id=org.id,
                account_id=acct.id,
                category_id=cat.id,
                description="row",
                amount=amount,
                type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED,
                date=today,
                settled_date=today,
                **kw,
            )

        r1 = _row(Decimal("100"))
        t1 = _row(Decimal("40"))
        t2 = _row(Decimal("40"))
        adj = _row(Decimal("25"), is_manual_adjustment=True)
        rej = _row(Decimal("15"), reconciliation_state="rejected")
        skp = _row(Decimal("7"), reconciliation_state="skipped")
        db.add_all([r1, t1, t2, adj, rej, skp])
        await db.commit()

        # Wire the transfer pair's self-FKs now that both rows have ids.
        t1.linked_transaction_id = t2.id
        t2.linked_transaction_id = t1.id
        await db.commit()

        return {"org_id": org.id}


def _sum_total_ast(*, include_non_reportable: bool = False) -> ReportsQuery:
    """SUM(amount) with a single category dimension (one bucket here)."""
    return ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
        filters=[],
        limit=100,
        include_non_reportable=include_non_reportable,
    )


@pytest.mark.asyncio
async def test_reports_exclude_non_reportable_by_default(session_factory):
    """Default query drops transfer legs, manual adjustments, and reverted
    reconciliation rows — only the reportable row counts."""
    ids = await _seed_reportability(session_factory)
    async with session_factory() as db:
        rows, _meta = await execute_query(
            db, _sum_total_ast(include_non_reportable=False), org_id=ids["org_id"]
        )
    assert len(rows) == 1
    assert Decimal(str(rows[0]["value"])) == Decimal("100")


@pytest.mark.asyncio
async def test_include_non_reportable_re_includes_transfers_and_adjustments(
    session_factory,
):
    """The opt-in flag re-includes transfer legs + manual adjustments, but the
    reverted rows (rejected + skipped) stay excluded: 100 + 40 + 40 + 25 = 205,
    not 227 (which would add the 15 rejected + 7 skipped)."""
    ids = await _seed_reportability(session_factory)
    async with session_factory() as db:
        rows, _meta = await execute_query(
            db, _sum_total_ast(include_non_reportable=True), org_id=ids["org_id"]
        )
    assert len(rows) == 1
    assert Decimal(str(rows[0]["value"])) == Decimal("205")


# ─── injection + isolation ─────────────────────────────────────────


def test_compile_injects_org_id_into_where():
    """The compiled statement always carries ``transactions.org_id = :param``,
    even when the AST has zero filters. We check the rendered SQL contains
    the predicate and the bound param matches.
    """
    ast = _sum_by_category_ast()
    stmt = compile_ast_to_query(ast, org_id=42, dialect_name="sqlite")

    compiled = stmt.compile(compile_kwargs={"literal_binds": False})
    sql = str(compiled).lower()
    assert "transactions.org_id" in sql
    # Param value 42 is bound (no string interpolation).
    params = dict(compiled.params)
    assert 42 in params.values()


@pytest.mark.asyncio
async def test_cross_org_isolation(session_factory):
    seeds = await _seed_world(session_factory)
    ast = _sum_by_category_ast()
    async with session_factory() as db:
        rows, meta = await execute_query(db, ast, org_id=seeds["org_a_id"])
    # Org A totals: Food 60, Transport 20. Org B's 9999 must not appear.
    totals = {row["category"]: row["value"] for row in rows}
    assert totals == {"Food": 60.0, "Transport": 20.0}
    assert 9999.0 not in totals.values()


@pytest.mark.asyncio
async def test_other_org_is_independent(session_factory):
    seeds = await _seed_world(session_factory)
    ast = _sum_by_category_ast()
    async with session_factory() as db:
        rows, _ = await execute_query(db, ast, org_id=seeds["org_b_id"])
    totals = {row["category"]: row["value"] for row in rows}
    assert totals == {"Food": 9999.0}


# ─── tag filter semantics ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_tag_filter_all_requires_every_tag(session_factory):
    """``tag_match=all``: only rows carrying BOTH tags. The 30-EUR food
    row (essentials only) must be excluded.
    """
    seeds = await _seed_world(session_factory)
    ast = ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
        filters=[
            Filter(
                field=FilterField.TAG_NAME,
                op=FilterOp.IN,
                value=["essentials", "treat"],
                tag_match=TagMatch.ALL,
            )
        ],
        limit=100,
    )
    async with session_factory() as db:
        rows, _ = await execute_query(db, ast, org_id=seeds["org_a_id"])
    totals = {row["category"]: row["value"] for row in rows}
    # Only the 10 + 20 food rows carry BOTH tags.
    assert totals == {"Food": 30.0}


@pytest.mark.asyncio
async def test_tag_filter_any_includes_either_tag(session_factory):
    """``tag_match=any``: rows carrying AT LEAST ONE tag. The 30-EUR
    food row (essentials only) must be included.
    """
    seeds = await _seed_world(session_factory)
    ast = ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
        filters=[
            Filter(
                field=FilterField.TAG_NAME,
                op=FilterOp.IN,
                value=["essentials", "treat"],
                tag_match=TagMatch.ANY,
            )
        ],
        limit=100,
    )
    async with session_factory() as db:
        rows, _ = await execute_query(db, ast, org_id=seeds["org_a_id"])
    totals = {row["category"]: row["value"] for row in rows}
    assert totals == {"Food": 60.0}


# ─── aggregation shape ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_count_aggregation(session_factory):
    seeds = await _seed_world(session_factory)
    ast = ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.COUNT, field=MeasureField.ID),
        dimensions=[Dimension.CATEGORY],
        limit=100,
    )
    async with session_factory() as db:
        rows, _ = await execute_query(db, ast, org_id=seeds["org_a_id"])
    totals = {row["category"]: row["value"] for row in rows}
    # Org A: 3 food rows + 2 transport rows.
    assert totals == {"Food": 3, "Transport": 2}


@pytest.mark.asyncio
async def test_date_between_filter(session_factory):
    """BETWEEN binds two date params and excludes rows outside the window."""
    seeds = await _seed_world(session_factory)
    # All rows in the fixture fall on 2026-05-15. A window of the
    # previous week excludes them all.
    ast = ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
        filters=[
            Filter(
                field=FilterField.DATE,
                op=FilterOp.BETWEEN,
                value=[date(2026, 5, 1), date(2026, 5, 8)],
            )
        ],
        limit=100,
    )
    async with session_factory() as db:
        rows, _ = await execute_query(db, ast, org_id=seeds["org_a_id"])
    assert rows == []


# ─── per-statement query timeout (spec §6 "Hard caps") ─────────────


def test_mysql_compile_includes_max_execution_time_hint():
    """When compiled for MySQL, the SELECT carries the
    ``MAX_EXECUTION_TIME(5000)`` optimizer hint so the server aborts
    runaway queries after 5 s without poisoning the connection.

    The hint lives in a ``/*+ ... */`` comment that lands right after
    the ``SELECT`` keyword (via SQLAlchemy ``prefix_with``).
    """
    from sqlalchemy.dialects import mysql

    ast = _sum_by_category_ast()
    stmt = compile_ast_to_query(ast, org_id=1, dialect_name="mysql")
    stmt = _apply_query_timeout(stmt, "mysql")

    compiled = stmt.compile(dialect=mysql.dialect())
    sql = str(compiled)
    assert f"MAX_EXECUTION_TIME({QUERY_TIMEOUT_MS})" in sql
    # Hint must live in the optimizer-hint comment so MySQL parses it.
    assert "/*+" in sql and "*/" in sql


def test_sqlite_compile_skips_timeout_hint():
    """On SQLite (the pytest backend) the hint is omitted — SQLite
    doesn't understand MySQL optimizer hints and would either ignore or
    error on the comment. The compiled SQL must not contain the hint.
    """
    ast = _sum_by_category_ast()
    stmt = compile_ast_to_query(ast, org_id=1, dialect_name="sqlite")
    stmt = _apply_query_timeout(stmt, "sqlite")

    compiled = stmt.compile(compile_kwargs={"literal_binds": False})
    assert "MAX_EXECUTION_TIME" not in str(compiled)


# ─── effective settled-date bucketing (cash-basis) ─────────────────


async def _seed_gblt(factory) -> dict:
    """Seed a single org with one GBLT-style transaction: dated in May,
    settled in June. Cash-basis bucketing must count it in June.
    """
    async with factory() as db:
        org = Organization(name="GBLT Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        at = AccountType(org_id=org.id, name="Checking")
        db.add(at)
        await db.commit()

        acct = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="Bank",
            currency="EUR",
            balance=Decimal("0"),
        )
        cat = Category(org_id=org.id, name="Food")
        db.add_all([acct, cat])
        await db.commit()

        tx = Transaction(
            org_id=org.id,
            account_id=acct.id,
            category_id=cat.id,
            description="GBLT",
            amount=Decimal("459.68"),
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=date(2026, 5, 31),
            settled_date=date(2026, 6, 15),
        )
        db.add(tx)
        await db.commit()
        return {"org_id": org.id}


@pytest.mark.asyncio
async def test_month_bucketing_uses_settled_date(session_factory):
    """A GBLT (dated 2026-05-31, settled 2026-06-15) groups by its
    SETTLED month — June, not May.
    """
    seeds = await _seed_gblt(session_factory)
    ast = ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.MONTH],
        limit=100,
    )
    async with session_factory() as db:
        rows, _ = await execute_query(db, ast, org_id=seeds["org_id"])
    by_month = {r["month"]: r["value"] for r in rows}
    assert "2026-06" in by_month and "2026-05" not in by_month


@pytest.mark.asyncio
async def test_date_filter_uses_settled_date(session_factory):
    """A DATE BETWEEN June filter INCLUDES the GBLT by its June settled
    date, even though its raw transaction date is in May.
    """
    seeds = await _seed_gblt(session_factory)
    ast = ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        filters=[
            Filter(
                field=FilterField.DATE,
                op=FilterOp.BETWEEN,
                value=[date(2026, 6, 1), date(2026, 6, 30)],
            )
        ],
        limit=100,
    )
    async with session_factory() as db:
        rows, _ = await execute_query(db, ast, org_id=seeds["org_id"])
    # No dimensions → a single aggregate row. SUM is the GBLT amount only
    # when the June filter includes it via its settled date (else 0/None).
    assert rows[0]["value"] == 459.68
