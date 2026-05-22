"""Notification copy templates (PR2 of AI tier train + PR3 of notif train).

This module centralizes notification ``title`` / ``body`` strings for
events the backend emits. The notification service (`PR1`) deliberately
left these as caller-passed strings; this module pins them in one
place so:

- All English copy lives in one file. Reviewing copy doesn't mean
  hunting through five router files.
- A future i18n migration is a mechanical refactor — every call site
  goes through a function in this module, so flipping these to
  ``(locale, ...) -> (title, body)`` is the only change.

Module shape:

- Each function returns a ``(title, body, link_url)`` tuple. Plain
  Python strings, no Markdown — the notification feed renders the
  body verbatim. ``link_url`` is set per the parent spec's
  "Categories — what fires what" table, or ``None`` when there is no
  destination screen.
"""
from __future__ import annotations

from typing import Optional


def ai_cap_soft_warning(
    *, feature_key: str, period: str, percent: int
) -> tuple[str, str, Optional[str]]:
    """Copy for the first-time soft-cap crossing in a billing period.

    Args:
        feature_key: e.g. ``"chat"`` or ``"categorize_transactions"``.
        period: ``YYYY-MM`` string for the calendar month the cap
            applies to.
        percent: how much of the soft cap has been consumed at the
            moment of the warning. Rounded to a whole integer.

    Returns:
        ``(title, body, link_url)``. ``link_url`` is None — admins
        investigate via the superadmin ``/admin/ai/usage`` debug
        endpoint until PR3+ ships a customer-facing usage page.
    """
    title = "AI spend approaching cap"
    body = (
        f"{feature_key} usage in {period} reached {percent}% "
        "of the soft cap."
    )
    return (title, body, None)


# ── PR3 of notification train: 5 sensitive-op hook templates ───────


def user_password_changed() -> tuple[str, str, Optional[str]]:
    """Copy for ``user.password.changed`` (security category).

    Self-target; recipient is the user whose password was rotated
    (or first-set, for SSO-only accounts). Always written — security
    category is force-on and cannot be opted out via the in-app
    preferences toggle.
    """
    title = "Your password was changed"
    body = (
        "Your account password was updated. "
        "If this wasn't you, secure your account immediately."
    )
    return (title, body, "/settings/security")


def user_mfa_enabled() -> tuple[str, str, Optional[str]]:
    """Copy for ``user.mfa.enabled`` (security category).

    Self-target. Confirms the user (or a session acting as them) just
    flipped MFA on. Quiet positive signal, paired with mfa_disabled
    as the louder counterpart.
    """
    title = "Two-factor authentication enabled"
    body = "MFA is now active on your account."
    return (title, body, "/settings/security")


def user_mfa_disabled() -> tuple[str, str, Optional[str]]:
    """Copy for ``user.mfa.disabled`` (security category).

    Self-target. MFA removal is the louder of the two MFA signals —
    the user (or admin-on-behalf path, when that exists) just removed
    a strong defense, and an unauthorized actor with the password
    alone now has fewer obstacles. Copy encourages re-enable rather
    than just "review your account."
    """
    title = "Two-factor authentication disabled"
    body = (
        "MFA was removed from your account. "
        "We recommend re-enabling it for stronger protection."
    )
    return (title, body, "/settings/security")


def user_email_changed(*, new_email: str) -> tuple[str, str, Optional[str]]:
    """Copy for ``user.email.changed`` (security category).

    Self-target. The notification row is dispatched to the user whose
    email just changed — i.e. the actor in the audit convention. The
    OLD email lives in the audit row's ``actor_email`` field; the
    NEW email is interpolated into the body so the recipient can
    confirm the change at a glance.

    Args:
        new_email: the address the account was changed TO.
    """
    title = "Your account email was changed"
    body = (
        f"Your account email was updated to {new_email}. "
        "Reply to support if this wasn't you."
    )
    return (title, body, "/settings/security")


def admin_org_plan_changed(
    *, new_plan_name: str, actor_email: str
) -> tuple[str, str, Optional[str]]:
    """Copy for ``admin.org.plan.changed`` (org_admin category).

    Broadcast — sent to every active org_admin (Role.OWNER + Role.ADMIN)
    of the affected org, NOT just the actor. The actor's email is
    surfaced in the body so admins can attribute the change without
    bouncing to the audit log.

    Args:
        new_plan_name: the plan the org was moved TO.
        actor_email: email of the operator who triggered the change.
    """
    title = f"Plan changed to {new_plan_name}"
    body = (
        f"Your organization's plan was updated by {actor_email}."
    )
    return (title, body, "/admin/organizations")
