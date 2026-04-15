"""Email service — sends emails via Mailgun in production, logs in development.

In development (no MAILGUN_API_KEY set), emails are logged to structlog
with full content including any links. This lets developers see verification
and password reset URLs without needing a real email provider.
"""

import structlog
import httpx

from app.config import settings

logger = structlog.get_logger()


async def send_email(
    to: str,
    subject: str,
    body_html: str,
    body_text: str | None = None,
) -> bool:
    """Send an email. Returns True if sent/logged successfully."""
    if not settings.mailgun_api_key:
        # Development: log the email content
        await logger.ainfo(
            "email_sent_dev",
            to=to,
            subject=subject,
            body_text=body_text or "(html only)",
            body_html=body_html,
        )
        return True

    # Production: send via Mailgun HTTP API
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.post(
                f"https://api.mailgun.net/v3/{settings.mailgun_domain}/messages",
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
            await logger.ainfo("email_sent", to=to, subject=subject, status=response.status_code)
            return True
    except Exception as exc:
        await logger.aerror("email_send_failed", to=to, subject=subject, error=str(exc))
        return False


async def send_password_reset_email(to: str, token: str) -> bool:
    """Send a password reset email with a link containing the reset token."""
    reset_url = f"{settings.app_url}/reset-password?token={token}"
    subject = "PFV2 — Reset Your Password"
    body_html = f"""
    <h2>Reset Your Password</h2>
    <p>You requested a password reset for your PFV2 account.</p>
    <p><a href="{reset_url}" style="display:inline-block;padding:12px 24px;background:#c8a951;color:#1a1a2e;text-decoration:none;border-radius:6px;font-weight:bold;">Reset Password</a></p>
    <p>Or copy this link: <code>{reset_url}</code></p>
    <p>This link expires in 1 hour. If you didn't request this, ignore this email.</p>
    """
    body_text = f"Reset your password: {reset_url}\n\nThis link expires in 1 hour."
    return await send_email(to, subject, body_html, body_text)


async def send_mfa_email_code(to: str, code: str) -> bool:
    """Send a one-time MFA verification code via email."""
    subject = "PFV2 — Your Login Verification Code"
    body_html = f"""
    <h2>Your Verification Code</h2>
    <p>Use this code to complete your sign-in:</p>
    <p style="font-size:32px;font-weight:bold;letter-spacing:8px;color:#c8a951;font-family:monospace;">{code}</p>
    <p>This code expires in 10 minutes. If you didn't try to sign in, you can ignore this email.</p>
    """
    body_text = f"Your PFV2 verification code: {code}\n\nThis code expires in 10 minutes."
    return await send_email(to, subject, body_html, body_text)


async def send_verification_email(to: str, token: str) -> bool:
    """Send an email verification link."""
    verify_url = f"{settings.app_url}/verify-email?token={token}"
    subject = "PFV2 — Verify Your Email"
    body_html = f"""
    <h2>Verify Your Email</h2>
    <p>Welcome to PFV2! Please verify your email address.</p>
    <p><a href="{verify_url}" style="display:inline-block;padding:12px 24px;background:#c8a951;color:#1a1a2e;text-decoration:none;border-radius:6px;font-weight:bold;">Verify Email</a></p>
    <p>Or copy this link: <code>{verify_url}</code></p>
    """
    body_text = f"Verify your email: {verify_url}"
    return await send_email(to, subject, body_html, body_text)
