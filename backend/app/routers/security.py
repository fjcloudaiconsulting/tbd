"""Public CSP violation-report sink.

Browsers POST Content-Security-Policy violation reports here when a
directive is violated. The endpoint is **public** (unauthenticated) by
design: browsers send these reports without any of our auth context,
and a CSP report is a side-channel the page can't attach a Bearer token
to. It is wired into the CSP via the ``report-uri`` / ``report-to``
directives + the ``Reporting-Endpoints`` response header (set on the
frontend in ``frontend/lib/security-headers.ts``).

Two body shapes arrive in the wild and both are accepted:

* **Legacy** ``Content-Type: application/csp-report`` — a single JSON
  object ``{"csp-report": {...}}`` (the ``report-uri`` directive).
* **Reporting API** ``Content-Type: application/reports+json`` — a JSON
  **array** of report envelopes ``[{"type": "csp-violation", "body":
  {...}}, ...]`` (the ``report-to`` / ``Reporting-Endpoints`` path).

Anything else (unknown content-type, malformed JSON, unexpected shape)
is tolerated: we never 500 on a report we can't parse. A browser
firing reports is not a client we control, so robustness beats
strictness here. The endpoint always answers ``204 No Content``.

Each parsed violation is persisted to ``audit_events`` via the shared
``record_audit_event`` independent-session path (event type
``security.csp_violation``, outcome ``failure`` — a CSP violation is by
definition a policy breach worth recording). Only a **bounded
allowlist** of fields is copied into ``detail``; full report bodies can
carry user-bearing URLs (``document-uri``, ``blocked-uri``,
``source-file``) so we truncate each to a fixed cap and never log the
raw payload at INFO.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import get_session_factory
from app.rate_limit import get_client_ip, limiter
from app.services import audit_service


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/security", tags=["security"])


CSP_VIOLATION_EVENT_TYPE = "security.csp_violation"

# Hard cap on the request body we will read. CSP reports are small
# JSON blobs; anything past this is either malformed or hostile. We
# read at most this many bytes and drop the rest, so a giant POST can
# never blow up memory on this unauthenticated route.
_MAX_BODY_BYTES = 16 * 1024  # 16 KiB

# Cap on how many violation envelopes we record from a single
# Reporting-API array. Browsers batch, but a single POST should never
# spawn an unbounded number of audit rows.
_MAX_REPORTS_PER_REQUEST = 20

# Per-string truncation cap for any value copied into the audit detail.
# Keeps URL-bearing fields bounded so the audit row can't balloon and
# we don't persist enormous attacker-controlled strings.
_MAX_FIELD_LEN = 512

# Bounded allowlist of CSP-report fields we persist. Both the legacy
# kebab-case keys and the Reporting-API camelCase keys are mapped onto
# a single normalized snake_case key so the audit detail is uniform
# regardless of which browser path delivered the report.
_FIELD_ALIASES: dict[str, str] = {
    # legacy (application/csp-report) keys
    "document-uri": "document_uri",
    "referrer": "referrer",
    "violated-directive": "violated_directive",
    "effective-directive": "effective_directive",
    "original-policy": "original_policy",
    "disposition": "disposition",
    "blocked-uri": "blocked_uri",
    "line-number": "line_number",
    "column-number": "column_number",
    "source-file": "source_file",
    "status-code": "status_code",
    "script-sample": "script_sample",
    # Reporting-API (application/reports+json) camelCase keys
    "documentURL": "document_uri",
    "violatedDirective": "violated_directive",
    "effectiveDirective": "effective_directive",
    "originalPolicy": "original_policy",
    "blockedURL": "blocked_uri",
    "lineNumber": "line_number",
    "columnNumber": "column_number",
    "sourceFile": "source_file",
    "statusCode": "status_code",
    "sample": "script_sample",
}


def _bounded(value: Any) -> Any:
    """Coerce a single report field to a JSON-safe, bounded value.

    Strings are truncated to ``_MAX_FIELD_LEN``. ints/bools pass
    through. Anything else (nested dict/list/None) is dropped by
    returning ``None`` so it isn't copied into the audit detail.
    """
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, str):
        return value[:_MAX_FIELD_LEN]
    return None


def _extract_detail(report_body: Any) -> Optional[dict[str, Any]]:
    """Map an allowlist of fields out of a single CSP-report body into a
    normalized, bounded ``detail`` dict. Returns ``None`` if the input
    isn't a dict (so the caller can skip it).
    """
    if not isinstance(report_body, dict):
        return None
    detail: dict[str, Any] = {}
    for raw_key, norm_key in _FIELD_ALIASES.items():
        if raw_key not in report_body:
            continue
        bounded = _bounded(report_body[raw_key])
        if bounded is None:
            continue
        # Skip empty/blank strings so an empty legacy alias (e.g.
        # ``blocked-uri: ""``) can't shadow a meaningful camelCase alias
        # (``blockedURL: "https://real"``). ``setdefault`` keeps the
        # first PRESENT value, so without this guard an empty string
        # would win purely because its key is iterated first.
        if isinstance(bounded, str) and not bounded.strip():
            continue
        # First non-empty writer wins: if both the kebab and camel alias
        # for the same normalized key are present, keep the first one
        # that carries a non-empty value.
        detail.setdefault(norm_key, bounded)
    return detail


def _parse_reports(payload: Any) -> list[dict[str, Any]]:
    """Normalize whatever JSON arrived into a list of report bodies.

    Handles both supported shapes and silently yields an empty list for
    anything unrecognized:

    * ``{"csp-report": {...}}``           → ``[{...}]``   (legacy)
    * ``[{"type": "...", "body": {...}}]``→ ``[{...}, ...]`` (Reporting API)
    """
    bodies: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        legacy = payload.get("csp-report")
        if isinstance(legacy, dict):
            bodies.append(legacy)
    elif isinstance(payload, list):
        for envelope in payload[:_MAX_REPORTS_PER_REQUEST]:
            if not isinstance(envelope, dict):
                continue
            body = envelope.get("body")
            if isinstance(body, dict):
                bodies.append(body)
    return bodies[:_MAX_REPORTS_PER_REQUEST]


def _request_id() -> Optional[str]:
    return structlog.contextvars.get_contextvars().get("request_id")


@router.post(
    "/csp-report",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
@limiter.limit("60/minute")
async def csp_report(request: Request) -> Response:
    """Accept a CSP violation report and persist it to ``audit_events``.

    Public (no auth). Always returns ``204``; never 500s on an
    unparseable report. Uses an independent session for the audit write
    (via ``record_audit_event``) so it doesn't depend on a request-
    scoped DB dependency on this anonymous route.

    **Audit outcome is always ``failure`` by design.** A CSP report is,
    by definition, a policy breach, so every row is recorded with
    ``outcome="failure"``. Because this is a public, anonymous, and
    potentially high-volume source, audit alerting and the default
    ``/admin/audit`` failure views MUST scope OUT
    ``event_type="security.csp_violation"`` — otherwise this stream
    dilutes the genuine admin-failure signal those views exist to
    surface. (The outcome value is intentionally not softened to keep
    the violation semantics honest; the filtering lives in the
    consumers, not here.)
    """
    # Pull the session factory directly (not via Depends) so this stays
    # a zero-dependency public route — nothing here resolves auth.
    session_factory: async_sessionmaker[AsyncSession] = get_session_factory()

    # Cheap pre-check on this unauthenticated route: if the client
    # advertises an oversized body, drop it before buffering anything.
    declared_len = request.headers.get("content-length")
    if declared_len is not None:
        try:
            if int(declared_len) > _MAX_BODY_BYTES:
                await logger.ainfo(
                    "security.csp_report.oversized",
                    content_length=declared_len,
                )
                return Response(status_code=status.HTTP_204_NO_CONTENT)
        except ValueError:
            pass

    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raw = raw[:_MAX_BODY_BYTES]

    payload: Any = None
    if raw:
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = None

    bodies = _parse_reports(payload)

    if not bodies:
        # Nothing recognizable. Emit a low-cardinality breadcrumb (no
        # payload) so a flood of malformed reports is still visible in
        # logs without leaking content, then accept gracefully.
        await logger.ainfo(
            "security.csp_report.unparsed",
            content_type=request.headers.get("content-type", ""),
            byte_len=len(raw),
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    client_ip = get_client_ip(request)
    request_id = _request_id()

    for body in bodies:
        detail = _extract_detail(body)
        # Structured breadcrumb at INFO — only the (bounded) directive,
        # never the full payload or URL-bearing fields.
        await logger.ainfo(
            "security.csp_violation",
            violated_directive=(detail or {}).get("violated_directive"),
            effective_directive=(detail or {}).get("effective_directive"),
        )
        await audit_service.record_audit_event(
            session_factory,
            event_type=CSP_VIOLATION_EVENT_TYPE,
            actor_user_id=None,
            # No authenticated actor on this public route; sentinel keeps
            # the NOT NULL actor_email column satisfied.
            actor_email="anonymous",
            target_org_id=None,
            target_org_name=None,
            request_id=request_id,
            ip_address=client_ip,
            # Always "failure": a CSP violation is a policy breach. See the
            # endpoint docstring — audit alerting / default /admin/audit
            # failure views must scope OUT this event_type so this
            # high-volume anonymous source doesn't dilute real admin signal.
            outcome="failure",
            detail=detail or None,
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)
