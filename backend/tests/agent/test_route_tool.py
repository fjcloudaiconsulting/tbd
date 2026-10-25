"""TBD-664 PR A: route-bound agent tools (``app.agent.route_tool``).

Fences: T-B1 identical budgets spec · T-B2 the fields allowlist · T-B3 the
closed dependency map · T-B4 execute runs the route's own handler · T-B5
HTTPException 4xx -> ToolError · T-B6 the verb rule · T-B7 the live route.
Every fence names the wrong implementation it kills.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import registry
from app.agent.registry import ToolError, ToolRegistrationError
from app.agent.route_tool import route_tool
from app.auth.org_permissions import require_org_admin
from app.auth.pat import require_interactive_session
from app.database import get_db
from app.deps import get_current_user
from app.models.budget import Budget
from app.models.user import Role, User
from app.services.feature_gate import Feature
from app.services.feature_gate import require_feature as require_product_area

from tests.agent.test_actions import (  # noqa: F401  (fixtures)
    P_START, _invoke, _refused, engine, factory, w,
)


# ── T-B1: budgets_update_amount, rebuilt through the factory, is identical ──

BUDGET_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "budget_id": {"title": "Budget Id", "type": "integer"},
        "amount": {
            "anyOf": [{"exclusiveMinimum": 0.0, "type": "number"}, {"type": "string"}],
            "title": "Amount",
        },
    },
    "required": ["budget_id", "amount"],
    "title": "BudgetAmountArgs",
    "type": "object",
}


def test_tb1_budgets_spec_is_identical():
    """FENCE T-B1 (spec). Wrong implementation: the factory leaking the REST
    body's ``Optional``/``null`` into the args, dropping a constraint, or
    renaming the args model the providers see."""
    spec = registry.get_tool("budgets_update_amount")
    assert (spec.risk, spec.product_area, spec.min_role, spec.mirrors_route) == (
        "write", Feature.BUDGETS, Role.MEMBER, ("PUT", "/api/v1/budgets/{budget_id}"),
    )
    assert spec.description == (
        "Change the amount of one existing budget. Returns a preview to confirm; "
        "nothing changes until it is confirmed."
    )
    assert spec.args.model_json_schema() == BUDGET_SCHEMA
    for bad in ({"budget_id": 1}, {"budget_id": 1, "amount": None}, {"budget_id": 1, "amount": "0"},
                {"budget_id": 1, "amount": "1.234"}, {"budget_id": 1, "amount": "1e20"},
                {"budget_id": 1, "amount": "NaN"}, {"budget_id": 1, "amount": "1", "x": 1}):
        with pytest.raises(PydanticValidationError):
            spec.args.model_validate(bad)


async def test_tb1_budgets_preview_and_result_are_identical(factory, w):
    """FENCE T-B1 (behaviour). The staged preview, the no-change and
    not-found refusals, and the executed result keep their v1 shape."""
    a = w["A"]
    out = await _invoke(factory, a["member"], "budgets_update_amount",
                        {"budget_id": a["b1"], "amount": "120"})
    out.pop("action_id"), out.pop("expires_at")
    assert out == {
        "summary": f"Change the amount of budget {a['b1']} from 100.00 to 120.00 EUR",
        "changes": [{"entity": "budgets", "id": a["b1"], "field": "amount",
                     "before": "100.00", "after": "120.00", "currency": "EUR"}],
        "warnings": [],
        "context": {"category_name": {"untrusted": "Food A"}, "period_start": P_START.isoformat()},
        "requires_confirmation": True,
    }
    err = await _refused(_invoke(factory, a["member"], "budgets_update_amount",
                                 {"budget_id": a["b1"], "amount": "100.00"}))
    assert (err.code, err.detail) == ("no_change", "the budget already has this amount")
    err = await _refused(_invoke(factory, a["member"], "budgets_update_amount",
                                 {"budget_id": w["B"]["b1"], "amount": "1.00"}))
    assert (err.code, err.detail) == ("not_found", "Budget not found")


# ── scratch routes ────────────────────────────────────────────────────────

class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=10)
    amount: Decimal | None = Field(default=None, gt=0)
    note: str | None = None


def _router(*deps, guard=None, verb=False, request=False, product=None):
    r = APIRouter(prefix="/api/v1/widgets",
                  dependencies=[Depends(require_product_area(product))] if product else [])

    async def _handler(widget_id: int, body: _Body, current_user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
        if guard is not None:
            guard(widget_id)
        return {"id": widget_id, **body.model_dump(exclude_unset=True)}

    async def _with_request(widget_id: int, body: _Body, request: Request,
                            current_user: User = Depends(get_current_user)):
        return {}

    handler = _with_request if request else _handler
    path = "/{widget_id}/approve" if verb else "/{widget_id}"
    (r.post if verb else r.put)(path, dependencies=list(deps))(handler)
    return r


def _tool(router=None, **over):
    path = "/api/v1/widgets/{widget_id}/approve" if over.pop("verb", False) else "/api/v1/widgets/{widget_id}"
    base = dict(
        name="widget_edit", risk="write", router=router or _router(),
        method="POST" if path.endswith("approve") else "PUT", path=path,
        fields=("name",), product_area=None, min_role=Role.MEMBER, description="t",
        entity=Budget, preview=_hand_preview,
    )
    base.update(over)
    return route_tool(**base)


async def _hand_preview(ctx, args):
    return registry.Preview(summary="s", changes=[])


# ── T-B2: the fields allowlist ────────────────────────────────────────────

def test_tb2_args_are_the_path_params_plus_the_allowlist_only():
    """FENCE T-B2. Wrong implementation: the whole REST body exposed (the
    agent could set ``amount`` or ``note`` through a tool that allows only
    ``name``), or an allowlist naming a field the route does not take."""
    spec = _tool()
    assert list(spec.args.model_fields) == ["widget_id", "name"]
    with pytest.raises(PydanticValidationError):
        spec.args.model_validate({"widget_id": 1, "amount": "5"})
    with pytest.raises(ToolRegistrationError, match="not a field of"):
        _tool(fields=("name", "colour"))


def test_tb2_the_allowlist_is_required():
    """FENCE T-B2. Wrong implementation: ``fields`` defaulting to the whole body."""
    with pytest.raises(TypeError):
        route_tool(name="x", risk="read", router=_router(), method="PUT",
                   path="/api/v1/widgets/{widget_id}", product_area=None,
                   min_role=Role.MEMBER, description="t")


def test_tb2_the_routes_own_validators_run_on_the_args():
    """FENCE T-B2. Wrong implementation: copying field types without the
    route model's validators (``max_length`` here; ``description_not_empty``
    on transactions, fenced by F-R4)."""
    spec = _tool()
    with pytest.raises(PydanticValidationError):
        spec.args.model_validate({"widget_id": 1, "name": "x" * 11})
    assert spec.args.model_validate({"widget_id": 1, "name": "ok"}).name == "ok"


def test_tb2_null_only_where_declared_nullable_and_required_is_required():
    """FENCE T-B2. Wrong implementation: an omitted-means-unchanged field
    accepting null (the preview would promise a clear the service ignores)."""
    spec = _tool(fields=("name", "note"), nullable=("note",), required=("name",))
    with pytest.raises(PydanticValidationError):
        spec.args.model_validate({"widget_id": 1, "name": None})
    with pytest.raises(PydanticValidationError):
        spec.args.model_validate({"widget_id": 1, "note": "n"})  # name is required
    ok = spec.args.model_validate({"widget_id": 1, "name": "a", "note": None})
    assert ok.model_fields_set == {"widget_id", "name", "note"}
    with pytest.raises(ToolRegistrationError):
        _tool(fields=("name",), nullable=("note",))


# ── T-B3: the closed dependency map ───────────────────────────────────────

def _unknown_dep():
    return None


@pytest.mark.parametrize("router,match", [
    (lambda: _router(Depends(require_interactive_session)), "interactive"),
    (lambda: _router(Depends(_unknown_dep)), "unknown dependency"),
    (lambda: _router(request=True), "request"),
    (lambda: _router(product=Feature.BUDGETS), "product area"),
    (lambda: _router(Depends(require_org_admin)), "role"),
], ids=["interactive-session", "unknown", "request-param", "product-gate", "role-gate"])
def test_tb3_registration_fails_closed(router, match):
    """FENCE T-B3. Wrong implementations: an unknown dependency silently
    skipped (its guard would never run in-process); a browser-only route
    (``require_interactive_session``) reachable by an agent; a handler that
    reads the raw ``Request``; a route gate stricter than the tool."""
    with pytest.raises(ToolRegistrationError, match=match):
        _tool(router=router())


def test_tb3_a_matching_gate_registers():
    """Control: the same gates register when the tool carries them."""
    _tool(router=_router(product=Feature.BUDGETS), product_area=Feature.BUDGETS)
    _tool(router=_router(Depends(require_org_admin)), min_role=Role.ADMIN)


# ── T-B4 / T-B5: execute runs the handler, 4xx becomes a ToolError ────────

def _ctx():
    return SimpleNamespace(user=SimpleNamespace(id=1, org_id=1), db=None, org_id=1)


async def test_tb4_execute_runs_the_route_handler_with_only_the_set_fields():
    """FENCE T-B4. Wrong implementation: execute calling a service directly
    (an inline handler guard would not run), or sending unset fields."""
    seen = []
    spec = _tool(router=_router(guard=seen.append), fields=("name", "note"), nullable=("note",))
    out = await spec.execute(_ctx(), spec.args.model_validate({"widget_id": 7, "note": None}))
    assert seen == [7]
    assert out == {"id": 7, "note": None}


@pytest.mark.parametrize("status,code", [
    (400, "invalid_arguments"), (403, "forbidden"), (404, "not_found"), (409, "conflict"),
    (422, "invalid_arguments"),
])
async def test_tb5_http_4xx_is_a_tool_error(status, code):
    """FENCE T-B5. Wrong implementation: an inline ``HTTPException`` escaping
    as an opaque internal error (or worse, as a 200)."""
    def guard(_):
        raise HTTPException(status_code=status, detail="refused here")

    spec = _tool(router=_router(guard=guard))
    with pytest.raises(ToolError) as exc:
        await registry.call_mapped(spec.execute, _ctx(), spec.args.model_validate({"widget_id": 1}))
    assert (exc.value.code, exc.value.detail) == (code, "refused here")


async def test_tb5_a_5xx_is_not_mapped():
    def guard(_):
        raise HTTPException(status_code=503, detail="down")

    spec = _tool(router=_router(guard=guard))
    with pytest.raises(HTTPException):
        await spec.execute(_ctx(), spec.args.model_validate({"widget_id": 1}))


# ── T-B6: the verb rule and the tiers ─────────────────────────────────────

def test_tb6_a_verb_route_needs_a_hand_written_preview():
    """FENCE T-B6. Wrong implementation: a templated "field before/after"
    preview for a verb route, which cannot know what the verb writes."""
    with pytest.raises(ToolRegistrationError, match="hand-written preview"):
        _tool(router=_router(verb=True), verb=True, preview=None)
    _tool(router=_router(verb=True), verb=True)  # control


def test_tb6_a_templated_preview_needs_an_entity_with_those_columns():
    with pytest.raises(ToolRegistrationError, match="entity"):
        _tool(preview=None, entity=None)
    with pytest.raises(ToolRegistrationError, match="column"):
        _tool(preview=None, fields=("note",))  # budgets has no ``note`` column


def test_tb6_tiers_are_enforced_by_register():
    """FENCE T-B6. A read tool on a body route, or a write tool on a GET,
    is refused by ``register`` (the factory does not bypass it)."""
    with pytest.raises(ToolRegistrationError):
        registry.register(_tool(name="widget_read", risk="read", preview=None, fields=()))
    assert registry.get_tool("widget_read") is None


# ── T-B7: the live route ──────────────────────────────────────────────────

def test_tb7_every_route_tool_is_bound_to_the_live_route():
    """FENCE T-B7. Wrong implementation: a tool built from a router the app
    does not mount (or mounts with extra dependencies), so the handler it runs
    is not the one REST runs."""
    from app.main import app
    from tests.app_routes import effective_routes

    live = {(m, r.path): r for r in effective_routes(app) for m in (r.methods or ())}
    bound = [t for t in registry.all_tools() if t.route is not None]
    assert {t.name for t in bound} >= {"budgets_update_amount", "transactions_get", "transactions_update"}
    for t in bound:
        r = live[t.mirrors_route]
        assert r.endpoint is t.route.endpoint, t.name

        def calls(dep):
            return [d.call for d in dep.dependencies]

        assert calls(r.dependant) == calls(t.route.dependant), t.name
