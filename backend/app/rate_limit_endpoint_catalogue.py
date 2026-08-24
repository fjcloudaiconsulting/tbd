"""Catalogue of endpoint patterns that the rate-limit override system
recognises (L4.10).

Why this exists. An override row carries a free-form
``endpoint_pattern`` string. Without a catalogue an operator can save
"transactiosn.list" (typo) or "ai.chat" (route doesn't exist yet) and
the override silently no-ops because no ``@limiter.limit`` decorator
in the codebase resolves against that string. The catalogue is the
single source of truth used to:

- 422-reject unknown / non-overridable patterns at the Pydantic schema
  layer.
- Populate the admin UI's pattern dropdown so an operator picks from a
  known-good list instead of typing.
- Document each pattern's default static limit and whether it is a
  pre-auth route (see ``PRE_AUTH_ENDPOINT_PATTERNS``).

How the catalogue maps to the codebase. Each entry corresponds to one
``@limiter.limit(...)`` decorator on a FastAPI route. The pattern
string is ``<router_module>.<short_action>`` chosen by the route
author (not derived from the function name) so renames stay tracked
here. When you add a new ``@limiter.limit(...)`` decorator, append the
matching pattern below. When you remove one, delete the pattern; an
override row referencing a removed pattern is harmless (it just
no-ops) but the catalogue must be truthful.

That instruction is no longer merely advisory: since TBD-353,
``backend/tests/test_rate_limit_catalogue_drift.py`` AST-walks every
router and fails when a decorator has no pattern, when a pattern has no
live decorator, or when a limit value changes without review. It had
drifted six times before that fence existed.

Two-tier split (architect-locked, 2026-05-22).
``OVERRIDABLE_ENDPOINT_PATTERNS`` lists the patterns where per-org or
per-user overrides ACTUALLY take effect at request time. These are
the only strings the schema layer accepts for create / update.
``PRE_AUTH_ENDPOINT_PATTERNS`` lists patterns whose decorator site
runs BEFORE the request has an authenticated identity (no Bearer JWT,
or only a cookie / one-time token). The override resolver short-
circuits on those routes, so overrides for them would be no-op rows.
They are exposed via the catalogue endpoint so the admin UI can show
the full surface (and explain why those routes are not overridable),
but the schema validator rejects them with a typed 422 to prevent
operators creating no-op config.

To tune a pre-auth route's limit, edit the static slowapi decorator
default in code instead.
"""
from __future__ import annotations


# Patterns where per-org / per-user overrides ACTUALLY take effect at
# request time. These are the only patterns the schema layer accepts
# on create / update. Update this whenever a post-auth
# ``@limiter.limit(...)`` decorator is added or removed. Order is
# alphabetical for human readability.
OVERRIDABLE_ENDPOINT_PATTERNS: frozenset[str] = frozenset({
    # accounts router
    "accounts.adjust_balance",
    # api-tokens router (superadmin PAT mint)
    "api_tokens.mint",
    # auth router (post-auth resend, requires get_current_user)
    "auth.resend_verification",
    # TBD-346. Post-auth: the route is interactive-session gated, so its
    # decorator runs with an identity behind it.
    #
    # ⚠ Listed for TRUTHFULNESS, not tunability. This catalogue does not
    # currently make anything adjustable: ``rate_limit_overrides.dynamic_limit``
    # has no call sites anywhere under ``app/`` and every router decorator is a
    # static string, so every stored override row is a no-op at request time.
    # Do not describe this entry as making the limit adjustable.
    "auth.sso_stepup_initiate",
    # feedback router
    "feedback.submit",
    # org_members router. Backfilled by TBD-353: the decorator has
    # existed without a pattern, so no override for it could be stored.
    "org_members.remove_member",
    # onboarding router
    "onboarding.complete",
    "onboarding.restart_tour",
    "onboarding.seed_demo",
    # orgs router
    "orgs.rename",
    # reports router. ``sankey_query`` is a SECOND decorator that was
    # sharing ``reports.query``'s pattern; backfilled by TBD-353 so the
    # two are separately addressable.
    "reports.query",
    "reports.sankey_query",
    # users router
    "users.cancel_pending_email",
    "users.change_password",
    "users.update_profile",
})


# Patterns whose decorator site runs BEFORE the request has an
# authenticated identity. The override resolver always falls back to
# the static default for these. Exposed via the catalogue endpoint
# (so the admin UI can render an informational list) but NOT accepted
# by the schema validator: overrides for these would create no-op
# rows that confuse operators. Tune via the slowapi decorator in code.
PRE_AUTH_ENDPOINT_PATTERNS: frozenset[str] = frozenset({
    "auth.check_username",
    "auth.forgot_password",
    # TBD-353 added the next five. All five routes are on the closed
    # public-endpoint allowlist and all five were unlimited; three of
    # them (logout and the two OAuth callbacks) wrote an ``audit_events``
    # row on a fully anonymous call.
    "auth.google_callback",
    "auth.google_login",
    "auth.login",
    "auth.logout",
    "auth.mfa_email_code",
    "auth.mfa_email_verify",
    "auth.mfa_recovery",
    "auth.mfa_verify",
    "auth.register",
    "auth.resend_verification_public",
    "auth.reset_password",
    "auth.sso_stepup_callback",
    "auth.verify",
    "auth.verify_email",
    "org_members.accept_invitation",
    "org_members.preview_invitation",
    # Backfilled by TBD-353. These three decorators pre-date this
    # catalogue entry and had none, which made the module's own
    # "single source of truth" claim false and left the admin UI's
    # dropdown implying the routes were unlimited.
    "public_stats.founder_count",
    "security.csp_report",
    "webhooks.mailgun",
})


# Convenience union for code paths that need the full surface (docs,
# audits, drift checks against ``@limiter.limit(...)`` decorators in
# the routers). The schema validator does NOT use this set; it uses
# ``OVERRIDABLE_ENDPOINT_PATTERNS`` exclusively.
ALL_KNOWN_ENDPOINT_PATTERNS: frozenset[str] = (
    OVERRIDABLE_ENDPOINT_PATTERNS | PRE_AUTH_ENDPOINT_PATTERNS
)


def is_overridable_pattern(pattern: str) -> bool:
    """Return True iff ``pattern`` may be persisted as an override.

    The schema validator calls this. Pre-auth patterns and unknown
    typos both return False; the validator distinguishes between
    them by checking ``PRE_AUTH_ENDPOINT_PATTERNS`` membership for a
    more specific error code.
    """
    return pattern in OVERRIDABLE_ENDPOINT_PATTERNS


def sorted_overridable_patterns() -> list[str]:
    """Overridable catalogue as a sorted list. Used by the GET
    catalogue endpoint so the admin UI dropdown is deterministic
    across requests.
    """
    return sorted(OVERRIDABLE_ENDPOINT_PATTERNS)


def sorted_pre_auth_patterns() -> list[str]:
    """Pre-auth catalogue as a sorted list. Surfaced via the
    catalogue endpoint so the admin UI can display the full surface
    even though these patterns are not selectable.
    """
    return sorted(PRE_AUTH_ENDPOINT_PATTERNS)
