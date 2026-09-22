"""TBD-553 -- the five starter-template "Net" widgets get a signed measure.

Existence checks (pinning ``config.measure``/``config.measures`` JSON) are
NOT behaviour: they also pass if ``TransactionsSource.validate()`` rejects
the new field, or if ``schemas/report_layout.py`` fails to accept it, or if
the compiler still sums the wrong column. The companion in this file builds
each widget's config into a REAL AST, runs ``TransactionsSource.validate()``
AND ``execute_query`` against an in-memory fixture, and asserts
income - expense.

⚠ FIXED DATES ONLY. ``get_report_templates()`` computes
``canvas_filters_json.date_range`` from ``date.today()`` at call time, so this
file never reads that key -- every AST built here carries no date filter (org
scoping alone is enough against a private in-memory fixture) or, where a
chronological order matters, a set of hand-picked fixed dates.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.reports import sources as registry
from app.reports.templates import get_report_templates
from app.schemas.report_layout import validate_layout_json
from app.schemas.reports_enums import Aggregation, Dataset, Dimension, MeasureField
from app.schemas.reports_query import (
    Filter,
    FilterField,
    FilterOp,
    Measure,
    ReportsQuery,
    SortDir,
    SortSpec,
)
from app.services import reports_query_service

SRC = registry.get_source("transactions")

P_START = datetime.date(2026, 1, 1)
P_END = datetime.date(2026, 3, 31)

# Fixed fixture, three months, each with a different (and non-trivially
# signed) net so a chronological-order check has something to distinguish.
JAN_INCOME, JAN_EXPENSE = Decimal("1000"), Decimal("400")   # net  600
FEB_INCOME, FEB_EXPENSE = Decimal("500"), Decimal("900")    # net -400
MAR_INCOME, MAR_EXPENSE = Decimal("300"), Decimal("100")    # net  200
TOTAL_NET = (JAN_INCOME - JAN_EXPENSE) + (FEB_INCOME - FEB_EXPENSE) + (MAR_INCOME - MAR_EXPENSE)  # 400

WIDGET_IDS = {
    "monthly_review": ["mr-kpi-net", "mr-line-net-trend"],
    "cash_flow_trend": ["cft-kpi-avg-net", "cft-line-net-by-month"],
    "settled_vs_pending": ["svp-kpi-net"],
}


# ── fixtures ─────────────────────────────────────────────────────────────


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


async def _txn(db, org, acct, cat, amount, ttype, d):
    db.add(Transaction(
        org_id=org.id, account_id=acct.id, category_id=cat.id,
        description="row", amount=amount, type=ttype,
        status=TransactionStatus.SETTLED, date=d, settled_date=d,
    ))


@pytest_asyncio.fixture
async def world(db) -> dict:
    org = Organization(name="Template Org", billing_cycle_day=1, primary_currency="EUR")
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug=None)
    db.add(at)
    await db.flush()
    cat = Category(org_id=org.id, name="General", slug="general", type=CategoryType.EXPENSE)
    db.add(cat)
    db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
    await db.flush()
    acct = Account(org_id=org.id, account_type_id=at.id, name="Checking",
                    balance=Decimal("0"), currency="EUR")
    db.add(acct)
    await db.flush()

    await _txn(db, org, acct, cat, JAN_INCOME, TransactionType.INCOME, datetime.date(2026, 1, 10))
    await _txn(db, org, acct, cat, JAN_EXPENSE, TransactionType.EXPENSE, datetime.date(2026, 1, 15))
    await _txn(db, org, acct, cat, FEB_INCOME, TransactionType.INCOME, datetime.date(2026, 2, 10))
    await _txn(db, org, acct, cat, FEB_EXPENSE, TransactionType.EXPENSE, datetime.date(2026, 2, 15))
    await _txn(db, org, acct, cat, MAR_INCOME, TransactionType.INCOME, datetime.date(2026, 3, 10))
    await _txn(db, org, acct, cat, MAR_EXPENSE, TransactionType.EXPENSE, datetime.date(2026, 3, 15))
    await db.commit()
    return {"org": org}


# ── helpers ──────────────────────────────────────────────────────────────


def _template(key: str) -> dict:
    for tpl in get_report_templates():
        if tpl["key"] == key:
            return tpl
    raise AssertionError(f"template {key!r} not found")


def _widget(key: str, widget_id: str) -> dict:
    for widget in _template(key)["layout_json"]["widgets"]:
        if widget["id"] == widget_id:
            return widget
    raise AssertionError(f"widget {widget_id!r} not found in template {key!r}")


def _measure_from_config(config: dict) -> Measure:
    if "measure" in config:
        m = config["measure"]
    else:
        m = config["measures"][0]["measure"]
    return Measure(agg=Aggregation(m["agg"]), field=MeasureField(m["field"]))


def _ast_from_widget(config: dict) -> ReportsQuery:
    sort = None
    if config.get("sort"):
        sort = SortSpec(by=config["sort"]["by"], dir=SortDir(config["sort"]["dir"]))
    return ReportsQuery(
        dataset=Dataset(config["dataset"]),
        measure=_measure_from_config(config),
        dimensions=[Dimension(d) for d in config.get("dimensions", [])],
        sort=sort,
        limit=100,
    )


# ── existence pins ───────────────────────────────────────────────────────


def test_every_starter_template_still_validates_against_layout_json() -> None:
    for tpl in get_report_templates():
        validate_layout_json(tpl["layout_json"])


def test_net_widgets_carry_sum_net_amount() -> None:
    for template_key, widget_ids in WIDGET_IDS.items():
        for widget_id in widget_ids:
            config = _widget(template_key, widget_id)["config"]
            measure = _measure_from_config(config)
            assert measure.agg is Aggregation.SUM
            assert measure.field is MeasureField.NET_AMOUNT, (
                f"{widget_id} still measures {measure.field.value!r}"
            )


def test_line_widgets_carry_dimension_sort() -> None:
    for widget_id in ("mr-line-net-trend", "cft-line-net-by-month"):
        template_key = "monthly_review" if widget_id.startswith("mr-") else "cash_flow_trend"
        config = _widget(template_key, widget_id)["config"]
        assert config.get("sort") == {"by": "dimension", "dir": "asc"}, widget_id


def test_cft_kpi_avg_net_retitled() -> None:
    widget = _widget("cash_flow_trend", "cft-kpi-avg-net")
    assert widget["title"] == "Net (12 mo)"


def test_no_template_description_claims_an_aggregation_it_does_not_compute() -> None:
    """A template DESCRIPTION is rendered to the user and is exactly as
    capable of lying as a tile title -- which is the whole of TBD-553.
    ``cash_flow_trend`` read "Average monthly net" while its KPI is a
    12-month TOTAL. The compiler has no divide, so avg-per-dimension is
    inexpressible, and ``avg`` is refused outright on net_amount. A
    description may therefore promise an average only if some widget in that
    same template really carries ``agg == "avg"``.
    """
    for tpl in get_report_templates():
        aggs = set()
        for widget in tpl["layout_json"]["widgets"]:
            config = widget["config"]
            measures = (
                [config["measure"]] if "measure" in config
                else [m["measure"] for m in config.get("measures", [])]
            )
            aggs.update(m["agg"] for m in measures)
        if "avg" in aggs:
            continue
        description = tpl["description"].lower()
        for claim in ("average", "avg", "mean"):
            assert claim not in description, (
                f"template {tpl['key']!r} description promises {claim!r} but no "
                f"widget in it aggregates with avg (aggs present: {sorted(aggs)})"
            )


# ── guard: templates_net_widgets_use_net_amount (behavioural companion) ──


async def test_kpi_net_widgets_compile_and_execute_to_income_minus_expense(db, world) -> None:
    for template_key, widget_ids in WIDGET_IDS.items():
        for widget_id in widget_ids:
            widget = _widget(template_key, widget_id)
            if widget["type"] != "kpi":
                continue
            ast = _ast_from_widget(widget["config"])
            SRC.validate(ast)  # would 422 a mis-keyed _DECLARED_AGG or a field
            # report_layout.py rejects.
            validate_layout_json(_template(template_key)["layout_json"])
            rows, _meta = await reports_query_service.execute_query(
                db, ast, org_id=world["org"].id,
            )
            assert len(rows) == 1
            assert Decimal(str(rows[0]["value"])) == TOTAL_NET, widget_id


# ── guard: time_series_templates_sort_by_dimension ───────────────────────


async def test_line_widgets_return_rows_chronologically(db, world) -> None:
    expected_months = ["2026-01", "2026-02", "2026-03"]
    expected_values = [
        JAN_INCOME - JAN_EXPENSE, FEB_INCOME - FEB_EXPENSE, MAR_INCOME - MAR_EXPENSE,
    ]
    config = _widget("cash_flow_trend", "cft-line-net-by-month")["config"]
    ast = _ast_from_widget(config)
    SRC.validate(ast)
    rows, _meta = await reports_query_service.execute_query(db, ast, org_id=world["org"].id)
    assert [r["month"] for r in rows] == expected_months
    assert [Decimal(str(r["value"])) for r in rows] == expected_values


async def test_daily_line_widget_returns_rows_chronologically(db, world) -> None:
    config = _widget("monthly_review", "mr-line-net-trend")["config"]
    ast = _ast_from_widget(config)
    SRC.validate(ast)
    rows, _meta = await reports_query_service.execute_query(db, ast, org_id=world["org"].id)
    days = [r["day"] for r in rows]
    assert days == sorted(days)
    assert len(days) == 6


# ── fence: expense_ranking_unchanged (mr-bar-category stays sum(amount)) ─


async def test_expense_ranking_unchanged(db) -> None:
    """Kills a stray ``mr-bar-category`` conversion to ``net_amount``.

    Two expense categories with UNEQUAL totals (a tie satisfies both
    orderings), ``limit 1``, ``sort value desc``, asserting the surviving
    row's DIMENSION KEY rather than the value's sign.

    ⚠ The AST below is built FROM THE WIDGET'S OWN CONFIG -- measure,
    dimensions, filters, sort and limit -- not hand-written. An earlier cut
    hardcoded ``MeasureField.AMOUNT`` and dropped ``config["filters"]``,
    which made the whole fixture vacuous: it re-asserted a constant the test
    itself supplied, so only the one-line JSON pin above was load-bearing.
    Wired to the config, the conversion really does flip the ranking --
    every row is an EXPENSE, so ``sum(net_amount)`` makes both totals
    NEGATIVE and ``value desc`` then ranks -5 (Coffee) above -1500 (Rent).
    """
    config = _widget("monthly_review", "mr-bar-category")["config"]
    measure = config["measure"]
    assert measure == {"agg": "sum", "field": "amount"}

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
    async with factory() as session:
        org = Organization(name="Ranking Org", billing_cycle_day=1, primary_currency="EUR")
        session.add(org)
        await session.flush()
        at = AccountType(org_id=org.id, name="Checking", slug=None)
        session.add(at)
        await session.flush()
        big = Category(org_id=org.id, name="Rent", slug="rent", type=CategoryType.EXPENSE)
        small = Category(org_id=org.id, name="Coffee", slug="coffee", type=CategoryType.EXPENSE)
        session.add_all([big, small])
        session.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
        await session.flush()
        acct = Account(org_id=org.id, account_type_id=at.id, name="Checking",
                        balance=Decimal("0"), currency="EUR")
        session.add(acct)
        await session.flush()
        d = datetime.date(2026, 1, 10)
        session.add(Transaction(org_id=org.id, account_id=acct.id, category_id=big.id,
                                 description="rent", amount=Decimal("1500"),
                                 type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                                 date=d, settled_date=d))
        session.add(Transaction(org_id=org.id, account_id=acct.id, category_id=small.id,
                                 description="coffee", amount=Decimal("5"),
                                 type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                                 date=d, settled_date=d))
        await session.commit()

        ast = ReportsQuery(
            dataset=Dataset(config["dataset"]),
            # From the widget's config, NOT hardcoded -- see the docstring.
            measure=_measure_from_config(config),
            dimensions=[Dimension(d) for d in config["dimensions"]],
            filters=[
                Filter(field=FilterField(k), op=FilterOp.EQ, value=v)
                for k, v in config["filters"].items()
            ],
            sort=SortSpec(
                by=config["sort"]["by"], dir=SortDir(config["sort"]["dir"])
            ),
            # limit 1, not the config's 10: two categories, so only a limit
            # that truncates can show WHICH one ranks first.
            limit=1,
        )
        SRC.validate(ast)
        rows, _meta = await reports_query_service.execute_query(db=session, ast=ast, org_id=org.id)
        assert len(rows) == 1
        assert rows[0]["category"] == "Rent"
    await engine.dispose()


# ── guard: currency_mixed_notice_survives_net ────────────────────────────


async def test_currency_mixed_notice_survives_net() -> None:
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
    async with factory() as session:
        org = Organization(name="Mixed Org", billing_cycle_day=1, primary_currency="EUR")
        session.add(org)
        await session.flush()
        at = AccountType(org_id=org.id, name="Checking", slug=None)
        session.add(at)
        await session.flush()
        cat = Category(org_id=org.id, name="General", slug="general", type=CategoryType.EXPENSE)
        session.add(cat)
        session.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
        await session.flush()
        eur_acct = Account(org_id=org.id, account_type_id=at.id, name="EUR",
                            balance=Decimal("0"), currency="EUR")
        usd_acct = Account(org_id=org.id, account_type_id=at.id, name="USD",
                            balance=Decimal("0"), currency="USD")
        session.add_all([eur_acct, usd_acct])
        await session.flush()
        d = datetime.date(2026, 1, 10)
        session.add(Transaction(org_id=org.id, account_id=eur_acct.id, category_id=cat.id,
                                 description="eur expense", amount=Decimal("100"),
                                 type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                                 date=d, settled_date=d))
        session.add(Transaction(org_id=org.id, account_id=usd_acct.id, category_id=cat.id,
                                 description="usd expense", amount=Decimal("100"),
                                 type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                                 date=d, settled_date=d))
        await session.commit()

        from app.schemas.reports_query import Filter, FilterField, FilterOp

        ast = ReportsQuery(
            dataset=Dataset.TRANSACTIONS,
            measure=Measure(agg=Aggregation.SUM, field=MeasureField.NET_AMOUNT),
            dimensions=[Dimension.CATEGORY],
            filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.IN, value=["EUR", "USD"])],
            limit=100,
        )
        SRC.validate(ast)
        rows, meta = await reports_query_service.execute_query(db=session, ast=ast, org_id=org.id)
        # EUR -100 + USD -100: signed mixing reads as -200, NOT the deceptive
        # 0.00 an unsigned mix would never produce here (both legs are
        # expenses) -- but the notice must still fire, because the figure
        # still adds EUR to USD.
        assert Decimal(str(rows[0]["value"])) == Decimal("-200")
        assert meta["warning"] is not None
        assert "excluded" not in meta["warning"]
    await engine.dispose()
