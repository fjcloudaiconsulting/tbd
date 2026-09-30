"""TBD-585: ``mcp.calls`` admission in the agent registry.

Driven through the real ``registry.invoke`` / ``confirm_action`` /
``cancel_action`` on a file-backed SQLite. Fences F-Q5r, F-Q12a, F2-own and
the MCP half of 402. Every fence names the wrong implementation it kills.
"""
from __future__ import annotations

import datetime
import secrets
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agent import registry
from app.agent.registry import ToolError, invoke
from app.models import Category, Organization
from app.models.agent_pending_action import AgentPendingAction
from app.models.api_token import ApiToken
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import CategoryType
from app.models.feature_override import OrgFeatureOverride
from app.models.limit_override import OrgLimitOverride
from app.models.usage_counter import UsageCounter
from app.models.user import Role, User
from app.security import hash_password

P_START = datetime.date.today().replace(day=1)


@pytest_asyncio.fixture
async def factory(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/a.db")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def w(factory):
    async with factory() as db:
        org = Organization(name="Org", billing_cycle_day=1, primary_currency="EUR")
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="m", email="m@x.example",
            password_hash=hash_password("pw-1234567"), role=Role.MEMBER, is_active=True,
            email_verified=True,
        )
        cat = Category(org_id=org.id, name="Food", type=CategoryType.EXPENSE)
        db.add_all([user, cat])
        await db.flush()
        budget = Budget(org_id=org.id, category_id=cat.id, amount=Decimal("100.00"),
                        period_start=P_START)
        token = ApiToken(
            token_hash=secrets.token_hex(32), token_prefix="pat_1", name="t", scope="agent:write",
            created_by_user_id=user.id, created_by_email="m@x.example",
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=30),
        )
        db.add_all([
            BillingPeriod(org_id=org.id, start_date=P_START), budget, token,
            OrgFeatureOverride(org_id=org.id, feature_key="ai.agent", value=True),
        ])
        await db.commit()
        return {"org": org.id, "user": user.id, "budget": budget.id, "token": token.id}


def _mcp(w, scope="agent:write"):
    return {"channel": "mcp", "scope": scope, "api_token_id": w["token"]}


async def _invoke(f, w, name, args, **kw):
    async with f() as db:
        u = await db.get(User, w["user"])
        return (await invoke(db, u, name, args, **kw))["data"]


async def _decide(f, w, which, action_id, **kw):
    async with f() as db:
        u = await db.get(User, w["user"])
        return await getattr(registry, which)(db, u, action_id, **kw)


async def _stage(f, w, amount="120.00", **kw):
    return await _invoke(f, w, "budgets_update_amount",
                         {"budget_id": w["budget"], "amount": amount}, **kw)


async def _count(f, w, meter="mcp.calls") -> int:
    async with f() as db:
        return int(await db.scalar(
            select(UsageCounter.value).where(UsageCounter.org_id == w["org"],
                                             UsageCounter.meter == meter)
        ) or 0)


async def _meters(f, w) -> set[str]:
    async with f() as db:
        return set((await db.scalars(
            select(UsageCounter.meter).where(UsageCounter.org_id == w["org"])
        )).all())


async def _limit(f, w, limit):
    async with f() as db:
        db.add(OrgLimitOverride(org_id=w["org"], meter="mcp.calls", period="month",
                                limit_value=limit))
        await db.commit()


async def _refused(coro) -> ToolError:
    with pytest.raises(ToolError) as exc:
        await coro
    return exc.value


IN_APP = {"channel": "in_app"}
IN_APP_D = {"channel": "in_app", "scope": None, "api_token_id": None}


async def test_fq5r_every_mcp_entry_counts_once_and_in_app_never(factory, w):
    """FENCE F-Q5r. Each mcp entry point moves ``mcp.calls`` by exactly one:
    a read, a write preview, an auto write (staged AND executed in one call),
    confirm_action, cancel_action. Listing tools reads no counter; in-app
    calls and the platform meters never move.

    Wrong implementations killed: counting in-app calls; the auto path
    counted twice (its confirm routed back through ``confirm_action``);
    confirm/cancel not counted."""
    registry.all_tools()
    registry.get_tool("accounts_list")
    assert await _count(factory, w) == 0

    await _invoke(factory, w, "accounts_list", {}, **IN_APP)
    staged = await _stage(factory, w, "110.00", **IN_APP)
    await _decide(factory, w, "confirm_action", staged["action_id"], **IN_APP_D)
    staged = await _stage(factory, w, "111.00", **IN_APP)
    await _decide(factory, w, "cancel_action", staged["action_id"], **IN_APP_D)
    assert await _count(factory, w) == 0

    steps = [
        lambda: _invoke(factory, w, "accounts_list", {}, **_mcp(w)),
        lambda: _stage(factory, w, "120.00", **_mcp(w)),
        lambda: _stage(factory, w, "130.00", **_mcp(w, "agent:auto")),
    ]
    for i, step in enumerate(steps, 1):
        out = await step()
        assert await _count(factory, w) == i
    assert out["status"] == "done"

    staged = await _stage(factory, w, "140.00", **_mcp(w))  # 4
    await _decide(factory, w, "confirm_action", staged["action_id"], **_mcp(w))  # 5
    assert await _count(factory, w) == 5
    staged = await _stage(factory, w, "150.00", **_mcp(w))  # 6
    await _decide(factory, w, "cancel_action", staged["action_id"], **_mcp(w))  # 7
    assert await _count(factory, w) == 7
    assert await _meters(factory, w) == {"mcp.calls"}


async def test_fq12a_refused_before_gate_one_spends_nothing(factory, w):
    """FENCE F-Q12a. Invalid args and an unknown tool over MCP are refused
    before admission. Wrong implementation: admission before gate 1 (or before
    the unknown-tool check), so garbage calls burn the plan's meter."""
    err = await _refused(_invoke(factory, w, "accounts_list", {"org_id": 1}, **_mcp(w)))
    assert err.code == "invalid_arguments"
    err = await _refused(_invoke(factory, w, "no_such_tool", {}, **_mcp(w)))
    assert err.code == "unknown_tool"
    assert await _count(factory, w) == 0


async def test_f2_own_counted_though_the_request_never_commits(factory, w):
    """FENCE F2-own. A read call whose session is closed WITHOUT a commit (the
    MCP front door never commits a read) still counted: a fresh session sees 1.
    Wrong implementation: admission flushed in the caller's session but not
    committed, so the close rolls the count back and reads are free."""
    async with factory() as db:
        u = await db.get(User, w["user"])
        await invoke(db, u, "accounts_list", {}, **_mcp(w))
        # no commit; the context manager closes (rolls back) the session
    assert await _count(factory, w) == 1


async def test_402_mcp_refusal_on_invoke_confirm_and_cancel(factory, w):
    """FENCE 402 (MCP half). At the limit, invoke, confirm_action and
    cancel_action each refuse with ``ToolError("plan_limit_reached")`` carrying
    the four facts, and the staged action is left undecided. Wrong
    implementations: the refusal swallowed into ``internal_error`` /
    ``internal`` (``_decide``'s catch-all), the reset time missing, confirm or
    cancel not metered."""
    staged = await _stage(factory, w, "120.00", **_mcp(w))
    await _limit(factory, w, 1)  # the month row already holds the staging call
    errs = [
        await _refused(_invoke(factory, w, "accounts_list", {}, **_mcp(w))),
        await _refused(_decide(factory, w, "confirm_action", staged["action_id"], **_mcp(w))),
        await _refused(_decide(factory, w, "cancel_action", staged["action_id"], **_mcp(w))),
    ]
    next_month = (P_START + datetime.timedelta(days=32)).replace(day=1)
    for err in errs:
        assert err.code == "plan_limit_reached"
        assert err.data == {
            "meter": "mcp.calls", "limit": 1, "period": "month",
            "resets_at": f"{next_month.isoformat()}T00:00:00+00:00",
        }
    assert await _count(factory, w) == 1
    async with factory() as db:
        row = await db.get(AgentPendingAction, staged["action_id"])
        assert row.status.value == "pending"
