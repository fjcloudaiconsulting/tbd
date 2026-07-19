"""Tests for the Mailgun batch-sending primitives (spec
``2026-07-18-admin-email-broadcast-design.md``, "Batch-sending revision
(2026-07-19)" + its LOCKED architect rulings R1-R5 / MA1-MA7).

This task builds PRIMITIVES only (send_batch, token render, recipient-vars);
the drain rewrite that wires them into the send loop is a separate task.

Covers:
- ``email_service.send_batch`` (MA4/MA5): dev mode returns True without
  calling httpx and logs ONLY ``count``/``subject`` (no address, no vars,
  no body); prod mode POSTs ``to`` as a repeated form field and
  ``recipient-variables`` as a JSON string, returns True on 2xx / False on
  a raised non-2xx status, and logs the same PII-bounded fields on both the
  success and failure paths.
- ``broadcast_service.build_batch_bodies`` (MA1): HTML carries
  ``%recipient.first_name_html%`` (never the literal ``{first_name}``);
  text carries ``%recipient.first_name_text%``; a stray ``%`` or an
  unrecognized ``%recipient.X%`` raises ``ValueError``; the real apology
  body (no ``%``) does not raise.
- ``broadcast_service.build_recipient_variables`` (MA2): keyed by the exact
  input email; null ``first_name`` falls back to "there" in both fields;
  an HTML-hazard name is escaped in ``first_name_html`` but raw in
  ``first_name_text``.
- MA3 parity: simulating Mailgun's substitution on ``build_batch_bodies``'
  output must equal ``render_email``'s html/text for the same name, across
  a normal name, ``None``, and an HTML-hazard name — proving dry-run
  (``render_email``) and the real batch payload produce byte-identical
  customer-facing output.

``send_batch`` is exercised via ``httpx.MockTransport`` (same pattern as
``tests/services/test_ai_adapters_chat.py``) so no real Mailgun call is
ever made. Dev-mode logging assertions reuse the
``caplog``-via-``setup_logging`` pattern from
``tests/services/test_notification_service.py``.
"""
from __future__ import annotations

import ast
import json
import logging
from urllib.parse import parse_qs

import httpx
import pytest

from app.services import broadcast_service, email_service
from app.services.broadcast_service import (
    build_batch_bodies,
    build_recipient_variables,
    render_email,
)


# ─── structlog → caplog plumbing (mirrors test_notification_service.py) ───


@pytest.fixture
def _structlog_via_stdlib():
    """Route structlog events through stdlib so ``caplog`` can see them.

    Without this, structlog falls back to its default ``PrintLogger`` in a
    bare unit-test process and ``caplog`` sees nothing (see the identical
    fixture + rationale in ``tests/services/test_notification_service.py``).
    """
    import structlog

    from app.logging import setup_logging

    original_config = structlog.get_config() if structlog.is_configured() else None
    setup_logging()
    yield
    if original_config is not None:
        structlog.configure(**original_config)


def _collect_structlog_events(caplog) -> list[dict]:
    """Normalize captured log records into structlog event dicts."""
    events: list[dict] = []
    for rec in caplog.records:
        candidate = rec.msg
        if isinstance(candidate, tuple) and candidate:
            candidate = candidate[0]
        if isinstance(candidate, dict):
            events.append(candidate)
            continue
        message = rec.getMessage()
        for parser in (json.loads, ast.literal_eval):
            try:
                payload = parser(message)
            except (ValueError, SyntaxError, TypeError):
                continue
            if isinstance(payload, dict):
                events.append(payload)
                break
    return events


# ─── httpx.MockTransport harness (mirrors test_ai_adapters_chat.py) ───



# Captured once at import time, BEFORE any test monkeypatches
# ``httpx.AsyncClient.__init__`` — a test that calls ``_install_transport``
# more than once (e.g. success-then-failure within one test) must always
# wrap the REAL init, not whatever the previous call's patched version was,
# else the second call's transport never takes effect (the first handler
# stays wired in).
_REAL_ASYNC_CLIENT_INIT = httpx.AsyncClient.__init__


def _install_transport(monkeypatch, handler) -> list[httpx.Request]:
    """Install an httpx.MockTransport on AsyncClient and capture requests."""
    captured: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_wrapped)

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        _REAL_ASYNC_CLIENT_INIT(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    return captured


def _fail_if_called(_request: httpx.Request) -> httpx.Response:
    raise AssertionError("httpx must not be called in dev mode")


# ─── send_batch ───


@pytest.mark.asyncio
async def test_send_batch_dev_mode_returns_true_without_httpx(monkeypatch):
    monkeypatch.setattr(email_service.settings, "mailgun_api_key", "")
    captured = _install_transport(monkeypatch, _fail_if_called)

    result = await email_service.send_batch(
        ["alice@x.io", "bob@x.io"],
        "Subject line",
        "<p>html</p>",
        "text",
        {"alice@x.io": {"first_name_html": "Alice", "first_name_text": "Alice"}},
    )

    assert result is True
    assert captured == []


@pytest.mark.asyncio
async def test_send_batch_dev_mode_logs_count_and_subject_only(
    monkeypatch, caplog, _structlog_via_stdlib
):
    monkeypatch.setattr(email_service.settings, "mailgun_api_key", "")
    to_list = ["alice@x.io", "bob@x.io", "carol@x.io"]

    with caplog.at_level(logging.INFO, logger="app.services.email_service"):
        await email_service.send_batch(
            to_list,
            "The Better Decision is back up",
            "<p>html</p>",
            "text",
            {addr: {"first_name_html": "there", "first_name_text": "there"} for addr in to_list},
        )

    events = [e for e in _collect_structlog_events(caplog) if e.get("event") == "broadcast_batch_sent"]
    assert len(events) == 1
    event = events[0]
    assert event["count"] == 3
    assert event["subject"] == "The Better Decision is back up"

    # No recipient PII anywhere in the captured log output (MA5).
    full_log_text = caplog.text
    for addr in to_list:
        assert addr not in full_log_text
    assert "first_name_html" not in full_log_text
    assert "first_name_text" not in full_log_text


@pytest.mark.asyncio
async def test_send_batch_prod_mode_request_shape(monkeypatch):
    monkeypatch.setattr(email_service.settings, "mailgun_api_key", "key-123")
    monkeypatch.setattr(email_service.settings, "mailgun_domain", "mg.example.com")
    monkeypatch.setattr(email_service.settings, "mailgun_region", "eu")
    monkeypatch.setattr(
        email_service.settings, "email_from", "The Better Decision <noreply@example.com>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.eu.mailgun.net/v3/mg.example.com/messages"
        form = parse_qs(request.content.decode("utf-8"))
        assert form["to"] == ["alice@x.io", "bob@x.io"]
        assert form["subject"] == ["Subject line"]
        assert form["from"] == ["The Better Decision <noreply@example.com>"]
        recipient_vars = json.loads(form["recipient-variables"][0])
        assert recipient_vars == {
            "alice@x.io": {"first_name_html": "Alice", "first_name_text": "Alice"},
            "bob@x.io": {"first_name_html": "there", "first_name_text": "there"},
        }
        return httpx.Response(200, json={"id": "abc", "message": "Queued"})

    captured = _install_transport(monkeypatch, handler)

    result = await email_service.send_batch(
        ["alice@x.io", "bob@x.io"],
        "Subject line",
        "<p>html</p>",
        "text",
        {
            "alice@x.io": {"first_name_html": "Alice", "first_name_text": "Alice"},
            "bob@x.io": {"first_name_html": "there", "first_name_text": "there"},
        },
    )

    assert result is True
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_send_batch_prod_mode_non_2xx_returns_false(monkeypatch):
    monkeypatch.setattr(email_service.settings, "mailgun_api_key", "key-123")
    monkeypatch.setattr(email_service.settings, "mailgun_domain", "mg.example.com")
    monkeypatch.setattr(email_service.settings, "mailgun_region", "us")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    _install_transport(monkeypatch, handler)

    result = await email_service.send_batch(
        ["alice@x.io"],
        "Subject",
        "<p>html</p>",
        "text",
        {"alice@x.io": {"first_name_html": "there", "first_name_text": "there"}},
    )

    assert result is False


@pytest.mark.asyncio
async def test_send_batch_prod_mode_logs_status_on_success_and_error_on_failure(
    monkeypatch, caplog, _structlog_via_stdlib
):
    monkeypatch.setattr(email_service.settings, "mailgun_api_key", "key-123")
    monkeypatch.setattr(email_service.settings, "mailgun_domain", "mg.example.com")
    monkeypatch.setattr(email_service.settings, "mailgun_region", "us")

    def ok_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "abc"})

    _install_transport(monkeypatch, ok_handler)
    with caplog.at_level(logging.INFO, logger="app.services.email_service"):
        await email_service.send_batch(
            ["alice@x.io"],
            "Subject",
            "<p>html</p>",
            "text",
            {"alice@x.io": {"first_name_html": "there", "first_name_text": "there"}},
        )
    ok_events = [e for e in _collect_structlog_events(caplog) if e.get("event") == "broadcast_batch_sent"]
    assert len(ok_events) == 1
    assert ok_events[0]["status"] == 200

    caplog.clear()

    def fail_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    _install_transport(monkeypatch, fail_handler)
    with caplog.at_level(logging.INFO, logger="app.services.email_service"):
        await email_service.send_batch(
            ["alice@x.io"],
            "Subject",
            "<p>html</p>",
            "text",
            {"alice@x.io": {"first_name_html": "there", "first_name_text": "there"}},
        )
    fail_events = [e for e in _collect_structlog_events(caplog) if e.get("event") == "broadcast_batch_failed"]
    assert len(fail_events) == 1
    assert "error" in fail_events[0]
    assert "alice@x.io" not in caplog.text


# ─── build_batch_bodies ───


def test_build_batch_bodies_html_has_html_token_not_literal_placeholder():
    html_out, _text_out = build_batch_bodies("Hi {first_name}, welcome")

    assert "%recipient.first_name_html%" in html_out
    assert "{first_name}" not in html_out


def test_build_batch_bodies_text_has_text_token():
    _html_out, text_out = build_batch_bodies("Hi {first_name}, welcome")

    assert "%recipient.first_name_text%" in text_out
    assert "{first_name}" not in text_out


def test_build_batch_bodies_stray_percent_raises():
    with pytest.raises(ValueError):
        build_batch_bodies("Enjoy 50% off, {first_name}")


def test_build_batch_bodies_unexpected_recipient_token_raises():
    with pytest.raises(ValueError):
        build_batch_bodies("Hi {first_name}, %recipient.unknown_field%")


def test_build_batch_bodies_normal_apology_body_does_not_raise():
    body_template = (
        "Hi {first_name},\n\n"
        "On Friday, July 17, a DNS change on our side made The Better "
        "Decision unreachable. The outage lasted more than 12 hours, and "
        "it was fully resolved in the early hours of Saturday, July 18. "
        "That was our mistake, and I'm sorry for the disruption and for "
        "how long it took to fix.\n\n"
        "Your account and your data were never at risk. This was a "
        "connectivity problem, so nothing in your account was touched or "
        "lost.\n\n"
        "Everything is back to normal now. If anything still looks off to "
        "you, just reply to this email and I will look into it right "
        "away.\n\n"
        "Thank you for your patience, and for trusting us with something "
        "as personal as your money.\n\n"
        "Warmly,\nThe Better Decision"
    )

    html_out, text_out = build_batch_bodies(body_template)

    assert "%recipient.first_name_html%" in html_out
    assert "%recipient.first_name_text%" in text_out


# ─── build_recipient_variables ───


def test_build_recipient_variables_keys_match_input_emails():
    recipients = [("alice@x.io", "Alice"), ("bob@x.io", "Bob")]

    variables = build_recipient_variables(recipients)

    assert set(variables.keys()) == {"alice@x.io", "bob@x.io"}


def test_build_recipient_variables_null_first_name_falls_back_to_there():
    variables = build_recipient_variables([("carol@x.io", None)])

    assert variables["carol@x.io"] == {
        "first_name_html": "there",
        "first_name_text": "there",
    }


def test_build_recipient_variables_escapes_html_only():
    variables = build_recipient_variables([("dave@x.io", "<x>")])

    assert variables["dave@x.io"]["first_name_html"] == "&lt;x&gt;"
    assert variables["dave@x.io"]["first_name_text"] == "<x>"


class _RecipientRow:
    """Stand-in for an ``EmailBroadcastRecipient`` row (object shape, not
    a tuple), exercising the other accepted input form."""

    def __init__(self, email: str, first_name: str | None) -> None:
        self.email = email
        self.first_name = first_name


def test_build_recipient_variables_accepts_object_shape():
    variables = build_recipient_variables(
        [_RecipientRow("erin@x.io", "Erin"), _RecipientRow("frank@x.io", None)]
    )

    assert variables["erin@x.io"]["first_name_html"] == "Erin"
    assert variables["frank@x.io"]["first_name_text"] == "there"


# ─── MA3: dry-run (render_email) vs batch-output parity ───


def _simulate_mailgun_substitution(html_tokenized: str, text_tokenized: str, name: str | None) -> tuple[str, str]:
    """Simulate what Mailgun does to ``build_batch_bodies``' output for one
    recipient: substitute the two known tokens with the SAME values
    ``build_recipient_variables`` would have put in the vars map."""
    resolved = name or "there"
    html_out = html_tokenized.replace(
        "%recipient.first_name_html%", __import__("html").escape(resolved)
    )
    text_out = text_tokenized.replace("%recipient.first_name_text%", resolved)
    return html_out, text_out


@pytest.mark.parametrize("name", ["Alice", None, "A<b>&"])
def test_ma3_batch_output_matches_render_email_after_substitution(name):
    body_template = "Hi {first_name}, thanks for your patience."

    html_tokenized, text_tokenized = build_batch_bodies(body_template)
    batch_html, batch_text = _simulate_mailgun_substitution(
        html_tokenized, text_tokenized, name
    )

    dry_run_html, dry_run_text = render_email(body_template, name)

    assert batch_html == dry_run_html
    assert batch_text == dry_run_text
