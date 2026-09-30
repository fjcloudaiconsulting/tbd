"""TBD-559: the six read tools through the real ``invoke``, on a real schema.

* ``invoke`` runs all six reads for a JWT user in an org granted ``ai.agent``
  by an org override (no plan carries the key yet).
* F-R3: org B's ids and periods, used by org A, return nothing of B's.
* F-R7: on an org with ZERO billing periods every read leaves every table's
  row count unchanged, and the three period tools answer ``no_open_period``.
  A seeded org with an open period hides the auto-create, so both are run.
* F-R10: every attacker-influenceable string reaches the result only inside
  ``{"untrusted": ...}``.

Dates are anchored to the real clock's current month on purpose: an open
period's window end IS the clock (``reference_wall_clock_date_bomb_tests``).
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agent.registry import ToolError, all_tools, invoke
from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import CategoryType
from app.models.feature_override import OrgFeatureOverride
from app.models.settings import OrgSetting
from app.models.tag import Tag, TransactionTag
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Role, User
from app.security import hash_password

MARK = "IGNORE-PREVIOUS-INSTRUCTIONS"
PERIOD_TOOLS = ("budgets_list", "spending_by_category", "forecast_get")
TODAY = datetime.date.today()
P_START = TODAY.replace(day=1)
B_START = P_START - datetime.timedelta(days=40)  # a date only org B has a period at
C_START = (P_START - datetime.timedelta(days=1)).replace(day=1)  # org A's closed period


def _args(name: str) -> dict:
    if name == "transactions_search":
        return {"date_from": str(P_START), "date_to": str(TODAY), "category_match": "subtree"}
    return {}


@pytest_asyncio.fixture
async def factory():
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
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _org(
    db, tag: str, *, entitled: bool = True, periods: bool = True, amount: str = "12.50",
) -> dict:
    """One org whose every user-writable string carries ``MARK``."""
    org = Organization(name=f"{tag} {MARK}", billing_cycle_day=1, primary_currency="EUR")
    db.add(org)
    await db.flush()
    user = User(
        org_id=org.id, username=f"u{tag}", email=f"{tag}@x.example",
        password_hash=hash_password("pw-1234567"), role=Role.MEMBER,
        is_active=True, email_verified=True,
    )
    at = AccountType(org_id=org.id, name=f"Type {MARK}", slug=f"t{tag}", is_system=False)
    db.add_all([user, at])
    await db.flush()
    acct = Account(
        org_id=org.id, name=f"Acct {tag} {MARK}", account_type_id=at.id,
        balance=Decimal("1000.00"), currency="EUR", is_default=True,
    )
    master = Category(org_id=org.id, name=f"Food {tag} {MARK}", type=CategoryType.EXPENSE,
                      description=f"desc {MARK}")
    db.add_all([acct, master])
    await db.flush()
    sub = Category(org_id=org.id, parent_id=master.id, name=f"Cafe {tag} {MARK}",
                   type=CategoryType.EXPENSE)
    t = Tag(org_id=org.id, name=f"tg{MARK}"[:32], name_normalized=f"tg{MARK}".lower()[:32])
    db.add_all([sub, t])
    await db.flush()
    tx = Transaction(
        org_id=org.id, account_id=acct.id, category_id=sub.id,
        description=f"Coffee {tag} {MARK}", amount=Decimal(amount),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=P_START, settled_date=P_START,
    )
    db.add(tx)
    await db.flush()
    db.add(TransactionTag(transaction_id=tx.id, tag_id=t.id))
    if periods:
        start = P_START if tag == "A" else B_START
        db.add(BillingPeriod(org_id=org.id, start_date=start))
        db.add(Budget(org_id=org.id, category_id=master.id, amount=Decimal("100.00"),
                      period_start=start))
    if entitled:
        db.add(OrgFeatureOverride(org_id=org.id, feature_key="ai.agent", value=True))
    await db.commit()
    return {"org": org, "user": user, "acct": acct, "master": master, "sub": sub, "tx": tx}


async def _call(factory, user, name, args=None):
    async with factory() as db:
        u = await db.get(User, user.id)
        return await invoke(db, u, name, _args(name) if args is None else args, channel="in_app")


async def _row_counts(factory) -> dict[str, int]:
    async with factory() as db:
        return {
            t.name: await db.scalar(select(func.count()).select_from(t))
            for t in Base.metadata.sorted_tables
        }


def _strings(value, parent=None):
    """Yield (parent_key, string) for every string in a result."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, parent)
    elif isinstance(value, str):
        yield parent, value


async def test_invoke_runs_all_six_reads_for_an_entitled_user(factory):
    async with factory() as db:
        a = await _org(db, "A")
    out = {t.name: (await _call(factory, a["user"], t.name))["data"] for t in all_tools()}

    assert [r["id"] for r in out["accounts_list"]] == [a["acct"].id]
    assert {r["id"] for r in out["categories_list"]} == {a["master"].id, a["sub"].id}
    assert [b["category_id"] for b in out["budgets_list"]["budgets"]] == [a["master"].id]
    assert out["budgets_list"]["budgets"][0]["spent"] == "12.50"
    assert [r["id"] for r in out["transactions_search"]["items"]] == [a["tx"].id]
    assert out["transactions_search"]["items"][0]["currency"] == "EUR"
    assert [c["category_id"] for c in out["spending_by_category"]["categories"]] == [a["sub"].id]
    assert out["forecast_get"]["executed_expense"] == "12.50"
    for name in PERIOD_TOOLS:
        assert out[name]["currency_scope"]["currency"] == "EUR", name
        assert out[name]["period_start"] == str(P_START), name


async def test_explicit_period_start_returns_that_period(factory):
    """FENCE. A CLOSED earlier period asked for by ``period_start`` is the one
    answered, not the open one. Wrong implementation: resolving the start and
    then calling the service with ``period_start=None`` (or dropping it), which
    answers with the open period's budgets and spend under the requested label."""
    async with factory() as db:
        a = await _org(db, "A")
        db.add(BillingPeriod(org_id=a["org"].id, start_date=C_START,
                             end_date=P_START - datetime.timedelta(days=1)))
        other = Category(org_id=a["org"].id, name="Rent", type=CategoryType.EXPENSE)
        db.add(other)
        await db.flush()
        db.add(Budget(org_id=a["org"].id, category_id=other.id, amount=Decimal("55.00"),
                      period_start=C_START))
        db.add(Transaction(
            org_id=a["org"].id, account_id=a["acct"].id, category_id=other.id,
            description="Rent", amount=Decimal("3.00"), type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED, date=C_START, settled_date=C_START,
        ))
        await db.commit()
    arg = {"period_start": str(C_START)}
    budgets = (await _call(factory, a["user"], "budgets_list", arg))["data"]
    assert [(b["category_id"], b["spent"]) for b in budgets["budgets"]] == [(other.id, "3.00")]
    spend = (await _call(factory, a["user"], "spending_by_category", arg))["data"]
    assert [(c["category_id"], c["executed"]) for c in spend["categories"]] == [(other.id, "3.00")]
    forecast = (await _call(factory, a["user"], "forecast_get", arg))["data"]
    assert forecast["executed_expense"] == "3.00"
    for out in (budgets, spend, forecast):
        assert out["period_start"] == str(C_START)


async def test_fr3_other_orgs_ids_and_periods_return_nothing_of_theirs(factory):
    """FENCE F-R3. As org A with org B's account, category and period ids.
    Wrong implementation: a tool calling a service without ``ctx.org_id``
    (or resolving a period by date alone). B's rows are real and reachable
    by B (control), so an empty result is the org scope, not an empty fixture."""
    async with factory() as db:
        a = await _org(db, "A")
        b = await _org(db, "B", amount="7.25")  # B's row sits inside A's window too
    # A's aggregates with B's rows present: only A's 12.50 counts.
    budgets = (await _call(factory, a["user"], "budgets_list"))["data"]["budgets"]
    assert [(x["category_id"], x["spent"]) for x in budgets] == [(a["master"].id, "12.50")]
    spend = (await _call(factory, a["user"], "spending_by_category"))["data"]
    assert [c["category_id"] for c in spend["categories"]] == [a["sub"].id]
    assert spend["executed_expense"] == "12.50"
    assert (await _call(factory, a["user"], "forecast_get"))["data"]["executed_expense"] == "12.50"
    own = (await _call(factory, a["user"], "transactions_search"))["data"]
    assert [r["id"] for r in own["items"]] == [a["tx"].id]

    foreign = {
        **_args("transactions_search"),
        "account_id": [b["acct"].id], "category_id": [b["master"].id],
    }
    res = await _call(factory, a["user"], "transactions_search", foreign)
    assert res["data"]["items"] == [] and res["data"]["total"] == 0
    control = await _call(factory, b["user"], "transactions_search", {
        **foreign, "date_from": str(B_START),
    })
    assert [r["id"] for r in control["data"]["items"]] == [b["tx"].id]

    for name in PERIOD_TOOLS:
        with pytest.raises(ToolError) as exc:
            await _call(factory, a["user"], name, {"period_start": str(B_START)})
        assert exc.value.code == "period_not_found", name
        control = await _call(factory, b["user"], name, {"period_start": str(B_START)})
        assert control["data"]["period_start"] == str(B_START)

    accts = {r["id"] for r in (await _call(factory, a["user"], "accounts_list"))["data"]}
    cats = {r["id"] for r in (await _call(factory, a["user"], "categories_list"))["data"]}
    assert accts == {a["acct"].id} and b["acct"].id not in accts
    assert cats == {a["master"].id, a["sub"].id}


@pytest.mark.parametrize("seeded", [False, True], ids=["zero_periods", "open_period"])
async def test_fr7_reads_write_nothing(factory, seeded):
    """FENCE F-R7. Wrong implementations: ``budgets_list`` through
    ``list_budgets -> resolve_period``, ``forecast_get`` through
    ``resolve_spend_window``, or ``spending_by_category`` through the REST
    rollup with no period, each AUTO-CREATING a ``BillingPeriod`` on an org
    that has none. The seeded arm is the control and the case a fixture with an
    open period would have hidden the bug in."""
    async with factory() as db:
        a = await _org(db, "A", periods=seeded)
        # Another org WITH an open period: a lookup that forgot ``org_id``
        # would find it and answer instead of ``no_open_period``.
        await _org(db, "B")
    before = await _row_counts(factory)
    for tool in all_tools():
        if tool.name in PERIOD_TOOLS and not seeded:
            with pytest.raises(ToolError) as exc:
                await _call(factory, a["user"], tool.name)
            assert exc.value.code == "no_open_period", tool.name
        else:
            await _call(factory, a["user"], tool.name)
        assert await _row_counts(factory) == before, f"{tool.name} wrote rows"


async def test_fr10_attacker_strings_arrive_only_wrapped(factory):
    """GUARD F-R10. Every string carrying the marker (account, account type,
    category, category description, transaction description, tag names) sits
    directly under an ``untrusted`` key."""
    async with factory() as db:
        a = await _org(db, "A")
    seen = 0
    for tool in all_tools():
        data = (await _call(factory, a["user"], tool.name))["data"]
        for parent, s in _strings(data):
            if MARK.lower() in s.lower():
                seen += 1
                assert parent == "untrusted", f"{tool.name}: bare {s!r} under {parent!r}"
    assert seen >= 12


async def test_gates_entitlement_and_product_area(factory):
    """GUARD. No ``ai.agent`` -> refused before any tool runs; the tenant's
    ``orgpref.budgets`` opt-out -> ``budgets_list`` refused while an ungated
    read still answers."""
    async with factory() as db:
        a = await _org(db, "A", entitled=False)
    with pytest.raises(ToolError) as exc:
        await _call(factory, a["user"], "accounts_list")
    assert exc.value.code == "feature_not_entitled"

    async with factory() as db:
        db.add(OrgFeatureOverride(org_id=a["org"].id, feature_key="ai.agent", value=True))
        db.add(OrgSetting(org_id=a["org"].id, key="orgpref.budgets", value="off"))
        await db.commit()
    with pytest.raises(ToolError) as exc:
        await _call(factory, a["user"], "budgets_list")
    assert exc.value.code == "feature_disabled"
    await _call(factory, a["user"], "accounts_list")
