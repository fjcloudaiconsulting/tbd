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
    # auth router (post-auth resend, requires get_current_user)
    "auth.resend_verification",
    # feedback router
    "feedback.submit",
    # onboarding router
    "onboarding.complete",
    "onboarding.restart_tour",
    "onboarding.seed_demo",
    # orgs router
    "orgs.rename",
    # reports router
    "reports.query",
    # users router
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
    "auth.login",
    "auth.mfa_email_code",
    "auth.mfa_email_verify",
    "auth.mfa_recovery",
    "auth.mfa_verify",
    "auth.register",
    "auth.resend_verification_public",
    "auth.verify",
    "auth.verify_email",
    "org_members.accept_invitation",
    "org_members.preview_invitation",
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
