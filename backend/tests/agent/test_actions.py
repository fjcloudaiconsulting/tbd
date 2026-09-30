"""TBD-577: preview-confirm engine, auto execution and ``budgets_update_amount``.

Driven through the real ``registry.invoke`` / ``confirm_action`` / ``cancel_action``
on a file-backed SQLite (two connections, so a forced interleave is real).

Fences: F-P1 F-P2 F-P3 F-P4 F-P5(route file) F-P7 F-P11 F-P12 F-P13 F-A1 F-A2
F-A3 F-A7. Guards: F-P8 F-P9. Every fence names the wrong implementation it
kills in its docstring.
"""
from __future__ import annotations

import asyncio
import datetime
import secrets
from decimal import Decimal

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict
from redis.exceptions import RedisError
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import redis_client
from app.agent import actions, registry
from app.agent.registry import Change, Preview, ToolError, ToolSpec, invoke
from app.models import Account, AccountType, Category, Organization
from app.models.agent_pending_action import AgentPendingAction
from app.models.api_token import ApiToken
from app.models.audit_event import AuditEvent
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import CategoryType
from app.models.feature_override import OrgFeatureOverride
from app.models.settings import OrgSetting
from app.models.user import Role, User
from app.security import hash_password
from app.services import budget_service
from app.services.feature_gate import Feature

P_START = datetime.date.today().replace(day=1)
MCP = {"channel": "mcp", "scope": "agent:write"}
AUTO = {"channel": "mcp", "scope": "agent:auto"}


# ── fixtures ──────────────────────────────────────────────────────────────

class _BarrierSession(AsyncSession):
    """Holds the FIRST UPDATE of ``agent_pending_actions`` on a barrier, so two
    confirms are forced past whatever they read before either writes."""

    barrier: asyncio.Barrier | None = None
    _hit = False

    async def execute(self, stmt, *a, **k):
        if (
            self.barrier is not None and not self._hit and getattr(stmt, "is_update", False)
            and stmt.table.name == "agent_pending_actions"
        ):
            self._hit = True
            await self.barrier.wait()
        return await super().execute(stmt, *a, **k)


@pytest_asyncio.fixture
async def engine(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")

    @event.listens_for(eng.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, class_=_BarrierSession, expire_on_commit=False)


def _user(org_id, name, role):
    return User(
        org_id=org_id, username=name, email=f"{name}@x.example",
        password_hash=hash_password("pw-1234567"), role=role, is_active=True,
        email_verified=True,
    )


def _token(user_id, n):
    return ApiToken(
        token_hash=secrets.token_hex(32), token_prefix=f"pat_{n}", name=f"t{n}",
        scope="agent:write", created_by_user_id=user_id, created_by_email="m@x.example",
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=30),
    )


@pytest_asyncio.fixture
async def w(factory):
    """Org A (member, admin, a second member, two member tokens, two budgets)
    and org B (one user, one budget)."""
    out: dict = {}
    async with factory() as db:
        for tag in ("A", "B"):
            org = Organization(name=f"Org {tag}", billing_cycle_day=1, primary_currency="EUR")
            db.add(org)
            await db.flush()
            member = _user(org.id, f"member{tag}", Role.MEMBER)
            db.add(member)
            master = Category(org_id=org.id, name=f"Food {tag}", type=CategoryType.EXPENSE)
            master2 = Category(org_id=org.id, name=f"Fun {tag}", type=CategoryType.EXPENSE)
            db.add_all([master, master2])
            await db.flush()
            db.add(BillingPeriod(org_id=org.id, start_date=P_START))
            b1 = Budget(org_id=org.id, category_id=master.id, amount=Decimal("100.00"),
                        period_start=P_START)
            b2 = Budget(org_id=org.id, category_id=master2.id, amount=Decimal("50.00"),
                        period_start=P_START)
            db.add_all([b1, b2, OrgFeatureOverride(org_id=org.id, feature_key="ai.agent", value=True)])
            await db.flush()
            out[tag] = {"org": org.id, "member": member.id, "b1": b1.id, "b2": b2.id}
        a = out["A"]
        admin = _user(a["org"], "adminA", Role.ADMIN)
        other = _user(a["org"], "otherA", Role.MEMBER)
        db.add_all([admin, other])
        await db.flush()
        t1, t2 = _token(a["member"], 1), _token(a["member"], 2)
        db.add_all([t1, t2])
        await db.flush()
        a.update(admin=admin.id, other=other.id, t1=t1.id, t2=t2.id)
        await db.commit()
    return out


@pytest.fixture
def scratch():
    """Register throwaway write tools; count their executions."""
    added: list[str] = []
    calls: list[str] = []

    class _Args(BaseModel):
        model_config = ConfigDict(extra="forbid")
        budget_id: int

    def _add(name="scratch_write", *, risk="write", min_role=Role.MEMBER, area=Feature.BUDGETS,
             on_preview=None, big=False):
        async def _preview(ctx, args):
            if on_preview:
                await on_preview(ctx)
            return Preview(
                summary="scratch", warnings=[],
                changes=[Change("budgets", args.budget_id, "amount", "1.00", "2.00", "EUR")],
                context={"category_name": "x" * 20_000} if big else {},
            )

        async def _execute(ctx, args):
            calls.append(name)
            return {"ok": True}

        registry.register(ToolSpec(
            name=name, risk=risk, args=_Args, product_area=area, min_role=min_role,
            mirrors_route=("PUT", "/api/v1/budgets/{budget_id}"), description="t",
            preview=_preview, execute=_execute,
        ))
        added.append(name)
        return calls

    yield _add
    for n in added:
        registry._TOOLS.pop(n, None)


# ── helpers ───────────────────────────────────────────────────────────────

async def _invoke(f, uid, name, args, **kw):
    async with f() as db:
        u = await db.get(User, uid)
        return (await invoke(db, u, name, args, **{"channel": "in_app", **kw}))["data"]


async def _stage(f, w, amount="120.00", *, budget="b1", uid=None, **kw):
    a = w["A"]
    return await _invoke(f, uid or a["member"], "budgets_update_amount",
                         {"budget_id": a[budget], "amount": amount}, **kw)


async def _confirm(f, uid, action_id, **kw):
    async with f() as db:
        u = await db.get(User, uid)
        return (await registry.confirm_action(
            db, u, action_id, **{"channel": "in_app", "scope": None, "api_token_id": None, **kw}
        ))["data"]


async def _cancel(f, uid, action_id, **kw):
    async with f() as db:
        u = await db.get(User, uid)
        return await registry.cancel_action(
            db, u, action_id, **{"channel": "in_app", "scope": None, "api_token_id": None, **kw}
        )


async def _row(f, action_id) -> AgentPendingAction:
    async with f() as db:
        return await db.get(AgentPendingAction, action_id)


async def _rows(f) -> list[AgentPendingAction]:
    async with f() as db:
        return list((await db.scalars(select(AgentPendingAction))).all())


async def _amount(f, budget_id) -> Decimal:
    async with f() as db:
        return (await db.get(Budget, budget_id)).amount


async def _set_amount(f, budget_id, amount):
    async with f() as db:
        await db.execute(update(Budget).where(Budget.id == budget_id).values(amount=Decimal(amount)))
        await db.commit()


async def _audits(f) -> list[AuditEvent]:
    async with f() as db:
        return list((await db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "agent.action.executed")
        )).all())


async def _refused(coro) -> ToolError:
    with pytest.raises(ToolError) as exc:
        await coro
    return exc.value


def _fake():
    return redis_client.get_client()


def _seed_pending(org_id, user_id, n, *, channel="in_app", token=None):
    now = datetime.datetime.utcnow()
    return [
        AgentPendingAction(
            id=secrets.token_hex(16), org_id=org_id, user_id=user_id, channel=channel,
            api_token_id=token, tool="budgets_update_amount", risk="write", mode="confirm",
            args_json={}, args_sha256="0" * 64, fingerprint="0" * 64, preview_json={},
            status="pending", created_at=now, expires_at=now + datetime.timedelta(minutes=10),
        )
        for _ in range(n)
    ]


# ── budgets_update_amount and the preview shape ───────────────────────────

async def test_preview_shape_writes_nothing_and_is_not_audited(factory, w):
    """GUARD (F-P8, preview half): a preview stages a row, changes no domain
    row and writes no audit row."""
    out = await _stage(factory, w)
    assert out["requires_confirmation"] is True
    assert out["changes"] == [{
        "entity": "budgets", "id": w["A"]["b1"], "field": "amount",
        "before": "100.00", "after": "120.00", "currency": "EUR",
    }]
    assert "Food A" not in out["summary"]
    assert out["context"]["category_name"] == {"untrusted": "Food A"}
    assert (await _amount(factory, w["A"]["b1"])) == Decimal("100.00")
    assert await _audits(factory) == []
    row = await _row(factory, out["action_id"])
    assert (row.status.value, row.mode.value, row.channel.value, row.risk.value) == (
        "pending", "confirm", "in_app", "write")
    assert row.api_token_id is None and len(row.fingerprint) == 64 == len(row.args_sha256)


async def test_preview_refusals(factory, w):
    """Same amount is ``no_change``; another org's budget is ``not_found``; the
    tool's own amount bounds hold. None stages a row."""
    a = w["A"]
    assert (await _refused(_stage(factory, w, "100.00"))).code == "no_change"
    err = await _refused(_invoke(factory, a["member"], "budgets_update_amount",
                                 {"budget_id": w["B"]["b1"], "amount": "120.00"}))
    assert err.code == "not_found"
    for bad in ("0", "-1", "1.005", "9999999999999.00"):
        err = await _refused(_stage(factory, w, bad))
        assert err.code == "invalid_arguments", bad
    assert await _rows(factory) == []


async def test_confirm_executes_and_audits_once(factory, w):
    a = w["A"]
    out = await _stage(factory, w)
    done = await _confirm(factory, a["member"], out["action_id"])
    assert done["status"] == "done" and done["result"]["amount"] == "120.00"
    assert (await _amount(factory, a["b1"])) == Decimal("120.00")
    row = await _row(factory, out["action_id"])
    assert row.status.value == "done" and row.result_json["amount"] == "120.00"


# ── F-P1 ──────────────────────────────────────────────────────────────────

async def test_fp1_double_confirm_sequential_is_409(factory, w):
    out = await _stage(factory, w)
    await _confirm(factory, w["A"]["member"], out["action_id"])
    err = await _refused(_confirm(factory, w["A"]["member"], out["action_id"]))
    assert (err.code, err.data["status"]) == ("action_already_decided", "done")


async def test_fp1_forced_interleave_executes_once(factory, w, monkeypatch):
    """FENCE F-P1. Wrong implementation: claim by SELECT-then-UPDATE. Both
    confirms are held at their first UPDATE of the row, so each would already
    have read ``pending``; only a conditional UPDATE lets exactly one win."""
    real = budget_service.update_budget
    executed: list[int] = []

    async def counting(*a, **k):
        executed.append(1)
        return await real(*a, **k)

    monkeypatch.setattr(budget_service, "update_budget", counting)
    out = await _stage(factory, w)
    barrier = asyncio.Barrier(2)

    async def go():
        async with factory() as db:
            db.barrier = barrier
            u = await db.get(User, w["A"]["member"])
            try:
                return await registry.confirm_action(
                    db, u, out["action_id"], channel="in_app", scope=None, api_token_id=None)
            except ToolError as exc:
                return exc

    r1, r2 = await asyncio.gather(go(), go())
    errs = [r for r in (r1, r2) if isinstance(r, ToolError)]
    assert len(errs) == 1, (r1, r2)
    assert errs[0].code in ("action_already_decided", "action_in_progress")
    assert executed == [1]
    assert len(await _audits(factory)) == 1


# ── F-P2 ──────────────────────────────────────────────────────────────────

async def test_fp2_expired_owner_410_non_owner_404(factory, w):
    """FENCE F-P2. Wrong implementation: expiry checked before ownership, so a
    non-owner learns the id exists (410 instead of 404)."""
    a = w["A"]
    out = await _stage(factory, w)
    async with factory() as db:
        await db.execute(update(AgentPendingAction).values(
            expires_at=datetime.datetime.utcnow() - datetime.timedelta(seconds=1)))
        await db.commit()
    assert (await _refused(_confirm(factory, a["member"], out["action_id"]))).code == "action_expired"
    for uid in (a["other"], w["B"]["member"]):
        assert (await _refused(_confirm(factory, uid, out["action_id"]))).code == "action_not_found"
    assert (await _refused(_cancel(factory, a["other"], out["action_id"]))).code == "action_not_found"
    assert (await _refused(_cancel(factory, a["member"], out["action_id"]))).code == "action_expired"
    assert (await _row(factory, out["action_id"])).status.value == "pending"
    assert (await _amount(factory, a["b1"])) == Decimal("100.00")


# ── F-P3 ──────────────────────────────────────────────────────────────────

async def test_fp3_drift_between_preview_and_confirm_is_stale(factory, w):
    """FENCE F-P3. Wrong implementation: fingerprint over args only, so the
    drifted budget still executes (100 -> 130 preview, 111 -> 120 executed)."""
    a = w["A"]
    out = await _stage(factory, w)
    await _set_amount(factory, a["b1"], "111.00")
    err = await _refused(_confirm(factory, a["member"], out["action_id"]))
    assert err.code == "preview_stale"
    fresh = err.data
    assert fresh["action_id"] != out["action_id"] and fresh["requires_confirmation"] is True
    assert fresh["changes"][0]["before"] == "111.00" and fresh["changes"][0]["after"] == "120.00"
    assert (await _amount(factory, a["b1"])) == Decimal("111.00")
    assert (await _row(factory, out["action_id"])).status.value == "stale"
    assert (await _row(factory, fresh["action_id"])).status.value == "pending"
    # the fresh preview confirms
    await _confirm(factory, a["member"], fresh["action_id"])
    assert (await _amount(factory, a["b1"])) == Decimal("120.00")
    audits = await _audits(factory)
    assert sorted((x.outcome.value, x.detail["status"]) for x in audits) == [
        ("failure", "stale"), ("success", "done")]


# ── F-P4 ──────────────────────────────────────────────────────────────────

async def test_fp4_principal_is_token_and_channel(factory, w):
    """FENCE F-P4. Wrong implementations: WHERE without token/channel, or
    ``api_token_id == :tok`` (an in-app NULL row could never be confirmed)."""
    a = w["A"]
    mcp = await _stage(factory, w, "121.00", api_token_id=a["t1"], **MCP)
    # second token of the same user
    assert (await _refused(_confirm(factory, a["member"], mcp["action_id"],
                                    api_token_id=a["t2"], **MCP))).code == "action_not_found"
    # in-app cannot confirm an mcp row
    assert (await _refused(_confirm(factory, a["member"], mcp["action_id"]))).code == "action_not_found"
    inapp = await _stage(factory, w, "122.00")
    # mcp cannot confirm an in-app row
    assert (await _refused(_confirm(factory, a["member"], inapp["action_id"],
                                    api_token_id=a["t1"], **MCP))).code == "action_not_found"
    assert (await _amount(factory, a["b1"])) == Decimal("100.00")
    # owners succeed: the NULL-token in-app row and the token-bound mcp row
    await _confirm(factory, a["member"], inapp["action_id"])
    async with factory() as db:
        await db.execute(update(Budget).values(amount=Decimal("100.00")))
        await db.commit()
    await _confirm(factory, a["member"], mcp["action_id"], api_token_id=a["t1"], **MCP)
    assert (await _amount(factory, a["b1"])) == Decimal("121.00")


# ── F-P7 ──────────────────────────────────────────────────────────────────

async def test_fp7_an_executing_row_is_never_rerun(factory, w, scratch):
    """FENCE F-P7. Wrong implementation: status reset to ``pending`` on error
    (or a retry allowed on ``executing``), so a failed action runs twice."""
    a = w["A"]
    calls = scratch()

    async def boom(ctx, args):
        calls.append("boom")
        raise RuntimeError("SELECT secret FROM users")

    registry._TOOLS["scratch_write"] = ToolSpec(**{**registry._TOOLS["scratch_write"].__dict__,
                                                    "execute": boom})
    out = await _invoke(factory, a["member"], "scratch_write", {"budget_id": a["b1"]})
    err = await _refused(_confirm(factory, a["member"], out["action_id"]))
    assert err.code == "internal"
    assert "secret" not in (err.detail or "")
    row = await _row(factory, out["action_id"])
    assert (row.status.value, row.error_code) == ("failed", "internal")
    err = await _refused(_confirm(factory, a["member"], out["action_id"]))
    assert (err.code, err.data["status"]) == ("action_already_decided", "failed")
    # a row already in flight
    async with factory() as db:
        db.add_all(_seed_pending(a["org"], a["member"], 1))
        await db.commit()
        rid = (await db.scalar(select(AgentPendingAction.id).where(
            AgentPendingAction.status == "pending")))
        await db.execute(update(AgentPendingAction).where(AgentPendingAction.id == rid)
                         .values(status="executing"))
        await db.commit()
    assert (await _refused(_confirm(factory, a["member"], rid))).code == "action_in_progress"
    assert calls == ["boom"]


# ── F-P11 ─────────────────────────────────────────────────────────────────

async def test_fp11_budgets_switched_off_between_preview_and_confirm(factory, w):
    """FENCE F-P11. Wrong implementation: no gate re-run at confirm."""
    a = w["A"]
    out = await _stage(factory, w)
    async with factory() as db:
        db.add(OrgSetting(org_id=a["org"], key="orgpref.budgets", value="off"))
        await db.commit()
    err = await _refused(_confirm(factory, a["member"], out["action_id"]))
    assert err.code == "feature_disabled"
    row = await _row(factory, out["action_id"])
    assert (row.status.value, row.error_code) == ("failed", "feature_disabled")
    assert (await _amount(factory, a["b1"])) == Decimal("100.00")
    assert [x.outcome.value for x in await _audits(factory)] == ["failure"]


async def test_fp11_role_demoted_between_preview_and_confirm(factory, w, scratch):
    a = w["A"]
    calls = scratch("admin_tool", min_role=Role.ADMIN)
    out = await _invoke(factory, a["admin"], "admin_tool", {"budget_id": a["b1"]})
    async with factory() as db:
        await db.execute(update(User).where(User.id == a["admin"]).values(role=Role.MEMBER))
        await db.commit()
    err = await _refused(_confirm(factory, a["admin"], out["action_id"]))
    assert err.code == "insufficient_role"
    assert (await _row(factory, out["action_id"])).status.value == "failed"
    assert calls == []


async def test_fp11_token_rescoped_to_read_between_preview_and_confirm(factory, w):
    a = w["A"]
    out = await _stage(factory, w, api_token_id=a["t1"], **MCP)
    err = await _refused(_confirm(factory, a["member"], out["action_id"], api_token_id=a["t1"],
                                  channel="mcp", scope="agent:read"))
    assert err.code == "scope_denied"
    assert (await _row(factory, out["action_id"])).status.value == "failed"
    assert (await _amount(factory, a["b1"])) == Decimal("100.00")


async def test_fp11_entitlement_and_retired_tool(factory, w, scratch):
    a = w["A"]
    out = await _stage(factory, w)
    async with factory() as db:
        await db.execute(update(OrgFeatureOverride).values(value=False))
        await db.commit()
    assert (await _refused(_confirm(factory, a["member"], out["action_id"]))).code == "feature_not_entitled"
    async with factory() as db:
        await db.execute(update(OrgFeatureOverride).values(value=True))
        await db.commit()
    scratch("retiring")
    out = await _invoke(factory, a["member"], "retiring", {"budget_id": a["b1"]})
    registry._TOOLS.pop("retiring")
    err = await _refused(_confirm(factory, a["member"], out["action_id"]))
    assert err.code == "tool_retired"
    assert (await _row(factory, out["action_id"])).error_code == "tool_retired"


# ── F-P12 ─────────────────────────────────────────────────────────────────

async def test_fp12_live_pending_ceilings(factory, w):
    """FENCE F-P12. Wrong implementations: no ceiling; per-principal only (one
    user with several tokens drains the org)."""
    a = w["A"]
    for i in range(10):
        await _stage(factory, w, f"{101 + i}.00", api_token_id=a["t1"], **MCP)
    err = await _refused(_stage(factory, w, "150.00", api_token_id=a["t1"], **MCP))
    assert err.code == "too_many_pending_actions"


async def test_fp12_per_user_ceiling_spans_tokens_and_in_app(factory, w):
    a = w["A"]
    for i in range(5):
        await _stage(factory, w, f"{101 + i}.00", api_token_id=a["t1"], **MCP)
        await _stage(factory, w, f"{111 + i}.00", api_token_id=a["t2"], **MCP)
    for kw in ({"api_token_id": a["t1"], **MCP}, {}):
        err = await _refused(_stage(factory, w, "150.00", **kw))
        assert err.code == "too_many_pending_actions"
    # another user of the org is unaffected
    await _stage(factory, w, "150.00", uid=a["other"])


async def test_fp12_per_org_ceiling(factory, w):
    a = w["A"]
    async with factory() as db:
        db.add_all(_seed_pending(a["org"], a["other"], 49))
        await db.commit()
    await _stage(factory, w, "150.00")  # the 50th
    err = await _refused(_stage(factory, w, "151.00"))
    assert err.code == "too_many_pending_actions"


async def test_fp12_expired_and_decided_rows_do_not_count(factory, w):
    a = w["A"]
    async with factory() as db:
        rows = _seed_pending(a["org"], a["member"], 10)
        for r in rows[:5]:
            r.expires_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
        for r in rows[5:]:
            r.status = "cancelled"
        db.add_all(rows)
        await db.commit()
    await _stage(factory, w, "150.00")



def _k(key: str) -> str:
    """The live windowed Redis key for bucket ``key`` (window from its suffix)."""
    window = {"min": 60, "day": 86_400}.get(key.rsplit(":", 1)[1], 3_600)
    return actions.window_key(key, window)


async def test_fp12_daily_preview_caps_fail_closed_at_the_boundary(factory, w):
    a = w["A"]
    fake = _fake()
    fake._kv[_k(f"agent:usr:{a['member']}:preview:day")] = 199
    await _stage(factory, w, "101.00")
    err = await _refused(_stage(factory, w, "102.00"))
    assert err.code == "preview_rate_limited"
    fake._kv.clear()
    fake._kv[_k(f"agent:org:{a['org']}:preview:day")] = 1000
    assert (await _refused(_stage(factory, w, "103.00"))).code == "preview_rate_limited"
    fake._kv.clear()
    fake._kv[_k(f"agent:tok:{a['t1']}:preview:day")] = 200
    assert (await _refused(_stage(factory, w, "104.00", api_token_id=a["t1"], **MCP))
            ).code == "preview_rate_limited"
    fake._kv.clear()
    fake._kv[_k(f"agent:tok:{a['t1']}:preview:min")] = 20
    assert (await _refused(_stage(factory, w, "105.00", api_token_id=a["t1"], **MCP))
            ).code == "preview_rate_limited"
    fake._kv.clear()
    await _stage(factory, w, "106.00")
    assert fake._ttls[_k(f"agent:usr:{a['member']}:preview:day")] == 86_400


class _DownRedis:
    async def incr(self, key):
        raise RedisError("down")

    async def expire(self, *a, **k):
        raise RedisError("down")


@pytest.mark.parametrize("client", [None, _DownRedis()], ids=["no-client", "redis-error"])
async def test_fp12_limits_unavailable_fails_closed_everywhere(factory, w, monkeypatch, client):
    """FENCE F-P12 / F-A7. Wrong implementation: a daily cap (or any write
    bucket) failing OPEN when Redis is absent or erroring."""
    a = w["A"]
    ok = await _stage(factory, w)
    monkeypatch.setattr(redis_client, "get_client", lambda: client)
    assert (await _refused(_stage(factory, w, "150.00"))).code == "limits_unavailable"
    assert (await _refused(_confirm(factory, a["member"], ok["action_id"]))).code == "limits_unavailable"
    assert (await _row(factory, ok["action_id"])).status.value == "pending"
    assert (await _refused(_stage(factory, w, "150.00", api_token_id=a["t1"], **AUTO))
            ).code == "limits_unavailable"
    assert len(await _rows(factory)) == 1
    assert (await _amount(factory, a["b1"])) == Decimal("100.00")


async def test_fp12_stale_re_preview_at_the_ceiling_is_409_not_429(factory, w):
    """FENCE F-P12. Wrong implementation: the live-pending ceiling applied to
    the stale re-preview. The claimed row has already left ``pending``; the
    other rows here sit AT the ceiling, so only an exemption returns 409."""
    a = w["A"]
    out = await _stage(factory, w)
    async with factory() as db:
        db.add_all(_seed_pending(a["org"], a["member"], 10))
        await db.commit()
    await _set_amount(factory, a["b1"], "111.00")
    err = await _refused(_confirm(factory, a["member"], out["action_id"]))
    assert err.code == "preview_stale"
    assert (await _row(factory, err.data["action_id"])).status.value == "pending"


async def test_payload_bounds(factory, w, scratch):
    scratch("big_tool", big=True)
    err = await _refused(_invoke(factory, w["A"]["member"], "big_tool", {"budget_id": w["A"]["b1"]}))
    assert err.code == "payload_too_large"
    assert await _rows(factory) == []


# ── F-P13 ─────────────────────────────────────────────────────────────────

async def test_fp13_entity_deleted_after_preview_fails_the_row(factory, w):
    """FENCE F-P13. Wrong implementation: the re-preview's exception escapes
    between claim and status update, leaving the row ``executing`` forever."""
    a = w["A"]
    out = await _stage(factory, w)
    async with factory() as db:
        await db.delete(await db.get(Budget, a["b1"]))
        await db.commit()
    err = await _refused(_confirm(factory, a["member"], out["action_id"]))
    assert err.code == "not_found"
    row = await _row(factory, out["action_id"])
    assert (row.status.value, row.error_code) == ("failed", "not_found")
    assert [x.outcome.value for x in await _audits(factory)] == ["failure"]


# ── F-P8 ──────────────────────────────────────────────────────────────────

async def test_fp8_one_audit_row_per_confirm_with_the_token(factory, w):
    a = w["A"]
    out = await _stage(factory, w, api_token_id=a["t1"], **MCP)
    await _stage(factory, w, "130.00", api_token_id=a["t1"], **MCP)  # a second preview: no audit
    assert await _audits(factory) == []
    await _confirm(factory, a["member"], out["action_id"], api_token_id=a["t1"], **MCP)
    (ev,) = await _audits(factory)
    assert ev.api_token_id == a["t1"] and ev.actor_user_id == a["member"]
    assert ev.target_org_id == a["org"] and ev.outcome.value == "success"
    assert ev.detail == {
        "action_id": out["action_id"], "tool": "budgets_update_amount", "channel": "mcp",
        "risk": "write", "mode": "confirm", "args_sha256": ev.detail["args_sha256"],
        "status": "done", "error_code": None,
    }
    inapp = await _stage(factory, w, "140.00")
    await _confirm(factory, a["member"], inapp["action_id"])
    assert len(await _audits(factory)) == 2


# ── cancel ────────────────────────────────────────────────────────────────

async def test_cancel_is_pending_only_unaudited_and_principal_bound(factory, w):
    a = w["A"]
    out = await _stage(factory, w, api_token_id=a["t1"], **MCP)
    assert (await _refused(_cancel(factory, a["member"], out["action_id"],
                                   api_token_id=a["t2"], **MCP))).code == "action_not_found"
    await _cancel(factory, a["member"], out["action_id"], api_token_id=a["t1"], **MCP)
    assert (await _row(factory, out["action_id"])).status.value == "cancelled"
    err = await _refused(_cancel(factory, a["member"], out["action_id"], api_token_id=a["t1"], **MCP))
    assert (err.code, err.data["status"]) == ("action_already_decided", "cancelled")
    err = await _refused(_confirm(factory, a["member"], out["action_id"], api_token_id=a["t1"], **MCP))
    assert err.code == "action_already_decided"
    assert await _audits(factory) == []
    assert (await _amount(factory, a["b1"])) == Decimal("100.00")


async def test_confirm_bucket_is_30_per_hour_and_checked_before_the_claim(factory, w):
    a = w["A"]
    out = await _stage(factory, w)
    _fake()._kv[_k(f"agent:usr:{a['member']}:confirm")] = 30
    assert (await _refused(_confirm(factory, a["member"], out["action_id"]))).code == "confirm_rate_limited"
    assert (await _row(factory, out["action_id"])).status.value == "pending"


async def test_sensitive_confirm_over_mcp_has_its_own_daily_bucket(factory, w, scratch):
    a = w["A"]
    calls = scratch("sens_tool", risk="sensitive")
    out = await _invoke(factory, a["member"], "sens_tool", {"budget_id": a["b1"]},
                        api_token_id=a["t1"], **MCP)
    _fake()._kv[_k(f"agent:tok:{a['t1']}:sensitive:day")] = 10
    err = await _refused(_confirm(factory, a["member"], out["action_id"], api_token_id=a["t1"], **MCP))
    assert err.code == "sensitive_budget_exhausted"
    assert (await _row(factory, out["action_id"])).status.value == "failed"
    assert calls == []
    # in-app sensitive confirms are not on that bucket
    inapp = await _invoke(factory, a["member"], "sens_tool", {"budget_id": a["b1"]})
    await _confirm(factory, a["member"], inapp["action_id"])
    assert calls == ["sens_tool"]


# ── auto ──────────────────────────────────────────────────────────────────

async def test_auto_executes_in_the_same_request_through_confirm(factory, w):
    a = w["A"]
    out = await _stage(factory, w, api_token_id=a["t1"], **AUTO)
    assert out["status"] == "done" and out["result"]["amount"] == "120.00"
    assert "requires_confirmation" not in out
    assert (await _amount(factory, a["b1"])) == Decimal("120.00")
    row = await _row(factory, out["action_id"])
    assert (row.status.value, row.mode.value) == ("done", "auto")
    (ev,) = await _audits(factory)
    assert ev.detail["mode"] == "auto" and ev.api_token_id == a["t1"]


async def test_fa1_auto_does_not_reach_a_sensitive_tool(factory, w, scratch):
    """FENCE F-A1. Wrong implementation: the auto branch keyed on token scope
    alone, so ``agent:auto`` executes a ``sensitive`` tool."""
    a = w["A"]
    calls = scratch("sens_tool", risk="sensitive")
    out = await _invoke(factory, a["member"], "sens_tool", {"budget_id": a["b1"]},
                        api_token_id=a["t1"], **AUTO)
    assert out["requires_confirmation"] is True and calls == []
    row = await _row(factory, out["action_id"])
    assert (row.status.value, row.mode.value, row.risk.value) == ("pending", "confirm", "sensitive")


async def test_in_app_never_reaches_auto(factory, w):
    out = await _stage(factory, w)
    assert out["requires_confirmation"] is True
    assert (await _row(factory, out["action_id"])).mode.value == "confirm"
    err = await _refused(_stage(factory, w, "121.00", scope="agent:auto"))
    assert err.code == "scope_denied"


async def test_fa2_auto_re_runs_the_gates_at_confirm(factory, w, scratch):
    """FENCE F-A2. Wrong implementation: the auto path calling ``spec.execute``
    directly after staging. Budgets are switched off DURING the preview (after
    invoke's own gates), so only confirm's gate re-run can refuse it."""
    a = w["A"]

    async def switch_off(ctx):
        if not await ctx.db.scalar(select(OrgSetting.id).where(OrgSetting.key == "orgpref.budgets")):
            ctx.db.add(OrgSetting(org_id=ctx.org_id, key="orgpref.budgets", value="off"))
            await ctx.db.commit()

    calls = scratch(on_preview=switch_off)
    err = await _refused(_invoke(factory, a["member"], "scratch_write", {"budget_id": a["b1"]},
                                 api_token_id=a["t1"], **AUTO))
    assert err.code == "feature_disabled" and calls == []
    (row,) = await _rows(factory)
    assert (row.status.value, row.mode.value, row.error_code) == ("failed", "auto", "feature_disabled")
    (ev,) = await _audits(factory)
    assert ev.detail["mode"] == "auto" and ev.outcome.value == "failure"


async def test_fa7_auto_budget_exhausted_is_429_never_a_silent_preview(factory, w):
    """FENCE F-A7. Wrong implementations: falling back to a pending preview
    when the auto budget is spent; failing open when Redis is down (see the
    fail-closed test)."""
    a = w["A"]
    fake = _fake()
    fake._kv[_k(f"agent:tok:{a['t1']}:auto:day")] = 99
    out = await _stage(factory, w, api_token_id=a["t1"], **AUTO)  # the 100th
    assert out["status"] == "done"
    err = await _refused(_stage(factory, w, "130.00", api_token_id=a["t1"], **AUTO))  # the 101st
    assert err.code == "auto_budget_exhausted"
    assert len(await _rows(factory)) == 1  # nothing staged
    assert (await _amount(factory, a["b1"])) == Decimal("120.00")
    # the per-user cap is separate
    fake._kv.clear()
    fake._kv[_k(f"agent:usr:{a['member']}:auto:day")] = 200
    assert (await _refused(_stage(factory, w, "131.00", api_token_id=a["t2"], **AUTO))
            ).code == "auto_budget_exhausted"


async def test_auto_confirm_bucket_429_cancels_the_staged_row(factory, w):
    a = w["A"]
    _fake()._kv[_k(f"agent:tok:{a['t1']}:confirm")] = 30
    err = await _refused(_stage(factory, w, api_token_id=a["t1"], **AUTO))
    assert err.code == "confirm_rate_limited"
    (row,) = await _rows(factory)
    assert row.status.value == "cancelled"
    assert (await _amount(factory, a["b1"])) == Decimal("100.00")


# ── F-A3 ──────────────────────────────────────────────────────────────────

# Bookkeeping tables an executing tool may touch without disclosing them.
# Adding one needs a comment naming why.
_BOOKKEEPING = {
    "agent_pending_actions",  # the action's own status row
    "audit_events",           # the one agent.action.executed row (and service audits)
    "usage_counters",         # meters
    "ai_usage_ledger",        # provider spend ledger
    "api_tokens",             # last-used stamps
}

_WRITE_CASES = {
    "budgets_update_amount": lambda a: {"budget_id": a["b1"], "amount": "120.00"},
}


def test_fa3_every_write_tool_has_a_case():
    live = {t.name for t in registry.all_tools() if t.risk != "read"}
    live -= {"scratch_write", "admin_tool", "sens_tool", "big_tool", "retiring"}
    assert live == set(_WRITE_CASES), "a write tool was added without an F-A3 case"


async def _snapshot(f) -> dict[str, dict]:
    out = {}
    async with f() as db:
        for t in Base.metadata.sorted_tables:
            pk = list(t.primary_key.columns.keys())
            rows = (await db.execute(select(t))).all()
            out[t.name] = {tuple(r._mapping[c] for c in pk): tuple(r) for r in rows}
    return out


@pytest.mark.parametrize("tool", sorted(_WRITE_CASES))
async def test_fa3_rows_written_are_a_subset_of_the_disclosed_changes(factory, w, tool):
    """FENCE F-A3. Wrong implementation: a ``write`` tool with an undisclosed
    side write. Whole-database row diff, not review."""
    a = w["A"]
    args = _WRITE_CASES[tool](a)
    out = await _invoke(factory, a["member"], tool, args)
    disclosed = {(c["entity"], (c["id"],)) for c in out["changes"]}
    assert disclosed
    before = await _snapshot(factory)
    await _confirm(factory, a["member"], out["action_id"])
    after = await _snapshot(factory)
    touched = set()
    for table in before:
        for pk in before[table].keys() | after[table].keys():
            if before[table].get(pk) != after[table].get(pk):
                touched.add((table, pk))
    undisclosed = {x for x in touched if x[0] not in _BOOKKEEPING} - disclosed
    assert not undisclosed, undisclosed
    assert disclosed <= touched, "a disclosed change was not written"


# ── review round 1 ────────────────────────────────────────────────────────

async def test_fp4_a_null_token_mcp_row_is_not_an_in_app_row(factory, w):
    """FENCE F-P4 (channel clause). An MCP row whose token was SET NULL looks
    like an in-app row on the token column alone; only ``channel`` keeps the
    in-app user from confirming or cancelling it."""
    a = w["A"]
    out = await _stage(factory, w, api_token_id=a["t1"], **MCP)
    async with factory() as db:
        await db.execute(update(AgentPendingAction).values(api_token_id=None))
        await db.commit()
    assert (await _refused(_confirm(factory, a["member"], out["action_id"]))).code == "action_not_found"
    assert (await _refused(_cancel(factory, a["member"], out["action_id"]))).code == "action_not_found"
    assert (await _row(factory, out["action_id"])).status.value == "pending"


async def test_fp12_mcp_per_user_daily_preview_bucket(factory, w):
    """FENCE F-P12. Wrong implementation: no per-USER daily bucket over MCP
    (a user with several tokens escapes the 200/day)."""
    a = w["A"]
    _fake()._kv[_k(f"agent:usr:{a['member']}:preview:day")] = 200
    err = await _refused(_stage(factory, w, api_token_id=a["t1"], **MCP))
    assert err.code == "preview_rate_limited"


async def test_auto_re_preview_reads_fresh_data(factory, w, engine):
    """FENCE (auto drift). Wrong implementation: the preview query serving the
    session's cached Budget, so a change made between stage and confirm in the
    auto path (same session) is invisible."""
    a = w["A"]
    spec = registry._TOOLS["budgets_update_amount"]
    real, n, keep = spec.preview, [], []

    async def preview_then_drift(ctx, args):
        out = await real(ctx, args)
        if not n:
            n.append(1)
            # Anything holding the Budget keeps it in the session's identity
            # map (it is weakly referenced otherwise).
            keep.append(await ctx.db.get(Budget, a["b1"]))
            other = async_sessionmaker(engine, expire_on_commit=False)
            async with other() as db:
                await db.execute(update(Budget).where(Budget.id == a["b1"]).values(amount=Decimal("111.00")))
                await db.commit()
        return out

    registry._TOOLS["budgets_update_amount"] = ToolSpec(**{**spec.__dict__, "preview": preview_then_drift})
    try:
        err = await _refused(_stage(factory, w, api_token_id=a["t1"], **AUTO))
    finally:
        registry._TOOLS["budgets_update_amount"] = spec
    assert err.code == "preview_stale"
    # The auto path's error data reaches the model through invoke: wrapped there.
    assert err.data["context"]["category_name"] == {"untrusted": "Food A"}
    assert (await _amount(factory, a["b1"])) == Decimal("111.00")


async def test_finish_commit_failure_still_leaves_one_failed_row_and_one_audit(factory, w, scratch):
    """GUARD. The status commit fails once: the ``finally`` recovery moves the
    row out of ``executing`` and writes exactly one audit row."""
    a = w["A"]
    scratch()
    out = await _invoke(factory, a["member"], "scratch_write", {"budget_id": a["b1"]})
    async with factory() as db:
        u = await db.get(User, a["member"])
        real, n = db.commit, []

        async def flaky():
            n.append(1)
            if len(n) == 2:  # 1 = the claim, 2 = the status commit
                raise RuntimeError("connection lost")
            return await real()

        db.commit = flaky
        err = await _refused(registry.confirm_action(
            db, u, out["action_id"], channel="in_app", scope=None, api_token_id=None))
    assert err.code == "internal"
    row = await _row(factory, out["action_id"])
    assert (row.status.value, row.error_code) == ("failed", "internal")
    assert [x.outcome.value for x in await _audits(factory)] == ["failure"]


async def test_cancel_over_mcp_needs_write_scope(factory, w):
    a = w["A"]
    out = await _stage(factory, w, api_token_id=a["t1"], **MCP)
    err = await _refused(_cancel(factory, a["member"], out["action_id"], api_token_id=a["t1"],
                                 channel="mcp", scope="agent:read"))
    assert err.code == "scope_denied"
    assert (await _row(factory, out["action_id"])).status.value == "pending"
    await _cancel(factory, a["member"], out["action_id"], api_token_id=a["t1"],
                  channel="mcp", scope="agent:auto")


async def test_error_data_is_wrapped_untrusted(factory, w):
    a = w["A"]
    out = await _stage(factory, w)
    await _confirm(factory, a["member"], out["action_id"])
    err = await _refused(_confirm(factory, a["member"], out["action_id"]))
    assert err.data["result"]["category_name"] == {"untrusted": "Food A"}
