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
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from dateutil.relativedelta import relativedelta
from sqlalchemy import select
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
        assert [(b.category_id, b.period_start, b.amount) for b in budgets] == [
            (master.id, PERIOD_START, Decimal("300.00"))]
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


def _refusals(w: dict) -> dict[str, tuple[dict, list[dict], str]]:
    """case -> (plan fields, goals, what the refusal says)."""
    fund = {"name": "Fund", "tier": "want", "kind": "savings_target", "monthly_amount": "10.00"}
    return {
        "funding account is a liability": (
            {"funding_account_id": w["card"]}, [], "funded from a credit card or loan"),
        "funding account is its own goal account": (
            {}, [{**fund, "account_id": w["main"]}], f"account {w['main']} is the goal account"),
        "goal account is a liability": (
            {}, [{**fund, "account_id": w["card"]}], "save into a credit card or loan"),
        "two enabled goals on one account": (
            {}, [{**fund, "account_id": w["pot"]}, {**fund, "name": "Two", "account_id": w["pot"]}],
            f"account {w['pot']} already backs another enabled goal"),
        "a transfer goal without an account": ({}, [fund], "needs an account to save into"),
        "goal account is another active plan's funding account": (
            {"funding_account_id": w["pot"]}, [{**fund, "account_id": w["main"]}],
            f"account {w['main']} funds an active plan"),
        "funding account is another active plan's goal account": (
            {"funding_account_id": w["savings"]}, [], f"account {w['savings']} is the goal account"),
        "the tag is another active plan's": ({"name": "Lisbon trip"}, [], "would be shared"),
    }


@pytest.mark.parametrize("case", list(_refusals({k: 0 for k in ("main", "card", "pot", "savings")})))
async def test_start_refuses_and_writes_nothing(factory, w, case):
    fields, goals, says = _refusals(w)[case]
    with _client(factory, "alice") as alice:
        # The other active plan: funded from main, saving into savings.
        first = _draft(alice, w)
        assert alice.post(f"/api/v1/scenarios/{first}/start").status_code == 200
        pid = _draft(alice, w, **{"name": "Second", **fields}, goals=goals)
        before = await _rows(factory, w["org"])
        res = alice.post(f"/api/v1/scenarios/{pid}/start")
        assert res.status_code == 422 and says in res.json()["detail"], (case, res.text)
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
    with _client(factory, "alice") as alice:
        res = alice.patch(f"/api/v1/scenarios/{pid}", json={"monthly_budget": "400.00"})
        assert res.status_code == 409 and "confirm" in res.json()["detail"]
        assert alice.patch(f"/api/v1/scenarios/{pid}?confirm=true",
                           json={"monthly_budget": "400.00"}).status_code == 200
        assert alice.patch(f"/api/v1/scenarios/{pid}?confirm=true",
                           json={"funding_account_id": w["pot"]}).status_code == 409
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
    async with factory() as db:
        assert [b.amount for b in (await db.scalars(select(Budget))).all()] == [Decimal("400.00")]
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
        stubs = await billing_service.ensure_future_periods(db, w["org"], count=1)
        assert (opened.start_date, [s.start_date for s in stubs]) == (nxt, [nxt + relativedelta(months=1)])
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START]
    monkeypatch.setattr(plan_lifecycle_service, "attention", real)
    async with factory() as db:
        await billing_service.ensure_future_periods(db, w["org"], count=1)
    assert await _budget_periods(factory, w["org"]) == [PERIOD_START, nxt, nxt + relativedelta(months=1)]
