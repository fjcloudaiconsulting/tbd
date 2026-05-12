"""Snapshot + security tests for L5.6 brand-aligned email templates.

Covers the five customer email helpers in ``app.services.email_service``:

* send_password_reset_email
* send_mfa_email_code
* send_verification_email
* send_invitation_email
* send_trial_expiring_email

Tests assert:

* Brand-mandatory structure: inline chevron SVG, wordmark, brass CTA pill,
  light page background, tagline footer.
* Brand voice: no em-dashes, no emoji, no off-brand phrases ("AI-powered",
  "revolutionize", "effortlessly"), full product name appears.
* Plain-text fallback is provided and carries the link / code.
* Security: HTML-escaping for user-controlled fields (XSS), URL-encoding
  for tokens (no attribute escape), and dev-mode logging redacts the
  rendered body + token.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.services import email_service

# ─── Brand structural constants the templates MUST emit. ───
_CHEVRON_SIGNATURE = 'aria-label="The Better Decision"'
_CHEVRON_BRASS_PATH = "M 14 8 L 23 16 L 14 24"
_WORDMARK_TEXT = "The Better Decision"
_BRASS_HEX = "#D4A64A"
_INK_HEX = "#0B1F3A"
_TAGLINE = "There's no best decision. Only better ones."
_LIGHT_BG = "#f0f2f5"

_OFF_BRAND_PHRASES = (
    "ai-powered",
    "revolutionize",
    "effortlessly",
    "effortless",
    "limited time",
    "don't miss out",
)


# ─── Async send-helper harness ───
# The send_* helpers funnel into ``send_email``; we patch that to capture
# the rendered payload without going near Mailgun.


@pytest.fixture
def captured_send(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``send_email`` with a capture list and return that list."""
    captured: list[dict[str, Any]] = []

    async def _fake_send(
        to: str, subject: str, body_html: str, body_text: str | None = None
    ) -> bool:
        captured.append(
            {
                "to": to,
                "subject": subject,
                "body_html": body_html,
                "body_text": body_text,
            }
        )
        return True

    monkeypatch.setattr(email_service, "send_email", _fake_send)
    return captured


def _assert_brand_chrome(body_html: str) -> None:
    """Every customer email MUST carry the brand chrome."""
    assert _CHEVRON_SIGNATURE in body_html, "missing inline chevron mark"
    assert _CHEVRON_BRASS_PATH in body_html, "missing brass chevron stroke"
    assert _WORDMARK_TEXT in body_html, "missing wordmark"
    assert _BRASS_HEX in body_html, "missing brass accent"
    assert _INK_HEX in body_html, "missing brand ink color"
    assert _LIGHT_BG in body_html, "missing light page background"
    assert _TAGLINE in body_html, "missing locked tagline in footer"


def _assert_brand_voice(*texts: str) -> None:
    """No em-dashes, no off-brand phrases."""
    for t in texts:
        assert "—" not in t and "–" not in t, f"em/en-dash found in: {t[:80]}..."
        lowered = t.lower()
        for phrase in _OFF_BRAND_PHRASES:
            assert phrase not in lowered, f"off-brand phrase {phrase!r} in: {t[:80]}..."


# ─── Snapshot tests ───


@pytest.mark.asyncio
async def test_password_reset_template(captured_send: list[dict[str, Any]]):
    ok = await email_service.send_password_reset_email(
        "user@example.com", "tok_abc123"
    )
    assert ok is True
    assert len(captured_send) == 1
    msg = captured_send[0]

    assert msg["to"] == "user@example.com"
    assert "password" in msg["subject"].lower()
    assert _WORDMARK_TEXT in msg["subject"]

    _assert_brand_chrome(msg["body_html"])
    _assert_brand_voice(msg["subject"], msg["body_html"], msg["body_text"])

    assert "Reset password" in msg["body_html"]
    assert "/reset-password?token=tok_abc123" in msg["body_html"]
    assert "1 hour" in msg["body_html"]

    # Plain-text fallback carries the link and the same expiry hint.
    assert msg["body_text"] is not None
    assert "/reset-password?token=tok_abc123" in msg["body_text"]
    assert "1 hour" in msg["body_text"]


@pytest.mark.asyncio
async def test_mfa_email_code_template(captured_send: list[dict[str, Any]]):
    ok = await email_service.send_mfa_email_code("user@example.com", "482915")
    assert ok is True
    msg = captured_send[0]

    _assert_brand_chrome(msg["body_html"])
    _assert_brand_voice(msg["subject"], msg["body_html"], msg["body_text"])

    # Code rendered prominently in HTML and plain-text.
    assert "482915" in msg["body_html"]
    assert "482915" in msg["body_text"]
    assert "10 minutes" in msg["body_html"]

    # MFA email has no CTA button; the code IS the call-to-action.
    assert "border-radius:999px" not in msg["body_html"]


@pytest.mark.asyncio
async def test_verification_email_template(captured_send: list[dict[str, Any]]):
    ok = await email_service.send_verification_email(
        "user@example.com", "verifytoken123"
    )
    assert ok is True
    msg = captured_send[0]

    _assert_brand_chrome(msg["body_html"])
    _assert_brand_voice(msg["subject"], msg["body_html"], msg["body_text"])

    assert "Confirm email" in msg["body_html"]
    assert "/verify-email?token=verifytoken123" in msg["body_html"]
    assert "/verify-email?token=verifytoken123" in msg["body_text"]


@pytest.mark.asyncio
async def test_invitation_email_template(captured_send: list[dict[str, Any]]):
    ok = await email_service.send_invitation_email(
        "guest@example.com",
        inviter_name="Alice Doe",
        org_name="Doe Household",
        accept_url="http://localhost/accept-invite?token=invtoken",
    )
    assert ok is True
    msg = captured_send[0]

    _assert_brand_chrome(msg["body_html"])
    _assert_brand_voice(msg["subject"], msg["body_html"], msg["body_text"])

    assert "Alice Doe" in msg["body_html"]
    assert "Doe Household" in msg["body_html"]
    assert "Accept invitation" in msg["body_html"]
    assert "7 days" in msg["body_html"]
    assert "accept-invite?token=invtoken" in msg["body_text"]


@pytest.mark.asyncio
async def test_trial_expiring_template_plural(captured_send: list[dict[str, Any]]):
    ok = await email_service.send_trial_expiring_email(
        "user@example.com", days_left=3, org_name="Doe Household"
    )
    assert ok is True
    msg = captured_send[0]

    _assert_brand_chrome(msg["body_html"])
    _assert_brand_voice(msg["subject"], msg["body_html"], msg["body_text"])

    assert "3 days" in msg["subject"]
    assert "Doe Household" in msg["body_html"]
    assert "Keep Pro" in msg["body_html"]
    assert "settings/billing" in msg["body_text"]


@pytest.mark.asyncio
async def test_trial_expiring_template_singular(
    captured_send: list[dict[str, Any]],
):
    await email_service.send_trial_expiring_email(
        "user@example.com", days_left=1, org_name="Doe Household"
    )
    msg = captured_send[0]
    assert "1 day" in msg["subject"]
    assert "1 days" not in msg["subject"]


# ─── Security tests ───


@pytest.mark.asyncio
async def test_invitation_escapes_inviter_and_org_for_xss(
    captured_send: list[dict[str, Any]],
):
    """Attacker-controlled inviter / org names must be HTML-escaped."""
    payload = '<script>alert(1)</script>'
    await email_service.send_invitation_email(
        "guest@example.com",
        inviter_name=payload,
        org_name=f'evil"{payload}',
        accept_url="http://localhost/accept-invite?token=t",
    )
    msg = captured_send[0]
    # Raw script tag must NOT appear inside the HTML body. (The subject is
    # plain-text per Mailgun semantics and is not HTML-rendered.)
    assert "<script>" not in msg["body_html"]
    # Escaped form is present.
    assert "&lt;script&gt;" in msg["body_html"]
    # The attribute-breaking quote in the org name should be escaped too.
    assert 'evil"' not in msg["body_html"]
    assert "evil&quot;" in msg["body_html"] or "evil&#34;" in msg["body_html"]


@pytest.mark.asyncio
async def test_token_is_url_encoded_in_query_string(
    captured_send: list[dict[str, Any]],
):
    """Token characters that could escape the query string are quoted."""
    # Token containing a quote-mark and an ampersand would otherwise let
    # an attacker pivot into another HTML attribute.
    bad_token = 'abc"&xss=1'
    await email_service.send_verification_email("user@example.com", bad_token)
    msg = captured_send[0]
    body = msg["body_html"]
    assert 'abc"&xss=1' not in body
    # quote_plus encodes " as %22, & as %26
    assert "abc%22%26xss%3D1" in body


@pytest.mark.asyncio
async def test_send_email_dev_mode_does_not_log_body_or_token(
    monkeypatch: pytest.MonkeyPatch,
):
    """Dev mode logs ``to`` / ``subject`` only. Body + token must not appear."""
    # Force dev mode.
    monkeypatch.setattr(email_service.settings, "mailgun_api_key", "")

    # Capture every kwarg the email service passes to the structlog logger
    # by replacing the module-level logger with a recorder. This avoids any
    # dependency on global structlog configuration (other tests in the
    # suite reconfigure it freely).
    captured: list[tuple[str, dict[str, Any]]] = []

    class _Recorder:
        async def ainfo(self, event: str, **kw: Any) -> None:
            captured.append((event, kw))

        async def aerror(self, event: str, **kw: Any) -> None:
            captured.append((event, kw))

    monkeypatch.setattr(email_service, "logger", _Recorder())

    ok = await email_service.send_email(
        "user@example.com",
        "Confirm your email for The Better Decision",
        '<a href="http://localhost/verify-email?token=SECRET_TOKEN_123">x</a>',
        "Open http://localhost/verify-email?token=SECRET_TOKEN_123",
    )
    assert ok is True

    dev_events = [(e, kw) for e, kw in captured if e == "email_sent_dev"]
    assert len(dev_events) == 1
    _event, kw = dev_events[0]
    assert kw == {
        "to": "user@example.com",
        "subject": "Confirm your email for The Better Decision",
    }
    # Tokens must never appear anywhere in the captured payload.
    assert "SECRET_TOKEN_123" not in repr(captured)


@pytest.mark.asyncio
async def test_send_email_failure_does_not_log_body(
    monkeypatch: pytest.MonkeyPatch,
):
    """A Mailgun transport failure must not log the rendered body."""
    monkeypatch.setattr(email_service.settings, "mailgun_api_key", "real-key")
    monkeypatch.setattr(email_service.settings, "mailgun_domain", "mg.example.com")
    monkeypatch.setattr(email_service.settings, "mailgun_region", "")

    class _BoomClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_BoomClient":
            return self

        async def __aexit__(self, *exc_info: Any) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("network down")

    monkeypatch.setattr(email_service.httpx, "AsyncClient", _BoomClient)

    captured: list[tuple[str, dict[str, Any]]] = []

    class _Recorder:
        async def ainfo(self, event: str, **kw: Any) -> None:
            captured.append((event, kw))

        async def aerror(self, event: str, **kw: Any) -> None:
            captured.append((event, kw))

    monkeypatch.setattr(email_service, "logger", _Recorder())

    ok = await email_service.send_email(
        "user@example.com",
        "Reset your The Better Decision password",
        '<a href="http://x/reset-password?token=SECRET_RESET">x</a>',
        "http://x/reset-password?token=SECRET_RESET",
    )
    assert ok is False
    fail_events = [(e, kw) for e, kw in captured if e == "email_send_failed"]
    assert len(fail_events) == 1
    _event, kw = fail_events[0]
    # Allowed keys only: to, subject, error. Body fields must be absent.
    assert "body_html" not in kw and "body_text" not in kw
    assert "SECRET_RESET" not in repr(kw)


# ─── Bulk hygiene sweep over every template ───


@pytest.mark.asyncio
async def test_no_offbrand_phrases_anywhere(monkeypatch: pytest.MonkeyPatch):
    """Render every template once with realistic args and sweep all text."""
    captured: list[dict[str, Any]] = []

    async def _fake_send(
        to: str, subject: str, body_html: str, body_text: str | None = None
    ) -> bool:
        captured.append(
            {"subject": subject, "body_html": body_html, "body_text": body_text}
        )
        return True

    monkeypatch.setattr(email_service, "send_email", _fake_send)

    await email_service.send_password_reset_email("u@x.com", "tok")
    await email_service.send_mfa_email_code("u@x.com", "123456")
    await email_service.send_verification_email("u@x.com", "tok")
    await email_service.send_invitation_email(
        "u@x.com",
        inviter_name="Alice",
        org_name="Doe Household",
        accept_url="http://localhost/accept-invite?token=t",
    )
    await email_service.send_trial_expiring_email("u@x.com", 3, "Doe Household")

    assert len(captured) == 5
    for m in captured:
        _assert_brand_voice(m["subject"], m["body_html"], m["body_text"])
        # Strip HTML tags for a soft emoji sweep over the visible text.
        visible = re.sub(r"<[^>]+>", " ", m["body_html"])
        assert not re.search(r"[\U0001F300-\U0001FAFF]", visible), (
            f"emoji found in rendered HTML: {visible[:120]!r}"
        )
