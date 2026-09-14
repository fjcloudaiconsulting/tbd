"""Router-level test for GET /api/v1/transactions.

Task 2 changed the list endpoint to return a ``ListEnvelope`` (was a bare
list) and to accept ``sort_by``/``sort_dir`` query params that drive
server-side ordering. Invalid sort columns/directions must surface as
HTTP 400 (the service's ``resolve_order_by`` raises ``ValidationError``).

Harness mirrors tests/routers/test_recurring_generate_route.py: an
in-memory SQLite ``session_factory``, ``make_app`` with get_db /
get_current_user overrides, and a ``_seed`` helper that builds an org +
user + account + category + a few transactions of distinct amounts.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.transactions import router as transactions_router
from app.security import hash_password
from tests.factories import make_test_app


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session_factory():
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


async def _resolve_superadmin(factory) -> User:
    from sqlalchemy import select as _select
    async with factory() as db:
        return (
            await db.execute(_select(User).where(User.is_superadmin.is_(True)))
        ).scalar_one()


def make_app(session_factory) -> FastAPI:
    return make_test_app(
        session_factory,
        routers=transactions_router,
        current_user=_resolve_superadmin,
    )


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="root",
            email="root@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        db.add_all([user, at])
        await db.flush()
        acct = Account(
            org_id=org.id, name="Acct A", account_type_id=at.id,
            balance=Decimal("1000"), currency="EUR",
        )
        db.add(acct)
        await db.flush()
        cat = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE, is_system=False,
        )
        db.add(cat)
        await db.flush()
        # Distinct amounts so the sort assertion is meaningful, inserted
        # out of order so a default order can't accidentally satisfy it.
        for i, amt in enumerate(["30.00", "10.00", "20.00"]):
            db.add(Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description=f"tx-{i}", amount=Decimal(amt),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=date(2026, 1, i + 1),
                settled_date=date(2026, 1, i + 1),
            ))
        await db.commit()
        return {"org_id": org.id, "acct_id": acct.id, "cat_id": cat.id}


@pytest_asyncio.fixture
async def client(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory)
    with TestClient(app) as c:
        yield c


# ── tests ────────────────────────────────────────────────────────────────


def test_list_returns_envelope(client):
    res = client.get("/api/v1/transactions?limit=2&offset=0")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"items", "total", "limit", "offset"}
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert isinstance(body["items"], list)
    assert body["total"] >= len(body["items"])


def test_list_server_side_sort_amount_asc(client):
    res = client.get("/api/v1/transactions?sort_by=amount&sort_dir=asc")
    assert res.status_code == 200
    amounts = [float(i["amount"]) for i in res.json()["items"]]
    assert amounts == sorted(amounts)


def test_invalid_sort_by_is_400(client):
    res = client.get("/api/v1/transactions?sort_by=evil")
    assert res.status_code == 400


def test_invalid_sort_dir_is_400(client):
    res = client.get("/api/v1/transactions?sort_by=amount&sort_dir=up")
    assert res.status_code == 400


def test_limit_zero_rejected(client):
    res = client.get("/api/v1/transactions?limit=0")
    assert res.status_code == 422


# ── TBD-268: collapse_transfers is opt-in ──────────────────────────────────


async def _seed_pair(factory) -> dict:
    """Add a second account plus one reciprocally-linked transfer pair.

    Expense leg first, mirroring ``create_transfer``, so the INCOME leg takes
    the higher id -- the asymmetry that made the reported bug a total blackout
    under ``?type=income``.
    """
    from sqlalchemy import select as _select

    async with factory() as db:
        org = (await db.execute(_select(Organization))).scalars().first()
        at = (await db.execute(_select(AccountType))).scalars().first()
        cat = (await db.execute(_select(Category))).scalars().first()
        acct_a = (await db.execute(_select(Account))).scalars().first()
        acct_b = Account(
            org_id=org.id, name="Acct B", account_type_id=at.id,
            balance=Decimal("0"), currency="EUR",
        )
        db.add(acct_b)
        await db.flush()
        exp = Transaction(
            org_id=org.id, account_id=acct_a.id, category_id=cat.id,
            description="transfer out", amount=Decimal("50.00"),
            type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
            date=date(2026, 2, 1), settled_date=date(2026, 2, 1),
        )
        db.add(exp)
        await db.flush()
        inc = Transaction(
            org_id=org.id, account_id=acct_b.id, category_id=cat.id,
            description="transfer in", amount=Decimal("50.00"),
            type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
            date=date(2026, 2, 1), settled_date=date(2026, 2, 1),
        )
        db.add(inc)
        await db.flush()
        assert inc.id > exp.id
        exp.linked_transaction_id = inc.id
        inc.linked_transaction_id = exp.id
        await db.commit()
        return {"expense_id": exp.id, "income_id": inc.id, "acct_b_id": acct_b.id}


@pytest_asyncio.fixture
async def paired(session_factory, client):
    return await _seed_pair(session_factory)


def test_b13_default_returns_both_transfer_legs(client, paired):
    """B13 — GET /api/v1/transactions with NO ``collapse_transfers`` returns
    BOTH legs of a transfer pair.

    Kills the default flipping to true. Default-false is load-bearing: this
    endpoint is reachable by superadmin Personal Access Token, so it is an
    external contract with unknown consumers, and every aggregate caller that
    sums per account needs both legs (each sits on a different account).
    """
    res = client.get("/api/v1/transactions?limit=200")
    assert res.status_code == 200
    ids = {i["id"] for i in res.json()["items"]}
    assert paired["expense_id"] in ids
    assert paired["income_id"] in ids
    # And no caller gets the partner-name field for free.
    assert all(i["linked_account_name"] is None for i in res.json()["items"])


def test_b13b_opt_in_collapses_and_returns_partner_account_name(client, paired):
    """The opt-in half of B13: the flag is actually plumbed through the router
    (a param the router accepts but never forwards would pass B13 alone)."""
    res = client.get("/api/v1/transactions?limit=200&collapse_transfers=true")
    assert res.status_code == 200
    body = res.json()
    ids = [i["id"] for i in body["items"]]
    assert paired["expense_id"] in ids
    assert paired["income_id"] not in ids
    assert body["total"] == len(body["items"])
    survivor = next(i for i in body["items"] if i["id"] == paired["expense_id"])
    assert survivor["linked_account_name"] == "Acct B"


def test_b13c_income_filter_no_longer_blacks_out(client, paired):
    """The reported severity, at the HTTP boundary: ``?type=income`` used to
    hide every returned row client-side because the income leg holds the
    higher id. The surviving leg must be the income one here."""
    res = client.get("/api/v1/transactions?type=income&collapse_transfers=true")
    assert res.status_code == 200
    body = res.json()
    assert [i["id"] for i in body["items"]] == [paired["income_id"]]
    assert body["total"] == 1


# ── TBD-463: multi-valued account/category filters, category-name search ────


async def _seed_multi(factory) -> dict:
    """Three extra accounts, a master "Housing" with child "Rent", and one
    row per bucket, so the multi-value filters have something to discriminate.
    """
    from sqlalchemy import select as _select

    from app.models.tag import Tag, TransactionTag

    async with factory() as db:
        org = (await db.execute(_select(Organization))).scalars().first()
        at = (await db.execute(_select(AccountType))).scalars().first()
        groceries = (await db.execute(_select(Category))).scalars().first()
        a1, a2, a3 = (
            Account(
                org_id=org.id, name=f"Multi {n}", account_type_id=at.id,
                balance=Decimal("0"), currency="EUR",
            )
            for n in ("1", "2", "3")
        )
        housing = Category(
            org_id=org.id, name="Housing", slug="housing",
            type=CategoryType.EXPENSE, is_system=False,
        )
        db.add_all([a1, a2, a3, housing])
        await db.flush()
        rent = Category(
            org_id=org.id, name="Rent", slug="rent",
            type=CategoryType.EXPENSE, is_system=False, parent_id=housing.id,
        )
        db.add(rent)
        await db.flush()

        def tx(acct, cat, desc):
            return Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description=desc, amount=Decimal("5.00"),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=date(2026, 3, 1), settled_date=date(2026, 3, 1),
            )

        on_a1 = tx(a1, groceries, "multi-a1")
        on_a2 = tx(a2, groceries, "multi-a2")
        on_a3 = tx(a3, groceries, "multi-a3")
        # Descriptions deliberately do not contain the category names, so a
        # name-search hit can only come from the category term.
        on_housing = tx(a1, housing, "multi-master")
        on_rent = tx(a1, rent, "multi-child")
        db.add_all([on_a1, on_a2, on_a3, on_housing, on_rent])
        await db.flush()
        t_x = Tag(org_id=org.id, name="x", name_normalized="x")
        t_y = Tag(org_id=org.id, name="y", name_normalized="y")
        db.add_all([t_x, t_y])
        await db.flush()
        db.add_all([
            TransactionTag(transaction_id=on_a1.id, tag_id=t_x.id),
            TransactionTag(transaction_id=on_a1.id, tag_id=t_y.id),
            TransactionTag(transaction_id=on_a2.id, tag_id=t_x.id),
        ])
        await db.commit()
        return {
            "a1": a1.id, "a2": a2.id, "a3": a3.id,
            "housing": housing.id, "rent": rent.id,
            "on_a1": on_a1.id, "on_a2": on_a2.id, "on_a3": on_a3.id,
            "on_housing": on_housing.id, "on_rent": on_rent.id,
        }


@pytest_asyncio.fixture
async def multi(session_factory, client):
    return await _seed_multi(session_factory)


def _ids(res) -> list[int]:
    assert res.status_code == 200, res.text
    return [i["id"] for i in res.json()["items"]]


def test_f1_repeated_account_id_is_or_over_the_set(client, multi):
    """F1: ``?account_id=A2&account_id=A3`` returns exactly the A2 and A3 rows
    (A1 carries rows too, and they stay out).

    Kills a scalar param (FastAPI keeps the last value, so A3 only) and AND
    semantics (a row sits on one account, so nothing).
    """
    res = client.get(
        f"/api/v1/transactions?account_id={multi['a2']}&account_id={multi['a3']}"
    )
    assert sorted(_ids(res)) == sorted([multi["on_a2"], multi["on_a3"]])
    assert res.json()["total"] == 2


@pytest.mark.parametrize("collapse", ["false", "true"])
def test_f2_master_and_its_child_return_each_row_once(client, multi, collapse):
    """F2: ``?category_id=M&category_id=C`` (C a child of M) returns the M row
    and the C row exactly once each, and ``total`` agrees.

    Kills a JOIN or UNION ALL implementation: the C row matches both the
    selected C and the selected M's subtree, so it would come back twice.
    """
    res = client.get(
        f"/api/v1/transactions?category_id={multi['housing']}"
        f"&category_id={multi['rent']}&collapse_transfers={collapse}"
    )
    assert sorted(_ids(res)) == sorted([multi["on_housing"], multi["on_rent"]])
    assert res.json()["total"] == 2


def test_f7_category_name_search_with_category_name_sort(client, multi):
    """F7: ``search=hous&sort_by=category_name`` is a 200 with the row once.

    The sort already joins ``Category``; a JOIN-based name search would join
    it a second time (an error) or leak the join into the count.
    """
    res = client.get("/api/v1/transactions?search=hous&sort_by=category_name")
    assert _ids(res) == [multi["on_housing"]]
    assert res.json()["total"] == 1


def test_guard_single_account_id_still_filters(client, multi):
    res = client.get(f"/api/v1/transactions?account_id={multi['a3']}")
    assert _ids(res) == [multi["on_a3"]]


def test_guard_single_category_id_defaults_to_subtree(client, multi):
    res = client.get(f"/api/v1/transactions?category_id={multi['housing']}")
    assert sorted(_ids(res)) == sorted([multi["on_housing"], multi["on_rent"]])


def test_guard_non_integer_account_id_is_422(client):
    assert client.get("/api/v1/transactions?account_id=abc").status_code == 422


def test_guard_tags_without_tag_match_stay_and(client, multi):
    """The side panel sends ``tag_match=any``; the API default stays ``all``."""
    assert _ids(client.get("/api/v1/transactions?tags=x,y")) == [multi["on_a1"]]
