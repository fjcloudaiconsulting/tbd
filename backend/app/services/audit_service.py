"""Audit-event recording and querying (L4.7).

The recording path uses an **independent session** opened from the
engine-wide ``async_sessionmaker`` so the audit write is a separate
transaction from whatever business operation triggered it. Two
properties this gives us:

- A failed business txn (e.g. ``admin.org.delete.failed``) still
  produces an audit row, because the audit write doesn't ride on
  the rolled-back session.
- A failed audit write (DB transient, FK violation, anything) never
  surfaces back to the caller. We log the failure via structlog and
  swallow — the structlog event the caller already emitted is the
  fallback channel.

Caller responsibilities:

- Pass the ``async_sessionmaker`` (not a session). Inject via
  ``Depends(get_session_factory)`` in routers.
- Call **after** ``await db.commit()`` (or after the rollback path)
  so the snapshot fields reflect the state the audit row should
  attest to.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit_event import AuditEvent, AuditOutcome
from app.services.list_query import resolve_order_by


logger = structlog.stdlib.get_logger()


# Closed whitelist of sortable columns for the admin audit list. Keys are
# the public sort tokens the frontend sends; values are the column to
# order by. Anything not here is a 400 (see ``list_query.resolve_order_by``).
#
# ``api_token_id`` is deliberately ABSENT (TBD-188 §5): you filter by a
# token id, you never order by one. ``_SORTABLE``'s keys are the frontend's
# sort tokens, so an entry with no ``SortableHeader`` behind it is dead
# surface on a closed whitelist. Adding it must be a deliberate act.
_SORTABLE = {
    "created_at": AuditEvent.created_at,
    "event_type": AuditEvent.event_type,
    "outcome": AuditEvent.outcome,
    "actor_email": AuditEvent.actor_email,
    "target_org_name": AuditEvent.target_org_name,
}


def _acting_api_token_id() -> Optional[int]:
    """The API token presented as the credential for the current request.

    Read from the request-scoped structlog contextvars, bound exactly once
    in ``app.auth.pat.authenticate_pat``. This is an *ambient* read on
    purpose (TBD-188 §2): threading a kwarg through the 108 audit call
    sites has a silent failure mode — a forgotten site is permanently NULL
    with CI green, and a call site added next quarter is NULL by default.
    Reading here inverts that: a new audit call site is correct by
    construction.

    ``None`` for every non-PAT request (interactive JWT, pre-auth /
    anonymous, and scheduler tasks whose lifespan-spawned context snapshot
    is empty). ``RequestContextMiddleware`` is pure-ASGI and calls
    ``clear_contextvars()`` per request, so nothing bleeds across requests.
    """
    return structlog.contextvars.get_contextvars().get("api_token_id")


def _build_audit_event(
    *,
    event_type: str,
    actor_user_id: Optional[int],
    actor_email: str,
    target_org_id: Optional[int],
    target_org_name: Optional[str],
    request_id: Optional[str],
    ip_address: Optional[str],
    outcome: Literal["success", "failure"],
    detail: Optional[dict[str, Any]] = None,
) -> AuditEvent:
    return AuditEvent(
        event_type=event_type,
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=target_org_id,
        target_org_name=target_org_name,
        request_id=request_id,
        ip_address=ip_address,
        outcome=AuditOutcome(outcome),
        detail=detail,
        # NOT a parameter — resolved from the request context so all 108
        # call sites are covered without call-site churn. See
        # ``_acting_api_token_id``.
        api_token_id=_acting_api_token_id(),
    )


async def record_audit_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_type: str,
    actor_user_id: Optional[int],
    actor_email: str,
    target_org_id: Optional[int],
    target_org_name: Optional[str],
    request_id: Optional[str],
    ip_address: Optional[str],
    outcome: Literal["success", "failure"],
    detail: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """Persist an audit event in its OWN transaction.

    Returns the newly persisted audit row's id on success, or
    ``None`` when the write failed (failure is logged via structlog
    and swallowed — this function never raises). Callers that don't
    need the id can keep ignoring the return value.

    The return value is the substrate for the notification system's
    forensic correlation (G5 in the 2nd-arch delta): notification
    rows carry the ``audit_event_id`` of the row that triggered them
    so an operator can correlate the two even when the business
    event_type alone is ambiguous.

    Use this when the audit write must succeed regardless of the
    business txn outcome (e.g. a failed delete still needs an audit
    row even though the business txn rolled back).

    For the inverse case — audit row should commit if-and-only-if
    the business txn commits — use ``add_audit_event_to_session``
    on the request-scoped session instead. That pattern is used for
    org deletion: the audit row carries a snapshot of the org's
    identifying fields, the FK to organizations is ON DELETE SET
    NULL, and writing in the same txn before the delete means a
    cascade-failure rolls back the audit row too (no orphan audit
    rows for non-deletes).
    """
    try:
        async with session_factory() as session:
            row = _build_audit_event(
                event_type=event_type,
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                target_org_id=target_org_id,
                target_org_name=target_org_name,
                request_id=request_id,
                ip_address=ip_address,
                outcome=outcome,
                detail=detail,
            )
            session.add(row)
            await session.commit()
            return row.id
    except Exception as exc:  # noqa: BLE001 — defensive: never bubble.
        # Kept as a backstop. Logged at ERROR so a regression in a
        # caller that doesn't pre-snapshot (like the original
        # org-delete path) surfaces immediately. After PR-C the
        # org-delete success path stages its audit row in the
        # business txn via add_audit_event_to_session, so any
        # audit.record.failed signal here is a genuine problem
        # worth alerting on.
        await logger.aerror(
            "audit.record.failed",
            event_type=event_type,
            actor_user_id=actor_user_id,
            target_org_id=target_org_id,
            outcome=outcome,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None


def add_audit_event_to_session(
    session: AsyncSession,
    *,
    event_type: str,
    actor_user_id: Optional[int],
    actor_email: str,
    target_org_id: Optional[int],
    target_org_name: Optional[str],
    request_id: Optional[str],
    ip_address: Optional[str],
    outcome: Literal["success", "failure"],
    detail: Optional[dict[str, Any]] = None,
) -> AuditEvent:
    """Stage an audit-event row on the caller's session so it commits
    in the SAME transaction as the business write. Use for the
    org-delete success path (the row should exist iff the delete
    commits) and other cases where the audit row's correctness
    depends on the business txn.

    Returns the staged AuditEvent so the caller can assert on it
    before commit if useful.
    """
    row = _build_audit_event(
        event_type=event_type,
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=target_org_id,
        target_org_name=target_org_name,
        request_id=request_id,
        ip_address=ip_address,
        outcome=outcome,
        detail=detail,
    )
    session.add(row)
    return row


# ⚠ TBD-439. Defined HERE, not in ``routers/security.py``, even though that
# router is the only writer. The consumer-side exclusion below has to name the
# same string, and a service importing a constant from a router inverts the
# layering. ``security.py`` imports it from here and re-exports it, so
# ``from app.routers.security import CSP_VIOLATION_EVENT_TYPE`` still resolves.
CSP_VIOLATION_EVENT_TYPE = "security.csp_violation"


async def list_audit_events(
    db: AsyncSession,
    *,
    actor_user_id: Optional[int] = None,
    target_org_id: Optional[int] = None,
    api_token_id: Optional[int] = None,
    event_type: Optional[str] = None,
    outcome: Optional[str] = None,
    from_dt: Optional[datetime] = None,
    to_dt: Optional[datetime] = None,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditEvent], int]:
    """Return ``(rows, total)`` for the admin audit table.

    Default ordering is ``created_at DESC`` (then id DESC for stable
    sort across same-timestamp events). ``sort_by`` is resolved against
    a closed whitelist (see ``_SORTABLE``); an unknown key raises
    ``ValidationError`` (router → 400). ``id`` desc is always appended as
    the stable tiebreaker.
    """
    where = []
    if actor_user_id is not None:
        where.append(AuditEvent.actor_user_id == actor_user_id)
    if target_org_id is not None:
        where.append(AuditEvent.target_org_id == target_org_id)
    if api_token_id is not None:
        # "Everything this token did" — the query ix_audit_events_api_token_id
        # exists for. Matches the ACTING credential only; a row that merely
        # names the token in ``detail`` (api_token.created / .revoked) is
        # correctly excluded.
        where.append(AuditEvent.api_token_id == api_token_id)
    if event_type:
        where.append(AuditEvent.event_type == event_type)
    else:
        # ⚠⚠ TBD-439. ``POST /api/v1/security/csp-report`` is public, takes no
        # credential, and writes one audit row per report body — 20 per
        # request at 60/minute, so 1200 rows/min from a single anonymous IP.
        # Ten such requests bury page 1 of /admin/audit entirely, every row
        # ``outcome=failure`` and ``actor_email=anonymous``.
        #
        # ``routers/security.py``'s own docstring has always said the
        # mitigation is consumer-side -- that alerting and the default audit
        # views "MUST scope OUT" this event type. Until now nothing did: the
        # string appeared ONLY in that router, never here.
        #
        # Excluded from the DEFAULT view only. Asking for the stream by name
        # (``?event_type=security.csp_violation``) still returns it, which is
        # what makes this a default rather than a censor.
        #
        # ⚠ Appended to ``where``, which feeds BOTH the row query and the
        # count query below. Excluding from the rows alone would leave
        # ``total`` counting the hidden rows, so the table would report a
        # page count it cannot produce.
        #
        # ⚠ ``event_type`` is ``nullable=False`` (models/audit_event.py:263),
        # so a bare ``!=`` cannot silently drop NULL rows the way it would on
        # a nullable column.
        where.append(AuditEvent.event_type != CSP_VIOLATION_EVENT_TYPE)
    if outcome:
        # The HTTP route now types this as Literal["success", "failure"]
        # so FastAPI returns 422 for typos before this branch runs.
        # Direct service callers (and tests) still pass strings, so
        # raise ValueError on bad input rather than silently
        # unfiltering — that's the bug we're closing.
        outcome_enum = AuditOutcome(outcome)
        where.append(AuditEvent.outcome == outcome_enum)
    if from_dt is not None:
        where.append(AuditEvent.created_at >= from_dt)
    if to_dt is not None:
        where.append(AuditEvent.created_at <= to_dt)

    base = select(AuditEvent)
    count_q = select(func.count()).select_from(AuditEvent)
    for clause in where:
        base = base.where(clause)
        count_q = count_q.where(clause)

    total = (await db.execute(count_q)).scalar_one()

    order_by = resolve_order_by(
        sort_by,
        sort_dir,
        allowed=_SORTABLE,
        default_key="created_at",
        default_dir="desc",
        tiebreaker=AuditEvent.id.desc(),
    )

    rows_result = await db.execute(
        base.order_by(*order_by)
        .limit(limit)
        .offset(offset)
    )
    rows = list(rows_result.scalars().all())
    return rows, total
