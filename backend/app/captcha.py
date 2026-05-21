"""CAPTCHA verification — Cloudflare Turnstile siteverify.

The register endpoint calls ``verify_captcha`` BEFORE any DB work or
email send. Fail-closed by design: any non-OK result (rejection,
timeout, network error, hostname/action mismatch) refuses registration.

The token is never logged. Only stable diagnostic fields (provider
error codes, expected vs. observed hostname/action, latency) are
emitted via structlog so an operator can tell why the gate fired
without inspecting the token itself.

Idempotency key: a fresh UUID per call. Cloudflare's docs note this
lets the SAME server-side attempt be safely retried by the SAME
process under a transient network failure. It does NOT protect
against a client re-submitting the same token a second time — the
token itself is single-use at Cloudflare's end (returns
``timeout-or-duplicate`` on the second redemption).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import structlog

from app.config import settings as app_settings


logger = structlog.get_logger(__name__)


# Result reason codes — stable strings so audit/log consumers can pin
# behavior without depending on Cloudflare's evolving error code set.
REASON_OK = "ok"
REASON_DISABLED = "disabled"  # CAPTCHA_REQUIRED=false; verify short-circuits ok
REASON_MISSING_TOKEN = "missing_token"
REASON_PROVIDER_REJECTED = "provider_rejected"
REASON_HOSTNAME_MISMATCH = "hostname_mismatch"
REASON_ACTION_MISMATCH = "action_mismatch"
REASON_TIMEOUT = "timeout"
REASON_NETWORK_ERROR = "network_error"
REASON_PROVIDER_ERROR = "provider_error"  # non-2xx response
REASON_MISCONFIGURED = "misconfigured"


@dataclass(frozen=True)
class CaptchaVerifyResult:
    ok: bool
    reason: str
    provider_error_codes: tuple[str, ...] = ()
    observed_hostname: str | None = None
    observed_action: str | None = None


async def verify_captcha(token: str | None, remote_ip: str | None) -> CaptchaVerifyResult:
    """Verify a captcha token against the configured provider.

    Returns a result object; never raises. Fail-closed: only returns
    ``ok=True`` when the provider confirms the token AND (when
    configured) hostname/action match expectations.

    When ``captcha_required`` is False the function short-circuits with
    ``reason=disabled, ok=True`` — the caller may still bypass
    verification entirely, but treating disabled as ok keeps the
    register handler's control flow linear.
    """
    if not app_settings.captcha_required:
        return CaptchaVerifyResult(ok=True, reason=REASON_DISABLED)

    if not app_settings.captcha_secret or not app_settings.captcha_verify_url:
        await logger.aerror("captcha.verify.misconfigured")
        return CaptchaVerifyResult(ok=False, reason=REASON_MISCONFIGURED)

    if not token:
        return CaptchaVerifyResult(ok=False, reason=REASON_MISSING_TOKEN)

    idempotency_key = str(uuid4())
    payload: dict[str, Any] = {
        "secret": app_settings.captcha_secret,
        "response": token,
        "idempotency_key": idempotency_key,
    }
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(app_settings.captcha_verify_timeout_s)
        ) as client:
            response = await client.post(
                app_settings.captcha_verify_url,
                data=payload,
            )
    except httpx.TimeoutException:
        await logger.awarning(
            "captcha.verify.timeout",
            timeout_s=app_settings.captcha_verify_timeout_s,
        )
        return CaptchaVerifyResult(ok=False, reason=REASON_TIMEOUT)
    except httpx.HTTPError as exc:
        await logger.awarning(
            "captcha.verify.network_error",
            error=type(exc).__name__,
        )
        return CaptchaVerifyResult(ok=False, reason=REASON_NETWORK_ERROR)

    if response.status_code >= 500 or response.status_code >= 400:
        await logger.awarning(
            "captcha.verify.provider_error",
            status_code=response.status_code,
        )
        return CaptchaVerifyResult(ok=False, reason=REASON_PROVIDER_ERROR)

    try:
        data = response.json()
    except ValueError:
        await logger.awarning("captcha.verify.invalid_json")
        return CaptchaVerifyResult(ok=False, reason=REASON_PROVIDER_ERROR)

    success = bool(data.get("success"))
    error_codes = tuple(str(c) for c in data.get("error-codes", []) or [])
    observed_hostname = data.get("hostname")
    observed_action = data.get("action")

    if not success:
        await logger.ainfo(
            "captcha.verify.failed",
            provider_error_codes=error_codes,
        )
        return CaptchaVerifyResult(
            ok=False,
            reason=REASON_PROVIDER_REJECTED,
            provider_error_codes=error_codes,
            observed_hostname=observed_hostname,
            observed_action=observed_action,
        )

    expected_hostname = app_settings.captcha_expected_hostname
    if expected_hostname and observed_hostname != expected_hostname:
        await logger.awarning(
            "captcha.verify.hostname_mismatch",
            expected=expected_hostname,
            observed=observed_hostname,
        )
        return CaptchaVerifyResult(
            ok=False,
            reason=REASON_HOSTNAME_MISMATCH,
            observed_hostname=observed_hostname,
            observed_action=observed_action,
        )

    expected_action = app_settings.captcha_expected_action
    if expected_action and observed_action != expected_action:
        await logger.awarning(
            "captcha.verify.action_mismatch",
            expected=expected_action,
            observed=observed_action,
        )
        return CaptchaVerifyResult(
            ok=False,
            reason=REASON_ACTION_MISMATCH,
            observed_hostname=observed_hostname,
            observed_action=observed_action,
        )

    await logger.ainfo(
        "captcha.verify.ok",
        hostname=observed_hostname,
        action=observed_action,
    )
    return CaptchaVerifyResult(
        ok=True,
        reason=REASON_OK,
        observed_hostname=observed_hostname,
        observed_action=observed_action,
    )
