"""Notification copy templates (PR2 of AI tier train).

This module centralizes notification ``title`` / ``body`` strings for
events the backend emits. The notification service (`PR1`) deliberately
left these as caller-passed strings; this module adds the first one
(``ai.cap.soft_warning``) and keeps the surface minimal so PR3+ can
grow it without re-shaping the API.

Module shape:

- Each function returns a ``(title, body, link_url)`` tuple. Plain
  Python strings, no Markdown — the notification feed renders the
  body verbatim. ``link_url`` is ``None`` for now because PR2 doesn't
  ship a usage-detail page (admins investigate via the new
  ``/api/v1/admin/ai/usage`` endpoint).
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
