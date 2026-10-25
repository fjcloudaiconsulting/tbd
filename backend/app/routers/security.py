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

Parsed violations are persisted to ``audit_events`` in one transaction
per request (event type ``security.csp_violation``, outcome ``failure`` —
a CSP violation is by definition a policy breach worth recording). The
sink is anonymous, so its growth is bounded (BE-10): a report identical
to one the same client network recorded in the last
``_DEDUPE_WINDOW_SECONDS`` is not recorded again, and per
``_BUDGET_WINDOW_SECONDS`` at most ``_CLIENT_BUDGET`` rows are recorded
per client network (``rate_limit_key``: the IP, IPv6 cut to its /64) and
``_GLOBAL_BUDGET`` across every client; once the global budget is spent
one row per directive per window still comes from a small reserve. A
report is marked seen only once its row has committed. The counters live in the shared
``rate_limits`` table; when it is unreachable nothing is recorded. Every
report, recorded or dropped, adds to the ``tbd.security.csp_reports``
counter by outcome and directive, so a drop stays visible without a
row. Only a **bounded
allowlist** of fields is copied into ``detail``; full report bodies can
carry user-bearing URLs (``document-uri``, ``blocked-uri``,
``source-file``, ``referrer``) so we cut their query string and fragment
(where one-time tokens live, FE-03), truncate each to a fixed cap and
never log the raw payload at INFO.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any, Optional

import logging
from fastapi import APIRouter, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opentelemetry import metrics

from app import rate_limit_db, tracing
from app.deps import get_session_factory
from app.rate_limit import get_client_ip, limiter, rate_limit_key
from app.request_body import read_capped_body
from app.services import audit_service

# ⚠ Re-exported, NOT redefined (TBD-439). The default-view exclusion in
# ``audit_service.list_audit_events`` must name the SAME string this writer
# emits; two spellings would silently stop excluding while every unit test
# stayed green, because each side's tests read the symbol its own module
# uses. ``tests/test_csp_report.py::test_csp_row_is_hidden_from_the_default_
# audit_view`` is the end-to-end fence that closes that gap.
from app.services.audit_service import CSP_VIOLATION_EVENT_TYPE  # noqa: F401
from app import logs


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/security", tags=["security"])



# Hard cap on the request body we will read. CSP reports are small
# JSON blobs; anything past this is either malformed or hostile. A
# body past it is dropped, declared length or not, and reading stops at
# the cap, so a giant POST can never blow up memory on this
# unauthenticated route.
_MAX_BODY_BYTES = 16 * 1024  # 16 KiB

# Cap on how many violation envelopes we record from a single
# Reporting-API array. Browsers batch, but a single POST should never
# spawn an unbounded number of audit rows.
_MAX_REPORTS_PER_REQUEST = 20

# BE-10: growth bounds for this anonymous writer. Browsers repeat the same
# violation on every page load, so one row per distinct report per client
# network per window keeps the signal. The per-client budget is spent
# before the global one, so one sender cannot use up the global budget.
# The bound per window: at most _GLOBAL_BUDGET rows (about
# _GLOBAL_BUDGET / _CLIENT_BUDGET = 20 client networks can spend it), plus
# a reserve of one row per directive label (len(_DIRECTIVES) + "other")
# kept after it is spent, so a flood cannot hide every genuine report.
# Drops are counted by outcome on ``CSP_REPORTS``; alert on global_budget.
_DEDUPE_WINDOW_SECONDS = 3600
_BUDGET_WINDOW_SECONDS = 3600
_CLIENT_BUDGET = 50
_GLOBAL_BUDGET = 1000
_BUDGET_KEY = "csp_report:budget"
_CLIENT_KEY_PREFIX = "csp_report:client:"
_SEEN_KEY_PREFIX = "csp_report:seen:"
_RESERVE_KEY_PREFIX = "csp_report:reserve:"

# Reports by ``outcome`` (recorded, reserve, duplicate, client_budget,
# global_budget, counter_unavailable, write_failed) and ``directive``
# (closed vocabulary).
CSP_REPORTS = metrics.get_meter(__name__).create_counter("tbd.security.csp_reports")
_DIRECTIVES = frozenset({
    "base-uri", "child-src", "connect-src", "default-src", "fenced-frame-src",
    "font-src", "form-action", "frame-ancestors", "frame-src", "img-src",
    "manifest-src", "media-src", "object-src", "prefetch-src",
    "require-trusted-types-for", "sandbox", "script-src", "script-src-attr",
    "script-src-elem", "style-src", "style-src-attr", "style-src-elem",
    "trusted-types", "upgrade-insecure-requests", "worker-src",
})

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

# Normalized keys whose value is a URL. Reset, verification and invite links
# carry one-time tokens in the query string, so only the part before the
# first ``?`` or ``#`` is kept (FE-03).
_URL_FIELDS = frozenset({"document_uri", "blocked_uri", "referrer", "source_file"})
_QUERY_OR_FRAGMENT = re.compile(r"[?#]")


def _without_query(value: str) -> str:
    return _QUERY_OR_FRAGMENT.split(value, maxsplit=1)[0]


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
        value = report_body[raw_key]
        if norm_key in _URL_FIELDS and isinstance(value, str):
            value = _without_query(value)
        bounded = _bounded(value)
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
    return logs.CONTEXT.get({}).get("request_id")


def _fingerprint(network: str, detail: dict[str, Any]) -> str:
    # The client network is part of the identity: one sender must not be
    # able to pre-seed a report and hide the same one from everybody else.
    canonical = json.dumps(
        [network, detail], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _directive(detail: dict[str, Any]) -> str:
    """Metric label: a known directive name, else ``other``. Report values
    are attacker-chosen, so they never become a label as-is."""
    raw = detail.get("effective_directive") or detail.get("violated_directive")
    name = raw.split(" ", 1)[0].strip().lower() if isinstance(raw, str) else ""
    return name if name in _DIRECTIVES else "other"


def _count(outcome: str, details) -> None:
    for detail in details:
        CSP_REPORTS.add(1, {"outcome": outcome, "directive": _directive(detail)})


Candidate = tuple[str, dict[str, Any]]


def _charge(key: str, budget: int, items: list[Candidate]) -> int:
    """Spend ``len(items)`` of the budget under ``key``; return how many fit."""
    used = rate_limit_db.hit(key, _BUDGET_WINDOW_SECONDS, amount=len(items))
    return max(0, budget - (used - len(items)))


def _admit(
    network: str, candidates: list[Candidate]
) -> tuple[list[Candidate], list[Candidate], dict[str, list[Candidate]]]:
    """Split the reports into those to record within the budgets, those to
    record from the reserve, and those dropped by reason. Sync (run in a
    thread); a counter error propagates and the caller records nothing.
    Marks nothing as seen and spends no reserve slot (``_mark_recorded``
    does both, after the commit)."""
    dropped: dict[str, list[Candidate]] = {
        "duplicate": [], "client_budget": [], "global_budget": [],
    }
    fresh: list[Candidate] = []
    for c in candidates:
        seen = rate_limit_db.get(_SEEN_KEY_PREFIX + c[0])
        (dropped["duplicate"] if seen else fresh).append(c)
    if fresh:
        room = _charge(_CLIENT_KEY_PREFIX + network, _CLIENT_BUDGET, fresh)
        dropped["client_budget"], fresh = fresh[room:], fresh[:room]
    if fresh:
        room = _charge(_BUDGET_KEY, _GLOBAL_BUDGET, fresh)
        dropped["global_budget"], fresh = fresh[room:], fresh[:room]
    # Global budget spent: one row per directive label per window still
    # gets through, so a distributed flood cannot hide every report.
    reserve: list[Candidate] = []
    labels: set[str] = set()
    for c in dropped["global_budget"]:
        label = _directive(c[1])
        if label in labels or rate_limit_db.get(_RESERVE_KEY_PREFIX + label):
            continue
        labels.add(label)
        reserve.append(c)
    reserved = {fp for fp, _detail in reserve}
    dropped["global_budget"] = [c for c in dropped["global_budget"] if c[0] not in reserved]
    return fresh, reserve, dropped


def _mark_recorded(fingerprints: list[str], reserve_labels: list[str]) -> None:
    for fp in fingerprints:
        rate_limit_db.hit(_SEEN_KEY_PREFIX + fp, _DEDUPE_WINDOW_SECONDS)
    for label in reserve_labels:
        rate_limit_db.hit(_RESERVE_KEY_PREFIX + label, _BUDGET_WINDOW_SECONDS)


@router.post(
    "/csp-report",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
@limiter.limit("60/minute")
async def csp_report(request: Request) -> Response:
    """Accept a CSP violation report and persist it to ``audit_events``.

    Public (no auth). Always returns ``204``; never 500s on an
    unparseable report. Opens its own session for the audit write (one
    commit for every row of the request) so it doesn't depend on a
    request-scoped DB dependency on this anonymous route. Duplicate and
    over-budget reports are dropped before the write (module docstring).

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
                logger.info(
                    "security.csp_report.oversized",
                    extra={
                        "content_length": declared_len,
                    },
                )
                return Response(status_code=status.HTTP_204_NO_CONTENT)
        except ValueError:
            pass

    # The pre-check misses a body with no declared length (chunked), so
    # count while reading and stop at the cap.
    raw = await read_capped_body(request, _MAX_BODY_BYTES)
    if raw is None:
        logger.info("security.csp_report.oversized")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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
        logger.info(
            "security.csp_report.unparsed",
            extra={
                "content_type": request.headers.get("content-type", ""),
                "byte_len": len(raw),
            },
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    client_ip = get_client_ip(request)
    request_id = _request_id()

    network = rate_limit_key(request)
    # Identical reports in one body collapse to the first (order kept).
    unique: dict[str, dict[str, Any]] = {}
    repeats: list[dict[str, Any]] = []
    for body in bodies:
        detail = _extract_detail(body) or {}
        # Structured breadcrumb at INFO — only the (bounded) directive,
        # never the full payload or URL-bearing fields.
        logger.info(
            "security.csp_violation",
            extra={
                "violated_directive": detail.get("violated_directive"),
                "effective_directive": detail.get("effective_directive"),
            },
        )
        fp = _fingerprint(network, detail)
        if fp in unique:
            repeats.append(detail)
        else:
            unique[fp] = detail
    _count("duplicate", repeats)

    try:
        within, reserve, dropped = await asyncio.to_thread(
            _admit, network, list(unique.items())
        )
    except SQLAlchemyError as exc:
        # Fail closed: without the counters the sink has no bound.
        _count("counter_unavailable", unique.values())
        logger.warning(
            "security.csp_report.counter_unavailable",
            extra={"error": tracing.error_summary(exc)},
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    admitted = within + reserve
    for outcome, items in dropped.items():
        _count(outcome, (detail for _fp, detail in items))
    if len(admitted) < len(bodies):
        logger.info(
            "security.csp_report.suppressed",
            extra={
                "reports": len(bodies),
                "recorded": len(admitted),
                "reserve": len(reserve),
                "duplicate": len(repeats) + len(dropped["duplicate"]),
                "client_budget": len(dropped["client_budget"]),
                "global_budget": len(dropped["global_budget"]),
            },
        )
    if not admitted:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    try:
        async with session_factory() as session:
            for _fp, detail in admitted:
                audit_service.add_audit_event_to_session(
                    session,
                    event_type=CSP_VIOLATION_EVENT_TYPE,
                    actor_user_id=None,
                    # No authenticated actor on this public route; sentinel
                    # keeps the NOT NULL actor_email column satisfied.
                    actor_email="anonymous",
                    target_org_id=None,
                    target_org_name=None,
                    request_id=request_id,
                    ip_address=client_ip,
                    # Always "failure": a CSP violation is a policy breach.
                    # See the endpoint docstring — audit alerting / default
                    # /admin/audit failure views must scope OUT this
                    # event_type so this high-volume anonymous source
                    # doesn't dilute real admin signal.
                    outcome="failure",
                    detail=detail or None,
                )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — a report never 500s.
        # Nothing was marked seen, so the next identical report is recorded.
        _count("write_failed", (detail for _fp, detail in admitted))
        logger.error(
            "security.csp_report.record_failed",
            extra={"rows": len(admitted), "error": tracing.error_summary(exc)},
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    _count("recorded", (detail for _fp, detail in within))
    _count("reserve", (detail for _fp, detail in reserve))
    try:
        await asyncio.to_thread(
            _mark_recorded,
            [fp for fp, _detail in admitted],
            [_directive(detail) for _fp, detail in reserve],
        )
    except SQLAlchemyError as exc:
        # The rows are written; the worst case is one more duplicate row.
        logger.warning(
            "security.csp_report.counter_unavailable",
            extra={"error": tracing.error_summary(exc)},
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)
