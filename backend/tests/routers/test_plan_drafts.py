"""Plan drafts (TBD-601): org-scoped plans and goals behind the paid gate.

- A second member of the org sees and projects a plan and its goals; a member
  of another org gets 404 on every route.
- Rights (the reports precedent): a legacy scenario is its creator's only; a
  plan is visible org-wide and changed by its creator or an org owner or admin
  (another member gets 403). The matrix test covers every cell.
- The paid gate (``Feature.PLANS``) answers 404 on every scenarios and goals route,
  when either the switch is off or the org isn't entitled to ``plans`` (TBD-564).
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
from app.models.feature_override import OrgFeatureOverride
from app.models.recurring import Frequency, RecurringTransaction
from app.models.scenario import PlanGoal, Scenario, ScenarioStatus, ScenarioType
from app.models.tag import Tag
from app.models.user import Organization, Role, User
from app.routers.plan_goals import router as goals_router
from app.routers.scenarios import router as scenarios_router
from app.services import feature_service
from app.services.feature_service import get_entitlements as real_get_entitlements
from app.services.plan_draft_service import NO_CURRENCY_SCOPE

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _enable_plans(monkeypatch, plans_entitled):
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


async def _org(db, name: str, currency: str | None, users: dict[str, Role]) -> dict:
    org = Organization(name=name, billing_cycle_day=1, primary_currency=currency)
    db.add(org)
    await db.flush()
    for u, role in users.items():
        db.add(User(
            org_id=org.id, username=u, email=f"{u}@{name}.test", password_hash="x",
            role=role, is_active=True, email_verified=True,
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
            # alice creates; bob is another plain member.
            "A": await _org(db, "acme", "EUR", {"olivia": Role.OWNER, "adam": Role.ADMIN,
                                                "alice": Role.MEMBER, "bob": Role.MEMBER}),
            "B": await _org(db, "other", "EUR", {"carol": Role.OWNER}),
            "legacy": await _org(db, "multi", None, {"dave": Role.OWNER}),
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
    with _client(factory, "alice") as alice:
        edited = alice.patch(f"/api/v1/scenarios/{pid}/goals/{goal['id']}",
                             json={"enabled": False, "tier": "dream", "account_id": None}).json()
        assert (edited["enabled"], edited["tier"], edited["account_id"]) == (False, "dream", None)
        assert alice.patch(f"/api/v1/scenarios/{pid}", json={"tag_id": None}).json()["tag_id"] is None


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
        ("GET", f"/api/v1/scenarios/projection?plan_id={pid}&lifetime=true", None),
        ("GET", f"/api/v1/scenarios/projection/compare?plan_id={pid}", None),
        ("POST", f"{base}/start", None),
        ("POST", f"{base}/finish", None),
        ("GET", f"{base}/progress", None),
    ]


def test_the_call_table_covers_every_route():
    """Keeps the two tables below honest when a route is added."""
    routes = {(m, r.path) for router in (scenarios_router, goals_router)
              for r in router.routes if isinstance(r, APIRoute) for m in r.methods}
    called = {(m, p.split("?")[0].replace("/1/", "/{scenario_id}/").replace("/goals/2", "/goals/{goal_id}")
               .replace("/scenarios/1", "/scenarios/{scenario_id}"))
              for m, p, _ in _every_route_call(1, 2)}
    assert called == routes


async def _seed_plan(factory, a: dict, user: str, *, legacy: bool = False) -> tuple[int, int | None]:
    """A plan (currency set) with one goal, or a legacy scenario (no plan
    fields, so no currency and no goal)."""
    async with factory() as db:
        uid = (await db.execute(select(User.id).where(User.username == user))).scalar_one()
        plan = Scenario(org_id=a["org"], user_id=uid, name="P", scenario_type=ScenarioType.CUSTOM,
                        params_json={"scenario_type": "custom", "label": "x", "events": []},
                        currency=None if legacy else "EUR")
        db.add(plan)
        await db.flush()
        goal = None
        if not legacy:
            goal = PlanGoal(org_id=a["org"], scenario_id=plan.id, name="G", tier="want", kind="lump")
            db.add(goal)
        await db.commit()
        return plan.id, goal and goal.id


OK = "ok"
# who -> (legacy (read, write, delete), plan (read, write, delete)).
RIGHTS = {
    "alice": ((OK, OK, OK), (OK, OK, OK)),        # the creator, a plain member
    "bob": ((404, 404, 404), (OK, 403, 403)),     # another member
    "adam": ((404, 404, 404), (OK, OK, OK)),      # org admin
    "olivia": ((404, 404, 404), (OK, OK, OK)),    # org owner
    "carol": ((404, 404, 404), (404, 404, 404)),  # another org
}


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "plan"])
@pytest.mark.parametrize("who", list(RIGHTS))
async def test_who_reads_writes_and_deletes_what(factory, w, who, legacy):
    """FENCE (review of #1090). Wrong implementations: org-wide access to a
    legacy scenario (a privacy regression), or any member editing a plan."""
    pid, gid = await _seed_plan(factory, w["A"], "alice", legacy=legacy)
    read, write, delete = RIGHTS[who][0 if legacy else 1]
    base = f"/api/v1/scenarios/{pid}"
    reads = [("GET", base, None), ("GET", f"{base}/goals", None), ("POST", f"{base}/simulate", None),
             ("POST", "/api/v1/scenarios/compare", {"scenario_ids": [pid], "horizon_months": 12})]
    writes = [("PATCH", base, {"name": "Changed"}), ("POST", f"{base}/goals", _GOAL)]
    if gid:
        writes += [("PATCH", f"{base}/goals/{gid}", {"enabled": False}),
                   ("DELETE", f"{base}/goals/{gid}", None)]
    with _client(factory, who) as c:
        listed = pid in [r["id"] for r in c.get("/api/v1/scenarios").json()]
        assert listed == (read == OK)
        for expected, calls in ((read, reads), (write, writes), (delete, [("DELETE", base, None)])):
            for method, path, body in calls:
                res = c.request(method, path, json=body)
                assert (res.status_code < 300 if expected == OK else res.status_code == expected), (
                    method, path, res.status_code, res.text)
    async with factory() as db:
        row = await db.get(Scenario, pid)
        assert (row is None) == (delete == OK)
        if row is not None:
            assert (row.name == "Changed") == (write == OK)


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


async def test_the_plans_entitlement_closes_every_route(factory, w, monkeypatch):
    """FENCE F-E5 (TBD-564), routes. The switch is on, but the org's plan
    doesn't grant ``plans``: every route answers 404 and nothing is written.
    An override granting it reopens them."""
    pid, gid = await _seed_plan(factory, w["A"], "alice")
    monkeypatch.setattr(feature_service, "get_entitlements", real_get_entitlements)
    with _client(factory, "alice") as alice:
        for method, path, body in _every_route_call(pid, gid):
            res = alice.request(method, path, json=body)
            assert res.status_code == 404, (method, path, res.text)
    async with factory() as db:
        assert await db.get(PlanGoal, gid) is not None
        db.add(OrgFeatureOverride(org_id=w["A"]["org"], feature_key="plans", value=True))
        await db.commit()
    with _client(factory, "alice") as alice:
        assert alice.get(f"/api/v1/scenarios/{pid}").status_code == 200


async def test_a_plan_takes_the_org_currency_and_refuses_another(factory, w):
    a = w["A"]
    with _client(factory, "alice") as alice:
        res = alice.post("/api/v1/scenarios", json=_plan(a["account"], currency="USD"))
        assert res.status_code == 422 and "EUR" in res.json()["detail"]
        assert _create(alice, a, currency="eur")["currency"] == "EUR"
        assert _create(alice, a, birth_year=1990)["currency"] == "EUR"
        # No plan field: a legacy scenario, which takes no currency.
        assert _create(alice, a)["currency"] is None
        res = alice.post("/api/v1/scenarios", json=_plan(a["account"], funding_account_id=a["usd"]))
        assert res.status_code == 422 and "USD" in res.json()["detail"]


async def test_an_org_without_a_currency_scope_is_refused(factory, w):
    lg = w["legacy"]
    with _client(factory, "dave") as dave:
        res = dave.post("/api/v1/scenarios", json=_plan(lg["account"], monthly_budget="100.00"))
        assert res.status_code == 422 and res.json()["detail"] == NO_CURRENCY_SCOPE
        # A plain scenario still works in a multi-currency org.
        assert dave.post("/api/v1/scenarios", json=_plan(lg["account"])).status_code == 201
    pid, _ = await _seed_plan(factory, lg, "dave", legacy=True)
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
    """Without ``confirm=true`` no field of a started plan, nor its goals, is
    editable (TBD-602, ``test_plan_lifecycle.py``). Delete works in any state."""
    pid, gid = await _seed_plan(factory, w["A"], "alice")
    async with factory() as db:
        (await db.get(Scenario, pid)).status = ScenarioStatus.ACTIVE
        await db.commit()
    with _client(factory, "alice") as alice:
        for res in (
            alice.patch(f"/api/v1/scenarios/{pid}", json={"monthly_budget": "10.00"}),
            alice.patch(f"/api/v1/scenarios/{pid}", json={"name": "Renamed"}),
            alice.post(f"/api/v1/scenarios/{pid}/goals", json=_GOAL),
            alice.patch(f"/api/v1/scenarios/{pid}/goals/{gid}", json={"enabled": False}),
            alice.delete(f"/api/v1/scenarios/{pid}/goals/{gid}"),
        ):
            assert res.status_code == 409, res.text
        assert alice.delete(f"/api/v1/scenarios/{pid}").status_code == 204


async def test_plan_field_validation(factory, w):
    a = w["A"]
    with _client(factory, "alice") as alice:
        for bad in ({"birth_year": date.today().year + 1}, {"return_pct": "101"},
                    {"monthly_budget": "-5"}, {"currency": "EURO"}):
            assert alice.post("/api/v1/scenarios", json=_plan(a["account"], **bad)).status_code == 422
        pid = _create(alice, a)["id"]
        assert alice.patch(f"/api/v1/scenarios/{pid}", json={"inflation_pct": None}).status_code == 422


async def test_a_goal_makes_a_legacy_scenario_a_plan(factory, w):
    """The visibility marker (``currency``) is set by a goal write too, so a
    legacy row with goals is never private to its creator alone by accident."""
    pid, _ = await _seed_plan(factory, w["A"], "alice", legacy=True)
    with _client(factory, "bob") as bob:
        assert bob.get(f"/api/v1/scenarios/{pid}").status_code == 404
    with _client(factory, "alice") as alice:
        assert alice.post(f"/api/v1/scenarios/{pid}/goals", json=_GOAL).status_code == 201
    with _client(factory, "bob") as bob:
        assert bob.get(f"/api/v1/scenarios/{pid}").json()["currency"] == "EUR"


async def test_a_goal_funding_series_must_be_in_the_plan_currency(factory, w):
    a = w["A"]
    async with factory() as db:
        rec = RecurringTransaction(
            org_id=a["org"], account_id=a["usd"], category_id=a["category"], description="USD",
            amount=Decimal("5"), type="expense", frequency=Frequency.MONTHLY,
            next_due_date=date(2026, 12, 1),
        )
        db.add(rec)
        await db.commit()
        usd_rec = rec.id
    with _client(factory, "alice") as alice:
        pid = _create(alice, a)["id"]
        res = alice.post(f"/api/v1/scenarios/{pid}/goals", json={**_GOAL, "funding_recurring_id": usd_rec})
        assert res.status_code == 422 and "USD" in res.json()["detail"]
        res = alice.post(f"/api/v1/scenarios/{pid}/goals", json={**_GOAL, "funding_recurring_id": a["recurring"]})
        assert res.status_code == 201


async def test_a_second_member_projects_plans_and_toggles_are_never_saved(factory, w):
    pid, gid = await _seed_plan(factory, w["A"], "alice")
    with _client(factory, "bob") as bob:
        on = bob.get(f"/api/v1/scenarios/projection?plan_id={pid}&lifetime=true")
        off = bob.get(f"/api/v1/scenarios/projection?plan_id={pid}&off={gid}")
        compare = bob.get(f"/api/v1/scenarios/projection/compare?plan_id={pid}&plan_id={pid}")
    assert on.status_code == off.status_code == compare.status_code == 200, on.text
    body = on.json()
    assert body["currency"] == "EUR" and body["months"] and body["lifetime"]["months"]
    assert off.json()["lifetime"] is None
    assert [c["plan_id"] for c in compare.json()] == [pid]
    async with factory() as db:
        assert (await db.get(PlanGoal, gid)).enabled is True


async def test_a_legacy_scenario_is_not_projected(factory, w):
    pid, _ = await _seed_plan(factory, w["A"], "alice", legacy=True)
    with _client(factory, "alice") as alice:
        res = alice.get(f"/api/v1/scenarios/projection?plan_id={pid}")
    assert res.status_code == 422 and "not a plan" in res.text


async def test_projection_refuses_a_plan_outside_the_org_currency(factory, w):
    pid, _ = await _seed_plan(factory, w["A"], "alice")
    async with factory() as db:
        (await db.get(Scenario, pid)).currency = "USD"
        await db.commit()
    with _client(factory, "alice") as alice:
        res = alice.get(f"/api/v1/scenarios/projection?plan_id={pid}")
    assert res.status_code == 422, res.text


async def test_projection_refuses_an_org_without_a_currency_scope(factory, w):
    pid, _ = await _seed_plan(factory, w["legacy"], "dave")
    async with factory() as db:
        (await db.get(Scenario, pid)).currency = "EUR"
        await db.commit()
    with _client(factory, "dave") as dave:
        res = dave.get(f"/api/v1/scenarios/projection?plan_id={pid}")
    assert res.status_code == 422 and "currency scope" in res.text


async def test_projection_of_another_orgs_plan_beside_your_own_is_404(factory, w):
    mine, _ = await _seed_plan(factory, w["A"], "alice")
    theirs, _ = await _seed_plan(factory, w["B"], "carol")
    with _client(factory, "alice") as alice:
        res = alice.get(f"/api/v1/scenarios/projection?plan_id={mine}&plan_id={theirs}")
    assert res.status_code == 404


async def test_an_extreme_birth_year_still_projects(factory, w):
    pid, _ = await _seed_plan(factory, w["A"], "alice")
    with _client(factory, "alice") as alice:
        res = alice.get(f"/api/v1/scenarios/projection?plan_id={pid}&lifetime=true&birth_year=32767")
    assert res.status_code == 200, res.text
    assert len(res.json()["lifetime"]["months"]) == 1000
