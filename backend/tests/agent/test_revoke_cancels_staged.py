"""TBD-664 review: a revoked agent token's staged actions die with it.

The app confirms what a connected agent staged (sensitive actions only ever
run from there), so a token revoked because it misbehaved must not leave its
proposals waiting to be applied for the rest of their ten minutes. Every
revoke path cancels the token's pending rows in the same transaction, and
only that token's.
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from app.models.api_token import ApiToken
from app.models.user import User
from app.routers import agent_tokens
from app.services import api_token_service as svc
from app.services import oauth_service

from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    MCP, _invoke, _refused, _row, _confirm, engine, factory, w,
)


def _request() -> Request:
    return Request({"type": "http", "method": "DELETE", "path": "/", "headers": [],
                    "client": ("203.0.113.9", 1)})


async def _own(db, a, token):
    return await db.get(User, a["member"]), await db.get(ApiToken, a[token])


async def _single(db, f, a):
    user, _ = await _own(db, a, "t1")
    assert await svc.revoke(db, a["t1"], user) is not None


async def _all(db, f, a):
    user, _ = await _own(db, a, "t1")
    await svc.revoke_all(db, user)


async def _all_agent(db, f, a):
    user, _ = await _own(db, a, "t1")
    await svc.revoke_all_agent(db, user)


async def _route(db, f, a):  # DELETE /agent/tokens/{id} and the admin's /org/{id}
    user, row = await _own(db, a, "t1")
    await agent_tokens._revoke(db, f, _request(), (user.id, user.email, user.org_id), row, {})


async def _oauth_reuse(db, f, a):  # OAuth code or refresh-token reuse
    _, row = await _own(db, a, "t1")
    await oauth_service._revoke(db, f, oauth_service._snap(row), "refresh_reuse", "203.0.113.9")


PATHS = [_single, _all, _all_agent, _route, _oauth_reuse]


@pytest.mark.parametrize("revoke", PATHS, ids=[p.__name__.strip("_") for p in PATHS])
async def test_every_revoke_cancels_the_tokens_staged_actions(factory, w, revoke):
    """FENCE. Wrong implementation: a revoke that only stamps ``revoked_at``,
    so a revoked token's staged delete stays one click from running in the app."""
    a = w["A"]
    staged = await _invoke(factory, a["member"], "transactions_delete", {"transaction_id": a["tx"]},
                           api_token_id=a["t1"], **MCP)
    mine = await _invoke(factory, a["member"], "budgets_update_amount",
                         {"budget_id": a["b1"], "amount": "120.00"})
    async with factory() as db:
        await revoke(db, factory, a)
    row = await _row(factory, staged["action_id"])
    assert row.status.value == "cancelled" and row.decided_at is not None
    err = await _refused(_confirm(factory, a["member"], staged["action_id"]))
    assert (err.code, err.data["status"]) == ("action_already_decided", "cancelled")
    # the in-app row is no token's: untouched
    assert (await _row(factory, mine["action_id"])).status.value == "pending"


async def test_a_single_revoke_leaves_the_users_other_token_alone(factory, w):
    a = w["A"]
    other = await _invoke(factory, a["member"], "transactions_delete", {"transaction_id": a["tx"]},
                          api_token_id=a["t2"], **MCP)
    async with factory() as db:
        await _single(db, factory, a)
    assert (await _row(factory, other["action_id"])).status.value == "pending"
