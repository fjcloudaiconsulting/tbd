"""Email service. Sends brand-aligned customer emails via Mailgun.

Templates here are L5.6 brand polish (see ``BRAND.md`` at repo root):

* Inline chevron mark (no remote SVG load, many clients strip it).
* "The Better Decision" wordmark as the visible product name.
* Brass pill CTA on a light-styled palette (email clients are inconsistent
  with ``prefers-color-scheme``, so we render light only).
* Inline styles only, most clients strip ``<style>`` blocks.
* No em-dashes (locked customer-copy policy).
* No emoji, no "AI-powered", no "revolutionize your finances" framing.

Security stance (audited L5.6):

* User-controlled strings (recipient name, org name, inviter name) are
  routed through :func:`html.escape` before HTML interpolation.
* URL params carrying tokens go through :func:`urllib.parse.quote_plus`
  so an attacker-controlled token shape cannot break out of the query
  string.
* Dev-mode logging redacts the rendered body and the bare token. We log
  ``to`` and ``subject`` only; the rendered HTML and plain-text bodies
  are NOT logged because they carry the reset/verify link with the raw
  token in plain view.
* The Mailgun sender identity / DKIM is unchanged.
"""

from __future__ import annotations

import html
import urllib.parse

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger()


# ─── Brand surface constants (mirrors frontend/lib/brand.ts) ───
# Email clients can't import a TS module, so we hold the canonical values
# as hex literals here. If ``frontend/lib/brand.ts`` changes, update this
# block too. The constants are deliberately scoped to the brand surface
# (not the app theme tokens) because email rendering has no theme.
_BRAND_INK = "#0B1F3A"              # navy ground, primary text on light
_BRAND_BRASS = "#D4A64A"            # primary CTA fill
_BRAND_SLATE = "#5a6a82"            # muted text / mark echo
_LIGHT_PAGE_BG = "#f0f2f5"          # mirrors --color-bg (light)
_LIGHT_SURFACE = "#ffffff"          # mirrors --color-surface (light)
_LIGHT_RULE = "#e5e7eb"             # hairline rule on light surface

# Inline chevron mark, copied verbatim from frontend/app/icon.svg so the
# email surface stays in lockstep with the favicon and the React Logo
# component. Sized to 40px for the email header.
_CHEVRON_MARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" '
    'viewBox="0 0 32 32" role="img" aria-label="The Better Decision" '
    'style="display:inline-block;vertical-align:middle;">'
    '<rect width="32" height="32" rx="7" fill="#0B1F3A"/>'
    '<path d="M 9 8 L 18 16 L 9 24" fill="none" stroke="#5a6a82" '
    'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" '
    'opacity="0.55"/>'
    '<path d="M 14 8 L 23 16 L 14 24" fill="none" stroke="#D4A64A" '
    'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)


def _render_html(
    *,
    heading: str,
    paragraphs: list[str],
    cta_label: str | None = None,
    cta_url: str | None = None,
    footnote: str | None = None,
) -> str:
    """Render a branded HTML email body.

    ``paragraphs`` strings are inserted as-is; callers are responsible for
    HTML-escaping any user-controlled substring before passing it in.
    ``cta_url`` is inserted into an ``href`` attribute and MUST be a safe
    same-origin URL (we build it ourselves from ``settings.app_url`` plus
    a URL-encoded token).
    """
    paragraph_html = "".join(
        f'<p style="margin:0 0 16px 0;color:{_BRAND_INK};'
        f'font-size:15px;line-height:1.55;">{para}</p>'
        for para in paragraphs
    )

    cta_html = ""
    if cta_label and cta_url:
        # Escape the label (display text) defensively. We control the URL
        # because we constructed it ourselves with quote_plus.
        cta_label_safe = html.escape(cta_label)
        cta_html = (
            '<p style="margin:24px 0 0 0;">'
            f'<a href="{cta_url}" '
            f'style="background:{_BRAND_BRASS};color:{_BRAND_INK};'
            'text-decoration:none;display:inline-block;padding:12px 28px;'
            'border-radius:999px;font-weight:600;font-size:15px;'
            'letter-spacing:0.01em;">'
            f"{cta_label_safe}</a></p>"
        )

    footnote_html = ""
    if footnote:
        footnote_html = (
            f'<p style="margin:24px 0 0 0;color:{_BRAND_SLATE};'
            'font-size:13px;line-height:1.5;">'
            f"{footnote}</p>"
        )

    heading_safe = html.escape(heading)

    return (
        "<!doctype html>"
        '<html><body style="margin:0;padding:0;'
        f'background:{_LIGHT_PAGE_BG};'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
        'Roboto,Helvetica,Arial,sans-serif;">'
        '<table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0" style="background:{_LIGHT_PAGE_BG};padding:32px 16px;">'
        '<tr><td align="center">'
        '<table role="presentation" width="560" cellpadding="0" '
        f'cellspacing="0" style="max-width:560px;background:{_LIGHT_SURFACE};'
        'border-radius:12px;padding:32px;">'
        # Header: chevron + wordmark
        '<tr><td style="padding-bottom:24px;">'
        f"{_CHEVRON_MARK_SVG}"
        '<span style="display:inline-block;vertical-align:middle;'
        f'margin-left:10px;font-size:17px;font-weight:600;color:{_BRAND_INK};'
        'letter-spacing:-0.01em;">The Better Decision</span>'
        "</td></tr>"
        # Heading
        '<tr><td style="padding-bottom:8px;">'
        f'<h1 style="margin:0;color:{_BRAND_INK};font-size:22px;'
        f'font-weight:600;line-height:1.3;">{heading_safe}</h1>'
        "</td></tr>"
        # Body
        f"<tr><td>{paragraph_html}{cta_html}{footnote_html}</td></tr>"
        # Footer
        '<tr><td style="padding-top:24px;">'
        f'<div style="border-top:1px solid {_LIGHT_RULE};'
        'padding-top:16px;"></div>'
        f'<p style="margin:0;color:{_BRAND_SLATE};font-size:12px;'
        'line-height:1.5;">'
        "The Better Decision. There's no best decision. Only better ones."
        "</p>"
        "</td></tr>"
        "</table></td></tr></table>"
        "</body></html>"
    )


def _safe_link(path: str, token: str) -> str:
    """Build a same-origin URL with a URL-encoded token query param.

    ``path`` is a developer-supplied static path (e.g. ``/verify-email``);
    ``token`` is the raw token text from the issuer. We pass it through
    ``quote_plus`` so unexpected characters can't break out of the query
    string into another attribute.
    """
    safe_token = urllib.parse.quote_plus(token)
    return f"{settings.app_url}{path}?token={safe_token}"


async def send_email(
    to: str,
    subject: str,
    body_html: str,
    body_text: str | None = None,
) -> bool:
    """Send an email. Returns True if sent/logged successfully.

    Dev mode (no ``mailgun_api_key``): we log ``to`` and ``subject`` only.
    Rendered HTML and plain-text bodies are NOT logged because verification
    and reset emails carry the raw token in the link. To inspect rendered
    HTML during local work, call the send helpers from a Python REPL
    inside the backend container.
    """
    if not settings.mailgun_api_key:
        await logger.ainfo("email_sent_dev", to=to, subject=subject)
        return True

    # Production: send via Mailgun HTTP API.
    api_host = (
        "api.eu.mailgun.net"
        if settings.mailgun_region.lower().strip() == "eu"
        else "api.mailgun.net"
    )
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.post(
                f"https://{api_host}/v3/{settings.mailgun_domain}/messages",
                auth=("api", settings.mailgun_api_key),
                data={
                    "from": settings.email_from,
                    "to": [to],
                    "subject": subject,
                    "html": body_html,
                    **({"text": body_text} if body_text else {}),
                },
            )
            response.raise_for_status()
            await logger.ainfo(
                "email_sent", to=to, subject=subject, status=response.status_code
            )
            return True
    except Exception as exc:
        # Never log the body, it carries the token. ``str(exc)`` from httpx
        # surfaces the response status / reason but not our payload.
        await logger.aerror(
            "email_send_failed", to=to, subject=subject, error=str(exc)
        )
        return False


async def send_password_reset_email(to: str, token: str) -> bool:
    """Send a password reset email with a link containing the reset token."""
    reset_url = _safe_link("/reset-password", token)
    subject = "Reset your The Better Decision password"
    body_html = _render_html(
        heading="Reset your password",
        paragraphs=[
            "Someone (you, we hope) asked to reset the password on this "
            "account. Use the button below to choose a new one.",
        ],
        cta_label="Reset password",
        cta_url=reset_url,
        footnote=(
            "This link expires in 1 hour. If you didn't request a reset, "
            "you can ignore this email and nothing will change."
        ),
    )
    body_text = (
        "Reset your password\n\n"
        "Someone asked to reset the password on this account. Open this "
        "link in your browser to choose a new one:\n\n"
        f"{reset_url}\n\n"
        "This link expires in 1 hour. If you didn't request a reset, you "
        "can ignore this email."
    )
    return await send_email(to, subject, body_html, body_text)


async def send_mfa_email_code(to: str, code: str) -> bool:
    """Send a one-time MFA verification code via email."""
    subject = "Your The Better Decision sign-in code"
    # Code is a short numeric string we generate. Escape defensively in
    # case the generator format ever changes.
    code_safe = html.escape(code)
    code_block = (
        '<span style="display:inline-block;margin-top:8px;'
        "font-family:'SFMono-Regular',Menlo,Consolas,monospace;"
        f"font-size:30px;font-weight:600;letter-spacing:0.18em;"
        f"color:{_BRAND_INK};background:{_LIGHT_PAGE_BG};"
        f'border-radius:8px;padding:14px 22px;">{code_safe}</span>'
    )
    body_html = _render_html(
        heading="Your sign-in code",
        paragraphs=[
            "Use this code to finish signing in. It works once and expires "
            "in 10 minutes.",
            code_block,
        ],
        footnote=(
            "If you didn't try to sign in, you can ignore this email. "
            "Your account stays as it was."
        ),
    )
    body_text = (
        "Your sign-in code\n\n"
        f"{code}\n\n"
        "This code expires in 10 minutes. If you didn't try to sign in, "
        "you can ignore this email."
    )
    return await send_email(to, subject, body_html, body_text)


async def send_verification_email(to: str, token: str) -> bool:
    """Send an email verification link."""
    verify_url = _safe_link("/verify-email", token)
    subject = "Confirm your email for The Better Decision"
    body_html = _render_html(
        heading="Confirm your email",
        paragraphs=[
            "Welcome. Confirm this email address so we know the account is "
            "yours, and so password resets and invitations reach you.",
        ],
        cta_label="Confirm email",
        cta_url=verify_url,
        footnote=(
            "If you didn't create an account, you can ignore this email."
        ),
    )
    body_text = (
        "Confirm your email\n\n"
        "Welcome. Open this link to confirm the email on your The Better "
        "Decision account:\n\n"
        f"{verify_url}\n\n"
        "If you didn't create an account, you can ignore this email."
    )
    return await send_email(to, subject, body_html, body_text)


async def send_invitation_email(
    to: str, *, inviter_name: str, org_name: str, accept_url: str
) -> bool:
    """Send an org-membership invitation link.

    ``inviter_name`` and ``org_name`` may contain user-supplied content
    (an inviter can rename themselves; an org name is set by an admin),
    so both are HTML-escaped before interpolation.
    """
    inviter_safe = html.escape(inviter_name)
    org_safe = html.escape(org_name)
    subject = f"{inviter_name} invited you to {org_name} on The Better Decision"
    body_html = _render_html(
        heading=f"Join {org_name} on The Better Decision",
        paragraphs=[
            f"<strong>{inviter_safe}</strong> invited you to share "
            f"<strong>{org_safe}</strong> on The Better Decision, a "
            "personal finance app for households who already share money.",
        ],
        cta_label="Accept invitation",
        cta_url=accept_url,
        footnote="This invitation expires in 7 days.",
    )
    body_text = (
        f"Join {org_name} on The Better Decision\n\n"
        f"{inviter_name} invited you to share {org_name} on The Better "
        "Decision, a personal finance app for households who already "
        "share money.\n\n"
        f"Accept here: {accept_url}\n\n"
        "This invitation expires in 7 days."
    )
    return await send_email(to, subject, body_html, body_text)


async def send_trial_expiring_email(to: str, days_left: int, org_name: str) -> bool:
    """Send a trial expiring notification."""
    upgrade_url = f"{settings.app_url}/settings/billing"
    org_safe = html.escape(org_name)
    day_word = "day" if days_left == 1 else "days"
    subject = (
        f"Your The Better Decision trial ends in {days_left} {day_word}"
    )
    body_html = _render_html(
        heading=f"Your trial ends in {days_left} {day_word}",
        paragraphs=[
            f"The Pro trial on <strong>{org_safe}</strong> ends in "
            f"{days_left} {day_word}. After that, the workspace switches "
            "to the Free plan and a few features go quiet.",
            "You can reserve your Pro spot now. No card is charged during "
            "beta; this just keeps the seat.",
        ],
        cta_label="Keep Pro",
        cta_url=upgrade_url,
        footnote=(
            "If you'd rather stay on Free, do nothing. The workspace will "
            "switch over on its own."
        ),
    )
    body_text = (
        f"Your trial ends in {days_left} {day_word}\n\n"
        f"The Pro trial on {org_name} ends in {days_left} {day_word}. "
        "After that, the workspace switches to the Free plan and a few "
        "features go quiet.\n\n"
        "Reserve your Pro spot (no card charged during beta):\n"
        f"{upgrade_url}\n\n"
        "If you'd rather stay on Free, do nothing. The workspace will "
        "switch over on its own."
    )
    return await send_email(to, subject, body_html, body_text)
