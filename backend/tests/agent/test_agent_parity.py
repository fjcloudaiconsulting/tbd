"""TBD-663 PR 1: agent parity fence.

Every mutating route in the RUNNING app (not an AST) is exactly one of: mirrored
by a registered tool that covers all its body fields, never exposable,
in AGENT_PARITY_GAPS (a ticket owns the tool), or in PARITY_EXEMPT (UI-only).
Stale entries fail too. F-R4 body-role scan: a mirrored route's role gates (inline
in the handler, or Depends() on the route) must not be stricter than the tool's
``min_role``.

If RED: add a tool mirroring the route; or add an AGENT_PARITY_GAPS entry with the
owning ticket (and lower MAX_GAPS in the same PR when you CLOSE a gap); or add a
PARITY_EXEMPT entry with a reviewed reason (UI-only routes).

Limits: nested body models are compared by top-level field name only; role checks
inside other helpers or services are not seen; fail-closed covers literal
``.role`` uses only.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest
from fastapi.routing import APIRoute

import app.agent.registry as registry
import app.agent.tools  # noqa: F401  (registers the tools)
from app.agent.registry import ROLE_RANK
from app.models.user import Role

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Ratchet: gaps may only go DOWN. Closing one (a tool now mirrors the route)
# lowers this number in the same PR (equality, so a silent deletion fails);
# raising it needs an architect ruling.
MAX_GAPS = 78

AGENT_PARITY_GAPS: dict[tuple[str, str], str] = {
    # TBD-664
    ("POST", "/api/v1/tags"): "TBD-664",
    ("DELETE", "/api/v1/tags/{tag_id}"): "TBD-664",
    ("PATCH", "/api/v1/tags/{tag_id}"): "TBD-664",
    ("POST", "/api/v1/transactions"): "TBD-664",
    ("POST", "/api/v1/transactions/batch"): "TBD-664",
    ("POST", "/api/v1/transactions/bulk-delete"): "TBD-664",
    ("POST", "/api/v1/transactions/bulk-update"): "TBD-664",
    ("POST", "/api/v1/transactions/pair"): "TBD-664",
    ("POST", "/api/v1/transactions/transfer"): "TBD-664",
    ("DELETE", "/api/v1/transactions/{transaction_id}"): "TBD-664",
    ("PUT", "/api/v1/transactions/{transaction_id}"): "TBD-664",
    ("POST", "/api/v1/transactions/{transaction_id}/convert-to-transfer"): "TBD-664",
    ("POST", "/api/v1/transactions/{transaction_id}/promote-to-recurring"): "TBD-664",
    ("POST", "/api/v1/transactions/{transaction_id}/skip"): "TBD-664",
    ("PUT", "/api/v1/transactions/{transaction_id}/tags"): "TBD-664",
    ("POST", "/api/v1/transactions/{transaction_id}/unpair"): "TBD-664",
    # TBD-665
    ("POST", "/api/v1/account-types"): "TBD-665",
    ("DELETE", "/api/v1/account-types/{type_id}"): "TBD-665",
    ("PUT", "/api/v1/account-types/{type_id}"): "TBD-665",
    ("POST", "/api/v1/accounts"): "TBD-665",
    ("DELETE", "/api/v1/accounts/{account_id}"): "TBD-665",
    ("PUT", "/api/v1/accounts/{account_id}"): "TBD-665",
    ("POST", "/api/v1/accounts/{account_id}/adjust-balance"): "TBD-665",
    ("DELETE", "/api/v1/accounts/{account_id}/cycle-payments/{year}/{month}"): "TBD-665",
    ("POST", "/api/v1/accounts/{account_id}/cycle-payments/{year}/{month}"): "TBD-665",
    ("PUT", "/api/v1/accounts/{account_id}/cycle-payments/{year}/{month}"): "TBD-665",
    # TBD-666
    ("POST", "/api/v1/budgets"): "TBD-666",
    ("POST", "/api/v1/budgets/copy-from-period"): "TBD-666",
    ("POST", "/api/v1/budgets/draft-next"): "TBD-666",
    ("POST", "/api/v1/budgets/from-forecast"): "TBD-666",
    ("POST", "/api/v1/budgets/rebalance"): "TBD-666",
    ("DELETE", "/api/v1/budgets/{budget_id}"): "TBD-666",
    ("POST", "/api/v1/categories"): "TBD-666",
    ("POST", "/api/v1/categories/batch-move"): "TBD-666",
    ("POST", "/api/v1/categories/restore-recommended"): "TBD-666",
    ("DELETE", "/api/v1/categories/{category_id}"): "TBD-666",
    ("PUT", "/api/v1/categories/{category_id}"): "TBD-666",
    ("PATCH", "/api/v1/categories/{category_id}/move"): "TBD-666",
    # TBD-667
    ("PUT", "/api/v1/notifications/preferences"): "TBD-667",
    ("POST", "/api/v1/recurring"): "TBD-667",
    ("POST", "/api/v1/recurring/generate"): "TBD-667",
    ("DELETE", "/api/v1/recurring/{recurring_id}"): "TBD-667",
    ("PUT", "/api/v1/recurring/{recurring_id}"): "TBD-667",
    ("POST", "/api/v1/recurring/{recurring_id}/materialise-next"): "TBD-667",
    ("POST", "/api/v1/recurring/{recurring_id}/skip-next"): "TBD-667",
    ("POST", "/api/v1/recurring/{recurring_id}/stop"): "TBD-667",
    ("PUT", "/api/v1/settings/billing-cycle"): "TBD-667",
    ("POST", "/api/v1/settings/billing-period"): "TBD-667",
    ("POST", "/api/v1/settings/billing-period/close"): "TBD-667",
    ("POST", "/api/v1/settings/billing-periods/ensure-future"): "TBD-667",
    # TBD-668
    ("POST", "/api/v1/forecast-plans/copy"): "TBD-668",
    ("POST", "/api/v1/forecast-plans/populate"): "TBD-668",
    ("POST", "/api/v1/forecast-plans/refresh-from-sources"): "TBD-668",
    ("POST", "/api/v1/forecast-plans/{plan_id}/activate"): "TBD-668",
    ("POST", "/api/v1/forecast-plans/{plan_id}/discard"): "TBD-668",
    ("POST", "/api/v1/forecast-plans/{plan_id}/items"): "TBD-668",
    ("POST", "/api/v1/forecast-plans/{plan_id}/items/bulk"): "TBD-668",
    ("DELETE", "/api/v1/forecast-plans/{plan_id}/items/{item_id}"): "TBD-668",
    ("PUT", "/api/v1/forecast-plans/{plan_id}/items/{item_id}"): "TBD-668",
    ("POST", "/api/v1/forecast-plans/{plan_id}/revert"): "TBD-668",
    ("POST", "/api/v1/scenarios"): "TBD-668",
    ("POST", "/api/v1/scenarios/compare"): "TBD-668",
    ("DELETE", "/api/v1/scenarios/{scenario_id}"): "TBD-668",
    ("PATCH", "/api/v1/scenarios/{scenario_id}"): "TBD-668",
    ("POST", "/api/v1/scenarios/{scenario_id}/simulate"): "TBD-668",
    # TBD-669
    ("POST", "/api/v1/reports"): "TBD-669",
    ("POST", "/api/v1/reports/query"): "TBD-669",
    ("POST", "/api/v1/reports/query/sankey"): "TBD-669",
    ("DELETE", "/api/v1/reports/{report_id}"): "TBD-669",
    ("PATCH", "/api/v1/reports/{report_id}"): "TBD-669",
    ("POST", "/api/v1/reports/{report_id}/duplicate"): "TBD-669",
    ("POST", "/api/v1/reports/{report_id}/reset"): "TBD-669",
    ("POST", "/api/v1/reports/{report_id}/versions/{version_id}/restore"): "TBD-669",
    # TBD-670
    ("POST", "/api/v1/import/confirm"): "TBD-670",
    ("POST", "/api/v1/import/ofx/preview"): "TBD-670",
    ("POST", "/api/v1/import/preview"): "TBD-670",
    ("POST", "/api/v1/import/tab/preview"): "TBD-670",
    ("POST", "/api/v1/import/{import_id}/reconcile"): "TBD-670",
}

PARITY_EXEMPT: dict[tuple[str, str], str] = {
    ("PATCH", "/api/v1/dashboard"): "layout is a UI arrangement",
    ("POST", "/api/v1/announcements/{announcement_id}/dismiss"): "read-state of UI chrome",
    ("POST", "/api/v1/notifications/mark-seen"): "read-state of UI chrome",
    ("PATCH", "/api/v1/notifications/{notification_id}"): "read-state of UI chrome",
}


def _live_mutating() -> dict[tuple[str, str], object]:
    from app.main import app
    from tests.app_routes import effective_routes

    return {
        (m, r.path): r
        for r in effective_routes(app)
        if isinstance(getattr(r, "route", r), APIRoute)
        for m in (r.methods or ())
        if m in MUTATING
    }


def _body_fields(route) -> set[str]:
    deps = [route.dependant]  # sub-dependencies declare params too
    for d in deps:
        deps.extend(d.dependencies)
    out: set[str] = {f.alias for d in deps for f in d.query_params}
    for f in (f for d in deps for f in d.body_params):
        model = f.field_info.annotation
        out |= set(getattr(model, "model_fields", None) or {f.alias})
    return out


def _problems(routes, tools, gaps, exempt, never=registry._never_exposable, live=None) -> list[str]:
    """routes: {key: body+query field set}; live: every live (method, path), reads included. Every returned string is a failure."""
    live = set(routes) if live is None else live
    mirrored = {
        t.mirrors_route
        for t in tools
        if t.mirrors_route in routes and routes[t.mirrors_route] <= set(t.args.model_fields)
    }
    out = []
    for key in sorted(routes):
        n = [
            name
            for name, hit in (
                ("mirrored", key in mirrored),
                ("never-exposable", never(key[1])),
                ("gap", key in gaps),
                ("exempt", key in exempt),
            )
            if hit
        ]
        if not n:
            out.append(f"{key}: in no list (add a tool, or a gap/exempt entry)")
        elif len(n) > 1:
            out.append(f"{key}: in {n}, must be exactly one")
    for name, ledger in (("gap", gaps), ("exempt", exempt)):
        out += [f"{k}: stale {name} entry, no such route" for k in sorted(ledger) if k not in routes]
    out += [
        f"{t.name}: mirrors {t.mirrors_route}, which is not a live mutating route (or, for a read tool, the route is gone)"
        for t in tools
        if t.mirrors_route not in live
    ]
    return out


def _live_all() -> set[tuple[str, str]]:
    from app.main import app
    from tests.app_routes import effective_routes

    return {(m, r.path) for r in effective_routes(app) for m in (r.methods or ())}


def test_every_mutating_route_is_classified_exactly_once():
    routes = {k: _body_fields(r) for k, r in _live_mutating().items()}
    assert _problems(routes, registry.all_tools(), AGENT_PARITY_GAPS, PARITY_EXEMPT, live=_live_all()) == []


def test_gaps_ratchet_only_goes_down():
    assert len(AGENT_PARITY_GAPS) == MAX_GAPS, (
        f"{len(AGENT_PARITY_GAPS)} gaps vs MAX_GAPS={MAX_GAPS}: lower MAX_GAPS when you "
        "close a gap; raising it needs an architect ruling"
    )
    assert all(v.startswith("TBD-") for v in AGENT_PARITY_GAPS.values())


def test_exempt_list_is_pinned():
    assert set(PARITY_EXEMPT) == {
        ("PATCH", "/api/v1/dashboard"),
        ("POST", "/api/v1/announcements/{announcement_id}/dismiss"),
        ("POST", "/api/v1/notifications/mark-seen"),
        ("PATCH", "/api/v1/notifications/{notification_id}"),
    }, "a new exemption needs a reviewed reason (and an update to this pin)"


# -- the fence's own defects (synthetic inputs) -----------------------------

class _T:
    def __init__(self, route, fields, risk="write"):
        self.name, self.risk, self.mirrors_route = "t", risk, route
        self.args = type("A", (), {"model_fields": dict.fromkeys(fields)})


_R = ("PUT", "/api/v1/x/{id}")


def test_fence_catches_unlisted_route():
    assert _problems({_R: {"a"}}, [], {}, {}, never=lambda p: False)


def test_fence_catches_stale_entries():
    for g, e in (({_R: "TBD-1"}, {}), ({}, {_R: "r"})):
        assert any("stale" in p for p in _problems({}, [], g, e, never=lambda p: False))


def test_fence_catches_route_in_two_lists():
    assert any("exactly one" in p for p in _problems({_R: {"a"}}, [], {_R: "TBD-1"}, {_R: "r"}, never=lambda p: False))
    # mirrored in full AND still listed as a gap
    assert any("exactly one" in p for p in _problems({_R: {"a"}}, [_T(_R, ["a", "id"])], {_R: "TBD-1"}, {}, never=lambda p: False))


def test_partial_mirror_without_gap_entry_fails_and_with_one_passes():
    t = [_T(_R, ["id"])]
    assert _problems({_R: {"a"}}, t, {}, {}, never=lambda p: False)
    assert not _problems({_R: {"a"}}, t, {_R: "TBD-1"}, {}, never=lambda p: False)


def test_fence_catches_tool_mirroring_missing_route():
    assert any("not a live" in p for p in _problems({}, [_T(_R, [])], {}, {}, never=lambda p: False))


# -- F-R4 body-role scan -----------------------------------------------------

_ADMIN_CALLS = {"_require_admin", "_is_admin_user", "require_org_admin"}
_OWNER_CALLS = {"require_org_owner"}


def _handler_required_role(fn) -> Role:
    """Strictest role the handler body enforces inline. An unclassifiable
    ``.role`` use raises: a guard the scan cannot read must not pass as none."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    need = Role.MEMBER

    def raise_to(r: Role):
        nonlocal need
        need = max(need, r, key=ROLE_RANK.__getitem__)

    compares = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)]
    classified: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name in _ADMIN_CALLS:
                raise_to(Role.ADMIN)
            elif name in _OWNER_CALLS:
                raise_to(Role.OWNER)
    for c in compares:
        subs = list(ast.walk(c))
        if not any(isinstance(s, ast.Attribute) and s.attr == "role" for s in subs):
            continue
        named = {s.attr for s in subs if isinstance(s, ast.Attribute) and isinstance(s.value, ast.Name) and s.value.id == "Role"}
        if not named or not named <= {"OWNER", "ADMIN"}:
            raise AssertionError(f"{fn.__name__}: unclassifiable .role comparison")
        raise_to(min((Role[n] for n in named), key=ROLE_RANK.__getitem__))
        classified.update(id(s) for s in subs)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "role" and id(node) not in classified:
            raise AssertionError(f"{fn.__name__}: unclassifiable .role use")
    return need


def _dependant_required_role(dep) -> Role:
    """Strictest role any Depends() in the route's flattened dependant tree
    enforces (decorator-, router- and parameter-level alike)."""
    need = Role.MEMBER
    for sub in dep.dependencies:
        name = getattr(sub.call, "__name__", "")
        if name == "require_permission" or name.startswith("require_permission_"):
            raise AssertionError(f"{name}: permission gate not mapped to a role")
        r = {"require_org_admin": Role.ADMIN, "require_org_owner": Role.OWNER}.get(name, Role.MEMBER)
        need = max(need, r, _dependant_required_role(sub), key=ROLE_RANK.__getitem__)
    return need


def _route_required_role(route) -> Role:
    return max(_handler_required_role(route.endpoint), _dependant_required_role(route.dependant), key=ROLE_RANK.__getitem__)


@pytest.mark.parametrize("tool", [t for t in registry.all_tools() if t.risk != "read"], ids=lambda t: t.name)
def test_fr4_min_role_covers_handler_body_checks(tool):
    route = _live_mutating()[tool.mirrors_route]
    need = _route_required_role(route)
    assert ROLE_RANK[tool.min_role] >= ROLE_RANK[need], (
        f"{tool.name} has min_role={tool.min_role.value} but its route enforces {need.value}: "
        "raise the tool's min_role"
    )


def _fake_admin_checked(current_user):
    if not _is_admin_user(current_user):  # noqa: F821
        raise RuntimeError


def _fake_role_cmp(current_user):
    if current_user.role not in (Role.OWNER, Role.ADMIN):
        raise RuntimeError


def _fake_role_opaque(current_user):
    return current_user.role.value


def test_body_role_scan_classifies_and_fails_closed():
    assert _handler_required_role(_fake_admin_checked) == Role.ADMIN
    assert _handler_required_role(_fake_role_cmp) == Role.ADMIN
    with pytest.raises(AssertionError, match="unclassifiable"):
        _handler_required_role(_fake_role_opaque)


def test_depends_gate_is_seen_on_a_real_route():
    r = _live_mutating()[("POST", "/api/v1/categories/restore-recommended")]
    assert _handler_required_role(r.endpoint) == Role.MEMBER  # the gate is a Depends, not in the body
    assert _route_required_role(r) == Role.OWNER
    # a MEMBER tool mirroring it fails the same comparison the F-R4 test makes
    assert ROLE_RANK[Role.MEMBER] < ROLE_RANK[_route_required_role(r)]


def test_query_params_count_as_fields_on_a_real_route():
    key = ("DELETE", "/api/v1/categories/{category_id}")
    fields = _body_fields(_live_mutating()[key])
    assert fields, "expected query params on this route"
    assert _problems({key: fields}, [_T(key, ["category_id"])], {}, {}, never=lambda p: False)
