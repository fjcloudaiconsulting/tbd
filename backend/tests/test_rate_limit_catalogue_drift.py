"""AST-level regression: every ``@limiter.limit`` decorator has a catalogue entry.

``app/rate_limit_endpoint_catalogue.py`` calls itself "the single source of
truth" and instructs: "When you add a new ``@limiter.limit(...)`` decorator,
append the matching pattern below." Until this file existed that instruction had
**zero** enforcement, and it had already been ignored six times: 31 decorators
under ``app/routers/`` against 25 catalogue patterns.

That matters even though ``rate_limit_overrides.dynamic_limit`` has no call
sites today, because the catalogue is rendered in the admin UI's pattern
dropdown. An operator who does not find ``auth.logout`` there concludes the
route is unlimited. An untruthful source of truth is worse than none, and
deleting the claim is not an option — the override schema validator accepts
``OVERRIDABLE_ENDPOINT_PATTERNS`` and nothing else, so three of the six misses
mean an operator literally cannot store an override for those routes.

Why a MAP and not a count
-------------------------
The obvious cheap fence is ``len(ALL_KNOWN_ENDPOINT_PATTERNS) == n_decorators``.
It is **not** sufficient, and the hole is the commonest real edit rather than an
exotic one: add a route and delete another in the same PR and the count nets to
zero while the catalogue is now wrong in two places, green. The pattern string
is deliberately NOT derivable from the decorator or the function name (the
catalogue's own docstring: chosen by the route author "so renames stay tracked
here"), so the join from call site to pattern has to be written down somewhere —
and today it is written down nowhere. :data:`DECORATOR_PATTERNS` is that join.
It is keyed by ``(module, function)``; the catalogue is keyed by pattern. They
are not two copies of one list, they are the two ends of a link.

Why ``ast`` and not ``grep``
----------------------------
A whole-file grep for ``@limiter.limit`` is satisfied by the five occurrences
inside the catalogue module's own docstring — this repo has a recorded incident
where exactly that happened (a grep for a missing config key passed because the
key appeared in the comment documenting its absence). The AST only sees real
decorator nodes.

Maintaining this file
---------------------
Adding a rate-limited route is two lines: the decorator, and an entry here
naming its catalogue pattern. If this test is red, the fix is to add the missing
catalogue pattern and the missing map entry — never to relax the comparison.

The end state, filed separately, is a ``@rate_limited(pattern, limit)`` wrapper
that registers the pair at import time. That makes the pattern derivable by
construction, deletes BOTH hand-maintained lists, and would give
``dynamic_limit`` its first call sites. It touches all 31 decorator sites, which
is why it is not folded into a security fix.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.rate_limit_endpoint_catalogue import (
    ALL_KNOWN_ENDPOINT_PATTERNS,
    OVERRIDABLE_ENDPOINT_PATTERNS,
    PRE_AUTH_ENDPOINT_PATTERNS,
)

ROUTERS_DIR = Path(__file__).resolve().parents[1] / "app" / "routers"


# ── The join: every ``@limiter.limit`` call site -> its catalogue pattern ────
#
# Keyed ``(module_stem, function_name)``. The value is the catalogue pattern
# the route author chose. The limit string is carried too, so a silent retune
# shows up in this file's diff and therefore in review.
DECORATOR_PATTERNS: dict[tuple[str, str], tuple[str, str]] = {
    ("accounts", "adjust_balance"): ("accounts.adjust_balance", "20/hour"),
    ("admin_users", "trigger_email_change"): ("admin_users.email_change", "10/hour"),
    ("admin_users", "cancel_admin_pending_email"): (
        "admin_users.pending_email_cancel",
        "10/hour",
    ),
    ("api_tokens", "mint_token"): ("api_tokens.mint", "10/hour"),
    ("auth", "check_username"): ("auth.check_username", "20/minute"),
    ("auth", "forgot_password"): ("auth.forgot_password", "5/minute"),
    ("auth", "google_callback"): ("auth.google_callback", "60/minute"),
    ("auth", "google_login"): ("auth.google_login", "60/minute"),
    ("auth", "login"): ("auth.login", "10/minute"),
    ("auth", "logout"): ("auth.logout", "120/minute"),
    ("auth", "mfa_email_code"): ("auth.mfa_email_code", "3/minute"),
    ("auth", "mfa_email_verify"): ("auth.mfa_email_verify", "10/minute"),
    ("auth", "mfa_recovery"): ("auth.mfa_recovery", "10/minute"),
    ("auth", "mfa_verify"): ("auth.mfa_verify", "10/minute"),
    ("auth", "register"): ("auth.register", "5/hour"),
    ("auth", "resend_verification"): ("auth.resend_verification", "3/hour"),
    ("auth", "resend_verification_public"): (
        "auth.resend_verification_public",
        "3/hour",
    ),
    ("auth", "reset_password"): ("auth.reset_password", "10/minute"),
    ("auth", "sso_stepup_callback"): ("auth.sso_stepup_callback", "60/hour"),
    ("auth", "sso_stepup_initiate"): ("auth.sso_stepup_initiate", "10/hour"),
    ("auth", "verify"): ("auth.verify", "120/minute"),
    ("auth", "verify_email"): ("auth.verify_email", "10/minute"),
    ("feedback", "submit_feedback"): ("feedback.submit", "5/hour"),
    ("onboarding", "complete_onboarding"): ("onboarding.complete", "10/hour"),
    ("onboarding", "restart_tour"): ("onboarding.restart_tour", "10/hour"),
    ("onboarding", "seed_demo"): ("onboarding.seed_demo", "3/hour"),
    ("org_members", "accept_invitation"): ("org_members.accept_invitation", "10/minute"),
    ("org_members", "preview_invitation"): (
        "org_members.preview_invitation",
        "30/minute",
    ),
    ("org_members", "remove_member"): ("org_members.remove_member", "30/minute"),
    ("orgs", "rename_org_endpoint"): ("orgs.rename", "10/hour"),
    ("public_stats", "founder_count"): ("public_stats.founder_count", "60/minute"),
    ("reports", "run_query"): ("reports.query", "60/minute"),
    ("reports", "run_sankey_query"): ("reports.sankey_query", "60/minute"),
    ("security", "csp_report"): ("security.csp_report", "60/minute"),
    ("users", "cancel_pending_email"): ("users.cancel_pending_email", "10/hour"),
    ("users", "change_password"): ("users.change_password", "5/hour"),
    ("users", "update_profile"): ("users.update_profile", "5/hour"),
    ("webhooks", "mailgun_webhook"): ("webhooks.mailgun", "300/minute"),
}


def _find_decorated_routes() -> dict[tuple[str, str], str]:
    """Every ``(module_stem, function_name) -> limit-string`` under routers/.

    Both ``limiter.limit("...")`` and ``limiter.shared_limit("...", scope=...)``
    with a literal first argument are collected.

    ⚠ ``shared_limit`` MUST be matched here. slowapi buckets a plain ``limit``
    on ``request.url.path`` -- the CONCRETE path -- so a route carrying a path
    parameter gets one private budget per parameter value. ``shared_limit`` is
    the only slowapi API that pins the scope. If this matcher knew only
    ``limit``, converting a route to ``shared_limit`` to FIX that would make it
    vanish from the inventory, silently un-fencing the very route being
    hardened.
    A non-literal argument (the ``dynamic_limit(...)`` shape, if it ever gains
    call sites) is collected with its source unparsed, so it still shows up
    here rather than vanishing from the inventory.
    """
    found: dict[tuple[str, str], str] = {}
    for path in sorted(ROUTERS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in ("limit", "shared_limit")
                    and isinstance(dec.func.value, ast.Name)
                    and dec.func.value.id == "limiter"
                    and dec.args
                ):
                    arg = dec.args[0]
                    limit = (
                        arg.value
                        if isinstance(arg, ast.Constant)
                        else ast.unparse(arg)
                    )
                    found[(path.stem, node.name)] = limit
    return found


def test_every_limiter_decorator_maps_to_a_catalogue_pattern():
    """Three assertions, separated so a failure names one direction.

    UNMAPPED  — a new ``@limiter.limit`` route has no entry in
                DECORATOR_PATTERNS. Add one plus its catalogue pattern.
    STALE     — a mapped call site no longer exists (renamed or removed).
    RETUNED   — the decorator's limit string changed without this file's diff
                recording it, so nobody reviewed the new number.
    """
    found = _find_decorated_routes()

    unmapped = sorted(set(found) - set(DECORATOR_PATTERNS))
    assert not unmapped, (
        "@limiter.limit route(s) with no DECORATOR_PATTERNS entry — add the "
        "entry AND the matching catalogue pattern in "
        "app/rate_limit_endpoint_catalogue.py:\n"
        + "\n".join(f"  - {m}::{fn} ({found[(m, fn)]})" for m, fn in unmapped)
    )

    stale = sorted(set(DECORATOR_PATTERNS) - set(found))
    assert not stale, (
        "DECORATOR_PATTERNS entr(ies) whose decorator no longer exists — drop "
        "the stale entry (and the catalogue pattern if nothing else uses "
        "it):\n" + "\n".join(f"  - {m}::{fn}" for m, fn in stale)
    )

    retuned = {
        site: (DECORATOR_PATTERNS[site][1], limit)
        for site, limit in found.items()
        if DECORATOR_PATTERNS[site][1] != limit
    }
    assert not retuned, (
        "@limiter.limit value(s) changed without updating this map, so the new "
        "number was never reviewed:\n"
        + "\n".join(
            f"  - {m}::{fn}: {was!r} -> {now!r}"
            for (m, fn), (was, now) in sorted(retuned.items())
        )
    )


def test_every_mapped_pattern_is_in_the_catalogue():
    """The join's right-hand side must exist in the catalogue, and every
    catalogue pattern must be reachable from some live decorator.

    The second direction is what makes a *deleted* decorator visible: without
    it, removing a route would leave an orphan pattern in the admin dropdown.
    """
    mapped = {pattern for pattern, _limit in DECORATOR_PATTERNS.values()}

    missing = sorted(mapped - ALL_KNOWN_ENDPOINT_PATTERNS)
    assert not missing, (
        "pattern(s) mapped from a live decorator but absent from the "
        "catalogue — add them to OVERRIDABLE_ENDPOINT_PATTERNS (post-auth) or "
        "PRE_AUTH_ENDPOINT_PATTERNS (pre-auth):\n"
        + "\n".join(f"  - {p}" for p in missing)
    )

    orphaned = sorted(ALL_KNOWN_ENDPOINT_PATTERNS - mapped)
    assert not orphaned, (
        "catalogue pattern(s) no live decorator maps to — the admin dropdown "
        "would offer a knob that controls nothing:\n"
        + "\n".join(f"  - {p}" for p in orphaned)
    )


def test_pre_auth_and_overridable_stay_disjoint():
    """A pattern in both sets would be accepted by the schema validator AND
    short-circuited by the override resolver — a knob that saves and no-ops.
    """
    overlap = OVERRIDABLE_ENDPOINT_PATTERNS & PRE_AUTH_ENDPOINT_PATTERNS
    assert overlap == frozenset(), overlap


# ── A path-parameter route must pin its bucket (TBD-362, widened by TBD-441) ─


def _path_param_routes_with_limits() -> dict[str, dict]:
    """Every rate-limited route whose path template carries a ``{parameter}``.

    ⚠⚠ RUNTIME, NOT AST -- AND THAT IS THE FINDING, NOT A PREFERENCE.

    The first attempt at this fence walked the AST, assembling each path from
    the router's literal ``prefix=`` plus the route decorator's first argument,
    and matching the decorator owner against the name ``router``. It was
    MEASURED to fail open on live code: ``backend/app/routers/tags.py`` declares
    a SECOND router,

        router                  = APIRouter(prefix="/api/v1/tags", ...)
        transaction_tags_router = APIRouter(prefix="/api/v1/transactions", ...)

    and mounts it at ``main.py`` via ``include_router(tags.transaction_tags_router)``.
    Its ``PUT /{transaction_id}/tags`` route hangs off the second name, so
    ``owner == "router"`` was False, the path resolved to the FIRST router's
    prefix, and the route vanished from the inventory. A plain ``limit`` added
    there passed the whole file.

    That is the exact failure the explicit roster was deleted to prevent,
    re-entered through a differently-named variable. The same walk was also
    blind to a non-literal prefix, ``add_api_route``, an aliased ``limiter``
    import, and a ``{param}`` contributed by ``include_router(prefix=...)`` --
    and breaking the prefix assembly outright was a SILENT PASS, because not
    one of the five known routes carries its parameter in the prefix.

    So the inventory is taken from the two things that cannot disagree with
    production: FastAPI's assembled ``app.routes`` (the real resolved path, all
    routers, all mount styles) and slowapi's own ``_route_limits`` registry
    (populated by the decorator itself, whatever the module named it).
    ``Limit.scope`` is ``""`` for a plain ``limit`` and the scope string for a
    ``shared_limit``, so the pinned/unpinned question is read off slowapi's own
    bookkeeping rather than inferred from source text.
    """
    from fastapi.routing import APIRoute

    from app.main import app
    from app.rate_limit import limiter

    out: dict[str, dict] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or "{" not in route.path:
            continue
        name = f"{route.endpoint.__module__}.{route.endpoint.__name__}"
        for lim in limiter._route_limits.get(name, []):
            out[name] = {
                "path": route.path,
                "scope": lim.scope or None,
                "limit": str(lim.limit),
            }
    return out


# Routes KNOWN to carry both a path parameter and a rate limit, as slowapi
# names them. NOT an allowlist of exemptions -- the rule below admits none.
#
# ⚠ It is a VACUITY GUARD, and its ceiling must be stated because the previous
# revision over-trusted exactly this shape: a SUBSET check proves the inventory
# is not TOTALLY dead. It cannot prove per-route coverage, so it would not have
# caught the `tags.py` miss above (all five known routes were still found while
# a sixth was silently dropped). What actually closes that gap is taking the
# inventory from the real route table instead of from source text; this guard
# only catches the walk collapsing entirely.
_KNOWN_PATH_PARAM_LIMITED: frozenset = frozenset({
    "app.routers.accounts.adjust_balance",
    "app.routers.admin_users.trigger_email_change",
    "app.routers.admin_users.cancel_admin_pending_email",
    "app.routers.org_members.remove_member",
    "app.routers.orgs.rename_org_endpoint",
})


def test_path_parameter_routes_pin_their_rate_limit_scope():
    """slowapi buckets a plain ``limit`` on the CONCRETE request path.

    ``limit_scope = lim.scope or endpoint`` in ``slowapi/extension.py``, and
    with the limiter's default ``key_style="url"`` (this app never sets it)
    ``endpoint`` is ``request["path"]`` -- not the route template. So a route
    carrying ``{user_id}`` gets ONE PRIVATE BUDGET PER TARGET ID.

    Measured against the real limiter, four requests with four different ids:
    the plain form admitted all four and wrote four storage keys; the pinned
    form wrote ONE key and 429'd the fourth.

    That is not a smaller bound, it is a different one. The abuse these limits
    exist to stop -- a session sweeping many targets -- varies the path
    parameter BY DEFINITION, so a plain ``limit`` bounds N attempts per victim
    and nothing in aggregate.

    Wrong implementations killed:
      * converting any pinned route back to ``@limiter.limit(...)``, which
        reads as a tightening and silently removes the aggregate bound;
      * adding a NEW rate-limited path-parameter route with a plain ``limit``,
        on ANY router variable, via any mount style -- the case the AST walk
        this replaced could not see.
    """
    routes = _path_param_routes_with_limits()

    missing = sorted(_KNOWN_PATH_PARAM_LIMITED - set(routes))
    assert not missing, (
        f"the route inventory no longer finds {missing}. Either these routes "
        "were renamed or lost their limit deliberately (update "
        "_KNOWN_PATH_PARAM_LIMITED and say why), or this fence has stopped "
        "seeing the app and is now vacuous."
    )

    unpinned = sorted(
        (name, meta["path"], meta["limit"])
        for name, meta in routes.items()
        if not meta["scope"]
    )
    assert not unpinned, (
        "these routes carry a path parameter and MUST use "
        f"`limiter.shared_limit(..., scope=...)`, not `limiter.limit(...)`: "
        f"{unpinned}. A plain `limit` buckets on the concrete request path, "
        "giving every path-parameter value its own private budget -- which "
        "bounds nothing in aggregate."
    )


def test_pinned_scopes_are_unique_per_route():
    """Two routes sharing a ``scope=`` string share ONE bucket.

    Occasionally intended; never by accident. A copy-pasted scope makes one
    route's traffic consume another's budget, and the symptom is a 429 on an
    endpoint the caller never touched. Nothing else in the suite notices.
    """
    routes = _path_param_routes_with_limits()
    seen: dict = {}
    clashes = []
    for name, meta in sorted(routes.items()):
        scope = meta["scope"]
        if scope is None:
            continue
        if scope in seen:
            clashes.append((scope, seen[scope], name))
        seen[scope] = name
    assert not clashes, f"duplicate rate-limit scopes: {clashes}"


def test_pinned_scope_equals_the_catalogue_pattern():
    """A pinned scope must equal the route's CATALOGUE pattern, not its
    function name.

    The two coincide for `accounts.adjust_balance` and
    `org_members.remove_member` and DIVERGE for `orgs.rename_org_endpoint`,
    whose pattern is `orgs.rename`. The pattern is the string an operator picks
    in the override dropdown and the key TBD-492's `dynamic_limit` will read,
    so the pattern is the correct choice -- but nothing enforced it, and a
    future author could pick a scope that silently diverges from the catalogue.
    """
    routes = _path_param_routes_with_limits()
    wrong = []
    for name, meta in sorted(routes.items()):
        if meta["scope"] is None:
            continue
        stem, func = name.rsplit(".", 2)[-2:]
        expected = DECORATOR_PATTERNS.get((stem, func), (None,))[0]
        if expected is not None and meta["scope"] != expected:
            wrong.append((name, meta["scope"], expected))
    assert not wrong, (
        "pinned scope must equal the catalogue pattern "
        f"(route, scope, expected): {wrong}"
    )
