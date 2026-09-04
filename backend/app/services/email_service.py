"""Email service. Sends brand-aligned customer emails via Mailgun.

Templates here are L5.6 brand polish (see ``docs/product/BRAND.md``):

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

import asyncio
import html
import json
import urllib.parse
from dataclasses import dataclass
from enum import Enum

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger()

# Per-phase transport bound for the Mailgun HTTP calls below.
MAILGUN_TIMEOUT = httpx.Timeout(10.0)

# Aggregate ceiling for ONE Mailgun send. ``MAILGUN_TIMEOUT`` above is a
# *per-phase* bound — connect / write / read / pool each get 10s, and
# ``read`` is charged per socket read rather than per response — so a
# server dribbling one byte just under the read budget keeps the send
# alive with no bound at all. TBD-179 verified that shape against a real
# drip-feed server on the Google OAuth exchange; this is the same defect
# on a lower-severity path (a send that fails closed, not a synchronous
# browser navigation).
#
# Unlike TBD-179's site there is exactly ONE awaited call per block, so
# the floor is one per-phase read budget rather than two, and a plain
# relative ``asyncio.timeout`` is as correct as a shared absolute
# deadline. If a second sequential call is ever added to either block,
# switch to ``asyncio.timeout_at`` with one deadline computed before the
# first — two nested relative bounds would permit their sum, which is
# the very thing this constant exists to cap.
#
# 20.0s is a deliberate narrowing and no value here can be shown
# "non-narrowing": per-phase also permits a connect and a write, so e.g.
# 3s connect + 9s read + 8s read violates no per-phase bound and still
# trips 20s. The judgement is that a Mailgun send taking longer than 20s
# is already failed from the caller's point of view. Do not restate this
# constant as provably safe.
MAILGUN_SEND_TOTAL_TIMEOUT_S = 20.0


# ─── Batch send outcome (TBD-330) ───
#
# ``send_batch`` used to return a bare ``bool``. That collapsed two
# epistemically OPPOSITE outcomes into one value:
#
#   * "Mailgun parsed the batch and refused it" (a 4xx, or our own MA2
#     pre-check refusing to issue the request at all) — nothing was
#     queued, so re-sending is not only safe, it is required; and
#   * "the request was written and no answer ever came" (the aggregate
#     deadline, a read timeout, a 5xx) — Mailgun may well be holding a
#     copy, so re-sending duplicates real customer email.
#
# The drain's only consumer read every falsy return as the first, wrote
# ``status='failed'``, and thereby invited an operator to Resume an
# unanswered 1000-address batch into 1000 duplicates (TBD-330). ``failed``
# is an ASSERTION that Mailgun did not accept the message, and a falsy
# return never licensed it.
#
# Why a typed object with ATTRIBUTE ACCESS rather than an ``Enum`` or a
# string: every ``Enum`` member and every non-empty string is truthy, so a
# stale ``if not ok:`` call site — or a stale test double still doing
# ``AsyncMock(return_value=True)`` — would keep compiling and silently
# treat a rejection as a success. Reading ``.disposition`` makes every
# such site raise ``AttributeError`` instead. The migration fails LOUD.
#
# For the same reason the drain reads ``.disposition`` OUTSIDE its
# defensive ``except Exception``: swallowing that ``AttributeError`` into
# "indeterminate" would put the silence straight back.


class SendDisposition(Enum):
    """What we actually know about a batch after ``send_batch`` returns."""

    #: A 2xx was observed (or dev mode short-circuited before any HTTP).
    #: Mailgun owns the message now.
    ACCEPTED = "accepted"
    #: Provably never queued: the MA2 pre-check refused to issue the
    #: request, Mailgun answered 4xx, or the connection was never
    #: established. Safe — and correct — to re-send.
    REJECTED = "rejected"
    #: Written, or maybe written, with no conclusive answer. Never
    #: re-send on this: it is informationally identical to a crash
    #: mid-call, which R2 already rules is not retried by ``resume``.
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class BatchSendResult:
    """``send_batch``'s return value.

    ``reason`` is a short, PII-free, operator-readable string explaining a
    non-ACCEPTED disposition. The drain writes it verbatim into
    ``email_broadcast_recipients.error``, which moves the
    rejection-vs-unknown distinction out of structlog and into a queryable
    column. It is ``None`` for ACCEPTED.
    """

    disposition: SendDisposition
    reason: str | None = None


_ACCEPTED = BatchSendResult(SendDisposition.ACCEPTED)


def _classify_send_exception(exc: BaseException) -> SendDisposition:
    """Bucket an exception raised out of the Mailgun POST.

    ⚠ The trap: httpx's own per-phase timeouts do NOT derive from builtin
    ``TimeoutError`` — ``ReadTimeout``/``WriteTimeout``/``ConnectTimeout``/
    ``PoolTimeout`` all descend from ``httpx.TimeoutException``. Only the
    ``asyncio.timeout`` aggregate bound raises the builtin. So classifying
    by ``except TimeoutError`` alone mis-buckets ``ReadTimeout``, which is
    the single most ambiguous outcome there is: the request was written
    and the answer was never read.

    REJECTED is claimed ONLY where the request provably never reached
    Mailgun's application. Everything else — including anything
    unrecognised — defaults to INDETERMINATE, which is the fail-safe
    direction under the never-double-send invariant.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        # 4xx: Mailgun parsed it and refused. Conclusive, nothing queued.
        # 5xx: can come from a proxy AFTER Mailgun enqueued. Not conclusive.
        if 400 <= exc.response.status_code < 500:
            return SendDisposition.REJECTED
        return SendDisposition.INDETERMINATE
    if isinstance(
        exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
    ):
        # No connection was ever established, so no bytes were written.
        # (``ConnectTimeout`` is NOT a subclass of ``ConnectError``; both
        # have to be named.)
        return SendDisposition.REJECTED
    return SendDisposition.INDETERMINATE


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
        async with httpx.AsyncClient(timeout=MAILGUN_TIMEOUT) as client:
            # Only the network await sits inside the bound. The status
            # check, the log write and the client's own aclose() stay
            # outside it, matching TBD-179: a response arriving near the
            # deadline must not be cancelled mid-log, which would lose
            # the very record the incident needs.
            async with asyncio.timeout(MAILGUN_SEND_TOTAL_TIMEOUT_S):
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
    except TimeoutError:
        # Exactly one name, and never a tuple with an httpx class:
        # ``asyncio.TimeoutError`` *is* the builtin ``TimeoutError`` on
        # 3.11+, and none of httpx's own timeout classes derive from it,
        # so this clause cannot steal a per-phase httpx timeout — those
        # keep landing in the handler below.
        #
        # It needs its own clause because ``asyncio.timeout`` raises a
        # BARE ``TimeoutError()`` and ``str(TimeoutError())`` is ``""``.
        # Routed through the handler below it would emit ``error=""`` on
        # the exact incident the aggregate bound above exists to survive
        # — a blank reason, indistinguishable from any other zero-message
        # exception and impossible to alert on. It is also the ONLY
        # operator signal on that path: ``routers/auth.py`` dispatches the
        # password-reset send through ``BackgroundTasks``, which discards
        # the ``False`` return entirely.
        #
        # Same split TBD-179 makes at ``auth.google.callback.exchange_timeout``:
        # "Mailgun rejected the send" and "Mailgun never answered" need
        # different operator remediations, so they get different events.
        # Snake_case, matching this module's other four events rather than
        # auth's dotted names.
        #
        # PII bound is the same as the handler below: ``to`` and
        # ``subject`` only, never the body (it carries the raw token).
        await logger.aerror(
            "email_send_timeout",
            to=to,
            subject=subject,
            error="timeout",
            timeout_s=MAILGUN_SEND_TOTAL_TIMEOUT_S,
        )
        return False
    except Exception as exc:
        # Never log the body, it carries the token. ``str(exc)`` from httpx
        # surfaces the response status / reason but not our payload.
        # ``error_type`` rides along because ``str(exc)`` is empty for any
        # zero-message exception; the class name is always there.
        await logger.aerror(
            "email_send_failed",
            to=to,
            subject=subject,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False


async def send_batch(
    to_list: list[str],
    subject: str,
    body_html: str,
    body_text: str,
    recipient_variables: dict,
    broadcast_id: int,
) -> BatchSendResult:
    """Send ONE Mailgun batch-sending call covering every address in
    ``to_list`` (spec ``2026-07-18-admin-email-broadcast-design.md``, "Batch
    sending revision" MA4). Mailgun fans this out into one individualized
    message per recipient, substituting ``recipient_variables`` into the
    ``%recipient.*%`` tokens in ``body_html``/``body_text``.

    Returns a :class:`BatchSendResult`, NOT a bool (TBD-330 — see the
    module-level note above ``SendDisposition``). The caller must branch on
    ``.disposition``: only ``REJECTED`` licenses reverting the batch's rows
    to ``failed``, because only ``REJECTED`` asserts that Mailgun did not
    take the message.

    Dev mode (no ``mailgun_api_key``): logs ``broadcast_batch_sent`` with
    ONLY ``count=len(to_list)`` and ``subject``, sends nothing, and is
    ACCEPTED — the drain must treat it exactly as it treats a real 2xx.

    Prod: mirrors ``send_email``'s HTTP shape, but ``to`` carries every
    address (httpx repeats a list-valued form field) and
    ``recipient-variables`` is sent as a JSON string — Mailgun's
    single-value-per-form-key model. ACCEPTED only on a 2xx response; any
    exception (including a raised non-2xx status) is caught and classified
    so one bad batch never crashes the caller.

    PII bound (MA5): logging here NEVER includes ``to_list``,
    ``recipient_variables``, or the rendered bodies — only the batch size,
    subject, and status/error. The address list and per-recipient names
    live in the request payload only.

    Recipient-variables precondition (MA2), enforced HERE and not only in
    the caller: this is the one function that can trigger Mailgun's
    all-addresses-in-the-``To``-header leak. Mailgun only individualizes a
    multi-address ``to`` when ``recipient-variables`` carries an entry for
    EVERY address; a missing or mismatched map makes every recipient see
    every other address. So for ``len(to_list) > 1`` the map's key set must
    equal ``to_list`` exactly — otherwise NOTHING is sent, a PII-bounded
    ``broadcast_batch_failed`` is logged (counts + reason only, never
    addresses), and the call returns ``REJECTED``. Conclusively rejected
    precisely BECAUSE no HTTP request is issued at all: the rows revert
    ``sent → failed`` and a resume can correctly retry them.

    A SINGLE-address ``to`` is deliberately allowed without a map entry:
    there is no cross-recipient leak possible with one address, and the
    dry-run/one-off paths rely on it. Tokens simply stay unsubstituted in
    that case, which the drain's own MA2 key-match prevents anyway.

    ``broadcast_id`` (W4, spec ``2026-07-20-mailgun-delivery-webhooks-design.md``)
    is sent as the Mailgun message-level user-variable ``v:broadcast_id``
    (stringified). Mailgun echoes message-level ``v:`` variables back on
    every recipient's delivery webhook event under
    ``event-data.user-variables`` as strings, which is the sole correlation
    carrier the webhook handler uses to find the recipient row (recipient
    variables are NOT echoed).
    """
    if len(to_list) > 1 and set(recipient_variables or {}) != set(to_list):
        await logger.aerror(
            "broadcast_batch_failed",
            count=len(to_list),
            subject=subject,
            variables_count=len(recipient_variables or {}),
            error=(
                "recipient-variables key set does not match the to-list; "
                "refusing to send a multi-address batch that Mailgun would "
                "deliver with every address exposed in the To header"
            ),
        )
        return BatchSendResult(
            SendDisposition.REJECTED,
            "recipient-variables key set did not match the to-list; no "
            "request was issued",
        )

    if not settings.mailgun_api_key:
        await logger.ainfo(
            "broadcast_batch_sent", count=len(to_list), subject=subject
        )
        return _ACCEPTED

    # Production: send via Mailgun HTTP API.
    api_host = (
        "api.eu.mailgun.net"
        if settings.mailgun_region.lower().strip() == "eu"
        else "api.mailgun.net"
    )
    try:
        async with httpx.AsyncClient(timeout=MAILGUN_TIMEOUT) as client:
            # Same bound, same placement as ``send_email`` above; see the
            # note there for why only the network await is inside it.
            async with asyncio.timeout(MAILGUN_SEND_TOTAL_TIMEOUT_S):
                response = await client.post(
                    f"https://{api_host}/v3/{settings.mailgun_domain}/messages",
                    auth=("api", settings.mailgun_api_key),
                    data={
                        "from": settings.email_from,
                        "to": to_list,
                        "subject": subject,
                        "html": body_html,
                        "text": body_text,
                        "recipient-variables": json.dumps(recipient_variables),
                        "v:broadcast_id": str(broadcast_id),
                    },
                )
            response.raise_for_status()
            await logger.ainfo(
                "broadcast_batch_sent",
                count=len(to_list),
                subject=subject,
                status=response.status_code,
            )
            return _ACCEPTED
    except TimeoutError:
        # Same clause, same reasoning, same PII bound as ``send_email``
        # above; see the note there. This clause catches ONLY the
        # ``asyncio.timeout`` aggregate bound — none of httpx's own
        # per-phase timeout classes derive from builtin ``TimeoutError``.
        #
        # TBD-330: the request was written and the answer never read, so
        # this is INDETERMINATE, not a rejection. Reverting these rows to
        # ``failed`` (the pre-TBD-330 behaviour) invites a Resume click to
        # duplicate a batch Mailgun may already be delivering.
        await logger.aerror(
            "broadcast_batch_timeout",
            count=len(to_list),
            subject=subject,
            error="timeout",
            timeout_s=MAILGUN_SEND_TOTAL_TIMEOUT_S,
        )
        return BatchSendResult(
            SendDisposition.INDETERMINATE,
            f"no answer from Mailgun within the {MAILGUN_SEND_TOTAL_TIMEOUT_S}s "
            "aggregate send deadline; the batch may or may not have been "
            "accepted, so these rows are NOT re-sent",
        )
    except Exception as exc:
        # Never log to_list / recipient_variables / bodies (MA5) — only the
        # count, subject, and the exception's str() (httpx surfaces status /
        # reason there, not our request payload). ``error_type`` rides along
        # because ``str(exc)`` is empty for any zero-message exception.
        await logger.aerror(
            "broadcast_batch_failed",
            count=len(to_list),
            subject=subject,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        disposition = _classify_send_exception(exc)
        if disposition is SendDisposition.REJECTED:
            reason = (
                f"Mailgun did not accept the batch ({type(exc).__name__}: "
                f"{exc}); nothing was queued"
            )
        else:
            reason = (
                f"the batch send did not complete ({type(exc).__name__}: "
                f"{exc}) and Mailgun's answer is unknown, so these rows are "
                "NOT re-sent"
            )
        return BatchSendResult(disposition, reason)


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


async def send_notification_email(
    to: str, *, title: str, body: str, link_url: str | None = None
) -> bool:
    """Send a generic notification email (title + body + optional CTA link).

    Mirrors the branded transactional emails. Best-effort: delegates to
    send_email, which logs-and-returns in dev and never raises here.
    """
    cta_url = f"{settings.app_url}{link_url}" if link_url else None
    prefs_url = f"{settings.app_url}/settings/notifications"
    title_safe = html.escape(title)
    body_safe = html.escape(body)
    body_html = _render_html(
        heading=title,
        paragraphs=[body_safe],
        cta_label="Open The Better Decision" if cta_url else None,
        cta_url=cta_url,
        footnote=(
            "You are receiving this because you are a member of this workspace. "
            f'<a href="{prefs_url}">Manage your notification preferences</a> '
            "to change or turn off these emails."
        ),
    )
    body_text = (
        f"{title}\n\n{body}"
        + (f"\n\n{cta_url}" if cta_url else "")
        + f"\n\nManage your notification preferences to change or turn off "
        f"these emails: {prefs_url}"
    )
    return await send_email(to, title_safe, body_html, body_text)


async def send_account_deleted_email(
    to: str,
    username: str,
) -> bool:
    """Send a final transactional email after an account is hard-deleted.

    This is the only customer-facing signal for an account deletion: the
    user row (and its in-app notification feed) is already gone, so the
    email to the last-known address is the sole safety net. It is sent
    AFTER both the delete commit and the audit-row commit succeed (see
    ``routers/admin_users.py``); a failure here is logged via the
    existing ``email_send_failed`` event and never rolls back the delete.

    Privacy posture: the recipient is now an EXTERNAL party (their account
    is gone), so the body deliberately does NOT name the acting
    administrator's email or request IP. Those identifiers stay in the
    ``audit_events`` row only (see ``routers/admin_users.py``), where they
    serve compliance / forensic lookups without being disclosed to the
    deleted user. The email says only that an administrator acted.

    GDPR posture: this is one final transactional email for a security
    event (an admin acting on the account), sent under legitimate
    interest, NOT marketing. It mirrors the password-change notification
    pattern. We do not store the recipient address beyond the audit
    snapshot that already exists for compliance reasons; nothing here
    creates new retained personal data.

    Args:
        to: the deleted user's last-known email address (audit snapshot).
        username: the deleted user's username, for a personal greeting.
    """
    username_safe = html.escape(username)
    subject = "[The Better Decision] Your account has been deleted"
    body_html = _render_html(
        heading="Your account has been deleted",
        paragraphs=[
            f"Hi {username_safe}, your The Better Decision account was "
            "deleted by an administrator.",
            "Your data has been removed and you can no longer sign in. "
            "If you did not expect this, contact support so we can help.",
        ],
        footnote=(
            "This is a one-time security notice. We will not send further "
            "messages to this address regarding the deleted account."
        ),
    )
    body_text = (
        "Your account has been deleted\n\n"
        f"Hi {username}, your The Better Decision account was deleted by "
        "an administrator.\n\n"
        "Your data has been removed and you can no longer sign in. If you "
        "did not expect this, contact support so we can help.\n\n"
        "This is a one-time security notice. We will not send further "
        "messages to this address regarding the deleted account."
    )
    return await send_email(to, subject, body_html, body_text)
