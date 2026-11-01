"""TBD-577: the in-app confirm / cancel / list routes (``/api/v1/agent/actions``).

F-P5: the executed args are the STORED args whatever the request carries.
Also the HTTP status map, the route guards, and the list filters.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.auth.pat import require_interactive_session
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models.agent_pending_action import AgentPendingAction
from app.models.user import User
from app.rate_limit import limiter

from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    _amount, _row, _set_amount, _stage, engine, factory, w,
)
from tests.app_routes import effective_routes


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


def _as(factory, uid):
    """Override auth + db so the routes run as user ``uid`` on ``factory``."""
    async def _db():
        async with factory() as db:
            yield db

    async def _user():
        async with factory() as db:
            return await db.get(User, uid)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_interactive_session] = _user


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


async def test_confirm_executes_the_stored_args_not_the_request_body(factory, w, client):
    """FENCE F-P5. Wrong implementation: confirm re-parsing args from the
    request. The body below names another budget and amount."""
    a = w["A"]
    out = await _stage(factory, w, "120.00")
    _as(factory, a["member"])
    r = await client.post(
        f"/api/v1/agent/actions/{out['action_id']}/confirm",
        json={"budget_id": a["b2"], "amount": "999.00", "tool": "x", "args": {"amount": "999.00"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done" and body["result"]["amount"] == "120.00"
    assert (await _amount(factory, a["b1"])) == Decimal("120.00")
    assert (await _amount(factory, a["b2"])) == Decimal("50.00")


async def test_status_map(factory, w, client):
    a = w["A"]
    _as(factory, a["member"])
    out = await _stage(factory, w, "120.00")
    url = f"/api/v1/agent/actions/{out['action_id']}"
    r = await client.post(f"/api/v1/agent/actions/{'0' * 32}/confirm")
    assert (r.status_code, r.json()["detail"]["code"]) == (404, "action_not_found")
    await _set_amount(factory, a["b1"], "111.00")
    r = await client.post(f"{url}/confirm")
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "preview_stale")
    fresh = r.json()["detail"]
    assert fresh["action_id"] != out["action_id"] and fresh["changes"][0]["before"] == "111.00"
    r = await client.post(f"{url}/confirm")
    d = r.json()["detail"]
    assert (r.status_code, d["code"], d["status"]) == (409, "action_already_decided", "stale")
    r = await client.post(f"/api/v1/agent/actions/{fresh['action_id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    r = await client.post(f"/api/v1/agent/actions/{fresh['action_id']}/cancel")
    assert r.status_code == 409
    # expired
    out = await _stage(factory, w, "130.00")
    async with factory() as db:
        import datetime
        await db.execute(update(AgentPendingAction).where(AgentPendingAction.id == out["action_id"])
                         .values(expires_at=datetime.datetime.utcnow() - datetime.timedelta(seconds=1)))
        await db.commit()
    r = await client.post(f"/api/v1/agent/actions/{out['action_id']}/confirm")
    assert (r.status_code, r.json()["detail"]["code"]) == (410, "action_expired")
    # gate refusal at confirm is 403, the row fails
    out = await _stage(factory, w, "140.00")
    async with factory() as db:
        from app.models.settings import OrgSetting
        db.add(OrgSetting(org_id=a["org"], key="orgpref.budgets", value="off"))
        await db.commit()
    r = await client.post(f"/api/v1/agent/actions/{out['action_id']}/confirm")
    assert (r.status_code, r.json()["detail"]["code"]) == (403, "feature_disabled")
    assert (await _row(factory, out["action_id"])).status.value == "failed"


async def test_list_is_own_rows_newest_first_with_filters(factory, w, client):
    a = w["A"]
    ids = [(await _stage(factory, w, f"{101 + i}.00"))["action_id"] for i in range(3)]
    await _stage(factory, w, "150.00", uid=a["other"])
    _as(factory, a["member"])
    r = await client.post(f"/api/v1/agent/actions/{ids[0]}/cancel")
    assert r.status_code == 200
    body = (await client.get("/api/v1/agent/actions")).json()
    assert [x["action_id"] for x in body["items"]] == ids[::-1]  # own rows only, newest first
    assert {"tool", "channel", "risk", "mode", "status", "preview", "expires_at"} <= set(body["items"][0])
    assert body["items"][0]["preview"]["changes"][0]["entity"] == "budgets"
    got = (await client.get("/api/v1/agent/actions", params={"status": "cancelled"})).json()
    assert [x["action_id"] for x in got["items"]] == [ids[0]]
    assert (await client.get("/api/v1/agent/actions", params={"mode": "auto"})).json()["items"] == []
    page = (await client.get("/api/v1/agent/actions", params={"limit": 1, "offset": 1})).json()
    assert [x["action_id"] for x in page["items"]] == [ids[1]]
    assert (await client.get("/api/v1/agent/actions", params={"limit": 51})).status_code == 422


async def test_b1_pending_means_live_and_live_agrees(factory, w, client, monkeypatch):
    """FENCE B1 (TBD-614). status=pending excludes a row with expires_at == now
    and a past one; each row's ``live`` agrees. Wrong implementations: no
    filter; ``>=``; ``live`` computed with a different ``now``."""
    import datetime as dt

    from app.models.agent_pending_action import AgentPendingAction as Action
    a = w["A"]
    t = dt.datetime(2026, 10, 20, 12, 0, 0)
    monkeypatch.setattr("app.routers.agent.utcnow_naive", lambda: t)
    past, edge, live = [(await _stage(factory, w, f"{101 + i}.00"))["action_id"] for i in range(3)]
    async with factory() as db:
        for aid, exp in ((past, t - dt.timedelta(minutes=1)), (edge, t), (live, t + dt.timedelta(minutes=5))):
            await db.execute(update(Action).where(Action.id == aid).values(expires_at=exp))
        await db.commit()
    _as(factory, a["member"])
    got = (await client.get("/api/v1/agent/actions", params={"status": "pending"})).json()["items"]
    assert [x["action_id"] for x in got] == [live]
    allrows = {x["action_id"]: x["live"] for x in (await client.get("/api/v1/agent/actions")).json()["items"]}
    assert allrows == {past: False, edge: False, live: True}


async def test_b2_channel_filter_excludes_mcp_pending(factory, w, client):
    """FENCE B2 (TBD-614). channel=in_app excludes the same user's MCP pending
    row. Wrong implementation: no channel filter."""
    from tests.agent.test_actions import _seed_pending
    a = w["A"]
    chat_id = (await _stage(factory, w, "111.00"))["action_id"]
    async with factory() as db:
        mcp = _seed_pending(a["org"], a["member"], 1, channel="mcp", token=a["t1"])
        db.add_all(mcp)
        await db.commit()
    _as(factory, a["member"])
    both = (await client.get("/api/v1/agent/actions", params={"status": "pending"})).json()["items"]
    assert len(both) == 2
    got = (await client.get("/api/v1/agent/actions", params={"status": "pending", "channel": "in_app"})).json()["items"]
    assert [x["action_id"] for x in got] == [chat_id]
    assert (await client.get("/api/v1/agent/actions", params={"channel": "bogus"})).status_code == 422


async def test_the_browser_lists_and_applies_what_a_connected_agent_staged(factory, w, client):
    """FENCE (TBD-664). Wrong implementation: the in-app confirm keyed on the
    channel, so a sensitive action staged over MCP (which MCP may never
    confirm) is listed in the app but no one can apply it."""
    from tests.agent.test_actions import _invoke
    from tests.agent.test_set_category import _tx

    a = w["A"]
    out = await _invoke(factory, a["member"], "transactions_delete", {"transaction_id": a["tx"]},
                        channel="mcp", scope="agent:write", api_token_id=a["t1"])
    _as(factory, a["member"])
    pending = (await client.get("/api/v1/agent/actions", params={"status": "pending"})).json()["items"]
    assert [(x["action_id"], x["channel"], x["risk"]) for x in pending] == [
        (out["action_id"], "mcp", "sensitive")]
    _as(factory, a["other"])  # another member of the org never reaches it
    r = await client.post(f"/api/v1/agent/actions/{out['action_id']}/confirm")
    assert (r.status_code, r.json()["detail"]["code"]) == (404, "action_not_found")
    _as(factory, a["member"])
    r = await client.post(f"/api/v1/agent/actions/{out['action_id']}/confirm")
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"deleted": True, "demoted_ids": []}
    assert await _tx(factory, a["tx"]) is None


async def test_the_list_names_the_token_that_staged_a_row(factory, w, client):
    """TBD-664 review: the person sees which of their agents proposed a change
    (the token's name); an in-app row names none."""
    from tests.agent.test_actions import _invoke

    a = w["A"]
    await _invoke(factory, a["member"], "transactions_delete", {"transaction_id": a["tx"]},
                  channel="mcp", scope="agent:write", api_token_id=a["t2"])
    await _stage(factory, w, "120.00")
    _as(factory, a["member"])
    items = (await client.get("/api/v1/agent/actions", params={"status": "pending"})).json()["items"]
    assert sorted((x["channel"], x["token_name"]) for x in items) == [("in_app", None), ("mcp", "t2")]


def test_routes_carry_the_interactive_and_entitlement_guards():
    """A PAT must never confirm (F-L7 shape) and the plan key gates the surface."""
    seen = {}
    for r in effective_routes(app):
        if getattr(r, "path", "").startswith("/api/v1/agent/actions"):
            calls = []

            def walk(dep):
                for sub in dep.dependencies:
                    calls.append(
                        f"{getattr(sub.call, '__module__', '')}.{getattr(sub.call, '__qualname__', '')}"
                    )
                    walk(sub)

            walk(r.dependant)
            seen[(tuple(sorted(r.methods)), r.path)] = calls
    assert len(seen) == 4, seen
    for key, calls in seen.items():
        assert "app.auth.pat.require_interactive_session" in calls, key
        assert "app.auth.feature_deps.require_feature.<locals>._dep" in calls, key
