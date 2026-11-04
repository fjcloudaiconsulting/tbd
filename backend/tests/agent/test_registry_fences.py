"""TBD-559: agent tool registry fences that need no database.

F-R1 no tenancy fields · F-R2 ToolContext built only in the registry · F-R4
route parity (guards and REST validators) · F-R5 no egress imports · F-R6 name
regex and half-declared tools · F-R9 role rank · F-A5 never-expose denylist.

Every fence runs over the LIVE registry and the LIVE ``app.main`` routes, so a
tool added later is covered without editing this file.
"""
from __future__ import annotations

import ast
import pathlib
import re
from types import SimpleNamespace

import pytest
from fastapi.dependencies.utils import request_params_to_args
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError
from starlette.datastructures import QueryParams

import app.agent.registry as registry
from app.agent.route_tool import _without_none
from app.agent.registry import (
    ROLE_RANK,
    ToolError,
    ToolRegistrationError,
    ToolSpec,
    invoke,
    register,
)
from app.models.user import Role
from app.schemas.account import BalanceAdjustmentRequest
from app.services.feature_gate import Feature

APP_DIR = pathlib.Path(registry.__file__).resolve().parents[1]
TOOLS = registry.all_tools()


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _run(ctx, args):
    return {"ok": True}


async def _noop(*a, **k):
    return None


def _spec(**over) -> ToolSpec:
    base = dict(
        name="fixture_tool", risk="read", args=_NoArgs, product_area=None,
        min_role=Role.MEMBER, mirrors_route=("GET", "/api/v1/accounts"),
        description="test", run=_run,
    )
    base.update(over)
    return ToolSpec(**base)


@pytest.fixture(autouse=True)
def _no_leaked_fixture_tool():
    """A refusal test whose mutant lets ``fixture_tool`` register must not
    turn every later test into a "duplicate name" failure."""
    yield
    registry._TOOLS.pop("fixture_tool", None)


@pytest.fixture
def scratch_tool():
    """Register tools for one test and remove them afterwards."""
    added: list[str] = []

    def _add(spec: ToolSpec) -> ToolSpec:
        register(spec)
        added.append(spec.name)
        return spec

    yield _add
    for name in added:
        registry._TOOLS.pop(name, None)


FORECAST_PLAN_CHANGES = (
    "forecast_plan_populate", "forecast_plan_refresh", "forecast_plan_copy", "forecast_plan_item_add",
    "forecast_plan_items_bulk", "forecast_plan_item_update", "forecast_plan_item_delete",
    "forecast_plan_activate", "forecast_plan_revert", "forecast_plan_discard",
)


def test_registry_ships_the_six_v1_reads_and_the_writes():
    assert sorted(t.name for t in TOOLS) == sorted([
        "accounts_list", "categories_list", "budgets_list",
        "transactions_search", "spending_by_category", "forecast_get", "transactions_get",
        "budgets_update_amount", "transactions_set_category", "transactions_update",
        "transactions_update_money", "transactions_create", "transactions_delete",
        "transactions_transfer",
        # TBD-667
        "recurring_list", "recurring_create", "recurring_update", "recurring_update_money", "recurring_stop",
        "recurring_delete", "recurring_skip_next", "recurring_materialise_next",
        "recurring_generate", "billing_cycle_get", "billing_periods_list", "billing_cycle_update",
        "billing_period_create", "billing_periods_ensure_future", "billing_period_close",
        # TBD-665
        "accounts_get", "account_types_list", "cycle_payments_list",
        "accounts_create", "accounts_update", "accounts_update_money", "accounts_delete",
        "accounts_adjust_balance", "account_types_create", "account_types_update", "account_types_delete",
        "cycle_payments_create", "cycle_payments_upsert", "cycle_payments_delete",
        # TBD-666
        "budgets_create", "budgets_from_forecast", "budgets_copy_from_period",
        "budgets_draft_next", "budgets_rebalance", "budgets_delete",
        "categories_create", "categories_update", "categories_move", "categories_batch_move",
        "categories_delete", "categories_restore_recommended",
        # TBD-669
        "reports_list", "reports_get", "report_versions_list", "reports_query", "reports_sankey_query",
        "reports_update", "reports_update_layout", "reports_create", "reports_duplicate", "reports_reset",
        "report_version_restore", "reports_delete",
        # TBD-787
        "notification_preferences_get", "notification_preferences_update",
        # TBD-668
        "forecast_plan_get", "scenarios_list", "scenarios_get", "scenarios_simulate", "scenarios_compare",
        *FORECAST_PLAN_CHANGES, "scenarios_create", "scenarios_update", "scenarios_delete",
        "plan_goal_create", "plan_goal_update", "plan_goal_delete",
    ])
    assert {t.name for t in TOOLS if t.risk == "write"} == {
        "budgets_update_amount", "transactions_set_category", "transactions_update",
        "recurring_update",
        "accounts_update", "account_types_update",
        "budgets_rebalance", "categories_update",
    }
    # TBD-664 B: everything that moves money, creates or deletes is sensitive
    # (never auto, never confirmed over MCP). TBD-667: so is every billing-period
    # write (it moves the boundary every budget and report counts by).
    # TBD-666: so is every budget and category create, seed, move and delete,
    # and the draft (it adds billing periods); a rebalance and a category
    # edit are writes with an inverse.
    assert {t.name for t in TOOLS if t.risk == "sensitive"} == {
        "transactions_update_money", "transactions_create", "transactions_delete",
        "transactions_transfer",
        "recurring_create", "recurring_update_money", "recurring_stop", "recurring_delete",
        "recurring_skip_next",
        "recurring_materialise_next", "recurring_generate", "billing_cycle_update",
        "billing_period_create", "billing_periods_ensure_future", "billing_period_close",
        "accounts_create", "accounts_update_money", "accounts_delete", "accounts_adjust_balance",
        "account_types_create", "account_types_delete",
        "cycle_payments_create", "cycle_payments_upsert", "cycle_payments_delete",
        "budgets_create", "budgets_from_forecast", "budgets_copy_from_period",
        "budgets_draft_next", "budgets_delete",
        "categories_create", "categories_move", "categories_batch_move", "categories_delete",
        "categories_restore_recommended",
        # TBD-669: a layout edit saves a version and may evict one; a reset or
        # restore has no inverse its own args could stage.
        # TBD-669, TBD-787: a rename of a colleague's shared report and a
        # silenced alert both need the person.
        "reports_update", "notification_preferences_update",
        "reports_update_layout", "reports_create", "reports_duplicate", "reports_reset",
        "report_version_restore", "reports_delete",
        # TBD-668: every forecast plan change states or freezes planned amounts;
        # a plan's params carry amounts and funding accounts.
        *FORECAST_PLAN_CHANGES, "scenarios_create", "scenarios_update", "scenarios_delete",
        "plan_goal_create", "plan_goal_update", "plan_goal_delete",
    }


# ── F-R1 ──────────────────────────────────────────────────────────────────

_TENANCY = re.compile(r"^(org_id|org|organization|organization_id|user_id|owner|owner_id|created_by)$")


def _property_names(schema) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        for key, sub in (schema.get("properties") or {}).items():
            names.add(key)
            names |= _property_names(sub)
        for key, sub in schema.items():
            if key != "properties":
                names |= _property_names(sub)
    elif isinstance(schema, list):
        for sub in schema:
            names |= _property_names(sub)
    return names


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_fr1_no_tenancy_field_in_any_args_schema(tool):
    """FENCE F-R1. Wrong implementation: a tool args model with ``org_id``
    (or any tenancy-shaped field), recursively through ``$defs``."""
    bad = {n for n in _property_names(tool.args.model_json_schema()) if _TENANCY.match(n)}
    assert not bad, f"{tool.name} args expose tenancy fields {bad}"


def test_fr1_detector_sees_a_nested_tenancy_field():
    class Inner(BaseModel):
        org_id: int

    class Outer(BaseModel):
        filt: Inner

    assert "org_id" in _property_names(Outer.model_json_schema())


# ── F-R2 ──────────────────────────────────────────────────────────────────

def _toolcontext_call_sites() -> set[str]:
    sites = set()
    for path in APP_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                if name == "ToolContext":
                    sites.add(str(path.relative_to(APP_DIR)))
    return sites


def test_fr2_toolcontext_is_built_only_in_the_registry():
    """FENCE F-R2, both directions. Wrong implementation: a tool or a front
    door building its own context (so org_id could come from somewhere other
    than the authenticated user). The positive half fails if the registry
    stopped building it and the scan silently matched nothing."""
    assert _toolcontext_call_sites() == {"agent/registry.py"}


# ── F-R4 ──────────────────────────────────────────────────────────────────

def _live_routes() -> dict[tuple[str, str], object]:
    from app.main import app
    from tests.app_routes import effective_routes

    out = {}
    for r in effective_routes(app):
        for m in getattr(r, "methods", None) or ():
            out[(m, r.path)] = r
    return out


_BENIGN = {
    ("app.database", "get_db"),
    ("app.database", "_session"),
    ("app.deps", "get_current_user"),
    ("app.deps", "get_session_factory"),
    ("app.deps", "get_caller_session"),
    ("app.rate_limit", "get_client_ip"),
    ("app.rate_limit_overrides", "load_rate_limit_overrides"),
}


def _route_guards(route) -> tuple[set[Feature], Role, set[str]]:
    """(product areas, min role, plan feature keys) a route enforces, read from
    its dependency tree. An unrecognised dependency FAILS: a guard the fence
    cannot classify must not be silently treated as no guard."""
    areas: set[Feature] = set()
    plan_keys: set[str] = set()
    role = Role.MEMBER

    def walk(dep):
        nonlocal role
        for sub in dep.dependencies:
            call = sub.call
            mod = getattr(call, "__module__", "")
            qual = getattr(call, "__qualname__", "")
            if mod.startswith("fastapi.security") or (mod, qual) in _BENIGN:
                pass
            elif (mod, qual) == ("app.services.feature_gate", "require_feature.<locals>._dep"):
                areas.add(dict(zip(call.__code__.co_freevars, (c.cell_contents for c in call.__closure__)))["feature"])
            elif (mod, qual) == ("app.auth.feature_deps", "require_feature.<locals>._dep"):
                plan_keys.add(dict(zip(call.__code__.co_freevars, (c.cell_contents for c in call.__closure__)))["key"])
            elif (mod, qual) == ("app.auth.feature_deps", "get_current_org_features"):
                pass
            elif (mod, qual) == ("app.auth.org_permissions", "require_org_admin"):
                role = max(role, Role.ADMIN, key=ROLE_RANK.__getitem__)
            elif (mod, qual) == ("app.auth.org_permissions", "require_org_owner"):
                role = Role.OWNER
            else:
                raise AssertionError(f"{route.path}: unclassified dependency {mod}.{qual}")
            walk(sub)

    walk(route.dependant)
    return areas, role, plan_keys


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_fr4_guards_are_a_superset_of_the_mirrored_route(tool):
    """FENCE F-R4 (guards). Wrong implementations: a tool dropping
    ``Feature.BUDGETS`` while the router has it; a tool with a lower
    ``min_role`` than its route; a mirror naming a route that does not exist;
    a ``write`` tool mirroring a GET."""
    routes = _live_routes()
    assert tool.mirrors_route in routes, f"{tool.name} mirrors a route that does not exist"
    areas, role, plan_keys = _route_guards(routes[tool.mirrors_route])
    assert areas <= ({tool.product_area, *tool.extra_areas} - {None}), (tool.name, areas)
    assert ROLE_RANK[tool.min_role] >= ROLE_RANK[role], (tool.name, role)
    # Every tool is gated on ai.agent by invoke; a route needing another plan
    # key is a guard the tool does not carry.
    assert plan_keys <= {registry.AGENT_FEATURE_KEY}, (tool.name, plan_keys)
    if tool.risk != "read":
        assert tool.mirrors_route[0] != "GET"


_VECTORS = [
    "", "abc", "-1", "0", "1.5", "51", "201", "99999999999", "true", "2026-13-01",
    "2026-02-30", "x" * 300, "exact", "subtree", "income", "settled", "none",
]

def _valid_base(tool) -> dict:
    """A payload the tool accepts, so each vector is the only thing wrong."""
    if tool.name == "transactions_search":
        return {"date_from": "2026-01-01", "date_to": "2026-01-31", "category_match": "exact"}
    if tool.name == "budgets_update_amount":
        return {"budget_id": 1, "amount": "10.00"}
    if tool.name == "transactions_set_category":
        return {"transaction_id": 1, "category_id": 2}
    if tool.name in ("transactions_get", "transactions_update", "transactions_update_money",
                     "transactions_delete"):
        return {"transaction_id": 1}
    if tool.name == "transactions_create":
        return {"account_id": 1, "category_id": 2, "description": "x", "amount": "1.00",
                "type": "expense", "date": "2026-01-01"}
    if tool.name == "transactions_transfer":
        return {"from_account_id": 1, "to_account_id": 2, "amount": "1.00", "date": "2026-01-01"}
    if tool.name == "recurring_create":
        return {"account_id": 1, "category_id": 2, "description": "x", "amount": "1.00",
                "type": "expense", "frequency": "monthly", "next_due_date": "2026-01-01"}
    if tool.name in ("recurring_update", "recurring_update_money", "recurring_stop", "recurring_delete"):
        return {"recurring_id": 1}
    if tool.name in ("recurring_skip_next", "recurring_materialise_next"):
        return {"recurring_id": 1, "occurrence_date": "2026-01-01"}
    if tool.name == "billing_cycle_update":
        return {"billing_cycle_day": 1}
    if tool.name == "billing_period_create":
        return {"start_date": "2026-01-01"}
    if tool.name in ("accounts_get", "cycle_payments_list", "accounts_update", "accounts_update_money",
                     "accounts_delete"):
        return {"account_id": 1}
    if tool.name == "accounts_create":
        return {"name": "x", "account_type_id": 1, "opening_balance_date": "2026-01-01"}
    if tool.name == "accounts_adjust_balance":
        return {"account_id": 1, "target_balance": "1.00"}
    if tool.name == "account_types_create":
        return {"name": "x"}
    if tool.name in ("account_types_update", "account_types_delete"):
        return {"type_id": 1, **({"name": "x"} if tool.name == "account_types_update" else {})}
    if tool.name.startswith("cycle_payments_"):
        base = {"account_id": 1, "year": 2026, "month": 1}
        return base if tool.name == "cycle_payments_delete" else {**base, "amount": "1.00"}
    if tool.name == "notification_preferences_update":
        return dict.fromkeys(tool.args.model_fields, True)
    if tool.name in ("reports_query", "reports_sankey_query"):
        return {"dataset": "transactions", "measure": {"agg": "sum", "field": "amount"}} if (
            tool.name == "reports_query") else {}
    if tool.name.startswith("report"):
        return {k: 1 for k in ("report_id", "version_id") if k in tool.args.model_fields} | (
            {"name": "x"} if tool.name == "reports_create" else {})
    return {
        "budgets_create": {"category_id": 1, "amount": "10.00"},
        "budgets_copy_from_period": {"source_period_start": "2026-01-01"},
        "budgets_draft_next": {"period_start": "2026-02-01"},
        "budgets_rebalance": {"items": [{"budget_id": 1, "expected_amount": "1.00", "amount": "1.00"}]},
        "budgets_delete": {"budget_id": 1},
        "categories_create": {"name": "x"},
        "categories_update": {"category_id": 1},
        "categories_move": {"category_id": 1, "target_parent_id": 2},
        "categories_batch_move": {"moves": [{"subcategory_id": 1, "target_parent_id": 2}]},
        "categories_delete": {"category_id": 1},
        # TBD-668
        "forecast_plan_copy": {"source_period_start": "2026-01-01"},
        "forecast_plan_item_add": {"plan_id": 1, "category_id": 1, "type": "expense", "planned_amount": "1.00"},
        "forecast_plan_items_bulk": {"plan_id": 1, "items": []},
        "forecast_plan_item_update": {"plan_id": 1, "item_id": 1, "planned_amount": "1.00"},
        "forecast_plan_item_delete": {"plan_id": 1, "item_id": 1},
        "forecast_plan_activate": {"plan_id": 1},
        "forecast_plan_revert": {"plan_id": 1},
        "forecast_plan_discard": {"plan_id": 1},
        "scenarios_get": {"scenario_id": 1},
        "scenarios_simulate": {"scenario_id": 1},
        "scenarios_compare": {"scenario_ids": [1], "horizon_months": 12},
        "scenarios_create": {"name": "x", "scenario_type": "custom", "params": {"scenario_type": "custom", "label": "x"}},
        "scenarios_update": {"scenario_id": 1},
        "plan_goal_create": {"scenario_id": 1, "name": "x", "tier": "want", "kind": "lump"},
        "plan_goal_update": {"scenario_id": 1, "goal_id": 1},
        "plan_goal_delete": {"scenario_id": 1, "goal_id": 1},
        "scenarios_delete": {"scenario_id": 1},
    }.get(tool.name, {})


# Tools whose every argument the route accepts in any form, so no vector can
# be rejected: the vacuity guard does not apply.
_NOTHING_TO_REJECT = {"account_types_create"}  # a free-text name

# Routes that parse their body by hand (so their gates answer before a 422):
# the model they validate it with.
_MANUAL_BODY = {
    ("POST", "/api/v1/accounts/{account_id}/adjust-balance"): BalanceAdjustmentRequest,
}


def _route_params(route) -> dict[str, object]:
    """name -> query/path ModelField, or the body MODEL class that owns it."""
    out: dict[str, object] = {}
    for f in route.dependant.query_params + route.dependant.path_params:
        out[f.alias] = f
    models = [_without_none(f.field_info.annotation) for f in route.dependant.body_params]
    models += [m for (verb, path), m in _MANUAL_BODY.items() if path == route.path and verb in route.methods]
    for model in models:
        for n in getattr(model, "model_fields", {}):
            out[n] = model
    return out


def _route_rejects(route, name: str, v: str) -> bool:
    """Whether the ROUTE's own validation (the code REST runs) rejects ``v``
    for param ``name``: query and path through FastAPI, body through the
    request model."""
    target = _route_params(route)[name]
    if isinstance(target, type):  # a body model
        try:
            target.model_validate({name: v})
        except PydanticValidationError as exc:
            return any(e["loc"] and e["loc"][0] == name for e in exc.errors())
        return False
    received = QueryParams({name: v}) if target in route.dependant.query_params else {name: v}
    _, errors = request_params_to_args([target], received)
    return bool(errors)


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_fr4_args_reject_everything_the_route_rejects(tool):
    """FENCE F-R4 (validators). For every arg the tool shares with its
    mirrored route (query, PATH and BODY-model fields), every vector the
    ROUTE's own validator rejects the tool's args model rejects too. Wrong
    implementation: an args model redeclaring a field without the route's
    constraint (``limit: int`` without ``le``, ``category_match: str``,
    ``amount: Decimal`` without ``gt=0``)."""
    route = _live_routes()[tool.mirrors_route]
    params = _route_params(route)
    shared = [n for n in tool.args.model_fields if n in params]
    # Every argument must be one the route validates, or it escapes this
    # fence silently (a field renamed away from the route's name).
    assert shared == list(tool.args.model_fields), (
        f"{tool.name}: args {set(tool.args.model_fields) - set(shared)} are not route params"
    )
    base = _valid_base(tool)
    tool.args.model_validate(base)  # the base itself is valid
    checked = 0
    for name in shared:
        is_list = "list" in str(tool.args.model_fields[name].annotation)
        for v in _VECTORS:
            if not _route_rejects(route, name, v):
                continue
            checked += 1
            with pytest.raises(PydanticValidationError):
                tool.args.model_validate({**base, name: [v] if is_list else v})
    if shared and tool.name not in _NOTHING_TO_REJECT:
        assert checked, f"{tool.name}: no vector was rejected by REST; the fence tested nothing"


def test_fr4_tightened_limits_hold():
    """GUARD. The tool's own bounds beyond REST's: page size <= 50 and a
    date range <= 366 days."""
    Args = registry.get_tool("transactions_search").args
    base = {"date_from": "2026-01-01", "date_to": "2026-01-31", "category_match": "exact"}
    Args.model_validate({**base, "limit": 50, "account_id": list(range(50)), "offset": 10_000})
    for bad in ({"limit": 51}, {"account_id": list(range(51))},
                {"category_id": list(range(51))}, {"offset": 10_001}):
        with pytest.raises(PydanticValidationError):
            Args.model_validate({**base, **bad})
    Args.model_validate({**base, "date_from": "2025-01-01", "date_to": "2026-01-01"})  # 366 days
    with pytest.raises(PydanticValidationError):
        Args.model_validate({**base, "date_from": "2025-01-01", "date_to": "2026-01-02"})
    with pytest.raises(PydanticValidationError):
        Args.model_validate({**base, "date_from": "2026-02-01", "date_to": "2026-01-01"})
    with pytest.raises(PydanticValidationError):
        Args.model_validate({"date_from": "2026-01-01", "date_to": "2026-01-31"})  # no category_match


# ── F-R5 ──────────────────────────────────────────────────────────────────

_EGRESS = re.compile(
    r"(^|\.)(https?|httpx|requests|aiohttp|urllib3?|socket|ssl|ftplib|\w*smtplib|boto3|botocore"
    r"|stripe|ai_providers|ai_dispatch|egress_guard|\w*mail\w*|notification\w*)(\.|$)"
)


def _imported_modules(path: pathlib.Path) -> set[str]:
    mods = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            mods.add(base)
            mods |= {f"{base}.{a.name}" for a in node.names}
    return mods


def _agent_tool_modules() -> list[pathlib.Path]:
    return sorted((APP_DIR / "agent").rglob("*.py"))


# Reviewed (TBD-787): the preference route's ``router`` and nothing else of
# its module (``test_preference_tools`` pins the use). The route the tool runs
# writes the caller's preference row and sends nothing.
_EGRESS_REVIEWED = {("preference_tools.py", "app.routers.notifications", ("router",))}


def _reviewed(path: pathlib.Path) -> set[str]:
    """The modules of ``path``'s reviewed ``from X import names`` statements."""
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            names = tuple(a.name for a in node.names)
            if (path.name, node.module, names) in _EGRESS_REVIEWED:
                out |= {node.module, *(f"{node.module}.{n}" for n in names)}
    return out


def test_fr5_no_tool_module_imports_an_egress_path():
    """FENCE F-R5. Wrong implementation: an added "fetch URL" or "email the
    report" tool (``import httpx``, ``from app.services import email_service``,
    ``notification_service``, the AI provider adapters). Parsed imports of the
    agent modules only; transitive imports through services are not claimed."""
    modules = _agent_tool_modules()
    assert any(p.name == "tools.py" for p in modules)
    offenders = {
        (p.name, m) for p in modules for m in _imported_modules(p)
        if _EGRESS.search(m) and m not in _reviewed(p)
    }
    assert not offenders, offenders


@pytest.mark.parametrize("mod", [
    "httpx", "app.services.email_service", "app.services.notification_service",
    "app.services.ai_providers.egress_guard", "app.services.mailgun_webhook",
    "http.client", "aiosmtplib", "app.services.ai_dispatch", "ssl",
])
def test_fr5_detector_matches_each_egress_shape(mod):
    assert _EGRESS.search(mod)


# ── F-R6 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_fr6_live_names_match_the_regex(tool):
    assert re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", tool.name)


@pytest.mark.parametrize("name", ["budgets.list", "Budgets_list", "1tool", "a" * 65, "tool-x", ""])
def test_fr6_bad_names_are_refused(name):
    """FENCE F-R6. Wrong implementation: dotted or otherwise provider-invalid names."""
    with pytest.raises(ToolRegistrationError):
        register(_spec(name=name))


@pytest.mark.parametrize("over", [
    dict(risk="write", run=None, preview=_noop, execute=None, mirrors_route=("PUT", "/api/v1/budgets/{budget_id}")),
    dict(risk="write", run=None, preview=None, execute=_noop, mirrors_route=("PUT", "/api/v1/budgets/{budget_id}")),
    dict(risk="write", run=_run, preview=_noop, execute=_noop, mirrors_route=("PUT", "/api/v1/budgets/{budget_id}")),
    dict(risk="read", run=None),
    dict(risk="read", preview=_noop),
    dict(risk="read", mirrors_route=("PUT", "/api/v1/budgets/{budget_id}")),
    dict(risk="write", run=None, preview=_noop, execute=_noop),  # write mirroring a GET
    dict(risk="destructive"),
])
def test_fr6_half_declared_tools_are_refused(over):
    """FENCE F-R6. Wrong implementation: a write tool registered without
    ``execute`` (or ``preview``), a read tool carrying write hooks."""
    with pytest.raises(ToolRegistrationError):
        register(_spec(**over))
    assert registry.get_tool("fixture_tool") is None


def test_fr6_duplicate_and_open_args_are_refused(scratch_tool):
    class OpenArgs(BaseModel):
        x: int = 0

    with pytest.raises(ToolRegistrationError):
        register(_spec(name="open_args", args=OpenArgs))
    scratch_tool(_spec(name="dup_tool"))
    with pytest.raises(ToolRegistrationError):
        register(_spec(name="dup_tool"))


def test_fr6_a_complete_write_tool_registers(scratch_tool):
    """Control: the refusals above are not refusing everything."""
    scratch_tool(_spec(
        name="fixture_write", risk="write", run=None, preview=_noop, execute=_noop,
        mirrors_route=("PUT", "/api/v1/budgets/{budget_id}"),
    ))


# ── TBD-788: domains ──────────────────────────────────────────────────────

def test_every_change_tool_has_a_domain():
    """FENCE (TBD-788). The in-app chat sends a write or sensitive tool only
    once the model loads its domain. Wrong implementation: a change tool with
    no domain, which no ``load_tools`` call could ever reach."""
    assert [t.name for t in TOOLS if t.risk != "read" and not t.domain] == []
    assert registry.domains() == [
        "accounts", "budgets_and_categories", "forecasts_and_plans", "notifications",
        "recurring_and_billing", "reports", "transactions",
    ]
    assert registry.get_tool("billing_cycle_update").domain == "recurring_and_billing"
    assert registry.get_tool("account_types_create").domain == "accounts"


def test_a_change_tool_without_a_domain_is_refused(scratch_tool):
    unmapped = ("PUT", "/api/v1/unmapped/{thing_id}")
    with pytest.raises(ToolRegistrationError, match="needs a domain"):
        register(_spec(name="fixture_write", risk="write", run=None, preview=_noop,
                       execute=_noop, mirrors_route=unmapped))
    assert registry.get_tool("fixture_write") is None
    scratch_tool(_spec(name="fixture_write", risk="write", run=None, preview=_noop,
                       execute=_noop, mirrors_route=unmapped, domain="accounts"))


# ── F-A5 ──────────────────────────────────────────────────────────────────

_NEVER = [
    ("GET", "/api/v1/system/api-tokens"),
    ("POST", "/api/v1/agent/tokens"),
    ("POST", "/api/v1/agent/actions/{action_id}/confirm"),
    ("POST", "/api/v1/agent/actions/{action_id}/cancel"),
    ("POST", "/api/v1/agent/actions/{action_id}/revert"),
    ("GET", "/api/v1/agent/actions"),
    ("GET", "/api/v1/settings/ai-providers"),
    ("PUT", "/api/v1/settings/ai-providers/routing/default"),
    ("PUT", "/api/v1/settings/ai-providers/caps/default"),
    ("POST", "/api/v1/settings/ai-providers/consent"),
    ("GET", "/api/v1/orgs/members"),
    ("POST", "/api/v1/orgs/invitations"),
    ("PATCH", "/api/v1/orgs/{org_id}/rename"),
    ("POST", "/api/v1/orgs/data/reset"),
    ("PUT", "/api/v1/users/me"),
    ("POST", "/api/v1/users/me/password"),
    ("POST", "/api/v1/auth/mfa/verify"),
    ("PUT", "/api/v1/settings"),
    ("DELETE", "/api/v1/settings/{key}"),
    ("PUT", "/api/v1/settings/features/{feature}"),
    ("PUT", "/api/v1/subscriptions/plan"),
    ("GET", "/api/v1/plans"),
    ("GET", "/api/v1/admin/orgs"),
    ("POST", "/api/v1/oauth/token"),
    ("GET", "/api/v1/transactions/export"),
    ("POST", "/api/v1/reports/{report_id}/share"),
    ("POST", "/api/v1/ai/budget/rebalance"),
    ("POST", "/api/v1/feedback"),
    ("PUT", "/api/v1/scheduler/settings"),
    ("PUT", "/api/v1/settings/manual-balance-adjustment"),
    ("POST", "/api/v1/webhooks/mailgun"),
    # No route is mounted here any more (TBD-661); the entry keeps the
    # ``/api/v1/public`` prefix rule under test.
    ("GET", "/api/v1/public/founder-count"),
    ("POST", "/api/v1/security/csp-report"),
]


@pytest.mark.parametrize("method,path", _NEVER)
def test_fa5_never_exposable_routes_are_refused(method, path):
    """FENCE F-A5. Wrong implementation: no denylist, so a tool could mirror a
    credential, member, settings, billing, export or sharing route."""
    risk = "read" if method == "GET" else "sensitive"
    hooks = dict(run=_run) if risk == "read" else dict(run=None, preview=_noop, execute=_noop)
    with pytest.raises(ToolRegistrationError, match="never exposable"):
        register(_spec(risk=risk, mirrors_route=(method, path), **hooks))


def test_fa5_write_on_a_delete_route_is_refused(scratch_tool):
    """FENCE F-A5. A delete is never ``write``; the same route as
    ``sensitive`` registers (control)."""
    route = ("DELETE", "/api/v1/budgets/{budget_id}")
    hooks = dict(run=None, preview=_noop, execute=_noop)
    with pytest.raises(ToolRegistrationError, match="never 'write'"):
        register(_spec(name="budget_delete", risk="write", mirrors_route=route, **hooks))
    scratch_tool(_spec(name="budget_delete", risk="sensitive", mirrors_route=route, **hooks))


def test_fa5_ordinary_routes_stay_allowed():
    for _m, path in [t.mirrors_route for t in TOOLS] + [
        ("GET", "/api/v1/settings/billing-periods"), ("GET", "/api/v1/recurring"),
    ]:
        assert not registry._never_exposable(path), path


# ── F-R9 and the other invoke gates ──────────────────────────────────────

def _user(role: Role):
    return SimpleNamespace(id=7, org_id=42, role=role)


@pytest.fixture
def entitled(monkeypatch):
    async def _has(db, org_id, key):
        assert key == "ai.agent"
        return True

    monkeypatch.setattr(registry.feature_service, "has_feature", _has)

    async def _admit(db, org_id, meter, n=1, *, now=None):
        # These tests pass db=None; mcp.calls admission is fenced on a real
        # database in tests/services/test_usage_service.py (TBD-585).
        assert meter == "mcp.calls"

    monkeypatch.setattr(registry.usage_service, "admit", _admit)


@pytest.mark.parametrize("role", [Role.MEMBER, Role.ADMIN, Role.OWNER])
async def test_fr9_every_role_passes_a_member_tool(entitled, scratch_tool, role):
    """FENCE F-R9. Wrong implementation: ``user.role >= spec.min_role`` on the
    str Enum, which is lexical ("admin" < "member"), so ADMIN fails a MEMBER
    gate."""
    scratch_tool(_spec(name="member_tool", min_role=Role.MEMBER))
    out = await invoke(None, _user(role), "member_tool", {}, channel="in_app")
    assert out == {"data": {"ok": True}}


@pytest.mark.parametrize("role,allowed", [
    (Role.MEMBER, False), (Role.ADMIN, True), (Role.OWNER, True),
])
async def test_fr9_admin_tool_rank(entitled, scratch_tool, role, allowed):
    """FENCE F-R9. MEMBER fails an ADMIN tool (lexically "member" > "admin",
    so the str comparison would let it through); OWNER passes (lexically
    "owner" > "admin" too, so this row alone would not catch the mutant)."""
    scratch_tool(_spec(name="admin_tool", min_role=Role.ADMIN))
    if allowed:
        await invoke(None, _user(role), "admin_tool", {}, channel="in_app")
    else:
        with pytest.raises(ToolError) as exc:
            await invoke(None, _user(role), "admin_tool", {}, channel="in_app")
        assert exc.value.code == "insufficient_role"


async def test_invoke_refuses_extra_args_and_unknown_tools(entitled, scratch_tool):
    scratch_tool(_spec(name="member_tool"))
    with pytest.raises(ToolError) as exc:
        await invoke(None, _user(Role.OWNER), "member_tool", {"org_id": 1}, channel="in_app")
    assert exc.value.code == "invalid_arguments"
    with pytest.raises(ToolError) as exc:
        await invoke(None, _user(Role.OWNER), "nope", {}, channel="in_app")
    assert exc.value.code == "unknown_tool"


async def test_invoke_refuses_without_the_agent_entitlement(monkeypatch, scratch_tool):
    async def _no(db, org_id, key):
        return False

    monkeypatch.setattr(registry.feature_service, "has_feature", _no)
    scratch_tool(_spec(name="member_tool"))
    with pytest.raises(ToolError) as exc:
        await invoke(None, _user(Role.OWNER), "member_tool", {}, channel="in_app")
    assert exc.value.code == "feature_not_entitled"


@pytest.mark.parametrize("channel,scope,risk,ok", [
    ("in_app", None, "read", True),
    ("in_app", "agent:write", "read", False),
    ("mcp", None, "read", False),
    ("mcp", "read", "read", False),        # a REST PAT scope
    ("mcp", "agent:read", "read", True),
    ("mcp", "agent:read", "sensitive", False),
    ("mcp", "agent:write", "read", True),
    ("mcp", "agent:write", "sensitive", "staged"),
    ("mcp", "agent:auto", "sensitive", "staged"),
    ("in_app", None, "write", "staged"),
    ("in_app", None, "sensitive", "staged"),
])
async def test_principal_scope_gate(entitled, scratch_tool, monkeypatch, channel, scope, risk, ok):
    """GUARD. ``agent:read`` reaches read tools only; MCP needs an agent scope;
    in-app carries none. A write the gates admit is handed to the preview
    engine (stubbed here) and never runs from ``invoke``."""
    from app.agent import actions

    async def _propose(ctx, spec, args, *, scope, reverts=None):
        return {"staged": spec.name}

    monkeypatch.setattr(actions, "propose", _propose)
    hooks = dict(run=_run) if risk == "read" else dict(run=None, preview=_noop, execute=_noop)
    route = ("GET", "/api/v1/accounts") if risk == "read" else ("PUT", "/api/v1/budgets/{budget_id}")
    scratch_tool(_spec(name="scoped_tool", risk=risk, mirrors_route=route, **hooks))
    # An mcp principal always carries its token id (gate 6 refuses one without).
    tok = 1 if channel == "mcp" else None
    call = invoke(None, _user(Role.OWNER), "scoped_tool", {}, channel=channel, scope=scope,
                  api_token_id=tok)
    if ok == "staged":
        assert await call == {"data": {"staged": "scoped_tool"}}
    elif ok:
        assert await call == {"data": {"ok": True}}
    else:
        with pytest.raises(ToolError) as exc:
            await call
        assert exc.value.code == "scope_denied"


async def test_mcp_call_without_a_token_id_is_refused_before_the_meter(entitled, scratch_tool):
    """FENCE (TBD-561 gate 6). Wrong implementation: gate 6 skipping a
    missing token id, so an mcp principal with no token is unthrottled."""
    scratch_tool(_spec(name="scoped_tool", run=_run))
    with pytest.raises(ToolError) as exc:
        await invoke(None, _user(Role.OWNER), "scoped_tool", {}, channel="mcp",
                     scope="agent:write", api_token_id=None)
    assert exc.value.code == "scope_denied"


async def test_unexpected_tool_failure_is_an_opaque_error(entitled, scratch_tool):
    """GUARD. A service exception never reaches the model as SQL or a trace."""
    async def _boom(ctx, args):
        raise RuntimeError("SELECT secret FROM users")

    scratch_tool(_spec(name="boom_tool", run=_boom))
    with pytest.raises(ToolError) as exc:
        await invoke(None, _user(Role.OWNER), "boom_tool", {}, channel="in_app")
    assert (exc.value.code, exc.value.detail) == ("internal_error", "the tool failed")


async def test_a_gate_failure_is_an_opaque_error_too(monkeypatch, scratch_tool):
    async def _db_down(db, org_id, key):
        raise RuntimeError("Lost connection to MySQL server")

    monkeypatch.setattr(registry.feature_service, "has_feature", _db_down)
    scratch_tool(_spec(name="member_tool"))
    with pytest.raises(ToolError) as exc:
        await invoke(None, _user(Role.OWNER), "member_tool", {}, channel="in_app")
    assert exc.value.code == "internal_error"


# ── F-R10 unit ────────────────────────────────────────────────────────────

def test_fr10_wrap_untrusted_wraps_only_listed_string_keys():
    out = registry.wrap_untrusted({
        "name": "x", "id": 1, "rows": [{"description": "d", "amount": "1.00", "category_name": None}],
        "tags": [{"name": "t", "name_normalized": "t"}], "type": "expense",
    })
    assert out == {
        "name": {"untrusted": "x"}, "id": 1,
        "rows": [{"description": {"untrusted": "d"}, "amount": "1.00", "category_name": None}],
        "tags": [{"name": {"untrusted": "t"}, "name_normalized": {"untrusted": "t"}}],
        "type": "expense",
    }


def test_role_rank_covers_every_role():
    assert set(ROLE_RANK) == set(Role)
    assert ROLE_RANK[Role.MEMBER] < ROLE_RANK[Role.ADMIN] < ROLE_RANK[Role.OWNER]
