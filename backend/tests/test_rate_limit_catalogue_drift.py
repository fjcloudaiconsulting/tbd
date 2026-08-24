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


# ── A path-parameter route must pin its bucket (TBD-362 follow-up) ──────────

# `(module_stem, function_name)` for every route whose bucket MUST be pinned
# with `shared_limit`. Deliberately an explicit list rather than "every route
# with a path parameter": three pre-existing routes
# (`accounts.adjust_balance`, `org_members.remove_member`, `orgs.rename`)
# carry the same split and are tracked separately, so a blanket rule would
# fail on code this change does not touch.
_MUST_PIN_SCOPE: tuple[tuple[str, str], ...] = (
    ("admin_users", "trigger_email_change"),
    ("admin_users", "cancel_admin_pending_email"),
)


def test_path_parameter_routes_pin_their_rate_limit_scope():
    """slowapi buckets a plain ``limit`` on the CONCRETE request path.

    ``limit_scope = lim.scope or endpoint`` in ``slowapi/extension.py``, and
    ``endpoint`` is ``request.url.path`` -- not the route template. So a route
    carrying ``{user_id}`` gets ONE PRIVATE BUDGET PER TARGET ID under a plain
    ``@limiter.limit``. Measured on the running app before this fix: request
    11 to ``/users/99999/email-change`` returned 429 while ``/users/99998``,
    ``/users/99997`` and ``/users/99996`` were admitted immediately.

    That is not a smaller bound, it is a different one. The abuse these limits
    exist to stop -- a stolen interactive superadmin session mass-repointing
    recovery channels -- varies ``user_id`` BY DEFINITION, so a plain ``limit``
    bounds ten attempts per victim and nothing whatsoever in aggregate.

    Wrong implementation killed: converting either route back to
    ``@limiter.limit(...)``, which reads as a tightening and silently removes
    the aggregate bound the catalogue comment claims.
    """
    import ast as _ast

    pinned: dict[tuple[str, str], str | None] = {}
    for path in sorted(ROUTERS_DIR.rglob("*.py")):
        tree = _ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            key = (path.stem, node.name)
            if key not in _MUST_PIN_SCOPE:
                continue
            for dec in node.decorator_list:
                if (
                    isinstance(dec, _ast.Call)
                    and isinstance(dec.func, _ast.Attribute)
                    and isinstance(dec.func.value, _ast.Name)
                    and dec.func.value.id == "limiter"
                ):
                    scope = next(
                        (k.value.value for k in dec.keywords if k.arg == "scope"
                         and isinstance(k.value, _ast.Constant)),
                        None,
                    )
                    pinned[key] = scope if dec.func.attr == "shared_limit" else None

    missing = [k for k in _MUST_PIN_SCOPE if k not in pinned]
    assert not missing, (
        f"expected a limiter decorator on {missing}; if a route was renamed or "
        "its limit removed, update _MUST_PIN_SCOPE deliberately"
    )
    unpinned = [k for k, scope in pinned.items() if not scope]
    assert not unpinned, (
        "these routes carry a path parameter and MUST use "
        f"`limiter.shared_limit(..., scope=...)`, not `limiter.limit(...)`: {unpinned}. "
        "A plain `limit` buckets on the concrete request path, giving every "
        "path-parameter value its own private budget."
    )
