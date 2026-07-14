"""Router tests for the Reports v2 substrate (spec
2026-05-22-reports-v2-flexible-canvas.md, PR1).

Pins the architect-locked invariants:

- Flag OFF → every route returns 404 via the router-level dep.
- Flag ON + anonymous → 401 (bearer scheme fires).
- AST validation: unknown dataset / aggregation / dimension → 422.
- AST validation: hard caps (limit, filter count, dimensions, 5y
  window) → 422.
- AST validation: extra ``org_id`` key → 422 (server-injected only).
- Cross-org isolation: a user in Org A queries via the endpoint and
  only sees Org A data.
- CRUD: list filters by visibility + ownership; visibility / ownership
  gate the edit / delete actions.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category
from app.models.report import Report, ReportVisibility
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.routers.reports import router as reports_router
from app.security import hash_password


# ─── fixtures ─────────────────────────────────────────────────────


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


def _make_app(session_factory, user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_user() -> User:
        return await user_resolver(session_factory)

    def override_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_session_factory] = override_factory
    app.include_router(reports_router)
    return app


async def _seed(factory) -> dict:
    """Two orgs (A + B), one user each (owner role). Org A has fixture
    transactions on two categories so the AST query has data to bite on.
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
        member_a = User(
            org_id=org_a.id,
            username="member_a",
            email="m@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            email_verified=True,
        )
        db.add_all([user_a, user_b, member_a])
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
        for amt in (Decimal("10"), Decimal("20"), Decimal("30")):
            db.add(
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
            db.add(
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
        db.add(
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
        await db.commit()
        return {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "user_a_id": user_a.id,
            "user_b_id": user_b.id,
            "member_a_id": member_a.id,
        }


def _resolver(username: str):
    async def resolve(session_factory):
        async with session_factory() as db:
            from sqlalchemy import select as _s
            return (
                await db.execute(_s(User).where(User.username == username))
            ).scalar_one()
    return resolve


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch):
    """Default every test in this file to FEATURE_REPORTS_V2 ON.

    The OFF-flag tests explicitly flip it back to False.
    """
    monkeypatch.setattr(app_settings, "feature_reports_v2", True)


# ─── flag gate ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flag_off_returns_404_on_query(session_factory, monkeypatch):
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query", json={
            "dataset": "transactions",
            "measure": {"agg": "sum", "field": "amount"},
        })
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_flag_off_returns_404_on_list(session_factory, monkeypatch):
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/reports")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_flag_off_returns_404_on_create(session_factory, monkeypatch):
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports", json={"name": "x"})
    assert res.status_code == 404


# ─── auth gate (flag ON, anonymous) ────────────────────────────────


@pytest.mark.asyncio
async def test_anonymous_request_returns_401(session_factory):
    """Flag ON, no Authorization header: the bearer scheme fires and
    the request is rejected 401 (NOT 404) before any handler runs.

    A bare FastAPI app with the router included — no
    ``get_current_user`` override — exercises the production bearer
    gate end-to-end.
    """
    await _seed(session_factory)
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(reports_router)
    with TestClient(app) as client:
        res = client.get("/api/v1/reports")
    # FastAPI's HTTPBearer with auto_error=True returns 403 when the
    # Authorization header is missing; both 401 / 403 are acceptable
    # auth-rejection codes here. The point is the route did NOT reach
    # the handler.
    assert res.status_code in (401, 403)


# ─── AST validation: hard caps + unknown enum members ─────────────


@pytest.mark.asyncio
async def test_next_cycle_relative_filter_resolves_and_windows(session_factory):
    """A ``{op:'relative', value:'next_cycle'}`` date filter is resolved
    server-side to the org's upcoming billing cycle BEFORE validate/compile.
    Proof: transactions publishes date as between/gte/lte only, so an
    unresolved ``op:'relative'`` would 422; a 200 means the pre-pass ran. The
    data assertion confirms it windowed to the next cycle (only the in-window
    txn survives; the 2026-05-15 seed rows are excluded)."""
    from app.services.billing_service import next_cycle_window

    seed = await _seed(session_factory)
    nxt_start, _nxt_end = next_cycle_window(1, date.today())  # org cycle_day=1
    in_next = nxt_start + timedelta(days=3)

    async with session_factory() as db:
        acct = (
            await db.execute(select(Account).where(Account.org_id == seed["org_a_id"]))
        ).scalars().first()
        cat = (
            await db.execute(
                select(Category).where(
                    Category.org_id == seed["org_a_id"], Category.name == "Food"
                )
            )
        ).scalars().first()
        db.add(
            Transaction(
                org_id=seed["org_a_id"],
                account_id=acct.id,
                category_id=cat.id,
                description="next-cycle",
                amount=Decimal("77"),
                type=TransactionType.EXPENSE,
                status=TransactionStatus.SETTLED,
                date=in_next,
                settled_date=in_next,
            )
        )
        await db.commit()

    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/reports/query",
            json={
                "dataset": "transactions",
                "measure": {"agg": "sum", "field": "amount"},
                "filters": [
                    {"field": "date", "op": "relative", "value": "next_cycle"}
                ],
            },
        )
    assert res.status_code == 200
    rows = res.json()["rows"]
    total = sum(Decimal(str(r["value"])) for r in rows)
    assert total == Decimal("77")


@pytest.mark.asyncio
async def test_relative_filter_rejects_non_date_field(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/reports/query",
            json={
                "dataset": "transactions",
                "measure": {"agg": "sum", "field": "amount"},
                "filters": [
                    {"field": "amount", "op": "relative", "value": "next_cycle"}
                ],
            },
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_relative_filter_rejects_unknown_token(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/reports/query",
            json={
                "dataset": "transactions",
                "measure": {"agg": "sum", "field": "amount"},
                "filters": [
                    {"field": "date", "op": "relative", "value": "last_decade"}
                ],
            },
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_unknown_dataset_rejected(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query", json={
            "dataset": "users",  # not in the enum
            "measure": {"agg": "count", "field": "id"},
        })
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_unknown_aggregation_rejected(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query", json={
            "dataset": "transactions",
            "measure": {"agg": "median", "field": "amount"},  # not in the enum
        })
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_unknown_dimension_rejected(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query", json={
            "dataset": "transactions",
            "measure": {"agg": "sum", "field": "amount"},
            "dimensions": ["merchant"],  # not whitelisted
        })
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_limit_over_500_rejected(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query", json={
            "dataset": "transactions",
            "measure": {"agg": "sum", "field": "amount"},
            "limit": 1000,
        })
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_too_many_dimensions_rejected(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query", json={
            "dataset": "transactions",
            "measure": {"agg": "sum", "field": "amount"},
            "dimensions": ["category", "account", "month"],  # 3 > MAX_DIMENSIONS=2
        })
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_too_many_filters_rejected(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query", json={
            "dataset": "transactions",
            "measure": {"agg": "sum", "field": "amount"},
            "filters": [
                {"field": "txn_type", "op": "eq", "value": "expense"}
                for _ in range(21)  # 21 > MAX_FILTERS=20
            ],
        })
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_date_window_over_5_years_rejected(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query", json={
            "dataset": "transactions",
            "measure": {"agg": "sum", "field": "amount"},
            "filters": [
                {
                    "field": "date",
                    "op": "between",
                    "value": ["2020-01-01", "2026-01-02"],  # 6+ years
                }
            ],
        })
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_extra_org_id_field_rejected(session_factory):
    """The spec is explicit: ``org_id`` is injected server-side and the
    AST has no way to express it. An extra key returns 422.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query", json={
            "dataset": "transactions",
            "measure": {"agg": "sum", "field": "amount"},
            "org_id": 99,  # forbidden extra field
        })
    assert res.status_code == 422


# ─── happy path + cross-org isolation ──────────────────────────────


@pytest.mark.asyncio
async def test_query_sum_by_category_returns_only_my_org(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query", json={
            "dataset": "transactions",
            "measure": {"agg": "sum", "field": "amount"},
            "dimensions": ["category"],
        })
    assert res.status_code == 200, res.text
    body = res.json()
    totals = {row["category"]: row["value"] for row in body["rows"]}
    # Org A: Food 60, Transport 20. Org B's 9999 stays out.
    assert totals == {"Food": 60.0, "Transport": 20.0}
    assert 9999.0 not in totals.values()


@pytest.mark.asyncio
async def test_query_other_org_is_isolated(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_b"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/query", json={
            "dataset": "transactions",
            "measure": {"agg": "sum", "field": "amount"},
            "dimensions": ["category"],
        })
    body = res.json()
    totals = {row["category"]: row["value"] for row in body["rows"]}
    assert totals == {"Food": 9999.0}


# ─── CRUD permissions ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_get_report(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports", json={
            "name": "My Report",
            "visibility": "private",
            "layout_json": {"version": 1, "widgets": []},
            "canvas_filters_json": {},
        })
        assert res.status_code == 201, res.text
        report_id = res.json()["id"]

        res2 = client.get(f"/api/v1/reports/{report_id}")
    assert res2.status_code == 200
    assert res2.json()["name"] == "My Report"


@pytest.mark.asyncio
async def test_list_returns_own_and_org_visible(session_factory):
    seeds = await _seed(session_factory)
    # Seed: user_a private + member_a org-shared in same org + user_b
    # private in other org. user_a should see two.
    async with session_factory() as db:
        db.add_all([
            Report(
                owner_user_id=seeds["user_a_id"],
                org_id=seeds["org_a_id"],
                visibility=ReportVisibility.PRIVATE,
                name="mine",
                layout_json={}, canvas_filters_json={},
            ),
            Report(
                owner_user_id=seeds["member_a_id"],
                org_id=seeds["org_a_id"],
                visibility=ReportVisibility.ORG,
                name="org-shared",
                layout_json={}, canvas_filters_json={},
            ),
            Report(
                owner_user_id=seeds["user_b_id"],
                org_id=seeds["org_b_id"],
                visibility=ReportVisibility.ORG,
                name="other-org",
                layout_json={}, canvas_filters_json={},
            ),
        ])
        await db.commit()

    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/reports")
    assert res.status_code == 200
    names = sorted([row["name"] for row in res.json()])
    assert names == ["mine", "org-shared"]


@pytest.mark.asyncio
async def test_get_cross_org_returns_404(session_factory):
    seeds = await _seed(session_factory)
    async with session_factory() as db:
        report = Report(
            owner_user_id=seeds["user_b_id"],
            org_id=seeds["org_b_id"],
            visibility=ReportVisibility.PRIVATE,
            name="b-private",
            layout_json={}, canvas_filters_json={},
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        rid = report.id

    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get(f"/api/v1/reports/{rid}")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_member_cannot_edit_org_shared_report_by_other(session_factory):
    """Non-owner member of the same org can VIEW an org-shared report
    but the edit/delete gate refuses (only owner + org owner/admin can).
    Org admins / owners CAN edit — exercised separately.
    """
    seeds = await _seed(session_factory)
    async with session_factory() as db:
        report = Report(
            owner_user_id=seeds["user_a_id"],  # owner role in org A
            org_id=seeds["org_a_id"],
            visibility=ReportVisibility.ORG,
            name="org-shared",
            layout_json={}, canvas_filters_json={},
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        rid = report.id

    # member_a is MEMBER role, cannot edit.
    app = _make_app(session_factory, _resolver("member_a"))
    with TestClient(app) as client:
        res = client.patch(f"/api/v1/reports/{rid}", json={"name": "hijack"})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_owner_can_delete_own_report(session_factory):
    seeds = await _seed(session_factory)
    async with session_factory() as db:
        report = Report(
            owner_user_id=seeds["user_a_id"],
            org_id=seeds["org_a_id"],
            visibility=ReportVisibility.PRIVATE,
            name="to-delete",
            layout_json={}, canvas_filters_json={},
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        rid = report.id

    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/reports/{rid}")
    assert res.status_code == 204


@pytest.mark.asyncio
async def test_org_admin_can_edit_org_shared_report(session_factory):
    """An org OWNER / ADMIN can edit any org-shared report inside the
    org, even when they are not the owner.
    """
    seeds = await _seed(session_factory)
    async with session_factory() as db:
        # member_a authored the org-shared report. user_a is OWNER role.
        report = Report(
            owner_user_id=seeds["member_a_id"],
            org_id=seeds["org_a_id"],
            visibility=ReportVisibility.ORG,
            name="shared",
            layout_json={}, canvas_filters_json={},
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        rid = report.id

    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.patch(f"/api/v1/reports/{rid}", json={"name": "renamed"})
    assert res.status_code == 200
    assert res.json()["name"] == "renamed"
