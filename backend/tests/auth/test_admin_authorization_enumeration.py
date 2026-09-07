"""Inventory + behavioural guard: the platform-privileged surface is closed,
reviewed, and actually refuses non-platform callers (TBD-440).

``tests/auth/test_public_route_allowlist.py`` fences **authentication** — it
proves ``get_current_user`` is wired into each route's dependency graph. It says
nothing about **authorization**. Until this file existed, a new
``/api/v1/admin/*`` route added with only ``Depends(get_current_user)`` was a
privilege-escalation bypass that **no test would catch**: any authenticated
non-admin could call it.

Spec: ``specs/2026-09-07-tbd-440-admin-authorization-fence.md``.

⚠⚠ WHICH LEG HOLDS WHICH LINE. Read this before trusting a green run.
----------------------------------------------------------------------
**Leg 1 (structural) is review-visibility. Leg 2 (behavioural) is the security
assertion.** Leg 1 proves a gate object is wired in; it stays GREEN over a gate
that is wired in and *broken* — gut ``has_permission`` to ``return True``, or any
of the six ``require_superadmin`` bodies to ``return current_user``, and every
structural assertion here still passes while the routes become world-writable.
Only Leg 2 goes red on those. Do not read a green Leg 1 as "authorization works".

Why runtime enumeration and not an AST scan
-------------------------------------------
The ticket asked for an AST fence over ``app/routers/admin_*.py``. Both are
wrong, and the evidence is in this file's sibling.
``test_public_route_allowlist.py`` records that an AST pass run during its design
reported ``PUT /api/v1/settings/features/{feature}`` as PUBLIC. It is not — it is
gated by ``require_settings_admin``, a locally-defined wrapper the name roster did
not know about. That is precisely the *authorization*-wrapper class this file
must detect, so the method has already been measured failing on this exact
problem. An AST fence would also have to re-implement router-level,
decorator-level and signature-level ``dependencies``, a prefix-less router
(``admin_roles.py`` declares no prefix), and ``include_router`` mounting. All of
that is free at runtime.

The ``admin_*.py`` glob is wrong twice over: it misses ``app/routers/admin.py``
(2 routes), and it excludes ``api_tokens.py``, which defines a
``require_superadmin`` and serves an admin-grade surface at ``/api/v1/system/*``.

⚠ THE DoD'S "or a router-level equivalent" IS A FAIL-OPEN, and this file
deliberately does not implement it. ``require_interactive_session``
(``app/auth/pat.py:231``) sits on **18** admin routes and **authorizes nobody** —
it checks only ``request.state.auth_method``, which any JWT satisfies. A
maintainer could reasonably read it as an "equivalent" and bless a route
reachable by every authenticated non-admin. Authorization is therefore defined
here by **effect** (Leg 2) and by **object identity** (Leg 1), never by name.
Control C4 pins that distinction directly.

How a gate is recognised
------------------------
Two mechanisms are in live use and neither can be matched by name:

* ``require_permission("x.y")`` is a dependency FACTORY returning a fresh closure
  per call, so it has no stable *function* identity — and it stamps
  ``dependency.__name__`` itself, so a name check is matching a value the code
  under test chose. But every closure it returns shares one ``__code__`` object,
  and the permission string is readable from ``__closure__[0].cell_contents``
  (``co_freevars == ('permission',)``). That is genuine identity.
* ``require_superadmin`` is defined **independently six times**. There is no
  trick available, so the six objects are imported explicitly and matched with
  ``is``. This fails CLOSED: a seventh definition in a new module is not in the
  roster, so its routes read as ungated and Leg 1a goes red, forcing either reuse
  of an existing gate or an explicit, reviewed edit here.

Maintaining the rosters
-----------------------
:data:`PLATFORM_GATED_ROUTES` is typed by hand and must stay that way. Never seed
it from app state and never regenerate it from a failing run — both produce a
tautologically green guard. If this file goes red, the fix is a security review
of the route followed by an explicit edit here AND to CONTRIBUTING.md's
"Platform-gated endpoints" section, not a widened predicate.

⚠ Declared as a TUPLE, not a dict literal. A dict literal silently absorbs a
duplicated key and keeps the LAST value, so a pasted-twice line could quietly
downgrade an entry's expected gate while the count still read 62. C5 exists to
make that visible, and it only works because the source of truth is a tuple.
Same posture, and same reason, as ``PUBLIC_ROUTES``.

Why the app import is safe
--------------------------
``_run_migrations()`` and the scheduler task are both created inside the FastAPI
lifespan, never at module import. The one rule is: **never enter the real app's
lifespan.** Read ``app.routes``; never ``with TestClient(app.main:app)``. That
rule is scoped to the REAL app — Legs 2 and 3 build their own application via
``make_test_app`` and must use ``with TestClient(...)`` on it, as every sibling
does.

Scope caveats — read before trusting a green run
------------------------------------------------
1. **Permission adequacy is not tested.** ``ROLE_PERMISSIONS`` is ``{}``
   (``permissions.py:79``), so every gate here collapses to ``is_superadmin`` and
   no identity that can exist today distinguishes ``users.delete`` from
   ``users.view``. Leg 1b pins *which* gate each route carries so a downgrade is
   visible in review, but it is latent rather than exploitable until L4.8 ships
   partial platform roles. **When it does, the right instrument is a second
   behavioural persona holding a partial role — not a bigger map.**
2. **Object-level authorization (IDOR) is not tested.**
   ``require_permission("orgs.manage")`` never checks *which* org.
3. **A new UNGATED platform route mounted outside ``/api/v1/admin/`` is caught by
   neither leg** — there is no gate to match on and no prefix to sweep.
4. Frontend ``hasPlatformPermission`` checks are UX, not a security control, and
   are correctly out of scope.
"""
from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Callable

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.auth.pat import require_interactive_session
from app.auth.permissions import ALL_PERMISSIONS, has_permission, require_permission
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.security import create_access_token
from tests.factories import make_test_app

from app.routers.admin import router as admin_router
from app.routers.admin_ai_usage import router as admin_ai_usage_router
from app.routers.admin_analytics import router as admin_analytics_router
from app.routers.admin_announcements import router as admin_announcements_router
from app.routers.admin_audit import router as admin_audit_router
from app.routers.admin_broadcasts import router as admin_broadcasts_router
from app.routers.admin_features import router as admin_features_router
from app.routers.admin_orgs import router as admin_orgs_router
from app.routers.admin_rate_limit_overrides import (
    router as admin_rate_limit_overrides_router,
)
from app.routers.admin_roles import router as admin_roles_router
from app.routers.admin_subscriptions import router as admin_subscriptions_router
from app.routers.admin_users import router as admin_users_router
from app.routers.api_tokens import router as api_tokens_router
from app.routers.plans import router as plans_router

UTC = timezone.utc
ADMIN_PREFIX = "/api/v1/admin/"

# ── The reviewed platform-privileged surface ────────────────────────────────
#
# (METHOD, path, descriptors). Method is part of the key: GET and POST on the
# same path can carry different gates.
#
# A descriptor is either ``perm:<permission>`` (the string the route's
# ``require_permission`` closure closes over) or ``superadmin:<module>`` (the
# module defining the matched ``require_superadmin`` object).
PLATFORM_GATED_ROUTES: tuple[tuple[str, str, frozenset[str]], ...] = (
    # ── app/routers/admin.py ────────────────────────────────────────────────
    # ⚠ This module is why the ticket's ``admin_*.py`` glob was rejected: the
    # glob does not match the filename ``admin.py``.
    ("GET", "/api/v1/admin/dashboard", frozenset({"perm:admin.view"})),
    # Cross-domain on purpose: whoever can edit plan features needs the key
    # catalog, so this admin-path route is gated on a plans permission.
    ("GET", "/api/v1/admin/feature-catalog", frozenset({"perm:plans.manage"})),
    # ── admin_ai_usage.py ───────────────────────────────────────────────────
    ("GET", "/api/v1/admin/ai/usage", frozenset({"superadmin:admin_ai_usage"})),
    # ── admin_analytics.py ──────────────────────────────────────────────────
    ("GET", "/api/v1/admin/analytics", frozenset({"perm:analytics.view"})),
    # ── admin_announcements.py ──────────────────────────────────────────────
    ("GET", "/api/v1/admin/announcements", frozenset({"superadmin:admin_announcements"})),
    ("POST", "/api/v1/admin/announcements", frozenset({"superadmin:admin_announcements"})),
    (
        "DELETE",
        "/api/v1/admin/announcements/{announcement_id}",
        frozenset({"superadmin:admin_announcements"}),
    ),
    (
        "PATCH",
        "/api/v1/admin/announcements/{announcement_id}",
        frozenset({"superadmin:admin_announcements"}),
    ),
    # ── admin_audit.py ──────────────────────────────────────────────────────
    ("GET", "/api/v1/admin/audit", frozenset({"perm:audit.view"})),
    # ── admin_broadcasts.py ─────────────────────────────────────────────────
    ("GET", "/api/v1/admin/broadcasts", frozenset({"superadmin:admin_broadcasts"})),
    ("POST", "/api/v1/admin/broadcasts", frozenset({"superadmin:admin_broadcasts"})),
    (
        "DELETE",
        "/api/v1/admin/broadcasts/{broadcast_id}",
        frozenset({"superadmin:admin_broadcasts"}),
    ),
    ("GET", "/api/v1/admin/broadcasts/{broadcast_id}", frozenset({"superadmin:admin_broadcasts"})),
    (
        "POST",
        "/api/v1/admin/broadcasts/{broadcast_id}/dry-run",
        frozenset({"superadmin:admin_broadcasts"}),
    ),
    (
        "GET",
        "/api/v1/admin/broadcasts/{broadcast_id}/preview",
        frozenset({"superadmin:admin_broadcasts"}),
    ),
    (
        "GET",
        "/api/v1/admin/broadcasts/{broadcast_id}/recipients",
        frozenset({"superadmin:admin_broadcasts"}),
    ),
    (
        "POST",
        "/api/v1/admin/broadcasts/{broadcast_id}/resume",
        frozenset({"superadmin:admin_broadcasts"}),
    ),
    (
        "POST",
        "/api/v1/admin/broadcasts/{broadcast_id}/send",
        frozenset({"superadmin:admin_broadcasts"}),
    ),
    # ── admin_features.py (router-level require_superadmin) ─────────────────
    ("GET", "/api/v1/admin/features", frozenset({"superadmin:admin_features"})),
    ("PUT", "/api/v1/admin/features/{feature}", frozenset({"superadmin:admin_features"})),
    ("GET", "/api/v1/admin/orgs/{org_id}/features", frozenset({"superadmin:admin_features"})),
    (
        "PUT",
        "/api/v1/admin/orgs/{org_id}/features/{feature}",
        frozenset({"superadmin:admin_features"}),
    ),
    # ── admin_orgs.py ───────────────────────────────────────────────────────
    ("GET", "/api/v1/admin/orgs", frozenset({"perm:orgs.view"})),
    (
        "POST",
        "/api/v1/admin/orgs/feature-overrides/sweep-expired",
        frozenset({"perm:orgs.manage"}),
    ),
    ("DELETE", "/api/v1/admin/orgs/{org_id}", frozenset({"perm:orgs.manage"})),
    ("GET", "/api/v1/admin/orgs/{org_id}", frozenset({"perm:orgs.view"})),
    (
        "DELETE",
        "/api/v1/admin/orgs/{org_id}/feature-overrides/{feature_key}",
        frozenset({"perm:orgs.manage"}),
    ),
    (
        "PUT",
        "/api/v1/admin/orgs/{org_id}/feature-overrides/{feature_key}",
        frozenset({"perm:orgs.manage"}),
    ),
    ("GET", "/api/v1/admin/orgs/{org_id}/feature-state", frozenset({"perm:orgs.view"})),
    ("GET", "/api/v1/admin/orgs/{org_id}/members", frozenset({"perm:orgs.view"})),
    # ⚠ Leg 3 must NOT pick this as the orgs.manage representative: it turns a
    # "target is superadmin" ConflictError into a 403, so a superadmin caller
    # can legitimately receive 403 here for a business reason.
    (
        "PATCH",
        "/api/v1/admin/orgs/{org_id}/members/{user_id}",
        frozenset({"perm:orgs.manage"}),
    ),
    ("PUT", "/api/v1/admin/orgs/{org_id}/subscription", frozenset({"perm:orgs.manage"})),
    # ── admin_rate_limit_overrides.py ───────────────────────────────────────
    (
        "GET",
        "/api/v1/admin/rate-limit-overrides",
        frozenset({"superadmin:admin_rate_limit_overrides"}),
    ),
    (
        "POST",
        "/api/v1/admin/rate-limit-overrides",
        frozenset({"superadmin:admin_rate_limit_overrides"}),
    ),
    (
        "GET",
        "/api/v1/admin/rate-limit-overrides/endpoint-catalogue",
        frozenset({"superadmin:admin_rate_limit_overrides"}),
    ),
    (
        "DELETE",
        "/api/v1/admin/rate-limit-overrides/{override_id}",
        frozenset({"superadmin:admin_rate_limit_overrides"}),
    ),
    (
        "PATCH",
        "/api/v1/admin/rate-limit-overrides/{override_id}",
        frozenset({"superadmin:admin_rate_limit_overrides"}),
    ),
    # ── admin_roles.py (⚠ router declares NO prefix; paths are per-decorator,
    #    which is why file/prefix-derived inventories cannot see this module) ─
    ("GET", "/api/v1/admin/permissions", frozenset({"perm:roles.manage"})),
    ("GET", "/api/v1/admin/roles", frozenset({"perm:roles.manage"})),
    ("POST", "/api/v1/admin/roles", frozenset({"perm:roles.manage"})),
    ("DELETE", "/api/v1/admin/roles/{role_id}", frozenset({"perm:roles.manage"})),
    ("GET", "/api/v1/admin/roles/{role_id}", frozenset({"perm:roles.manage"})),
    ("PATCH", "/api/v1/admin/roles/{role_id}", frozenset({"perm:roles.manage"})),
    # ── admin_subscriptions.py ──────────────────────────────────────────────
    ("GET", "/api/v1/admin/subscriptions", frozenset({"perm:subscriptions.view"})),
    ("GET", "/api/v1/admin/subscriptions/kpis", frozenset({"perm:subscriptions.view"})),
    (
        "GET",
        "/api/v1/admin/subscriptions/{subscription_id}",
        frozenset({"perm:subscriptions.view"}),
    ),
    # ── admin_users.py ──────────────────────────────────────────────────────
    ("GET", "/api/v1/admin/users", frozenset({"perm:users.view"})),
    # Merging users moves org membership, hence orgs.manage rather than a
    # users.* permission.
    ("POST", "/api/v1/admin/users/merge", frozenset({"perm:orgs.manage"})),
    ("DELETE", "/api/v1/admin/users/{user_id}", frozenset({"perm:users.delete"})),
    ("GET", "/api/v1/admin/users/{user_id}", frozenset({"perm:users.view"})),
    (
        "POST",
        "/api/v1/admin/users/{user_id}/email-change",
        frozenset({"perm:users.reset_credentials"}),
    ),
    (
        "DELETE",
        "/api/v1/admin/users/{user_id}/pending-email",
        frozenset({"perm:users.reset_credentials"}),
    ),
    # ── ADJACENT SURFACE: platform-gated but NOT under /api/v1/admin/ ───────
    #
    # These 10 reach the same platform gates through a different path prefix.
    # They are in the roster because ``/api/v1/admin/`` is a routing convention,
    # not a security boundary — a fence that drew its perimeter from the path
    # would leave them unguarded. Enumerating BY GATE (Leg 1b) finds them with
    # no second glob to maintain.
    #
    # ⚠ Their presence here asserts this is a closed, reviewed set. Unlike the
    # public allowlist there is no prior security review backing that claim for
    # these ten; it rests on the TBD-440 Phase 0 sweep and nothing older.
    ("POST", "/api/v1/plans", frozenset({"perm:plans.manage"})),
    ("GET", "/api/v1/plans/all", frozenset({"perm:plans.manage"})),
    ("DELETE", "/api/v1/plans/{plan_id}", frozenset({"perm:plans.manage"})),
    ("GET", "/api/v1/plans/{plan_id}", frozenset({"perm:plans.manage"})),
    ("PUT", "/api/v1/plans/{plan_id}", frozenset({"perm:plans.manage"})),
    ("POST", "/api/v1/plans/{plan_id}/duplicate", frozenset({"perm:plans.manage"})),
    ("GET", "/api/v1/system/api-tokens", frozenset({"superadmin:api_tokens"})),
    ("POST", "/api/v1/system/api-tokens", frozenset({"superadmin:api_tokens"})),
    ("POST", "/api/v1/system/api-tokens/revoke-all", frozenset({"superadmin:api_tokens"})),
    ("DELETE", "/api/v1/system/api-tokens/{token_id}", frozenset({"superadmin:api_tokens"})),
)

EXPECTED_ROUTE_COUNT = 62

# Routes under /api/v1/admin/ deliberately exempt from carrying a platform gate.
# EMPTY, and it must stay that way without an explicit security review. An entry
# here is a hole in the fence, not a convenience.
UNGATED_ADMIN_EXEMPTIONS: frozenset[tuple[str, str]] = frozenset()
EXPECTED_EXEMPTION_COUNT = 0

# The six independently-defined require_superadmin modules (spec §3).
SUPERADMIN_MODULES = (
    "admin_ai_usage",
    "admin_announcements",
    "admin_broadcasts",
    "admin_features",
    "admin_rate_limit_overrides",
    "api_tokens",
)

# ── The refusal vocabulary ──────────────────────────────────────────────────
#
# ⚠⚠ THE SHARPEST CONTROL IN THIS FILE (C3).
# ``HTTPBearer(auto_error=True)`` (``app/deps.py:24``) answers **403 "Not
# authenticated"** when the Authorization header is missing. So if this file's
# token fixture ever breaks, every route 403s and Leg 2 passes GREEN while
# asserting nothing whatsoever. Asserting the status code alone is not enough.
#
# An ALLOW set, never a deny set: a new refusal message on a security gate
# should go red and be read. That brittleness is the feature.
AUTHZ_REFUSAL_DETAILS = frozenset(
    {
        "Forbidden",  # permissions.py + 5 of the 6 require_superadmin defs
        "Superadmin access required",  # admin_features.py, the sixth
    }
)

# Refusals that mean the fence itself is broken, not that a gate fired.
# Named so a failure is attributable rather than "something else happened".
DIAGNOSTIC_DETAILS = {
    "Not authenticated": "no credential was sent (HTTPBearer) — token fixture is broken",
    "Invalid authentication credentials": "the Bearer scheme prefix is missing",
    "This action requires an interactive session": "a PAT was sent; wrong gate under test",
    "Admin access required": "require_settings_admin fired — an ORG-scoped gate, not a platform one",
    "Admin or owner role required": "an org-scoped gate fired, not a platform one",
    "Owner role required": "an org-scoped gate fired, not a platform one",
}

PLACEHOLDER_ID = "999999"


# ── Gate identification (never by name) ─────────────────────────────────────


def _require_permission_code():
    """The single code object shared by every ``require_permission`` closure.

    Resolved from the factory imported by identity, so a rename or restructure
    raises here — RED — rather than silently matching nothing.
    """
    probe = require_permission("admin.view")
    assert probe.__code__.co_freevars == ("permission",), (
        "require_permission's inner closure no longer closes over exactly "
        "('permission',). The descriptor extraction below reads "
        "__closure__[0].cell_contents and would now read the wrong cell."
    )
    return probe.__code__


REQUIRE_PERMISSION_CODE = _require_permission_code()


def _superadmin_objects() -> dict[Callable[..., Any], str]:
    out: dict[Callable[..., Any], str] = {}
    for mod in SUPERADMIN_MODULES:
        module = importlib.import_module(f"app.routers.{mod}")
        out[getattr(module, "require_superadmin")] = mod
    return out


SUPERADMIN_GATES = _superadmin_objects()


def iter_dependant_calls(dependant, seen: set[int] | None = None) -> Iterator[Any]:
    """Every dependency call in the transitive closure, cycle-guarded."""
    if seen is None:
        seen = set()
    if id(dependant) in seen:
        return
    seen.add(id(dependant))
    if dependant.call is not None:
        yield dependant.call
    for sub in dependant.dependencies:
        yield from iter_dependant_calls(sub, seen)


def descriptors_for(route) -> frozenset[str]:
    """The platform-authorization gates this route is actually behind."""
    found: set[str] = set()
    for call in iter_dependant_calls(route.dependant):
        if getattr(call, "__code__", None) is REQUIRE_PERMISSION_CODE:
            found.add("perm:" + call.__closure__[0].cell_contents)
        elif call in SUPERADMIN_GATES:
            found.add("superadmin:" + SUPERADMIN_GATES[call])
    return frozenset(found)


def _api_routes():
    from fastapi.routing import APIRoute

    from app.main import app

    return [r for r in app.routes if isinstance(r, APIRoute)]


def _observed_by_gate() -> dict[tuple[str, str], frozenset[str]]:
    out = {}
    for r in _api_routes():
        d = descriptors_for(r)
        if not d:
            continue
        for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
            out[(m, r.path)] = d
    return out


def _observed_admin_paths() -> dict[tuple[str, str], frozenset[str]]:
    out = {}
    for r in _api_routes():
        if not r.path.startswith(ADMIN_PREFIX):
            continue
        d = descriptors_for(r)
        for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
            out[(m, r.path)] = d
    return out


ROSTER = {(m, p): d for m, p, d in PLATFORM_GATED_ROUTES}


# ── Leg 1a — every admin route is gated (catches UNGATED) ───────────────────


def test_leg1a_no_admin_route_is_ungated():
    """⚠ THE FENCE THE TICKET EXISTS FOR.

    An ungated route has an EMPTY descriptor set, so it never appears in the
    by-gate map at all — Leg 1b compares 62 to 62 and passes. Only a sweep over
    resolved PATHS can see it.

    This is not the ``admin_*.py`` filename glob the ticket asked for and this
    file rejects. It is a prefix sweep over paths the running app resolved, so
    ``admin.py`` (which the filename glob misses) and ``admin_roles.py`` (which
    declares no prefix at all) are both covered for free.
    """
    ungated = sorted(
        key for key, desc in _observed_admin_paths().items()
        if not desc and key not in UNGATED_ADMIN_EXEMPTIONS
    )
    assert not ungated, (
        "UNGATED admin route(s) — reachable by ANY authenticated user:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in ungated)
        + "\n\nAdd require_permission(...) or an existing require_superadmin to "
        "the route, then add it to PLATFORM_GATED_ROUTES. Do NOT add it to "
        "UNGATED_ADMIN_EXEMPTIONS without a security review."
    )


# ── Leg 1b — the gated surface is exactly the reviewed one ──────────────────


def test_leg1b_gated_surface_matches_the_roster():
    """Two-way. Catches UNREVIEWED, STALE and DOWNGRADE.

    Enumerated BY GATE rather than by path, so a newly gated route mounted
    anywhere — including outside ``/api/v1/admin/`` — lands here with no second
    path glob to keep honest.
    """
    observed = _observed_by_gate()
    unreviewed = sorted(set(observed) - set(ROSTER))
    stale = sorted(set(ROSTER) - set(observed))
    downgraded = sorted(
        (k, sorted(ROSTER[k]), sorted(observed[k]))
        for k in set(ROSTER) & set(observed)
        if ROSTER[k] != observed[k]
    )
    problems = []
    if unreviewed:
        problems.append(
            "UNREVIEWED (gated, but not in the roster — security-review it, then "
            "add it here and to CONTRIBUTING.md):\n  "
            + "\n  ".join(f"{m} {p} {sorted(observed[(m, p)])}" for m, p in unreviewed)
        )
    if stale:
        problems.append(
            "STALE (in the roster, but no longer on the app — deleted, renamed, "
            "or its router stopped being mounted):\n  "
            + "\n  ".join(f"{m} {p}" for m, p in stale)
        )
    if downgraded:
        problems.append(
            "DOWNGRADE (the gate changed — this is the privilege-escalation "
            "class, confirm it was deliberate):\n  "
            + "\n  ".join(f"{k[0]} {k[1]}: expected {e}, found {o}" for k, e, o in downgraded)
        )
    assert not problems, "\n\n".join(problems)


# ── Controls ────────────────────────────────────────────────────────────────


def test_c4_platform_authorization_is_not_confused_with_any_authorization():
    """C4 — the discrimination control.

    The DoD's "or a router-level equivalent" is a fail-open. These three routes
    each carry a gate that is NOT platform authorization, and all three must
    read as ungated. If any yields a descriptor, the detector has been broadened
    into exactly the hole this file exists to close.

    ``PUT /api/v1/settings/features/{feature}`` is the route the AST pass got
    wrong during the authentication fence's design, which is why it is pinned
    here specifically.
    """
    by_path = {}
    for r in _api_routes():
        for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
            by_path[(m, r.path)] = descriptors_for(r)

    cases = {
        ("GET", "/api/v1/tags"): "no authorization at all",
        ("PUT", "/api/v1/users/me"): "require_interactive_session only",
        ("PUT", "/api/v1/settings/features/{feature}"): "require_settings_admin (org-scoped)",
    }
    for key, why in cases.items():
        assert key in by_path, f"control route vanished: {key[0]} {key[1]}"
        assert by_path[key] == frozenset(), (
            f"{key[0]} {key[1]} ({why}) was detected as PLATFORM-gated. "
            "The gate predicate has been broadened and now blesses a gate that "
            "does not authorize."
        )


def test_c4b_require_interactive_session_is_never_a_platform_gate():
    """C4b — the same rule stated over the whole app rather than three samples.

    18 admin routes carry ``require_interactive_session``. Every one of them
    must ALSO carry a real platform gate; none may rely on it alone.
    """
    offenders = []
    for r in _api_routes():
        calls = list(iter_dependant_calls(r.dependant))
        if not any(c is require_interactive_session for c in calls):
            continue
        if r.path.startswith(ADMIN_PREFIX) and not descriptors_for(r):
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                offenders.append(f"{m} {r.path}")
    assert not offenders, (
        "admin route(s) gated ONLY by require_interactive_session, which "
        "authorizes nobody — any authenticated non-admin on a browser session "
        "can reach them:\n  " + "\n  ".join(offenders)
    )


def test_c5_roster_is_the_declared_size_and_has_no_duplicates():
    """C5 — a duplicated line must be visible.

    Only meaningful because PLATFORM_GATED_ROUTES is a TUPLE. As a dict literal
    a repeated key would be silently absorbed, keeping the LAST value — which
    could downgrade an entry's expected gate while the count still read 62.
    """
    assert len(PLATFORM_GATED_ROUTES) == EXPECTED_ROUTE_COUNT
    keys = [(m, p) for m, p, _ in PLATFORM_GATED_ROUTES]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"duplicated roster entries: {dupes}"
    assert len(ROSTER) == EXPECTED_ROUTE_COUNT


def test_ungated_exemption_list_has_not_grown():
    """The exemption list is Leg 1a's fail-open surface.

    Leg 1a's failure message tells the reader not to add to
    UNGATED_ADMIN_EXEMPTIONS without a security review — but nothing enforced
    that, so the cheapest way to make a red Leg 1a go green was to add an entry.
    Same posture as ``test_no_roster_entry_is_ungated`` for the roster.
    """
    assert len(UNGATED_ADMIN_EXEMPTIONS) == EXPECTED_EXEMPTION_COUNT, (
        "an admin route has been exempted from carrying an authorization gate. "
        "That is a hole in the fence: it must come with a security review and a "
        "deliberate bump of EXPECTED_EXEMPTION_COUNT, never a silent edit."
    )


def test_c7_superadmin_vocabulary_is_exactly_the_six_known_gates():
    """C7 — the six gate objects are distinct, and all six are actually wired up.

    ⚠ SCOPE, precisely. ``observed`` is built by :func:`descriptors_for`, which
    can only emit ``superadmin:<m>`` for ``m`` already in
    :data:`SUPERADMIN_MODULES`. So the ``⊆`` direction is true BY CONSTRUCTION
    and cannot fail. What this test really asserts is the ``⊇`` direction: one
    of the six exists but is wired to no route — a gate that was removed from
    its last route, which review would otherwise not notice.

    It is NOT the backstop for a seventh ``require_superadmin`` in a new module.
    That is caught by Leg 1a, and only when the route sits under
    ``/api/v1/admin/``; a seventh gate on a route mounted elsewhere is caught by
    nothing here (module docstring, scope caveat 3).

    Deleting one of the six IS caught, but by the ``getattr`` in
    :func:`_superadmin_objects` raising at import, not by this assertion.
    """
    assert len(SUPERADMIN_GATES) == len(SUPERADMIN_MODULES), (
        "two of the six require_superadmin definitions are the same object — "
        "if they were deliberately consolidated, shrink SUPERADMIN_MODULES and "
        "re-review the roster's superadmin: descriptors."
    )
    observed = {d for ds in _observed_by_gate().values() for d in ds if d.startswith("superadmin:")}
    assert observed == {f"superadmin:{m}" for m in SUPERADMIN_MODULES}


def test_c8_every_permission_descriptor_is_a_real_permission():
    """C8 — ALL_PERMISSIONS is an independent frozenset, so this is not circular.

    This repo has no type checker, so the ``Permission`` Literal is unenforced
    and a typo'd permission string fails CLOSED — invisible to a superadmin
    because ``has_permission`` short-circuits on ``is_superadmin``.
    """
    used = {d.split(":", 1)[1] for _, _, ds in PLATFORM_GATED_ROUTES for d in ds if d.startswith("perm:")}
    unknown = sorted(used - set(ALL_PERMISSIONS))
    assert not unknown, f"permission strings not in ALL_PERMISSIONS: {unknown}"


def test_no_roster_entry_is_ungated():
    """The roster is its own fail-open surface.

    The natural repair when this file goes red on a new route is to paste the
    route in. This makes pasting it in with an EMPTY gate set impossible, at the
    DB-free layer that still runs if the behavioural legs are skipped or their
    fixtures break.
    """
    empty = sorted((m, p) for m, p, d in PLATFORM_GATED_ROUTES if not d)
    assert not empty, f"roster entries with no gate: {empty}"


# ── Behavioural legs ────────────────────────────────────────────────────────
#
# ⚠ Everything below builds its OWN application. The "never enter the lifespan"
# rule is about ``app.main:app``; these use ``make_test_app`` and MUST use
# ``with TestClient(...)`` on it, exactly as the sibling fences do.
#
# ⚠ NOT ``make_test_app(..., current_user=...)``. That override replaces
# ``get_current_user`` outright, so the whole require_permission chain would
# resolve off a fabricated user and prove nothing. Only get_db /
# get_session_factory are overridden here; identity is resolved through the real
# seam, as in production.

ROUTERS = [
    admin_router,
    admin_ai_usage_router,
    admin_analytics_router,
    admin_announcements_router,
    admin_audit_router,
    admin_broadcasts_router,
    admin_features_router,
    admin_orgs_router,
    admin_rate_limit_overrides_router,
    admin_roles_router,
    admin_subscriptions_router,
    admin_users_router,
    api_tokens_router,
    plans_router,
]


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _make_user(factory, *, superadmin: bool) -> User:
    async with factory() as s:
        org = Organization(name="Acme" if not superadmin else "Platform", billing_cycle_day=1)
        s.add(org)
        await s.commit()
        u = User(
            org_id=org.id,
            username="root" if superadmin else "owner",
            email=("root" if superadmin else "owner") + "@example.com",
            password_hash="hashed",
            # OWNER in BOTH cases. For the non-platform caller this is the
            # MOST PRIVILEGED non-platform identity: a MEMBER-based fence would
            # pass an admin route mis-gated with an org-scoped guard, which
            # every org owner could then reach.
            role=Role.OWNER,
            is_superadmin=superadmin,
            is_active=True,
            email_verified=True,
            last_active_at=datetime.now(UTC),
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        s.expunge(u)
        return u


@pytest.fixture
async def org_owner(factory) -> User:
    return await _make_user(factory, superadmin=False)


@pytest.fixture
async def platform_admin(factory) -> User:
    return await _make_user(factory, superadmin=True)


@pytest.fixture
def app(factory):
    application = make_test_app(factory, routers=ROUTERS, override_session_factory=True)
    application.state.limiter = limiter
    limiter.reset()
    try:
        yield application
    finally:
        limiter.reset()


def _concrete(path: str) -> str:
    """Substitute a placeholder for every {param}.

    Verified during design: this shadows 0 of the 62 onto a different route.
    Gates are DEPENDENCIES, so they resolve before the handler body and before
    the top-level dependant's own param validation — a nonexistent id therefore
    yields the gate's 403, never a 404 or 422.
    """
    out = []
    for seg in path.split("/"):
        out.append(PLACEHOLDER_ID if seg.startswith("{") and seg.endswith("}") else seg)
    return "/".join(out)


def _request(client, method: str, path: str, jwt: str):
    # json={} keeps a content-type on the request. A MALFORMED body would raise
    # RequestValidationError before solve_dependencies and defeat the leg, so
    # never pass a raw content= string here.
    return client.request(
        method, _concrete(path), headers={"Authorization": f"Bearer {jwt}"}, json={}
    )


def test_c6_behavioural_app_covers_the_same_gated_surface(app):
    """C6 — otherwise Leg 2's coverage could silently shrink.

    If a router were left out of ROUTERS, Leg 2 would 404 on its routes rather
    than exercising them. Comparing against the roster makes that RED.
    """
    from fastapi.routing import APIRoute

    mounted = {
        (m, r.path)
        for r in app.routes
        if isinstance(r, APIRoute)
        for m in sorted(r.methods - {"HEAD", "OPTIONS"})
    }
    missing = sorted(set(ROSTER) - mounted)
    assert not missing, (
        "roster routes absent from the behavioural app — Leg 2 would 404 rather "
        "than test them:\n  " + "\n  ".join(f"{m} {p}" for m, p in missing)
    )


@pytest.mark.parametrize(
    "method,path",
    [(m, p) for m, p, _ in PLATFORM_GATED_ROUTES],
    ids=[f"{m}_{p}" for m, p, _ in PLATFORM_GATED_ROUTES],
)
async def test_leg2_non_platform_caller_is_refused(app, org_owner, method, path):
    """⚠⚠ THE SECURITY ASSERTION.

    Leg 1 is satisfied by a gate that is wired in and broken. This is not: gut
    ``has_permission`` or any ``require_superadmin`` body and this leg goes red
    on every affected route.

    ⚠ Parametrized over the hand-typed ROSTER, never over the observed set. An
    injected ungated route is not observed, so an observed-driven leg would
    never call it and would pass green.

    ⚠ 403 EXACTLY, never "not 200" — a 404 also satisfies "not 200" and means
    the router was never mounted, which is a vacuous pass.
    """
    from fastapi.testclient import TestClient

    jwt = create_access_token(org_owner.id, org_owner.org_id, org_owner.role.value)
    with TestClient(app) as client:
        res = _request(client, method, path, jwt)

    assert res.status_code == 403, (
        f"{method} {path} did not refuse a non-platform org OWNER "
        f"(got {res.status_code}): {res.text[:300]}"
    )

    # C3 — the status code alone is not enough. A missing Authorization header
    # also produces 403, so a broken token fixture would make this whole leg
    # vacuously green.
    detail = res.json().get("detail") if res.headers.get("content-type", "").startswith("application/json") else None
    assert isinstance(detail, str), (
        f"{method} {path} refused with a non-string detail ({detail!r}); expected "
        f"one of {sorted(AUTHZ_REFUSAL_DETAILS)}"
    )
    assert detail in AUTHZ_REFUSAL_DETAILS, (
        f"{method} {path} returned 403 but NOT from a platform authorization "
        f"gate. detail={detail!r}. "
        + DIAGNOSTIC_DETAILS.get(detail, "unrecognised refusal — investigate")
    )


# One route per distinct descriptor. NOT all 62: some handlers legitimately 403
# a superadmin for business reasons, and an all-62 positive leg would need an
# exemption list, which is a fail-open knob.
#
# ``EXPECT_200`` where a superadmin genuinely gets a payload; ``NOT_REFUSED``
# only where no such route exists for that descriptor, with the reason named.
EXPECT_200 = "200"
NOT_REFUSED = "not-refused"

ADMISSION_SAMPLES: tuple[tuple[str, str, str, str, str], ...] = (
    ("perm:admin.view", "GET", "/api/v1/admin/dashboard", EXPECT_200, ""),
    ("perm:analytics.view", "GET", "/api/v1/admin/analytics", EXPECT_200, ""),
    ("perm:audit.view", "GET", "/api/v1/admin/audit", EXPECT_200, ""),
    ("perm:orgs.view", "GET", "/api/v1/admin/orgs", EXPECT_200, ""),
    ("perm:plans.manage", "GET", "/api/v1/plans/all", EXPECT_200, ""),
    ("perm:roles.manage", "GET", "/api/v1/admin/roles", EXPECT_200, ""),
    ("perm:subscriptions.view", "GET", "/api/v1/admin/subscriptions", EXPECT_200, ""),
    ("perm:users.view", "GET", "/api/v1/admin/users", EXPECT_200, ""),
    # ⚠ Query string supplied deliberately. Without it this route answers 422
    # for a superadmin (org_id and period are mandatory Query(...) params) —
    # which would have forced a weaker NOT_REFUSED expectation here. That the
    # SAME route answers 403 for the non-platform caller in Leg 2 is the direct
    # demonstration that the gate preempts body/param validation.
    (
        "superadmin:admin_ai_usage",
        "GET",
        "/api/v1/admin/ai/usage?org_id=1&period=2026-01",
        EXPECT_200,
        "",
    ),
    ("superadmin:admin_announcements", "GET", "/api/v1/admin/announcements", EXPECT_200, ""),
    ("superadmin:admin_broadcasts", "GET", "/api/v1/admin/broadcasts", EXPECT_200, ""),
    ("superadmin:admin_features", "GET", "/api/v1/admin/features", EXPECT_200, ""),
    (
        "superadmin:admin_rate_limit_overrides",
        "GET",
        "/api/v1/admin/rate-limit-overrides",
        EXPECT_200,
        "",
    ),
    ("superadmin:api_tokens", "GET", "/api/v1/system/api-tokens", EXPECT_200, ""),
    # No GET-shaped route exists for these three; every route carrying them
    # mutates or addresses a specific entity, so a placeholder id 404s.
    (
        "perm:orgs.manage",
        "POST",
        "/api/v1/admin/orgs/feature-overrides/sweep-expired",
        NOT_REFUSED,
        "sweep is a mutation; asserts only that the gate admitted the caller",
    ),
    (
        "perm:users.delete",
        "DELETE",
        "/api/v1/admin/users/{user_id}",
        NOT_REFUSED,
        "every users.delete route addresses an entity; id 999999 404s",
    ),
    (
        "perm:users.reset_credentials",
        "DELETE",
        "/api/v1/admin/users/{user_id}/pending-email",
        NOT_REFUSED,
        "same; no listing route carries this permission",
    ),
)


def test_admission_samples_cover_every_descriptor():
    """A sample table that silently stopped covering a gate would hide F8."""
    covered = {d for d, *_ in ADMISSION_SAMPLES}
    all_desc = {d for _, _, ds in PLATFORM_GATED_ROUTES for d in ds}
    assert covered == all_desc, (
        f"descriptors with no admission sample: {sorted(all_desc - covered)}; "
        f"samples for descriptors that no longer exist: {sorted(covered - all_desc)}"
    )


@pytest.mark.parametrize(
    "descriptor,method,path,expectation,why",
    ADMISSION_SAMPLES,
    ids=[d for d, *_ in ADMISSION_SAMPLES],
)
async def test_leg3_platform_admin_is_admitted(
    app, platform_admin, descriptor, method, path, expectation, why
):
    """Leg 3 — proves the fence is not simply asserting "everything 403s".

    ⚠ NOT "not 403". ``get_current_user`` raises **401** on an unknown sub, an
    inactive user, or ``iat < token_cutoff`` — so a Leg 3 fixture that forgot a
    commit would return 401 everywhere and a ``!= 403`` assertion would pass on
    all of them, leaving this file asserting only "everything refuses everyone",
    which is exactly what this leg exists to disprove.
    """
    from fastapi.testclient import TestClient

    jwt = create_access_token(platform_admin.id, platform_admin.org_id, platform_admin.role.value)
    with TestClient(app) as client:
        res = _request(client, method, path, jwt)

    if expectation == EXPECT_200:
        assert res.status_code == 200, (
            f"{descriptor}: {method} {path} did not admit a superadmin "
            f"(got {res.status_code}): {res.text[:300]}"
        )
    else:
        assert res.status_code not in (401, 403), (
            f"{descriptor}: {method} {path} refused a superadmin "
            f"({res.status_code}) — {why}: {res.text[:300]}"
        )
        # ⚠ "not refused" alone cannot tell "the gate admitted the caller and
        # the handler then 404'd on id 999999" from "the route never matched at
        # all" — a routing 404 satisfies it just as well. That is this file's
        # own critique of `!= 403`, one level down. Starlette's router emits a
        # bare {"detail": "Not Found"}; a handler that ran emits its own body.
        if res.status_code == 404:
            body = res.json().get("detail")
            assert body != "Not Found", (
                f"{descriptor}: {method} {path} returned a ROUTING 404 — the "
                "path did not match any route, so this sample proves nothing "
                "about admission. Fix the sample path."
            )


async def test_c9_leg3_identity_is_real(app, platform_admin):
    """C9 — makes a broken Leg 3 fixture attributable in one line.

    If the superadmin JWT is not resolving to a live, active user, this fails
    with 401 here rather than silently weakening all 17 samples above.
    """
    from fastapi.testclient import TestClient

    jwt = create_access_token(platform_admin.id, platform_admin.org_id, platform_admin.role.value)
    with TestClient(app) as client:
        res = _request(client, "GET", "/api/v1/admin/dashboard", jwt)
    assert res.status_code == 200, (
        f"the Leg 3 superadmin identity does not resolve (got {res.status_code}: "
        f"{res.text[:300]}). Every Leg 3 assertion above is untrustworthy until "
        "this passes."
    )


async def test_c1_the_non_platform_caller_really_holds_no_platform_power(org_owner):
    """C1 — an accidentally-privileged caller would make Leg 2 vacuous.

    Iterating ALL_PERMISSIONS rather than a hand-list means this extends itself
    the day a new Permission literal is added.
    """
    from app.auth.permissions import _platform_roles

    assert org_owner.is_superadmin is False
    assert _platform_roles(org_owner) == frozenset()
    granted = sorted(p for p in ALL_PERMISSIONS if has_permission(org_owner, p))
    assert not granted, f"the Leg 2 caller holds platform permissions: {granted}"
