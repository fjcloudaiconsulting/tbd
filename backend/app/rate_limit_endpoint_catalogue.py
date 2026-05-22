"""Catalogue of endpoint patterns that the rate-limit override system
recognises (L4.10).

Why this exists. An override row carries a free-form
``endpoint_pattern`` string. Without a catalogue an operator can save
"transactiosn.list" (typo) or "ai.chat" (route doesn't exist yet) and
the override silently no-ops because no ``@limiter.limit`` decorator
in the codebase resolves against that string. The catalogue is the
single source of truth used to:

- 422-reject unknown patterns at the Pydantic schema layer.
- Populate the admin UI's pattern dropdown so an operator picks from a
  known-good list instead of typing.
- Document each pattern's default static limit and whether it is a
  pre-auth route (see ``PRE_AUTH_PATTERNS``).

How the catalogue maps to the codebase. Each entry corresponds to one
``@limiter.limit(...)`` decorator on a FastAPI route. The pattern
string is ``<router_module>.<short_action>`` — chosen by the route
author, not derived from the function name, so renames stay tracked
here. When you add a new ``@limiter.limit(...)`` decorator, append the
matching pattern below. When you remove one, delete the pattern; an
override row referencing a removed pattern is harmless (it just
no-ops) but the catalogue must be truthful.

Pre-auth limitation (see ``rate_limit_overrides.dynamic_limit``). The
override resolver needs an authenticated identity. Patterns listed in
``PRE_AUTH_PATTERNS`` will accept overrides without error, but the
overrides will NOT take effect because the resolver short-circuits
when no user/org identity can be extracted from the request. Tune
those routes via the static slowapi decorator string instead.
"""
from __future__ import annotations


# Every pattern an operator may attach to an override. Update this
# whenever a ``@limiter.limit(...)`` decorator is added or removed.
# Order is alphabetical for human readability.
RATE_LIMITED_ENDPOINT_PATTERNS: frozenset[str] = frozenset({
    # accounts router
    "accounts.adjust_balance",
    # auth router
    "auth.check_username",
    "auth.forgot_password",
    "auth.login",
    "auth.mfa_email_code",
    "auth.mfa_email_verify",
    "auth.mfa_recovery",
    "auth.mfa_verify",
    "auth.register",
    "auth.resend_verification",
    "auth.resend_verification_public",
    "auth.verify",
    "auth.verify_email",
    # feedback router
    "feedback.submit",
    # onboarding router
    "onboarding.complete",
    "onboarding.restart_tour",
    "onboarding.seed_demo",
    # org_members router
    "org_members.accept_invitation",
    "org_members.preview_invitation",
    # orgs router
    "orgs.rename",
    # reports router
    "reports.query",
    # users router
    "users.change_password",
    "users.update_profile",
})


# Patterns whose decorator site runs BEFORE the request has an
# authenticated identity (no Bearer JWT, or only a cookie / one-time
# token). Overrides on these patterns are accepted by the schema (so
# the catalogue can stay opinion-free) but the resolver always falls
# back to the static default. Operators see the warning in the admin
# UI and the module docstring of ``rate_limit_overrides``.
PRE_AUTH_PATTERNS: frozenset[str] = frozenset({
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


def is_known_pattern(pattern: str) -> bool:
    """Return True iff ``pattern`` is in the catalogue.

    Pure helper so the schema validator stays a one-liner and tests
    can pin the contract without importing the frozenset directly.
    """
    return pattern in RATE_LIMITED_ENDPOINT_PATTERNS


def sorted_patterns() -> list[str]:
    """Catalogue as a sorted list. Used by the GET catalogue endpoint
    so the admin UI dropdown is deterministic across requests.
    """
    return sorted(RATE_LIMITED_ENDPOINT_PATTERNS)
