"""Plans lifecycle (TBD-602): Draft -> Start -> Finish | Delete, edit after
start, budget rollover at period close, broken links.

- Every transition run twice leaves identical rows.
- Start creates the master category, one child per enabled goal, the tag (an
  existing one adopted), the funding series, the start balances, the current
  Budget and the frozen baseline; a refused Start writes nothing.
- Finish and Delete stop the series, keep the current Budget and all history,
  and drop the later Budgets.
- Progress is keyed on the goal's category and account, never on the series.
- Rollover: scheduler close, manual close and ``ensure_future_periods`` each
  give every healthy active plan the new period's Budget; a plan with a broken
  link needs attention and is skipped.

The concurrent-Start fence needs InnoDB row locks: ``test_plan_lifecycle_mysql.py``.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from dateutil.relativedelta import relativedelta
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.models import Base
from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.recurring import RecurringTransaction
from app.models.scenario import PlanGoal, Scenario, ScenarioStatus
from app.models.tag import Tag
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.schemas.recurring import RecurringCreate
from app.services import billing_service, plan_lifecycle_service, recurring_service
from app.services.scheduler.jobs.billing_close import BillingCloseJob
from tests.routers.test_plan_drafts import _client

pytestmark = pytest.mark.asyncio

TODAY = datetime.date.today()
PERIOD_START = billing_service.current_cycle_window(1, TODAY)[0]


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


@pytest_asyncio.fixture
async def w(factory) -> dict:
    """alice (a member, the creator) and bob (another member) in acme, carol
    in another org; checking, savings, a card and a dollar account; the open
    period of the current cycle."""
    async with factory() as db:
        org = Organization(name="acme", billing_cycle_day=1, primary_currency="EUR")
        other = Organization(name="other", billing_cycle_day=1, primary_currency="EUR")
        db.add_all([org, other])
        await db.flush()
        for name, o, role in (("alice", org, Role.MEMBER), ("bob", org, Role.MEMBER),
                              ("carol", other, Role.OWNER)):
            db.add(User(org_id=o.id, username=name, email=f"{name}@x.test", password_hash="x",
                        role=role, is_active=True, email_verified=True))
        types = {slug: AccountType(org_id=org.id, name=slug, slug=slug, is_system=True)
                 for slug in ("checking", "savings", "credit_card")}
        db.add_all(types.values())
        await db.flush()

        def acc(name, slug, balance, currency="EUR"):
            return Account(org_id=org.id, account_type_id=types[slug].id, name=name,
                           balance=Decimal(balance), currency=currency, is_active=True)

        accounts = {"main": acc("Main", "checking", "1000"), "savings": acc("Savings", "savings", "200"),
                    "pot": acc("Pot", "savings", "0"), "card": acc("Card", "credit_card", "0"),
                    "usd": acc("Dollars", "checking", "10", "USD")}
        db.add_all(accounts.values())
        db.add(BillingPeriod(org_id=org.id, start_date=PERIOD_START))
        await db.flush()
        # Pending on the savings account: part of its start balance, never progress.
        other_cat = Category(org_id=org.id, name="Other", type=CategoryType.BOTH)
        db.add(other_cat)
        await db.flush()
        db.add(Transaction(org_id=org.id, account_id=accounts["savings"].id, category_id=other_cat.id,
                           description="Interest", amount=Decimal(30), type=TransactionType.INCOME,
                           status=TransactionStatus.PENDING, date=TODAY))
        await db.commit()
        return {"org": org.id, "other_cat": other_cat.id, **{k: a.id for k, a in accounts.items()}}


TARGET = TODAY + relativedelta(months=11)
FLIGHTS_PERIOD = billing_service.current_cycle_window(1, TODAY + datetime.timedelta(days=90))[0]


def _periods(n: int) -> list[datetime.date]:
    return [PERIOD_START + relativedelta(months=i) for i in range(n)]


async def _budget_amounts(factory, org_id: int) -> dict[datetime.date, Decimal]:
    async with factory() as db:
        return {b.period_start: b.amount for b in (await db.scalars(
            select(Budget).where(Budget.org_id == org_id))).all()}


def _goals(w: dict) -> list[dict]:
    return [
        {"name": "Flights", "tier": "essential", "kind": "lump", "target_amount": "600.00",
         "target_date": (TODAY + datetime.timedelta(days=90)).isoformat()},
        {"name": "Food", "tier": "want", "kind": "monthly_spend", "monthly_amount": "100.00"},
        {"name": "Fund", "tier": "dream", "kind": "savings_target", "target_amount": "1000.00",
         "target_date": TARGET.isoformat(), "monthly_amount": "50.00", "account_id": w["savings"]},
        {"name": "Later", "tier": "dream", "kind": "monthly_spend", "monthly_amount": "10.00",
         "enabled": False},
    ]


def _draft(c, w: dict, *, name="Lisbon trip!", goals=None, **fields) -> int:
    body = {"name": name, "scenario_type": "custom", "horizon_months": 24,
            "params": {"scenario_type": "custom", "label": "x", "events": []},
            "monthly_budget": "300.00", "funding_account_id": w["main"], **fields}
    res = c.post("/api/v1/scenarios", json=body)
    assert res.status_code == 201, res.text
    pid = res.json()["id"]
    for g in _goals(w) if goals is None else goals:
        assert c.post(f"/api/v1/scenarios/{pid}/goals", json=g).status_code == 201
    return pid


_SKIP = {"created_at", "updated_at", "started_at", "finished_at", "first_seen_at"}


async def _rows(factory, org_id: int) -> dict:
    """Every row a transition can touch, as plain values."""
    out = {}
    async with factory() as db:
        for model in (Scenario, PlanGoal, Category, Tag, Budget, RecurringTransaction, Transaction):
            rows = (await db.scalars(select(model).where(model.org_id == org_id).order_by(model.id))).all()
            out[model.__tablename__] = [
                {c.key: getattr(r, c.key) for c in model.__mapper__.column_attrs if c.key not in _SKIP}
                for r in rows]
    return out


async def _goal(factory, pid: int, name: str) -> PlanGoal:
    async with factory() as db:
        return (await db.scalars(select(PlanGoal).where(
            PlanGoal.scenario_id == pid, PlanGoal.name == name))).one()


async def test_start_creates_one_set_of_rows_and_a_second_start_changes_nothing(factory, w):
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w)
        res = alice.post(f"/api/v1/scenarios/{pid}/start")
        assert res.status_code == 200, res.text
        plan = res.json()
        assert plan["status"] == "active" and plan["started_at"]
        assert plan["baseline_projection_json"]["currency"] == "EUR"
        once = await _rows(factory, w["org"])
        assert alice.post(f"/api/v1/scenarios/{pid}/start").status_code == 200
    assert await _rows(factory, w["org"]) == once

    async with factory() as db:
        row = await db.get(Scenario, pid)
        master = await db.get(Category, row.category_id)
        assert (master.name, master.parent_id, master.type) == ("Lisbon trip!", None, CategoryType.BOTH)
        tag = await db.get(Tag, row.tag_id)
        assert tag.name_normalized == "lisbon trip"
        budgets = (await db.scalars(select(Budget).where(Budget.org_id == w["org"]))).all()
        # TBD-802: the cap plus this period's Food payment; Flights falls in a later period.
        assert [(b.category_id, b.period_start, b.amount) for b in budgets] == [
            (master.id, PERIOD_START, Decimal("400.00"))]
        goals = {g.name: g for g in (await db.scalars(select(PlanGoal).where(PlanGoal.scenario_id == pid))).all()}
        series = {}
        for name in ("Flights", "Food", "Fund"):
            g = goals[name]
            child = await db.get(Category, g.category_id)
            assert (child.parent_id, child.name) == (master.id, name)
            series[name] = await db.get(RecurringTransaction, g.funding_recurring_id)
        # The disabled goal gets nothing.
        assert (goals["Later"].category_id, goals["Later"].funding_recurring_id) == (None, None)
        flights, food, fund = series["Flights"], series["Food"], series["Fund"]
        assert (flights.type, flights.amount, flights.account_id, flights.next_due_date,
                flights.occurrence_count) == ("expense", Decimal("600.00"), w["main"],
                                              TODAY + datetime.timedelta(days=90), 1)
        assert (food.type, food.amount, food.account_id, food.next_due_date, food.occurrence_count) == (
            "expense", Decimal("100.00"), w["main"], TODAY, None)
        # Today, then monthly through the target date: twelve contributions.
        assert (fund.type, fund.amount, fund.account_id, fund.to_account_id, fund.occurrence_count) == (
            "transfer", Decimal("50.00"), w["main"], w["savings"], 12)
        # Balance plus the pending delta.
        assert goals["Fund"].start_balance == Decimal("230.00")

    # Each later period: the cap and Food; the lump only in its own period.
    async with factory() as db:
        await billing_service.ensure_future_periods(db, w["org"], count=4)
    assert await _budget_amounts(factory, w["org"]) == {
        d: Decimal("1000.00") if d == FLIGHTS_PERIOD else Decimal("400.00")
        for d in _periods(5)}


async def test_start_adopts_an_existing_tag_of_the_same_normalised_name(factory, w):
    async with factory() as db:
        db.add(Tag(org_id=w["org"], name="Lisbon  Trip", name_normalized="lisbon trip"))
        await db.commit()
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w, goals=[])
        assert alice.post(f"/api/v1/scenarios/{pid}/start").status_code == 200
    async with factory() as db:
        tags = (await db.scalars(select(Tag).where(Tag.org_id == w["org"]))).all()
        assert len(tags) == 1 and (await db.get(Scenario, pid)).tag_id == tags[0].id


TAKEN = "plan_account_taken"


def _refusals(w: dict) -> dict[str, tuple[dict, list[dict], str | None, str]]:
    """case -> (plan fields, goals, the 409's code (None: a 422), what it says).
    An account another claim holds is a conflict (409); a wrong kind of
    account, or a missing one, is invalid input (422)."""
    fund = {"name": "Fund", "tier": "want", "kind": "savings_target", "monthly_amount": "10.00"}
    return {
        "funding account is a liability": (
            {"funding_account_id": w["card"]}, [], None, "funded from a credit card or loan"),
        "funding account is its own goal account": (
            {}, [{**fund, "account_id": w["main"]}], TAKEN, f"account {w['main']} is the goal account"),
        "goal account is a liability": (
            {}, [{**fund, "account_id": w["card"]}], None, "save into a credit card or loan"),
        "two enabled goals on one account": (
            {}, [{**fund, "account_id": w["pot"]}, {**fund, "name": "Two", "account_id": w["pot"]}],
            TAKEN, f"account {w['pot']} already backs another enabled goal"),
        "a transfer goal without an account": ({}, [fund], None, "needs an account to save into"),
        "goal account is another active plan's funding account": (
            {"funding_account_id": w["pot"]}, [{**fund, "account_id": w["main"]}],
            TAKEN, f"account {w['main']} funds an active plan"),
        "funding account is another active plan's goal account": (
            {"funding_account_id": w["savings"]}, [], TAKEN, f"account {w['savings']} is the goal account"),
        "the tag is another active plan's": ({"name": "Lisbon trip"}, [], "plan_tag_taken", "would be shared"),
    }


def _refused(res, code: str | None, says: str) -> bool:
    """A coded 409 (``{"code", "message"}``) or a plain 422 that says ``says``."""
    if code is None:
        return res.status_code == 422 and says in res.json()["detail"]
    detail = res.json()["detail"]
    return res.status_code == 409 and detail["code"] == code and says in detail["message"]


@pytest.mark.parametrize("case", list(_refusals({k: 0 for k in ("main", "card", "pot", "savings")})))
async def test_start_refuses_and_writes_nothing(factory, w, case):
    fields, goals, code, says = _refusals(w)[case]
    with _client(factory, "alice") as alice:
        # The other active plan: funded from main, saving into savings.
        first = _draft(alice, w)
        assert alice.post(f"/api/v1/scenarios/{first}/start").status_code == 200
        pid = _draft(alice, w, **{"name": "Second", **fields}, goals=goals)
        before = await _rows(factory, w["org"])
        res = alice.post(f"/api/v1/scenarios/{pid}/start")
        assert _refused(res, code, says), (case, res.text)
    assert await _rows(factory, w["org"]) == before


async def test_start_refuses_a_funding_account_in_another_currency(factory, w):
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w, goals=[])
        async with factory() as db:  # the link was valid when set; the account changed since
            (await db.get(Account, w["main"])).currency = "USD"
            await db.commit()
        before = await _rows(factory, w["org"])
        res = alice.post(f"/api/v1/scenarios/{pid}/start")
        assert res.status_code == 422 and "USD" in res.json()["detail"]
    assert await _rows(factory, w["org"]) == before


async def test_only_the_creator_or_an_admin_moves_the_lifecycle(factory, w):
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w, goals=[])
    for who, code in (("bob", 403), ("carol", 404)):
        with _client(factory, who) as c:
            for path in ("start", "finish"):
                assert c.post(f"/api/v1/scenarios/{pid}/{path}").status_code == code
            assert c.delete(f"/api/v1/scenarios/{pid}").status_code == code
    async with factory() as db:
        assert (await db.get(Scenario, pid)).status == ScenarioStatus.DRAFT


async def test_finish_and_start_refuse_the_wrong_state(factory, w):
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w, goals=[])
        assert alice.post(f"/api/v1/scenarios/{pid}/finish").status_code == 409
        assert alice.post(f"/api/v1/scenarios/{pid}/start").status_code == 200
        assert alice.post(f"/api/v1/scenarios/{pid}/finish").status_code == 200
        assert alice.post(f"/api/v1/scenarios/{pid}/start").status_code == 409


async def _add_future_period_and_history(factory, w, pid: int) -> None:
    """A stub period after the current one (its Budget comes with it) and a
    settled purchase in a goal's category."""
    async with factory() as db:
        await billing_service.ensure_future_periods(db, w["org"], count=1)
    food = await _goal(factory, pid, "Food")
    async with factory() as db:
        db.add(Transaction(org_id=w["org"], account_id=w["main"], category_id=food.category_id,
                           description="Market", amount=Decimal(40), type=TransactionType.EXPENSE,
                           status=TransactionStatus.SETTLED, date=TODAY, settled_date=TODAY))
        await db.commit()


async def _budget_periods(factory, org_id: int) -> list[datetime.date]:
    async with factory() as db:
        return list((await db.scalars(select(Budget.period_start).where(Budget.org_id == org_id)
                                      .order_by(Budget.period_start))).all())


async def test_finish_stops_the_series_keeps_the_current_budget_and_history(factory, w):
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w)
        alice.post(f"/api/v1/scenarios/{pid}/start")
        await _add_future_period_and_history(factory, w, pid)
        assert len(await _budget_periods(factory, w["org"])) == 2
        res = alice.post(f"/api/v1/scenarios/{pid}/finish")
        assert res.status_code == 200 and res.json()["status"] == "finished"
        once = await _rows(factory, w["org"])
        assert alice.post(f"/api/v1/scenarios/{pid}/finish").status_code == 200
    assert await _rows(factory, w["org"]) == once
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START]
    assert all(not r["is_active"] for r in once["recurring_transactions"])
    assert [t["description"] for t in once["transactions"]] == ["Interest", "Market"]
    # A finished plan is a read-only record.
    with _client(factory, "alice") as alice:
        res = alice.patch(f"/api/v1/scenarios/{pid}?confirm=true", json={"name": "x"})
        assert res.status_code == 409


async def test_delete_of_a_started_plan_keeps_its_rows_as_ordinary_rows(factory, w):
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w)
        alice.post(f"/api/v1/scenarios/{pid}/start")
        await _add_future_period_and_history(factory, w, pid)
        before = await _rows(factory, w["org"])
        assert alice.delete(f"/api/v1/scenarios/{pid}").status_code == 204
    after = await _rows(factory, w["org"])
    assert after["scenarios"] == [] and after["plan_goals"] == []
    for table in ("categories", "tags", "transactions"):
        assert after[table] == before[table], table
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START]
    assert [r["id"] for r in after["recurring_transactions"]] == [
        r["id"] for r in before["recurring_transactions"]]
    assert all(not r["is_active"] for r in after["recurring_transactions"])
    async with factory() as db:
        assert (await db.get(Account, w["savings"])) is not None


async def test_delete_of_a_draft_deletes_only_the_plan_and_its_goals(factory, w):
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w)
        before = await _rows(factory, w["org"])
        assert alice.delete(f"/api/v1/scenarios/{pid}").status_code == 204
    after = await _rows(factory, w["org"])
    assert after["scenarios"] == [] and after["plan_goals"] == []
    assert {k: v for k, v in after.items() if k not in ("scenarios", "plan_goals")} == {
        k: v for k, v in before.items() if k not in ("scenarios", "plan_goals")}


async def test_progress_is_keyed_on_category_and_account_and_survives_stopping_the_series(factory, w):
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w)
        alice.post(f"/api/v1/scenarios/{pid}/start")
        fund, food = await _goal(factory, pid, "Fund"), await _goal(factory, pid, "Food")
        async with factory() as db:
            # The series' first contribution settled, and the pending interest landed.
            savings = await db.get(Account, w["savings"])
            savings.balance += Decimal(50) + Decimal(30)
            db.add(Transaction(org_id=w["org"], account_id=w["savings"], category_id=fund.category_id,
                               description="Fund", amount=Decimal(50), type=TransactionType.INCOME,
                               status=TransactionStatus.SETTLED, date=TODAY, settled_date=TODAY,
                               recurring_id=fund.funding_recurring_id))
            db.add(Transaction(org_id=w["org"], account_id=w["main"], category_id=food.category_id,
                               description="Market", amount=Decimal(40), type=TransactionType.EXPENSE,
                               status=TransactionStatus.SETTLED, date=TODAY, settled_date=TODAY,
                               recurring_id=food.funding_recurring_id))
            await db.commit()
        first = alice.get(f"/api/v1/scenarios/{pid}/progress").json()
        by_goal = {g["goal_id"]: g for g in first["goals"]}
        assert by_goal[fund.id]["saved"] == "50.00"
        assert by_goal[food.id]["spent"] == "40.00"
        async with factory() as db:  # the user stops both series themselves
            for g in (fund, food):
                await recurring_service.stop_recurring(db, w["org"], g.funding_recurring_id)
        again = alice.get(f"/api/v1/scenarios/{pid}/progress").json()
    assert again["goals"] == first["goals"]
    assert again["needs_attention"]  # a stopped series is a broken link


async def _start(factory, w) -> int:
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w)
        assert alice.post(f"/api/v1/scenarios/{pid}/start").status_code == 200
    return pid


def _silence(monkeypatch):
    from tests.services.test_scheduler_job_billing_close import _silence_side_effects
    _silence_side_effects(monkeypatch)


async def test_rollover_covers_scheduler_close_manual_close_and_ensure_future_periods(factory, w, monkeypatch):
    await _start(factory, w)
    nxt = PERIOD_START + relativedelta(months=1)
    async with factory() as db:  # scheduler
        _silence(monkeypatch)
        org = await db.get(Organization, w["org"])
        assert (await BillingCloseJob().run(db, org, nxt + datetime.timedelta(days=2))).outcome == "success"
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START, nxt]
    async with factory() as db:  # manual: the settings route's call, with a date
        await billing_service.close_period(db, w["org"], nxt + relativedelta(months=1) - datetime.timedelta(days=1),
                                           today=nxt + relativedelta(months=1))
    third = nxt + relativedelta(months=1)
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START, nxt, third]
    async with factory() as db:
        await billing_service.ensure_future_periods(db, w["org"], count=2)
        await billing_service.ensure_future_periods(db, w["org"], count=2)
    assert await _budget_periods(factory, w["org"]) == [
        PERIOD_START, nxt, third, third + relativedelta(months=1), third + relativedelta(months=2)]


async def test_rollover_writes_no_budget_while_the_org_is_not_entitled_to_plans(factory, w, monkeypatch):
    """TBD-802 (a): a downgraded org cannot reach its plan (every route is
    gated), so the rollover must not keep writing its Budgets. The next
    ``ensure_future_periods`` after the entitlement returns fills them in."""
    import dataclasses

    from app.services import feature_service

    await _start(factory, w)
    entitled = feature_service.get_entitlements

    async def downgraded(db, org_id, **kwargs):
        ent = await entitled(db, org_id, **kwargs)
        return dataclasses.replace(ent, features={**ent.features, "plans": False})

    monkeypatch.setattr(feature_service, "get_entitlements", downgraded)
    nxt = PERIOD_START + relativedelta(months=1)
    async with factory() as db:
        await billing_service.close_period(db, w["org"], nxt - datetime.timedelta(days=1), today=nxt)
        await billing_service.ensure_future_periods(db, w["org"], count=1)
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START]

    monkeypatch.setattr(feature_service, "get_entitlements", entitled)
    async with factory() as db:
        await billing_service.ensure_future_periods(db, w["org"], count=1)
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START, nxt, nxt + relativedelta(months=1)]


async def test_a_plan_without_a_cap_gets_a_budget_for_its_spend_goals_only(factory, w, monkeypatch):
    """TBD-802 (b), architects' ruling: no ``monthly_budget`` but a spend goal
    is still a Budget, 0 in a period with nothing due (a skipped row would
    leave the plan owed forever, so every Budgets visit would lock); no cap
    and no spend goal is none."""
    with _client(factory, "alice") as alice:
        lump = _draft(alice, w, monthly_budget=None, goals=[_goals(w)[0]])
        bare = _draft(alice, w, name="Bare", monthly_budget=None, goals=[])
        assert alice.post(f"/api/v1/scenarios/{lump}/start").status_code == 200
        assert alice.post(f"/api/v1/scenarios/{bare}/start").status_code == 200
    async with factory() as db:
        await billing_service.ensure_future_periods(db, w["org"], count=4)
        master = (await db.get(Scenario, lump)).category_id
        assert {b.category_id for b in (await db.scalars(select(Budget))).all()} == {master}
    assert await _budget_amounts(factory, w["org"]) == {
        d: Decimal("600.00") if d == FLIGHTS_PERIOD else Decimal("0.00") for d in _periods(5)}
    calls = []
    real = plan_lifecycle_service.lock_plans

    async def spy(db, org_id, **k):
        calls.append(org_id)
        return await real(db, org_id, **k)

    monkeypatch.setattr(plan_lifecycle_service, "lock_plans", spy)
    async with factory() as db:
        await billing_service.ensure_future_periods(db, w["org"], count=4)
    assert calls == []


async def test_a_lump_budgets_where_its_series_pays_after_the_date_moves(factory, w):
    """TBD-802 review: the Recurring page can move a lump's payment; the
    Budget follows the series, not the goal's ``target_date``."""
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w, monthly_budget=None, goals=[_goals(w)[0]])
        assert alice.post(f"/api/v1/scenarios/{pid}/start").status_code == 200
    flights = await _goal(factory, pid, "Flights")
    moved = FLIGHTS_PERIOD + relativedelta(months=1)
    async with factory() as db:
        (await db.get(RecurringTransaction, flights.funding_recurring_id)).next_due_date = moved
        await db.commit()
        await billing_service.ensure_future_periods(db, w["org"], count=5)
    amounts = await _budget_amounts(factory, w["org"])
    assert {d for d, a in amounts.items() if a} == {moved} and amounts[moved] == Decimal("600.00")


def test_a_monthly_goal_counts_the_payments_its_series_makes_in_the_window():
    """TBD-802 (b), both architects: count the series' payments, not "target
    on or after the period start". Started Jan 20 with a Mar 10 target: Jan 20
    paid, Feb 20 due, then nothing; March has no payment."""
    from types import SimpleNamespace

    d = datetime.date
    series = SimpleNamespace(next_due_date=d(2027, 2, 20), occurrence_count=2, occurrences_elapsed=1,
                             last_spent_date=d(2027, 1, 20))
    payments = plan_lifecycle_service._payments
    assert [payments(series, d(2027, m, 1), d(2027, m + 1, 1) - datetime.timedelta(days=1))
            for m in (1, 2, 3)] == [1, 1, 0]
    open_ended = SimpleNamespace(next_due_date=d(2027, 2, 20), occurrence_count=None,
                                 occurrences_elapsed=1, last_spent_date=d(2027, 1, 20))
    assert payments(open_ended, d(2027, 5, 1), d(2027, 5, 31)) == 1
    # A lump is a one-payment series: counted where its series pays, before and after.
    lump = SimpleNamespace(next_due_date=d(2027, 3, 12), occurrence_count=1, occurrences_elapsed=0,
                           last_spent_date=None)
    paid = SimpleNamespace(next_due_date=d(2027, 4, 12), occurrence_count=1, occurrences_elapsed=1,
                           last_spent_date=d(2027, 3, 12))
    for s in (lump, paid):
        assert [payments(s, d(2027, m, 1), d(2027, m + 1, 1) - datetime.timedelta(days=1))
                for m in (2, 3, 4)] == [0, 1, 0]


def test_the_roster_tail_budget_ends_where_its_billing_cycle_does():
    """TBD-802 review: an open period with nothing after it is bounded by its
    cycle, not by a month from its start (cycle day 31 from Feb 28 runs to Mar
    30, so a payment on Mar 28 or 29 still counts), and never unbounded."""
    from types import SimpleNamespace

    d = datetime.date
    tail = [SimpleNamespace(start_date=d(2027, 2, 28), end_date=None)]
    assert plan_lifecycle_service._window_end(tail, 0, 31) == d(2027, 3, 30)
    roster = [SimpleNamespace(start_date=d(2027, 2, 28), end_date=None),
              SimpleNamespace(start_date=d(2027, 3, 31), end_date=d(2027, 4, 29))]
    assert [plan_lifecycle_service._window_end(roster, i, 31) for i in (0, 1)] == [
        d(2027, 3, 30), d(2027, 4, 29)]
    # Off the grid (a close on Jan 9 with cycle day 15 opens Jan 10): the next
    # stub starts Feb 15, so this window runs to Feb 14, not Jan 14.
    off = [SimpleNamespace(start_date=d(2027, 1, 10), end_date=None)]
    assert plan_lifecycle_service._window_end(off, 0, 15) == d(2027, 2, 14)


async def test_a_broken_link_needs_attention_and_rollover_skips_the_plan(factory, w):
    pid = await _start(factory, w)
    food = await _goal(factory, pid, "Food")
    async with factory() as db:
        other = Category(org_id=w["org"], name="Elsewhere", type=CategoryType.BOTH)
        db.add(other)
        await db.flush()
        (await db.get(Category, food.category_id)).parent_id = other.id  # re-parented
        await db.commit()
    with _client(factory, "alice") as alice:
        attention = alice.get(f"/api/v1/scenarios/{pid}/progress").json()["needs_attention"]
    assert attention == [f"goal {food.id}: its category is no longer under the plan's category"]
    async with factory() as db:
        await billing_service.ensure_future_periods(db, w["org"], count=1)
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START]


async def test_a_deleted_tag_or_account_needs_attention(factory, w):
    pid = await _start(factory, w)
    async with factory() as db:
        row = await db.get(Scenario, pid)
        await db.delete(await db.get(Tag, row.tag_id))
        row.funding_account_id = None  # the account's FK is ON DELETE SET NULL
        await db.commit()
    async with factory() as db:
        reasons = await plan_lifecycle_service.attention(db, await db.get(Scenario, pid))
    assert reasons[:2] == ["the plan's tag is gone", "the plan's funding account is gone"]


async def test_edit_after_start_needs_confirmation_and_runs_starts_steps(factory, w):
    pid = await _start(factory, w)
    past = PERIOD_START - relativedelta(months=1)
    async with factory() as db:  # a later stub with its Budget, and a past period's Budget (history)
        await billing_service.ensure_future_periods(db, w["org"], count=1)
        db.add(BillingPeriod(org_id=w["org"], start_date=past, end_date=PERIOD_START - datetime.timedelta(days=1)))
        db.add(Budget(org_id=w["org"], category_id=(await db.get(Scenario, pid)).category_id,
                      amount=Decimal("1.00"), period_start=past, period_end=PERIOD_START - datetime.timedelta(days=1)))
        await db.commit()
    with _client(factory, "alice") as alice:
        res = alice.patch(f"/api/v1/scenarios/{pid}", json={"monthly_budget": "400.00"})
        assert res.status_code == 409 and "confirm" in res.json()["detail"]
        assert alice.patch(f"/api/v1/scenarios/{pid}?confirm=true",
                           json={"monthly_budget": "400.00"}).status_code == 200
        assert alice.patch(f"/api/v1/scenarios/{pid}?confirm=true",
                           json={"funding_account_id": w["pot"]}).status_code == 409
        # Hiding a running plan waits for Archive (TBD-606): the rollover keys on status.
        assert alice.patch(f"/api/v1/scenarios/{pid}?confirm=true",
                           json={"is_active": False}).status_code == 409
        before = await _rows(factory, w["org"])
        new = {"name": "Hotel", "tier": "want", "kind": "monthly_spend", "monthly_amount": "70.00"}
        assert alice.post(f"/api/v1/scenarios/{pid}/goals", json=new).status_code == 409
        res = alice.post(f"/api/v1/scenarios/{pid}/goals?confirm=true", json=new)
        assert res.status_code == 201, res.text
        hotel = res.json()
        later = await _goal(factory, pid, "Later")
        assert alice.patch(f"/api/v1/scenarios/{pid}/goals/{later.id}?confirm=true",
                           json={"monthly_amount": "1.00"}).status_code == 409
        assert alice.patch(f"/api/v1/scenarios/{pid}/goals/{later.id}?confirm=true",
                           json={"enabled": True}).status_code == 200
        assert alice.delete(f"/api/v1/scenarios/{pid}/goals/{later.id}").status_code == 409
    # TBD-802: the new cap, Food, and the two goals started here (Hotel 70, Later 10),
    # in the open period and the stub after it; the past Budget is history.
    assert await _budget_amounts(factory, w["org"]) == {
        past: Decimal("1.00"), PERIOD_START: Decimal("580.00"), _periods(2)[1]: Decimal("580.00")}
    after = await _rows(factory, w["org"])
    # Only the two goals' rows are new: a child category and a series each.
    assert len(after["categories"]) == len(before["categories"]) + 2
    assert len(after["recurring_transactions"]) == len(before["recurring_transactions"]) + 2
    for gid in (hotel["id"], later.id):
        g = next(x for x in after["plan_goals"] if x["id"] == gid)
        assert g["category_id"] and g["funding_recurring_id"]
    assert [r for r in after["recurring_transactions"] if r["id"] in {
        x["id"] for x in before["recurring_transactions"]}] == before["recurring_transactions"]


async def test_a_failing_rollover_never_fails_the_close(factory, w, monkeypatch):
    """GUARD. The rollover runs after the close has committed: an error in it
    is logged and rolled back, the close and the stubs stand, and the next
    ``ensure_future_periods`` fills the Budget in."""
    await _start(factory, w)
    real = plan_lifecycle_service.attention

    async def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(plan_lifecycle_service, "attention", broken)
    nxt = PERIOD_START + relativedelta(months=1)
    async with factory() as db:
        opened = await billing_service.close_period(db, w["org"], nxt - datetime.timedelta(days=1), today=nxt)
        assert opened.start_date == nxt
        stubs = await billing_service.ensure_future_periods(db, w["org"], count=1)
        assert [s.start_date for s in stubs] == [nxt + relativedelta(months=1)]
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START]
    monkeypatch.setattr(plan_lifecycle_service, "attention", real)
    async with factory() as db:
        await billing_service.ensure_future_periods(db, w["org"], count=1)
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START, nxt, nxt + relativedelta(months=1)]


async def test_start_refuses_a_linked_category_that_already_has_budgets(factory, w):
    """FENCE (review of #1120, finding 2). Budgets key on (org, category,
    period), so a plan on a category the user already budgets would adopt
    those Budgets: overwrite them on a budget edit, delete the later ones on
    Finish. Start refuses it; the user's Budgets are untouched."""
    nxt = PERIOD_START + relativedelta(months=1)
    async with factory() as db:
        await billing_service.ensure_future_periods(db, w["org"], count=1)
        travel = Category(org_id=w["org"], name="Travel", type=CategoryType.BOTH)
        db.add(travel)
        await db.flush()
        for start in (PERIOD_START, nxt):
            db.add(Budget(org_id=w["org"], category_id=travel.id, amount=Decimal(900), period_start=start))
        await db.commit()
        travel_id = travel.id
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w, category_id=travel_id, monthly_budget="50.00")
        before = await _rows(factory, w["org"])
        res = alice.post(f"/api/v1/scenarios/{pid}/start")
        assert _refused(res, "plan_category_taken", "link a fresh category"), res.text
    assert await _rows(factory, w["org"]) == before
    async with factory() as db:
        assert [(b.period_start, b.amount) for b in (await db.scalars(
            select(Budget).where(Budget.category_id == travel_id).order_by(Budget.period_start))).all()] == [
            (PERIOD_START, Decimal("900.00")), (nxt, Decimal("900.00"))]


async def test_start_refuses_another_started_plans_category(factory, w):
    first = await _start(factory, w)
    async with factory() as db:
        taken = (await db.get(Scenario, first)).category_id
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w, name="Second", category_id=taken, goals=[], monthly_budget=None)
        res = alice.post(f"/api/v1/scenarios/{pid}/start")
        assert _refused(res, "plan_category_taken", f"plan {first}"), res.text


async def test_a_database_error_in_the_rollover_leaves_the_close_standing(factory, w, monkeypatch, caplog):
    """FENCE (review of #1120, finding 3). A deadlock rolls the whole
    transaction back, savepoint and all; the rollover's commit failing must
    not fail a close that has committed, and the session stays usable."""
    from sqlalchemy.exc import OperationalError

    await _start(factory, w)
    real = plan_lifecycle_service._ensure_budgets

    async def then_fail_the_commit(db, *a, **k):
        await real(db, *a, **k)

        async def deadlock():
            del db.commit  # once
            raise OperationalError("COMMIT", {}, Exception("1213 Deadlock found"))
        db.commit = deadlock

    monkeypatch.setattr(plan_lifecycle_service, "_ensure_budgets", then_fail_the_commit)
    nxt = PERIOD_START + relativedelta(months=1)
    async with factory() as db:
        opened = await billing_service.close_period(db, w["org"], nxt - datetime.timedelta(days=1), today=nxt)
        assert opened.start_date == nxt
        assert (await billing_service.find_open_period(db, w["org"])).id == opened.id  # still usable
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START]
    failed = [r for r in caplog.records if r.getMessage() == "plans.rollover.failed"]
    assert [(r.levelname, bool(r.exc_info)) for r in failed] == [("ERROR", True)]


async def test_ensure_future_periods_commits_and_locks_only_when_something_changed(factory, w, monkeypatch):
    """GUARD (review of #1120, finding 4). A Budgets page visit that adds no
    stub and owes no plan Budget neither commits nor locks the plans."""
    await _start(factory, w)
    async with factory() as db:
        await billing_service.ensure_future_periods(db, w["org"], count=1)
    calls = []
    real_lock = plan_lifecycle_service.lock_plans

    async def spy_lock(db, org_id, **k):
        calls.append("lock")
        return await real_lock(db, org_id, **k)

    monkeypatch.setattr(plan_lifecycle_service, "lock_plans", spy_lock)
    async with factory() as db:
        real_commit = db.commit

        async def spy_commit():
            calls.append("commit")
            await real_commit()

        db.commit = spy_commit
        assert await billing_service.ensure_future_periods(db, w["org"], count=1) == []
    assert calls == []


async def test_a_goal_delete_locks_the_plan_first(factory, w, monkeypatch):
    """GUARD (review of #1120, finding 6): as POST and PATCH, so it cannot
    race a Start; the draft check runs on the locked row."""
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w)
        later = await _goal(factory, pid, "Later")
        seen = []
        real = plan_lifecycle_service.lock

        async def spy(db, plan):
            row = await real(db, plan)
            seen.append(row.status)
            return row

        monkeypatch.setattr(plan_lifecycle_service, "lock", spy)
        assert alice.delete(f"/api/v1/scenarios/{pid}/goals/{later.id}").status_code == 204
    assert seen == [ScenarioStatus.DRAFT]


async def test_a_tag_insert_lost_to_a_concurrent_start_is_a_coded_409(factory, w, monkeypatch):
    """GUARD (review of #1120, finding 7). The plan locks serialise Starts, so
    the loser normally sees the winner's tag; should its insert still collide
    (the unique key on the normalised name), it is the taken-tag 409, not a 500."""
    from app.services import tag_service

    async with factory() as db:
        db.add(Tag(org_id=w["org"], name="lisbon trip", name_normalized="lisbon trip"))
        await db.commit()

    async def unseen(*a, **k):
        return None

    monkeypatch.setattr(plan_lifecycle_service, "_find_tag", unseen)
    monkeypatch.setattr(tag_service, "_get_tag_by_normalized", unseen)
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w, goals=[])
        res = alice.post(f"/api/v1/scenarios/{pid}/start")
        assert _refused(res, "plan_tag_taken", "lisbon trip"), res.text


def test_the_instalment_count_steps_as_generation_does():
    """GUARD (review of #1120, finding 8). Generation steps a month-end series
    Jan 31 -> Feb 28 -> Mar 28, so a Mar 30 target gets three contributions;
    closed-form months from Jan 31 (Mar 31) would count two."""
    plan = Scenario(funding_account_id=1)
    goal = PlanGoal(kind="savings_target", monthly_amount=Decimal(10), account_id=2,
                    target_date=datetime.date(2027, 3, 30))
    spec = plan_lifecycle_service._series_spec(plan, goal, datetime.date(2027, 1, 31))
    assert spec["occurrence_count"] == 3


async def test_a_failed_rollover_does_not_break_the_scheduler_close(factory, w, monkeypatch):
    """FENCE (second review of #1120, finding B). The rollover's rollback
    used to expire every object of the session, so the job's next
    ``is_due(org)`` hit a lazy load (MissingGreenlet): the run raised, losing
    its audit row and notification, after one step of a catch-up. With the
    commit failing once, the job closes both periods, audits and notifies."""
    from sqlalchemy.exc import OperationalError

    from app.services.scheduler.jobs import billing_close

    await _start(factory, w)
    nxt = PERIOD_START + relativedelta(months=1)
    async with factory() as db:
        # A stub makes the job close one period per step; its Budget removed,
        # the first step's rollover owes one, and that is the commit that fails.
        await billing_service.ensure_future_periods(db, w["org"], count=1)
        await db.execute(delete(Budget).where(Budget.period_start == nxt))
        await db.commit()
    real = plan_lifecycle_service._ensure_budgets
    failed = []

    async def then_fail_the_commit(db, *a, **k):
        added = await real(db, *a, **k)
        if not failed:
            async def deadlock():
                del db.commit  # once
                failed.append(True)
                raise OperationalError("COMMIT", {}, Exception("1213 Deadlock found"))
            db.commit = deadlock
        return added

    monkeypatch.setattr(plan_lifecycle_service, "_ensure_budgets", then_fail_the_commit)
    sent = []

    async def audit(**k):
        sent.append("audit")
        return 1

    async def notify(*a, **k):
        sent.append("notify")
        return 1

    monkeypatch.setattr(billing_close, "record_run", audit)
    monkeypatch.setattr(billing_close, "dispatch_notification_to_org_members", notify)
    third = PERIOD_START + relativedelta(months=2)
    async with factory() as db:
        org = await db.get(Organization, w["org"])
        result = await BillingCloseJob().run(db, org, third + datetime.timedelta(days=2))
    assert (result.outcome, result.counts["steps"], failed, sent) == ("success", 2, [True], ["audit", "notify"])
    async with factory() as db:
        assert (await billing_service.find_open_period(db, w["org"])).start_date == third
    # The failed step's Budget is lost; the second close's rollover gave the next one.
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START, third]


async def test_a_plan_that_needs_attention_is_not_owed_a_budget(factory, w, monkeypatch):
    """GUARD (second review of #1120, nit). The rollover skips such a plan, so
    it must not make a Budgets page visit take every plan lock for nothing."""
    pid = await _start(factory, w)
    food = await _goal(factory, pid, "Food")
    async with factory() as db:
        (await db.get(Category, food.category_id)).parent_id = w["other_cat"]
        await db.commit()
    calls = []
    real = plan_lifecycle_service.lock_plans

    async def spy(db, org_id, **k):
        calls.append(org_id)
        return await real(db, org_id, **k)

    monkeypatch.setattr(plan_lifecycle_service, "lock_plans", spy)
    async with factory() as db:
        assert len(await billing_service.ensure_future_periods(db, w["org"], count=1)) == 1
    assert calls == [] and await _budget_periods(factory, w["org"]) == [PERIOD_START]


async def test_start_refuses_a_series_the_user_linked_to_a_goal(factory, w):
    """Ruling on #1120: Start makes each goal its own series. Adopting the
    user's series would flag the plan at once (its category is not the goal's),
    and Finish or Delete would stop it."""
    async with factory() as db:
        mine = await recurring_service.create_recurring_no_commit(db, w["org"], RecurringCreate(
            type="transfer", account_id=w["main"], to_account_id=w["savings"], amount=Decimal("500.00"),
            category_id=w["other_cat"], frequency="monthly", next_due_date=TODAY,
            description="My savings"))
        await db.commit()
        sid = mine.id
    fund = {**_goals(w)[2], "funding_recurring_id": sid}
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w, goals=[fund])
        before = await _rows(factory, w["org"])
        res = alice.post(f"/api/v1/scenarios/{pid}/start")
        assert _refused(res, "plan_series_linked", "unlink"), res.text
    assert await _rows(factory, w["org"]) == before


async def test_delete_of_a_finished_plan_leaves_what_the_user_did_after_finish(factory, w):
    """Ruling on #1120: Finish already wound the plan down. A series the user
    resumed, and a Budget they set later, are theirs."""
    pid = await _start(factory, w)
    with _client(factory, "alice") as alice:
        assert alice.post(f"/api/v1/scenarios/{pid}/finish").status_code == 200
    food = await _goal(factory, pid, "Food")
    async with factory() as db:
        await billing_service.ensure_future_periods(db, w["org"], count=1)
        series = await db.get(RecurringTransaction, food.funding_recurring_id)
        series.is_active = True
        plan = await db.get(Scenario, pid)
        later = max(p.start_date for p in (await db.scalars(select(BillingPeriod))).all())
        db.add(Budget(org_id=w["org"], category_id=plan.category_id, amount=Decimal(250), period_start=later))
        await db.commit()
    with _client(factory, "alice") as alice:
        assert alice.delete(f"/api/v1/scenarios/{pid}").status_code == 204
    async with factory() as db:
        assert (await db.get(RecurringTransaction, food.funding_recurring_id)).is_active
    assert later in await _budget_periods(factory, w["org"])


# ── TBD-803 item 1: lock() refuses a write already sent in this transaction ──


async def _a_plan(factory) -> int:
    async with factory() as db:
        org = Organization(name="locks", billing_cycle_day=1, primary_currency="EUR")
        db.add(org)
        await db.flush()
        user = User(org_id=org.id, username="lk", email="lk@x.test", password_hash="x",
                    role=Role.MEMBER, is_active=True, email_verified=True)
        db.add(user)
        await db.flush()
        plan = Scenario(org_id=org.id, user_id=user.id, name="P", scenario_type="custom",
                        params_json={"scenario_type": "custom", "label": "x", "events": []}, currency="EUR")
        db.add(plan)
        await db.commit()
        return plan.id


async def test_lock_refuses_a_flushed_orm_write(factory):
    """F1a. Wrong implementation: a guard on new/dirty/deleted only (a flush empties them)."""
    pid = await _a_plan(factory)
    async with factory() as db:
        plan = await db.get(Scenario, pid)
        plan.name = "changed"
        await db.flush()
        with pytest.raises(RuntimeError):
            await plan_lifecycle_service.lock(db, plan)
        assert (await db.get(Scenario, pid)).name == "changed"  # still in the transaction


async def test_lock_refuses_an_orm_update_statement(factory):
    """F1b. Wrong implementation: only an after_flush listener (Core-style ORM DML never flushes)."""
    pid = await _a_plan(factory)
    async with factory() as db:
        plan = await db.get(Scenario, pid)
        await db.execute(update(Scenario).where(Scenario.id == pid).values(name="changed"))
        with pytest.raises(RuntimeError):
            await plan_lifecycle_service.lock(db, plan)


async def test_lock_is_fine_after_the_write_committed(factory):
    """F1c. Wrong implementation: an unkeyed or sticky flag."""
    pid = await _a_plan(factory)
    async with factory() as db:
        plan = await db.get(Scenario, pid)
        plan.name = "changed"
        await db.flush()
        await db.commit()
        await plan_lifecycle_service.lock(db, plan)  # no read after the commit
    async with factory() as db:
        plan = await db.get(Scenario, pid)
        plan.name = "again"
        await db.flush()
        await db.commit()
        await db.scalar(select(Scenario.id))  # a new transaction, read only
        await plan_lifecycle_service.lock(db, plan)


async def test_a_second_lock_keeps_its_locks_over_a_write_after_the_first(factory):
    """F1d. Wrong implementation: the check outside lock()'s first-call branch."""
    pid = await _a_plan(factory)
    async with factory() as db:
        plan = await plan_lifecycle_service.lock(db, await db.get(Scenario, pid))
        plan.name = "changed"
        await db.flush()
        await plan_lifecycle_service.lock(db, plan)


async def test_lock_on_a_fresh_session_is_fine(factory):
    """F1e. Wrong implementation: a guard without ``held_in is not None`` (None is None)."""
    pid = await _a_plan(factory)
    async with factory() as other:
        org_id = await other.scalar(select(Scenario.org_id).where(Scenario.id == pid))
    async with factory() as db:  # no transaction begun yet
        await plan_lifecycle_service.lock(db, SimpleNamespace(org_id=org_id, id=pid))


# ── TBD-803 item 4: a past-dated lump is refused in plan words ──

PAST = PERIOD_START - datetime.timedelta(days=1)


def _lump(target: datetime.date, **extra) -> dict:
    return {"name": "Flights", "tier": "essential", "kind": "lump", "target_amount": "600.00",
            "target_date": target.isoformat(), **extra}


async def _counts(factory, org_id: int) -> list[int]:
    async with factory() as db:
        return [await db.scalar(select(func.count()).select_from(m).where(m.org_id == org_id))
                for m in (Category, RecurringTransaction, Budget)]


async def test_start_refuses_a_lump_dated_before_the_cycle_started(factory, w):
    """F4a."""
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w, goals=[_lump(PAST)])
        before = await _counts(factory, w["org"])
        res = alice.post(f"/api/v1/scenarios/{pid}/start")
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert "Flights" in detail and PERIOD_START.isoformat() in detail and "next_due_date" not in detail
    async with factory() as db:
        assert (await db.get(Scenario, pid)).status == ScenarioStatus.DRAFT
    assert await _counts(factory, w["org"]) == before


async def test_start_accepts_a_lump_dated_on_the_cycle_start(factory, w):
    """F4b. Wrong implementation: ``<=``, or a check against today."""
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w, goals=[_lump(PERIOD_START)])
        assert alice.post(f"/api/v1/scenarios/{pid}/start").status_code == 200


async def test_enabling_a_past_dated_lump_on_an_active_plan_is_refused(factory, w):
    """F4c. Wrong implementation: the check in start() only."""
    with _client(factory, "alice") as alice:
        pid = _draft(alice, w, goals=[_lump(PAST, enabled=False)])
        assert alice.post(f"/api/v1/scenarios/{pid}/start").status_code == 200
        gid = (await _goal(factory, pid, "Flights")).id
        res = alice.patch(f"/api/v1/scenarios/{pid}/goals/{gid}?confirm=true", json={"enabled": True})
    assert res.status_code == 422, res.text
    assert "Flights" in res.json()["detail"] and "next_due_date" not in res.json()["detail"]
