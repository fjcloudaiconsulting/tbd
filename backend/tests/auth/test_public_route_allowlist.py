"""Inventory guard: the anonymous API surface is a closed, reviewed set.

CLAUDE.md and CONTRIBUTING.md both say the public endpoint set is
"deliberately small and closed". Until this file existed that policy had
**zero** automated enforcement: every router test builds its app through
``make_test_app(..., current_user=...)``, which writes
``app.dependency_overrides[get_current_user]``. Under that override a route
that has *lost* its ``Depends(get_current_user)`` behaves exactly like one
that still has it — so deleting the dependency from any handler made the
route public and **not one test went red**.

This guard closes that hole by enumerating the real ``app.main:app`` and
comparing the routes reachable without authentication against
:data:`PUBLIC_ROUTES`, a hand-typed literal below.

How "authenticated" is decided
------------------------------
Exactly one rule: ``app.deps.get_current_user`` appears as ``.call``
somewhere in the transitive closure of ``route.dependant``. See
:func:`_reaches_get_current_user`.

The check is on **object identity, never on a name.** Auth is declared four
different ways in this codebase — handler signature, router-level
``dependencies=[...]``, router-level feature gates, and decorator-level
``dependencies=[...]`` — and FastAPI merges all four into the same
``route.dependant`` tree, so one walk covers them all. A name-based roster
would have to list ~13 wrapper names, six of which are the same name
(``require_superadmin``) defined independently in six different modules,
and it would **fail open** the day someone adds a ``require_x`` that looks
authoritative but never actually authenticates. Matching the leaf object is
what makes this immune to wrappers that do not exist yet. It also means
``require_permission("orgs.view")`` — a fresh closure per call, so
un-nameable — is handled for free.

``get_current_user_optional`` is deliberately **not** authentication: it
returns ``None`` on every failure path rather than raising. It is a
different object and correctly does not match. Its one consumer,
``GET /api/v1/auth/status``, is genuinely public, and control **P4** below
pins exactly that.

Scope caveat — read this before trusting the guard
--------------------------------------------------
This is a **structural** fence. It proves ``get_current_user`` is wired into
each route's dependency graph. It does **not** prove ``get_current_user``
still authenticates: gutting its body would leave this file green. That is
out of scope here and is covered by the behavioural auth suites
(``tests/auth/test_permissions.py``, ``test_pat_authentication.py``,
``tests/routers/test_auth.py``). Do not read a green run as "auth works".

Why runtime enumeration and not an AST scan
-------------------------------------------
An AST pass run during design reported
``PUT /api/v1/settings/features/{feature}`` as PUBLIC. It is not — it is
gated by ``require_settings_admin``, a locally-defined wrapper the name
roster did not know about. Runtime needs zero names; it needs one object
identity.

Why the app import is safe
--------------------------
``_run_migrations()`` and the scheduler task are both created **inside** the
FastAPI lifespan, never at module import; ``create_async_engine`` is lazy and
does not connect. The one rule is therefore: **never enter the lifespan.**
Read ``app.routes``; never ``with TestClient(app)``. ``from app.main import
app`` stays inside the test bodies because ``main.py`` calls
``setup_logging()`` at import.

Maintaining this file
---------------------
:data:`PUBLIC_ROUTES` is typed by hand and must stay that way. Never seed it
from app state (``[r for r in app.routes if not authed(r)]``) and never parse
it out of CONTRIBUTING.md — both produce a tautologically green guard that
enforces nothing. If this test goes red, the fix is a security review of the
new route followed by an explicit edit here **and** to CONTRIBUTING.md's
"Public endpoints" section, not a widened predicate.
"""
from __future__ import annotations

from typing import Any


# ── The reviewed anonymous surface ──────────────────────────────────────────
#
# Exact ``(METHOD, path)`` tuples. Method is part of the key on purpose:
# ``POST /auth/resend-verification-public`` is public while
# ``POST /auth/resend-verification`` is not.
#
# Prefixes are disqualified, and CONTRIBUTING's own ``/api/v1/auth/mfa/*``
# glob is the proof: a prefix rule would also allowlist ``mfa/setup``,
# ``mfa/enable``, ``mfa/disable`` and ``mfa/recovery-codes``, which are
# authenticated AND interactive-session-gated (see
# ``test_interactive_session_enumeration.py``). It would silently bless the
# whole account-takeover surface. The literal expands that glob to its four
# concrete pre-auth challenge routes instead.
#
# The set splits in two, mirroring CONTRIBUTING.md's "Public endpoints"
# section. "Open" routes carry no identity check at all. "Credential-bearing"
# routes do authenticate the caller — just via a mechanism that lives outside
# the dependency graph (refresh cookie, MFA challenge token, invitation JWT,
# reset/verify JWT, OAuth state cookie, Mailgun HMAC), which is why
# ``get_current_user`` cannot be attached to them.

# Declared as a TUPLE, not a set literal: a set literal would silently absorb
# a duplicated line, and P6 exists to make that visible in review.
PUBLIC_ROUTES: tuple[tuple[str, str], ...] = (
        # ── Open: no identity check at all ──────────────────────────────────
        # Liveness/readiness probes for the platform. Declared directly on
        # ``app``, not on a router, which is why the unit of enumeration has
        # to be the app rather than the router set.
        ("GET", "/health"),
        ("GET", "/ready"),
        # Serves feature flags and auth state to anonymous AND authenticated
        # callers. Uses ``get_current_user_optional``, which returns ``None``
        # rather than raising, so the route is reachable anonymously by
        # design. This is control P4 — see below.
        ("GET", "/api/v1/auth/status"),
        # Signup-time availability probe; runs before any account exists.
        ("GET", "/api/v1/auth/check-username"),
        # Account creation. Nothing to authenticate yet.
        ("POST", "/api/v1/auth/register"),
        # Password reset request. Answers uniformly regardless of whether the
        # address exists, so it leaks no account inventory.
        ("POST", "/api/v1/auth/forgot-password"),
        # Re-sends a verification email to an unverified account, which by
        # definition cannot hold a usable session yet. The non-``-public``
        # sibling ``POST /auth/resend-verification`` IS authenticated — the
        # method+path key is what keeps the two apart.
        ("POST", "/api/v1/auth/resend-verification-public"),
        # Starts the Google OAuth redirect; the caller is anonymous by
        # construction at this point in the flow.
        ("GET", "/api/v1/auth/google"),
        # Founding-members counter rendered on the marketing/signup surface,
        # before any account exists. Returns a single aggregate integer.
        ("GET", "/api/v1/public/founder-count"),
        # CSP violation sink. Browsers post these with no auth context and no
        # way to attach a bearer token; wired in via ``report-uri``/
        # ``report-to``. Always answers 204.
        ("POST", "/api/v1/security/csp-report"),
        # ── Credential-bearing: authenticated outside the dependency graph ──
        # Verifies username + password. The credential IS the request body;
        # there is no prior session to present.
        ("POST", "/api/v1/auth/login"),
        # Mints a new access token from the httpOnly refresh cookie. The
        # expired access token is precisely what the caller does not have.
        ("POST", "/api/v1/auth/refresh"),
        # Authenticated by the httpOnly refresh cookie, not a bearer token:
        # Next.js RSC renders hold the cookie and have no Authorization
        # header by construction. Shares the whole validation chain with
        # ``/auth/refresh`` via ``_validate_refresh_cookie`` so the two cannot
        # drift, and never emits ``Set-Cookie``.
        ("POST", "/api/v1/auth/verify"),
        # Must succeed when the access token has already expired, or a user
        # cannot log out of a stale session. The refresh cookie's HMAC
        # signature is verified before any session family is revoked, so an
        # anonymous caller cannot revoke a session they do not hold; with no
        # cookie it is a no-op cookie clear.
        ("POST", "/api/v1/auth/logout"),
        # The single-use reset JWT is the credential. A caller who could
        # already authenticate would not need this route.
        ("POST", "/api/v1/auth/reset-password"),
        # The emailed verification JWT is the credential; the account is not
        # yet usable for login at this point.
        ("POST", "/api/v1/auth/verify-email"),
        # ── The four pre-auth MFA challenge routes. Each is the SECOND leg of
        # a login that has already passed password verification and holds a
        # short-lived ``mfa_token``; no access token exists yet. Listed
        # concretely rather than as an ``mfa/*`` prefix so the authenticated
        # setup/enable/disable/recovery-codes routes stay out.
        ("POST", "/api/v1/auth/mfa/verify"),
        ("POST", "/api/v1/auth/mfa/recovery"),
        ("POST", "/api/v1/auth/mfa/email-code"),
        ("POST", "/api/v1/auth/mfa/email-verify"),
        # Browser redirect back from Google carrying ``code`` + ``state``. No
        # Authorization header exists on a top-level navigation; identity is
        # bound by the state cookie and the provider's verified email.
        ("GET", "/api/v1/auth/google/callback"),
        # Step-up redirect arriving from Google with no Authorization header.
        # Identity is bound by the httpOnly ``oauth_state`` cookie issued at
        # ``/sso-stepup/initiate``, which DOES require authentication, and is
        # re-checked against the Google account's verified email; the return
        # target is an allowlisted key, never a caller-supplied URL.
        ("GET", "/api/v1/auth/sso-stepup/callback"),
        # The invitee has no account yet, so there is no credential to
        # present. Gated by a signed, 7-day, email-bound invitation JWT;
        # every failure mode returns one uniform 410 so the response cannot
        # distinguish "not yours" from "does not exist".
        ("GET", "/api/v1/orgs/invitations/preview"),
        # Creates the account, so it necessarily runs before the caller has
        # one. The signed invitation JWT is the credential: ``org_id`` and
        # ``role`` are read from the locked DB row and never from the request
        # body, the role can never be OWNER, and the token is consumed
        # single-use under ``SELECT ... FOR UPDATE``.
        ("POST", "/api/v1/orgs/invitations/accept"),
        # Mailgun delivery/bounce webhook. Signature-verified against the
        # signing key on every call — not open, just not bearer-authenticated.
        ("POST", "/api/v1/webhooks/mailgun"),
)

# The comparison form. Derived from the literal above and nowhere else — never
# from app state.
_PUBLIC_ROUTE_SET: frozenset[tuple[str, str]] = frozenset(PUBLIC_ROUTES)

# Non-``APIRoute`` entries FastAPI registers itself when the docs are enabled
# (``APP_ENV`` is ``development`` under ``conftest.py``): the OpenAPI schema,
# Swagger UI, and the oauth2-redirect page Swagger auto-registers alongside
# it. Asserted as a SUBSET, not equality, so flipping the docs off does not
# break the guard while anything genuinely new still fails closed.
KNOWN_NON_API_ROUTE_PATHS: frozenset[str] = frozenset(
    {
        "/api/openapi.json",
        "/api/docs",
        "/docs/oauth2-redirect",
    }
)


def _reaches_get_current_user(dependant: Any, get_current_user: Any) -> bool:
    """Does ``get_current_user`` appear anywhere in this dependency tree?

    Identity comparison (``is``), never a name comparison — that is the whole
    point of the mechanism. See the module docstring.
    """
    stack = [dependant]
    while stack:
        node = stack.pop()
        if node.call is get_current_user:
            return True
        stack.extend(node.dependencies)
    return False


def _enumerate() -> dict[str, Any]:
    """Partition the REAL app's routes into authenticated / public.

    Imports live in here, not at module scope: ``app.main`` runs
    ``setup_logging()`` on import. The lifespan is never entered — we only
    read ``app.routes``.
    """
    from fastapi.routing import APIRoute

    from app.deps import get_current_user
    from app.main import app

    api_routes = [r for r in app.routes if isinstance(r, APIRoute)]
    # Partition rather than filter: an ``isinstance`` filter alone would also
    # silently swallow a future ``app.mount()`` or ``WebSocketRoute`` — a hole
    # of exactly the shape this guard exists to close. P5 asserts on these.
    other_routes = [r for r in app.routes if not isinstance(r, APIRoute)]

    public: set[tuple[str, str]] = set()
    authed: set[tuple[str, str]] = set()
    for route in api_routes:
        target = (
            authed
            if _reaches_get_current_user(route.dependant, get_current_user)
            else public
        )
        for method in route.methods:
            target.add((method, route.path))

    return {
        "app": app,
        "api_routes": api_routes,
        "other_routes": other_routes,
        "public": public,
        "authed": authed,
    }


# ── The guard ───────────────────────────────────────────────────────────────


def test_public_route_set_matches_reviewed_allowlist():
    """Two-way equality between the real anonymous surface and the allowlist.

    Both directions are load-bearing. Without the STALE direction the
    allowlist rots into a permanent superset that admits anything.
    """
    found = _enumerate()["public"]

    unprotected = sorted(found - _PUBLIC_ROUTE_SET)
    assert not unprotected, (
        "UNPROTECTED: reachable without authentication and not on the "
        f"reviewed public allowlist: {unprotected}. "
        "Either the route lost its Depends(get_current_user), or it is "
        "intentionally public — in which case it needs a security review, an "
        "entry in PUBLIC_ROUTES with a justification comment, and an entry in "
        "CONTRIBUTING.md's 'Public endpoints' section."
    )

    stale = sorted(_PUBLIC_ROUTE_SET - found)
    assert not stale, (
        "STALE: allowlisted route no longer exists or is now authenticated; "
        f"drop the entry: {stale}. Leaving it in place lets the allowlist "
        "grow into a superset that would bless a future route at the same "
        "method+path."
    )


# ── Positive controls ───────────────────────────────────────────────────────
#
# A guard over a zero-length route list passes trivially, and a detector that
# always returns False passes trivially too. These make both states visible.


def test_p1_enumeration_is_not_empty():
    """P1 — the import actually yielded routes (258 at time of writing)."""
    api_routes = _enumerate()["api_routes"]
    assert len(api_routes) >= 250, (
        f"only {len(api_routes)} APIRoutes enumerated — the app import "
        "probably did not register the routers, which would make every other "
        "assertion in this file vacuous"
    )


def test_p2_known_authenticated_routes_are_detected():
    """P2 — one route per declaration shape must read as authenticated.

    Kills a detector that always returns ``False`` (which would make the
    public set the whole app and the allowlist meaningless).
    """
    result = _enumerate()
    authed = result["authed"]

    # Shape 1: handler signature ``Depends(get_current_user)``.
    assert ("GET", "/api/v1/tags") in authed

    # Shape 2: router-level ``dependencies=[Depends(require_superadmin)]``.
    # ``require_superadmin`` is defined independently in six modules — the
    # exact reason a name roster was rejected.
    admin = {
        (m, p) for (m, p) in authed | result["public"] if p.startswith("/api/v1/admin/")
    }
    assert admin, "no /api/v1/admin/ routes found — control cannot fire"
    assert admin <= authed, (
        f"admin routes not detected as authenticated: {sorted(admin - authed)}"
    )

    # Shape 3: router-level feature gate.
    assert ("GET", "/api/v1/reports") in authed

    # Shape 4: transitively, through a wrapper dependency
    # (``require_interactive_session`` -> ``Depends(get_current_user)``).
    assert ("PUT", "/api/v1/users/me") in authed


def test_p3_auth_me_is_authenticated():
    """P3 — makes the "route lost its auth" mutant attributable.

    ``GET /api/v1/auth/me`` flips with that mutant and only with it.
    """
    assert ("GET", "/api/v1/auth/me") in _enumerate()["authed"]


def test_p4_optional_current_user_does_not_count_as_authentication():
    """P4 — the sharpest control in this file. Do not delete it.

    ``GET /api/v1/auth/status`` declares ``Depends(get_current_user_optional)``
    AND ``Depends(get_db)``, and its optional bearer dependency uses the same
    ``HTTPBearer`` scheme as the real one. So it goes RED if the detector ever
    degrades into matching on:

      * the mere presence of dependencies,
      * the ``HTTPBearer`` security scheme, or
      * a name containing ``current_user``.

    Nothing else in this file catches that class, and every one of those
    degradations is green on unmodified ``main`` without this control.
    """
    assert ("GET", "/api/v1/auth/status") in _enumerate()["public"], (
        "get_current_user_optional was counted as authentication — it returns "
        "None on failure and authenticates nobody. The detector must match "
        "app.deps.get_current_user by object identity."
    )


def test_p5_non_apiroute_entries_are_known():
    """P5 — a future ``app.mount()`` or ``WebSocketRoute`` cannot escape.

    Those carry no ``dependant`` and are invisible to the auth walk, so they
    are asserted against a known set instead of being filtered away.
    """
    other = _enumerate()["other_routes"]
    paths = {getattr(r, "path", repr(r)) for r in other}
    unexpected = sorted(paths - KNOWN_NON_API_ROUTE_PATHS)
    assert not unexpected, (
        f"unrecognised non-APIRoute entries on the app: {unexpected}. These "
        "bypass the dependency-graph auth check entirely; review what they "
        "expose before adding them to KNOWN_NON_API_ROUTE_PATHS."
    )


def test_p6_allowlist_literal_is_the_expected_size():
    """P6 — a silent "regenerated from app state" edit shows up in review.

    The count is asserted separately from the set so a diff that quietly
    reshapes the literal cannot land without touching this number.
    """
    assert len(PUBLIC_ROUTES) == 25, (
        f"PUBLIC_ROUTES holds {len(PUBLIC_ROUTES)} entries, expected 25. If a "
        "route was legitimately added to or removed from the public surface, "
        "update this number, CONTRIBUTING.md, and record the security review."
    )
    # Deduped count must match too: a duplicated (method, path) line would
    # otherwise inflate the literal while the comparison set stayed the same.
    assert len(_PUBLIC_ROUTE_SET) == 25, (
        "PUBLIC_ROUTES contains duplicate entries: "
        f"{sorted({e for e in PUBLIC_ROUTES if PUBLIC_ROUTES.count(e) > 1})}"
    )


def test_p7_real_app_is_under_inspection():
    """P7 — no ``dependency_overrides``, so this is not a test-factory app.

    ``make_test_app`` overrides ``get_current_user``; enumerating such an app
    would prove nothing about production routing.
    """
    app = _enumerate()["app"]
    assert not app.dependency_overrides, (
        "app.main:app carries dependency_overrides — something in the test "
        f"session mutated the real app: {sorted(app.dependency_overrides)}"
    )
