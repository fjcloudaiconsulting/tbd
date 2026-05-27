"""Pins the L4.4 audit-event taxonomy seeded in ``audit_event.__doc__``.

This is the substrate-PR (PR 1 of the L4.4 admin-slices train) safety
net: the nine new event-type strings are documented in the model's
module docstring. A future careless edit that drops the contract while
the emitting routers (PRs 2-5) reference these strings would break this
test and force a conscious update.

Spec: ``specs/2026-05-22-l4-4-admin-slices.md`` §8.
"""
from __future__ import annotations

import app.models.audit_event as audit_event_module


# The nine new event-type strings PR 2-5 will emit. Listed verbatim
# (no f-strings, no concatenation) so a grep on any one of these lands
# both here and at the docstring.
L4_4_NEW_AUDIT_EVENT_TYPES = (
    "admin.platform_admin.invitation.sent",
    "admin.platform_admin.invitation.revoked",
    "admin.platform_admin.invitation.accepted",
    "admin.user.password_reset.triggered",
    "admin.user.email_change.triggered",
    "admin.user.mfa_disabled",
    "admin.impersonation.entered",
    "admin.impersonation.exited",
    "admin.impersonation.revoked",
)


def test_module_docstring_documents_each_l4_4_event_type() -> None:
    doc = audit_event_module.__doc__ or ""

    for event_type in L4_4_NEW_AUDIT_EVENT_TYPES:
        assert event_type in doc, (
            f"L4.4 event type {event_type!r} is missing from "
            "audit_event.py's module docstring. The taxonomy seed "
            "is the substrate contract for PRs 2-5 of the train; "
            "see specs/2026-05-22-l4-4-admin-slices.md §8."
        )


def test_via_platform_admin_flag_documented() -> None:
    # The org-admin invite reuses the existing org.invitation.sent /
    # org.invitation.accepted event types; the L4.4 spec adds a
    # detail.via_platform_admin flag instead of a new event type.
    # PR 2 must hook this in; the seed records it in the docstring.
    doc = audit_event_module.__doc__ or ""

    assert "via_platform_admin" in doc
    assert "org.invitation.sent" in doc
    assert "org.invitation.accepted" in doc
