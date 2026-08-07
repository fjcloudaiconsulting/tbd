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

import asyncio
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
            # Only the network await sits inside the aggregate bound. The
            # status check, the .json() parse, the logging and the
            # client's own aclose() stay outside it, matching TBD-179 and
            # TBD-266: a response arriving near the deadline must not be
            # cancelled mid-log, which would lose the very record the
            # incident needs. There is exactly ONE awaited call here, so
            # a relative ``asyncio.timeout`` is as correct as a shared
            # absolute deadline; if a second sequential call is ever
            # added, switch to ``asyncio.timeout_at`` with one deadline
            # computed before the first — two nested relative bounds
            # would permit their sum, the very thing this caps.
            async with asyncio.timeout(app_settings.captcha_verify_total_timeout_s):
                response = await client.post(
                    app_settings.captcha_verify_url,
                    data=payload,
                )
    except (httpx.TimeoutException, TimeoutError) as exc:
        # ``TimeoutError`` shares this clause rather than getting its own
        # (TBD-328). ``asyncio.timeout`` raises a BARE builtin
        # ``TimeoutError`` — an ``OSError`` subclass deriving from
        # NEITHER ``httpx.TimeoutException`` nor ``httpx.HTTPError``, so
        # without this name it would escape ``verify_captcha`` entirely.
        # This function's contract is "never raises" and the register
        # handler leans on it: ``routers/auth.py`` calls it with no
        # ``try`` and ``main.py`` registers no generic ``Exception``
        # handler, so an escape is an unhandled 500 on a public, pre-auth
        # endpoint — exactly the failure TBD-179 existed to fix.
        #
        # Note this is the opposite of TBD-266's split at
        # ``email_send_timeout``: that site already had a surrounding
        # ``except Exception`` whose ``error=str(exc)`` would have logged
        # "" for a bare ``TimeoutError``, so it needed its OWN clause to
        # stay alertable. Here the clause it lands in is already a
        # dedicated, well-named timeout handler, so sharing it keeps the
        # reason code and event name stable for the call site and every
        # log consumer. Cannot steal httpx's per-phase timeouts either
        # way: none of httpx's timeout classes derive from the builtin.
        #
        # ``total_timeout_s`` is additive and load-bearing: an aggregate
        # trip logging only ``timeout_s`` would assert the per-phase
        # bound fired, an active falsehood in the one line the incident
        # reads.
        #
        # ``bound`` is what the event name can no longer carry. Sharing
        # one clause keeps ``captcha.verify.timeout`` stable for existing
        # consumers (the reason TBD-179 and TBD-266 could each give the
        # aggregate its own event and this site cannot), but it also
        # makes the two trips indistinguishable in the log — and they
        # want DIFFERENT Cloudflare remediations. ``per_phase`` means one
        # phase stalled: a connect that never completed, a socket read
        # that never returned. ``aggregate`` means every individual phase
        # stayed inside its budget while the call as a whole did not —
        # the drip feed, which no per-phase bound can ever catch. An
        # incident that cannot tell those apart chases the wrong one.
        #
        # The test is exact, not a heuristic: the two hierarchies are
        # disjoint (verified against httpx 0.28.1 — no httpx timeout
        # class derives from the builtin, and the builtin derives from
        # ``OSError``, not from ``httpx.HTTPError``), so every exception
        # reaching this clause matches exactly one arm.
        bound = "per_phase" if isinstance(exc, httpx.TimeoutException) else "aggregate"
        await logger.awarning(
            "captcha.verify.timeout",
            bound=bound,
            timeout_s=app_settings.captcha_verify_timeout_s,
            total_timeout_s=app_settings.captcha_verify_total_timeout_s,
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
