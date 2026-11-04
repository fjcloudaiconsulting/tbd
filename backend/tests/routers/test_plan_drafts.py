"""Plan drafts (TBD-601): org-scoped plans and goals behind the paid gate.

- A second member of the org sees, edits and projects a plan and its goals;
  a member of another org gets 404 on every route.
- The paid gate (``Feature.PLANS``) answers 404 on every scenarios and goals route.
- A plan's currency is the org's currency scope; no scope is refused.
- Every link on a plan or goal must belong to the plan's org; a goal's
  ``org_id`` is its plan's.
- Legacy (pre-plan) scenarios keep working for their org and stay out of others.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.scenario import PlanGoal, Scenario, ScenarioStatus, ScenarioType
from app.models.tag import Tag
from app.models.user import Organization, Role, User
from app.routers.plan_goals import router as goals_router
from app.routers.scenarios import router as scenarios_router
from app.services.plan_draft_service import NO_CURRENCY_SCOPE

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _enable_plans(monkeypatch):
    monkeypatch.setattr(app_settings, "feature_plans", True)


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _org(db, name: str, currency: str | None, users: list[str]) -> dict:
    org = Organization(name=name, billing_cycle_day=1, primary_currency=currency)
    db.add(org)
    await db.flush()
    for i, u in enumerate(users):
        db.add(User(
            org_id=org.id, username=u, email=f"{u}@{name}.test", password_hash="x",
            role=Role.OWNER if i == 0 else Role.MEMBER, is_active=True, email_verified=True,
        ))
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    acc = Account(org_id=org.id, account_type_id=at.id, name="Main", balance=Decimal("1000"),
                  currency=currency or "EUR", is_active=True)
    usd = Account(org_id=org.id, account_type_id=at.id, name="Dollars", balance=Decimal("10"),
                  currency="USD", is_active=True)
    cat = Category(org_id=org.id, name="Goals", type=CategoryType.EXPENSE)
    tag = Tag(org_id=org.id, name="trip", name_normalized="trip")
    db.add_all([acc, usd, cat, tag])
    await db.flush()
    rec = RecurringTransaction(
        org_id=org.id, account_id=acc.id, category_id=cat.id, description="Save",
        amount=Decimal("50"), type="expense", frequency=Frequency.MONTHLY,
        next_due_date=date(2026, 12, 1),
    )
    db.add(rec)
    await db.flush()
    return {"org": org.id, "account": acc.id, "usd": usd.id, "category": cat.id,
            "tag": tag.id, "recurring": rec.id}


@pytest_asyncio.fixture
async def w(factory) -> dict:
    async with factory() as db:
        out = {
            "A": await _org(db, "acme", "EUR", ["alice", "bob"]),
            "B": await _org(db, "other", "EUR", ["carol"]),
            "legacy": await _org(db, "multi", None, ["dave"]),
        }
        await db.commit()
    return out


def _client(factory, username: str) -> TestClient:
    app = FastAPI()

    async def db_dep():
        async with factory() as s:
            yield s

    async def user_dep():
        async with factory() as s:
            return (await s.execute(select(User).where(User.username == username))).scalar_one()

    app.dependency_overrides[get_db] = db_dep
    app.dependency_overrides[get_current_user] = user_dep
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.include_router(scenarios_router)
    app.include_router(goals_router)
    return TestClient(app)


def _plan(account_id: int, **plan) -> dict:
    return {
        "name": "Sabbatical", "scenario_type": "custom", "horizon_months": 24,
        "params": {"scenario_type": "custom", "label": "Year off", "events": []},
        **plan,
    }


_GOAL = {"name": "Emergency fund", "tier": "essential", "kind": "savings_target",
         "target_amount": "6000.00", "target_date": "2028-01-01", "monthly_amount": "250.00"}


def _create(c: TestClient, a: dict, **plan) -> dict:
    res = c.post("/api/v1/scenarios", json=_plan(a["account"], **plan))
    assert res.status_code == 201, res.text
    return res.json()


async def test_second_member_sees_edits_and_projects_the_plan_and_its_goals(factory, w):
    a = w["A"]
    with _client(factory, "alice") as alice:
        plan = _create(alice, a, funding_account_id=a["account"], category_id=a["category"],
                       tag_id=a["tag"], monthly_budget="300.00", birth_year=1990,
                       return_pct="4.50", inflation_pct="2.00")
        goal = alice.post(f"/api/v1/scenarios/{plan['id']}/goals",
                          json={**_GOAL, "account_id": a["account"]}).json()
    assert plan["status"] == "draft" and plan["currency"] == "EUR"
    assert (plan["birth_year"], plan["return_pct"], plan["tag_id"]) == (1990, "4.50", a["tag"])
    assert (goal["org_id"], goal["tier"], goal["kind"]) == (a["org"], "essential", "savings_target")
    pid = plan["id"]
    with _client(factory, "bob") as bob:
        assert [p["id"] for p in bob.get("/api/v1/scenarios").json()] == [pid]
        assert bob.get(f"/api/v1/scenarios/{pid}").json()["monthly_budget"] == "300.00"
        assert [g["id"] for g in bob.get(f"/api/v1/scenarios/{pid}/goals").json()] == [goal["id"]]
        assert bob.post(f"/api/v1/scenarios/{pid}/simulate").json()["projection_json"]
        assert bob.post("/api/v1/scenarios/compare",
                        json={"scenario_ids": [pid], "horizon_months": 24}).status_code == 200
        edited = bob.patch(f"/api/v1/scenarios/{pid}/goals/{goal['id']}",
                           json={"enabled": False, "tier": "dream", "account_id": None}).json()
        assert (edited["enabled"], edited["tier"], edited["account_id"]) == (False, "dream", None)
        assert bob.patch(f"/api/v1/scenarios/{pid}", json={"tag_id": None}).json()["tag_id"] is None


def _every_route_call(pid: int, gid: int) -> list[tuple[str, str, dict | None]]:
    base = f"/api/v1/scenarios/{pid}"
    return [
        ("GET", "/api/v1/scenarios", None),
        ("POST", "/api/v1/scenarios", {}),
        ("GET", base, None),
        ("PATCH", base, {"name": "x"}),
        ("DELETE", base, None),
        ("POST", f"{base}/simulate", None),
        ("POST", "/api/v1/scenarios/compare", {"scenario_ids": [pid], "horizon_months": 12}),
        ("GET", f"{base}/goals", None),
        ("POST", f"{base}/goals", _GOAL),
        ("PATCH", f"{base}/goals/{gid}", {"name": "x"}),
        ("DELETE", f"{base}/goals/{gid}", None),
    ]


def test_the_call_table_covers_every_route():
    """Keeps the two tables below honest when a route is added."""
    routes = {(m, r.path) for router in (scenarios_router, goals_router)
              for r in router.routes if isinstance(r, APIRoute) for m in r.methods}
    called = {(m, p.replace("/1/", "/{scenario_id}/").replace("/goals/2", "/goals/{goal_id}")
               .replace("/scenarios/1", "/scenarios/{scenario_id}"))
              for m, p, _ in _every_route_call(1, 2)}
    assert called == routes


async def _seed_plan(factory, a: dict, user: str) -> tuple[int, int]:
    async with factory() as db:
        uid = (await db.execute(select(User.id).where(User.username == user))).scalar_one()
        plan = Scenario(org_id=a["org"], user_id=uid, name="P", scenario_type=ScenarioType.CUSTOM,
                        params_json={"scenario_type": "custom", "label": "x", "events": []},
                        currency="EUR")
        db.add(plan)
        await db.flush()
        goal = PlanGoal(org_id=a["org"], scenario_id=plan.id, name="G", tier="want", kind="lump")
        db.add(goal)
        await db.commit()
        return plan.id, goal.id


async def test_a_member_of_another_org_gets_404_on_every_route(factory, w):
    pid, gid = await _seed_plan(factory, w["A"], "alice")
    with _client(factory, "carol") as carol:
        assert carol.get("/api/v1/scenarios").json() == []
        for method, path, body in _every_route_call(pid, gid):
            if path == "/api/v1/scenarios":
                continue  # carol's own org: the list is asserted empty above
            res = carol.request(method, path, json=body)
            assert res.status_code == 404, (method, path, res.text)
    async with factory() as db:
        assert (await db.get(Scenario, pid)).name == "P"
        assert await db.get(PlanGoal, gid) is not None


async def test_the_paid_gate_closes_every_route(factory, w, monkeypatch):
    pid, gid = await _seed_plan(factory, w["A"], "alice")
    monkeypatch.setattr(app_settings, "feature_plans", False)
    with _client(factory, "alice") as alice:
        for method, path, body in _every_route_call(pid, gid):
            res = alice.request(method, path, json=body)
            assert res.status_code == 404, (method, path, res.text)
    async with factory() as db:
        assert await db.get(PlanGoal, gid) is not None


async def test_a_plan_takes_the_org_currency_and_refuses_another(factory, w):
    a = w["A"]
    with _client(factory, "alice") as alice:
        res = alice.post("/api/v1/scenarios", json=_plan(a["account"], currency="USD"))
        assert res.status_code == 422 and "EUR" in res.json()["detail"]
        assert _create(alice, a, currency="eur")["currency"] == "EUR"
        assert _create(alice, a)["currency"] == "EUR"
        res = alice.post("/api/v1/scenarios", json=_plan(a["account"], funding_account_id=a["usd"]))
        assert res.status_code == 422 and "USD" in res.json()["detail"]


async def test_an_org_without_a_currency_scope_is_refused(factory, w):
    lg = w["legacy"]
    with _client(factory, "dave") as dave:
        res = dave.post("/api/v1/scenarios", json=_plan(lg["account"]))
        assert res.status_code == 422 and res.json()["detail"] == NO_CURRENCY_SCOPE
    pid, gid = await _seed_plan(factory, lg, "dave")
    with _client(factory, "dave") as dave:
        # A legacy row still reads and renames; plan fields and goals are refused.
        assert dave.get(f"/api/v1/scenarios/{pid}").status_code == 200
        assert dave.patch(f"/api/v1/scenarios/{pid}", json={"name": "Old"}).status_code == 200
        for res in (
            dave.patch(f"/api/v1/scenarios/{pid}", json={"birth_year": 1980}),
            dave.post(f"/api/v1/scenarios/{pid}/goals", json=_GOAL),
        ):
            assert res.status_code == 422 and res.json()["detail"] == NO_CURRENCY_SCOPE


@pytest.mark.parametrize("field,key", [
    ("funding_account_id", "account"), ("category_id", "category"), ("tag_id", "tag"),
])
async def test_a_plan_cannot_link_another_orgs_rows(factory, w, field, key):
    """FENCE (#1069 review). Wrong implementation: trusting the FK, which
    only checks the row exists."""
    a, b = w["A"], w["B"]
    with _client(factory, "alice") as alice:
        res = alice.post("/api/v1/scenarios", json=_plan(a["account"], **{field: b[key]}))
        assert res.status_code == 422 and "not found in this organization" in res.json()["detail"]
        pid = _create(alice, a)["id"]
        res = alice.patch(f"/api/v1/scenarios/{pid}", json={field: b[key]})
        assert res.status_code == 422
        assert alice.get(f"/api/v1/scenarios/{pid}").json()[field] is None


@pytest.mark.parametrize("field,key", [
    ("account_id", "account"), ("category_id", "category"), ("funding_recurring_id", "recurring"),
])
async def test_a_goal_cannot_link_another_orgs_rows(factory, w, field, key):
    a, b = w["A"], w["B"]
    with _client(factory, "alice") as alice:
        pid = _create(alice, a)["id"]
        res = alice.post(f"/api/v1/scenarios/{pid}/goals", json={**_GOAL, field: b[key]})
        assert res.status_code == 422 and "not found in this organization" in res.json()["detail"]
        goal = alice.post(f"/api/v1/scenarios/{pid}/goals", json={**_GOAL, field: a[key]}).json()
        assert goal[field] == a[key]
        res = alice.patch(f"/api/v1/scenarios/{pid}/goals/{goal['id']}", json={field: b[key]})
        assert res.status_code == 422


async def test_a_goal_takes_its_org_from_the_plan_and_validates_its_values(factory, w):
    a = w["A"]
    with _client(factory, "alice") as alice:
        pid = _create(alice, a)["id"]
        url = f"/api/v1/scenarios/{pid}/goals"
        assert alice.post(url, json={**_GOAL, "org_id": w["B"]["org"]}).status_code == 422
        assert alice.post(url, json={**_GOAL, "tier": "nice_to_have"}).status_code == 422
        assert alice.post(url, json={**_GOAL, "target_amount": "-1"}).status_code == 422
        gid = alice.post(url, json=_GOAL).json()["id"]
        assert alice.patch(f"{url}/{gid}", json={"name": None}).status_code == 422
        assert alice.delete(f"{url}/{gid}").status_code == 204
        assert alice.get(url).json() == []


async def test_a_goal_of_another_plan_is_not_found_through_this_one(factory, w):
    a = w["A"]
    with _client(factory, "alice") as alice:
        p1, p2 = _create(alice, a)["id"], _create(alice, a)["id"]
        gid = alice.post(f"/api/v1/scenarios/{p1}/goals", json=_GOAL).json()["id"]
        assert alice.patch(f"/api/v1/scenarios/{p2}/goals/{gid}", json={"name": "x"}).status_code == 404


async def test_only_a_draft_is_edited_here(factory, w):
    """Lifecycle transitions belong to TBD-602; until then a started plan's
    plan fields and goals are not editable through the draft routes."""
    pid, gid = await _seed_plan(factory, w["A"], "alice")
    async with factory() as db:
        (await db.get(Scenario, pid)).status = ScenarioStatus.ACTIVE
        await db.commit()
    with _client(factory, "bob") as bob:
        for res in (
            bob.patch(f"/api/v1/scenarios/{pid}", json={"monthly_budget": "10.00"}),
            bob.post(f"/api/v1/scenarios/{pid}/goals", json=_GOAL),
            bob.patch(f"/api/v1/scenarios/{pid}/goals/{gid}", json={"enabled": False}),
            bob.delete(f"/api/v1/scenarios/{pid}/goals/{gid}"),
        ):
            assert res.status_code == 409, res.text


async def test_plan_field_validation(factory, w):
    a = w["A"]
    with _client(factory, "alice") as alice:
        for bad in ({"birth_year": date.today().year + 1}, {"return_pct": "101"},
                    {"monthly_budget": "-5"}, {"currency": "EURO"}):
            assert alice.post("/api/v1/scenarios", json=_plan(a["account"], **bad)).status_code == 422
        pid = _create(alice, a)["id"]
        assert alice.patch(f"/api/v1/scenarios/{pid}", json={"inflation_pct": None}).status_code == 422
