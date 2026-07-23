"""Tests for the CC statement reminder + close notification templates
(CC Statement Alerts V1, Task 6).

These templates produce the exact title/body/link strings the two
scheduler jobs (Tasks 8/9) dispatch. The functions receive a
pre-formatted ``amount_str`` (e.g. ``"1,240.00"``) + ``currency`` code
and render ``f"{amount_str} {currency}"`` -- they do not format money
themselves and must never emit a "$" literal.
"""
from __future__ import annotations

from datetime import date

from app.services import notification_templates as t


def test_reminder_copy():
    title, body, link = t.scheduler_cc_statement_reminder(
        "Amex Gold", date(2026, 8, 1), 2, 42
    )
    assert title == "Amex Gold statement closes soon"
    assert "2026-08-01" in body and "2 day" in body and "—" not in body
    assert link == "/accounts?edit=42"


def test_close_copy_amount_in_app_not_email():
    title, in_app, email, link = t.scheduler_cc_statement_closed(
        "Amex Gold", "1,240.00", "EUR", date(2026, 8, 1), 42
    )
    assert title == "Amex Gold statement closed"
    assert "1,240.00 EUR is due on 2026-08-01" in in_app
    assert "1,240.00" not in email and "Open the app" in email
    assert link == "/accounts?edit=42"


def test_close_zero_due_body():
    title, in_app, email, link = t.scheduler_cc_statement_closed(
        "Amex Gold", None, "EUR", date(2026, 8, 1), 42
    )
    assert "nothing due" in in_app


def test_no_em_dash_anywhere():
    """House copy rule: no em-dashes in any produced string."""
    reminder_strs = t.scheduler_cc_statement_reminder(
        "Amex Gold", date(2026, 8, 1), 2, 42
    )
    closed_amount_strs = t.scheduler_cc_statement_closed(
        "Amex Gold", "1,240.00", "EUR", date(2026, 8, 1), 42
    )
    closed_zero_strs = t.scheduler_cc_statement_closed(
        "Amex Gold", None, "EUR", date(2026, 8, 1), 42
    )
    for s in (*reminder_strs, *closed_amount_strs, *closed_zero_strs):
        if s is not None:
            assert "—" not in s
