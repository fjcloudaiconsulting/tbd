"""Notification copy templates.

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

import datetime
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


# ── Sensitive-op hook templates (5) ───────


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


def user_password_reset() -> tuple[str, str, Optional[str]]:
    """Copy for ``user.password.reset`` (security category).

    Self-target; recipient is the user who just completed the
    forgot-password flow. This is the account-takeover path — the
    highest-value alert of the batch, since a reset without the user's
    knowledge means an attacker holds a valid reset link. Copy names
    the action and tells the reader to act if it wasn't them. Always
    written — security category is force-on and cannot be opted out.
    """
    title = "Your password was reset"
    body = (
        "Your account password was reset using the forgot-password flow. "
        "If this wasn't you, your account may be compromised — reset your "
        "password again and contact support immediately."
    )
    return (title, body, "/settings/security")


def user_mfa_recovery_codes_regenerated() -> tuple[str, str, Optional[str]]:
    """Copy for ``user.mfa.recovery_codes.regenerated`` (security category).

    Self-target. Regenerating recovery codes invalidates every prior
    code, so an actor who did this without the user's knowledge has
    replaced the account's fallback authentication set. Copy encourages
    review if the regeneration was unexpected. Always written — security
    category is force-on.
    """
    title = "Recovery codes regenerated"
    body = (
        "Your MFA recovery codes were regenerated. Your previous codes no "
        "longer work. If this wasn't you, secure your account immediately."
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


def api_token_created(*, name: str, prefix: str) -> tuple[str, str, Optional[str]]:
    """Copy for ``api_token.created`` (security category).

    Self-target, force-on. A superadmin PAT was just minted with the acting
    superadmin as owner. Fires email + in-app so an attacker-minted token is
    immediately visible to the human (SEC-R6a). The prefix identifies which
    token without revealing the secret.
    """
    title = "API token created"
    body = (
        f'A new API token "{name}" ({prefix}...) was created on your account. '
        "If you did not create this, revoke it immediately from the API tokens "
        "page."
    )
    return (title, body, "/system/api-tokens")


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


def user_email_changed_old_address(
    *, new_email: str
) -> tuple[str, str, Optional[str]]:
    """EMAIL copy for ``user.email.changed``, sent to the OLD address.

    This is the critical hijack signal — the old inbox is what a victim
    still controls after a malicious email swap, so the copy names the
    new address and tells the reader to act immediately if the change
    wasn't theirs. The recipient value must be the PRE-mutation snapshot
    captured by the route before the user row is updated.

    Email-only copy: the in-app row keeps using ``user_email_changed``
    (the account holder sees it wherever they log in). Rendering goes
    through ``send_notification_email``, which HTML-escapes title and
    body — interpolating the raw address here is safe.

    Args:
        new_email: the address the account was changed TO.
    """
    title = "Your account email was changed"
    body = (
        f"The login email for your account was changed to {new_email}. "
        "This notice was sent to the previous address on the account. "
        "If this wasn't you, your account may be compromised. Reset "
        "your password immediately and contact support."
    )
    return (title, body, "/settings/security")


def user_email_changed_new_address(
    *, old_email: str
) -> tuple[str, str, Optional[str]]:
    """EMAIL copy for ``user.email.changed``, sent to the NEW address.

    Confirmation counterpart of ``user_email_changed_old_address`` —
    tells the new inbox it is now the login email and names the address
    it replaced. A separate verification email (with the confirm link)
    is issued by the route; this message is the security notice, not
    the verification carrier.

    Args:
        old_email: the address the account was changed FROM.
    """
    title = "This address is now your login email"
    body = (
        f"This address is now the login email for your The Better "
        f"Decision account, replacing {old_email}. A separate "
        "verification email is on its way; confirm it to finish the "
        "change. If this wasn't you, contact support."
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


# ── Second hook-batch templates ─────────


def account_role_changed(
    *, new_role: str
) -> tuple[str, str, Optional[str]]:
    """Copy for ``account.role_changed`` (account category).

    Target is the member whose role changed (NOT the actor). The new
    role is interpolated so the recipient can confirm what they were
    moved to at a glance. Encourages contacting an admin if the change
    was unexpected, since the member cannot self-revert a role change.

    Args:
        new_role: the role the member was changed TO (e.g. ``"admin"``).
    """
    title = "Your role was changed"
    body = (
        f"Your role in the organization was changed to {new_role}. "
        "If this wasn't expected, contact an organization admin."
    )
    return (title, body, None)


def org_renamed(
    *, old_name: str, new_name: str
) -> tuple[str, str, Optional[str]]:
    """Copy for ``org.renamed`` (org_admin category).

    Broadcast to every active org admin (Role.OWNER + Role.ADMIN) of
    the renamed org. Both the old and new names are surfaced so admins
    can confirm the change without bouncing to the audit log.

    Args:
        old_name: the organization's previous name.
        new_name: the organization's new name.
    """
    title = "Your organization was renamed"
    body = (
        f"Your organization was renamed from {old_name} to {new_name}."
    )
    return (title, body, None)


def org_data_reset(
    *, actor_email: str
) -> tuple[str, str, Optional[str]]:
    """Copy for ``org.data_reset`` (org_admin category).

    Broadcast to every active org admin of the org whose data was
    reset. The actor's email is surfaced so admins can attribute the
    (destructive) reset without bouncing to the audit log.

    Args:
        actor_email: email of the owner who triggered the reset.
    """
    title = "Organization data was reset"
    body = (
        f"Your organization's data was reset by {actor_email}. "
        "Accounts, transactions, budgets, and related records were "
        "removed and defaults restored."
    )
    return (title, body, None)


# ── scheduler notification templates ────────────────────────────────


def scheduler_recurring_generated(*, generated: int, settled: int) -> tuple[str, str, Optional[str]]:
    """Copy for scheduler.recurring_generation.success (org_activity)."""
    title = "Recurring transactions generated"
    body = (
        f"We added {generated} scheduled transaction(s) to your ledger "
        f"({settled} already settled). Review them on your transactions page."
    )
    return (title, body, "/transactions")


def scheduler_billing_close_reminder(
    *, close_date: datetime.date, days_until: int
) -> tuple[str, str, Optional[str]]:
    """Copy for scheduler.billing_close.reminder (org_activity)."""
    title = "Your budget period closes soon"
    body = (
        f"Your current budget period closes on {close_date.isoformat()} "
        f"(in {days_until} day(s)). A new period will open automatically."
    )
    return (title, body, "/budgets")


def scheduler_billing_closed(*, new_period_start: datetime.date) -> tuple[str, str, Optional[str]]:
    """Copy for scheduler.billing_close.success (org_activity)."""
    title = "Your budget period closed"
    body = (
        f"Your budget period was closed and a new one started on "
        f"{new_period_start.isoformat()}."
    )
    return (title, body, "/budgets")


# ── CC Statement Alerts V1 templates (Task 6) ───────────────────────


def scheduler_cc_statement_reminder(
    card_name: str, close_date: datetime.date, days_until: int, account_id: int
) -> tuple[str, str, str]:
    """Copy for the CC statement close reminder job (Task 8).

    Fires a few days before a credit card's statement closes, so the
    amount due isn't yet known -- the body only names the close date
    and promises a follow-up notification once the statement closes.

    Args:
        card_name: display name of the credit card account.
        close_date: the date the current statement cycle closes.
        days_until: whole days between "now" and ``close_date``.
        account_id: the credit card account's id, used to deep-link the
            reader to its editor (consistent with the close alert).

    Returns:
        ``(title, body, link_url)``. ``link_url`` deep-links to the
        card's editor on the Accounts page.
    """
    title = f"{card_name} statement closes soon"
    body = (
        f"Your {card_name} statement closes on {close_date.isoformat()} "
        f"(in {days_until} day(s)). We'll send the amount due once it closes."
    )
    return (title, body, f"/accounts?edit={account_id}")


def scheduler_cc_statement_closed(
    card_name: str,
    amount_str: Optional[str],
    currency: str,
    payment_date: datetime.date,
    account_id: int,
) -> tuple[str, str, str, str]:
    """Copy for the CC statement closed job (Task 9).

    Fires once a credit card's statement cycle closes and the carried
    balance (if any) is known. Unlike most templates in this module,
    this one returns SEPARATE in-app and email bodies: the in-app body
    may state the amount due, but the email body never does -- it only
    tells the reader to open the app. ``amount_str`` and ``currency``
    are pre-formatted by the caller (this function does not format
    money); the amount renders as ``f"{amount_str} {currency}"``, never
    a "$" literal.

    Args:
        card_name: display name of the credit card account.
        amount_str: pre-formatted carried-balance amount (e.g.
            ``"1,240.00"``), or ``None`` when nothing is due.
        currency: ISO currency code paired with ``amount_str``.
        payment_date: the date the carried balance is due.
        account_id: the credit card account's id, used to deep-link
            the reader to its editor.

    Returns:
        ``(title, in_app_body, email_body, link_url)``.
    """
    title = f"{card_name} statement closed"
    if amount_str is None:
        in_app_body = f"Your {card_name} statement closed with nothing due."
    else:
        in_app_body = (
            f"Your {card_name} statement closed. {amount_str} {currency} "
            f"is due on {payment_date.isoformat()}."
        )
    email_body = f"Your {card_name} statement closed. Open the app to see what's due."
    link = f"/accounts?edit={account_id}"
    return (title, in_app_body, email_body, link)
