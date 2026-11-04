"""TBD-669: the report tools beyond the honesty fence (test_agent_honesty).

The read POSTs, the route's own limit on the token, and the router's
visibility rules (see, edit) on every write.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app import rate_limit_db
from app.agent import registry
from app.agent.registry import ToolRegistrationError, ToolSpec
from app.models.report import ReportVersion
from app.models.user import Role
from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    _a_report, _invoke, _refused, _rows, engine, factory, reports_on, w,
)

QUERY = {"dataset": "transactions", "measure": {"agg": "sum", "field": "amount"}, "dimensions": ["category"]}
READ = {"channel": "mcp", "scope": "agent:read"}


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _run(ctx, args):
    return {}


def test_a_read_tool_mirrors_a_get_or_a_listed_read_post():
    """FENCE. Wrong implementation: any POST accepted as a read."""
    base = dict(risk="read", args=_NoArgs, product_area=None, min_role=Role.MEMBER,
                description="t", run=_run)
    with pytest.raises(ToolRegistrationError, match="must mirror a GET"):
        registry.register(ToolSpec(name="fixture_post_read", mirrors_route=("POST", "/api/v1/reports"), **base))
    try:
        registry.register(ToolSpec(name="fixture_post_read", mirrors_route=("POST", "/api/v1/reports/query"), **base))
    finally:
        registry._TOOLS.pop("fixture_post_read", None)


def test_the_query_tools_carry_their_routes_limits():
    assert registry.get_tool("reports_query").rate_limit == ("reports.query", 60, 60)
    assert registry.get_tool("reports_sankey_query").rate_limit == ("reports.sankey_query", 60, 60)


async def test_query_answers_and_the_route_limit_is_drawn_per_token(factory, w):
    """FENCE. Wrong implementation: the query tool escaping the REST route's
    60/minute because an in-process call never meets slowapi."""
    a = w["A"]
    await reports_on(factory, a)
    out = await _invoke(factory, a["member"], "reports_query", QUERY, api_token_id=a["t1"], **READ)
    assert out["meta"]["row_count"] == len(out["rows"]) == 1
    assert out["rows"][0]["category"] == {"untrusted": "Food A"}
    rate_limit_db.hit(f"agent:tok:{a['t1']}:route:reports.query", 60, amount=59)
    err = await _refused(_invoke(factory, a["member"], "reports_query", QUERY, api_token_id=a["t1"], **READ))
    assert err.code == "token_rate_limited"
    # another token, the sankey bucket and the in-app channel are unaffected
    await _invoke(factory, a["member"], "reports_query", QUERY, api_token_id=a["t2"], **READ)
    await _invoke(factory, a["member"], "reports_sankey_query", {}, api_token_id=a["t1"], **READ)
    await _invoke(factory, a["member"], "reports_query", QUERY)


async def test_query_refusals(factory, w):
    a = w["A"]
    err = await _refused(_invoke(factory, a["member"], "reports_query", QUERY))
    assert err.code == "feature_disabled"  # Reports v2 off for the org
    await reports_on(factory, a)
    bad = {**QUERY, "dimensions": ["category"], "measure": {"agg": "sum", "field": "balance"}}
    assert (await _refused(_invoke(factory, a["member"], "reports_query", bad))).code == "invalid_arguments"


async def test_another_members_private_report_is_not_found_to_every_tool(factory, w):
    """FENCE. Wrong implementation: the org-scoped row read alone, which
    previews (and names) a colleague's private report."""
    a = w["A"]
    rid = await _a_report(factory, a, owner="other")
    async with factory() as db:
        vid = await db.scalar(select(ReportVersion.id).where(ReportVersion.report_id == rid))
    for tool, args in (
        ("reports_get", {}), ("reports_update", {"name": "x"}), ("reports_update_layout", {"visibility": "org"}),
        ("reports_duplicate", {}), ("reports_reset", {}), ("reports_delete", {}),
        ("report_version_restore", {"version_id": vid}),
    ):
        err = await _refused(_invoke(factory, a["admin"], tool, {"report_id": rid, **args}))
        assert err.code == "not_found", tool
    assert await _rows(factory) == []


async def test_an_org_shared_report_is_edited_by_its_owner_or_an_admin_only(factory, w):
    a = w["A"]
    rid = await _a_report(factory, a, owner="other", visibility="org")
    err = await _refused(_invoke(factory, a["member"], "reports_update", {"report_id": rid, "name": "x"}))
    assert err.code == "forbidden"
    # seeing it is enough to copy it
    out = await _invoke(factory, a["member"], "reports_duplicate", {"report_id": rid})
    assert out["requires_confirmation"] is True
    out = await _invoke(factory, a["admin"], "reports_update", {"report_id": rid, "name": "x"})
    assert out["changes"][0]["after"] == {"untrusted": "x"}


async def test_reset_and_restore_refuse_what_would_change_nothing(factory, w):
    a = w["A"]
    rid = await _a_report(factory, a)
    other = await _a_report(factory, a)
    async with factory() as db:
        foreign = await db.scalar(select(ReportVersion.id).where(ReportVersion.report_id == other))
    err = await _refused(_invoke(factory, a["member"], "reports_reset", {"report_id": rid}))
    assert err.code == "no_change"
    err = await _refused(_invoke(factory, a["member"], "report_version_restore",
                                 {"report_id": rid, "version_id": foreign}))
    assert err.code == "not_found"
