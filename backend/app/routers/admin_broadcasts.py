"""Superadmin email-broadcast router (spec
``2026-07-18-admin-email-broadcast-design.md``).

Mounted at ``/api/v1/admin/broadcasts``. Every endpoint requires
``is_superadmin=True``, mirroring ``admin_announcements.py`` — a broadcast
is platform-level content, not org-scoped, so the gate is the same
``require_superadmin`` dependency rather than the role-based
``orgs.manage`` style.

Every mutating endpoint writes an ``audit_events`` row via
``record_audit_event`` on an independent session (survives a rollback of
the business transaction). Per Ruling 13, audit ``detail`` carries NO
recipient PII — only ``broadcast_id``, ``segment``, counts, and (at most)
the subject. Never a recipient email address, never the rendered body.

The ``POST /{id}/send`` gate is the safety-critical path (spec §API): six
checks, in order, each with a machine-readable ``code`` so the frontend can
render the right message. On pass, materialization + the ``draft->sending``
CAS + ``confirmed_at`` all happen in ONE transaction, which is committed
BEFORE the drain is launched (Ruling 2) — the drain's independent session
must be able to see the freshly materialized ``pending`` rows.
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.email_broadcast import (
    BroadcastStatus,
    EmailBroadcast,
    EmailBroadcastRecipient,
)
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.common import ListEnvelope
from app.schemas.email_broadcast import (
    BroadcastCreate,
    BroadcastResponse,
    BroadcastSendRequest,
    PreviewResponse,
    RecipientResponse,
)
from app.services import audit_service
from app.services.broadcast_service import (
    assert_only_known_tokens,
    build_batch_bodies,
    count_segment,
    delivery_counts,
    delivery_counts_for_broadcasts,
    launch_drain,
    launch_resume,
    materialize_recipients,
    render_email,
)
from app.services.email_service import send_email


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/admin/broadcasts", tags=["admin-broadcasts"])

# Sample first name used to render a human-eyeballable preview. The
# fallback branch (``None``) is exercised separately so an operator can see
# both greeting variants before sending.
_PREVIEW_SAMPLE_FIRST_NAME = "Alex"


def _request_id() -> Optional[str]:
    return structlog.contextvars.get_contextvars().get("request_id")


async def require_superadmin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Dependency gate — 403 if the caller isn't a superadmin.

    Broadcasts are global, cross-org content (Ruling 8), gated above the
    role system, so we lock on ``is_superadmin`` directly rather than
    ``require_permission`` — same rationale as ``admin_announcements``.
    """
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        )
    return current_user


def _audit_detail(row: EmailBroadcast, **extra) -> dict:
    """No recipient PII (Ruling 13): id, segment, counts, subject only."""
    return {
        "broadcast_id": row.id,
        "segment": row.segment,
        "subject": row.subject,
        "total_recipients": row.total_recipients,
        "sent_count": row.sent_count,
        "failed_count": row.failed_count,
        "skipped_count": row.skipped_count,
        **extra,
    }


async def _get_broadcast_or_404(db: AsyncSession, broadcast_id: int) -> EmailBroadcast:
    row = await db.get(EmailBroadcast, broadcast_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Broadcast not found",
        )
    return row


async def _to_response(
    db: AsyncSession,
    row: EmailBroadcast,
    counts: Optional[dict] = None,
) -> BroadcastResponse:
    """Build the response shape, filling in the live ``recipient_count`` plus
    the four derived Mailgun delivery counts (Ruling W9).

    While DRAFT the segment count can still shift, so we show a live
    ``count_segment`` preview; once materialized (``total_recipients`` set),
    that snapshot is the count that actually matters.

    ``counts`` lets a caller pass a precomputed
    ``delivery_counts_for_broadcasts`` bucket (the LIST endpoint, avoiding
    N+1); when omitted, a single-broadcast ``delivery_counts`` query runs.
    """
    if row.total_recipients is not None:
        recipient_count = row.total_recipients
    else:
        recipient_count = await count_segment(db, row.segment)
    if counts is None:
        counts = await delivery_counts(db, row.id)
    data = BroadcastResponse.model_validate(row).model_dump()
    data["recipient_count"] = recipient_count
    data.update(counts)
    return BroadcastResponse(**data)


@router.post("", response_model=BroadcastResponse, status_code=status.HTTP_201_CREATED)
async def create_broadcast(
    body: BroadcastCreate,
    request: Request,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Create a draft broadcast. Response carries a live ``recipient_count``
    preview for the segment (no rows materialized yet). Audit
    ``broadcast.create``."""
    actor_user_id = current_user.id
    actor_email = current_user.email

    try:
        recipient_count = await count_segment(db, body.segment)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "unknown_segment", "message": str(exc)},
        ) from exc

    row = EmailBroadcast(
        subject=body.subject,
        body_template=body.body_template,
        segment=body.segment,
        created_by_user_id=actor_user_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    await audit_service.record_audit_event(
        session_factory,
        event_type="broadcast.create",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=_audit_detail(row),
    )

    await logger.ainfo(
        "broadcast.create", broadcast_id=row.id, segment=row.segment
    )

    data = BroadcastResponse.model_validate(row).model_dump()
    data["recipient_count"] = recipient_count
    return BroadcastResponse(**data)


@router.get("", response_model=ListEnvelope[BroadcastResponse])
async def list_broadcasts(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """List broadcasts, newest first."""
    total = (await db.scalar(select(func.count()).select_from(EmailBroadcast))) or 0
    result = await db.execute(
        select(EmailBroadcast)
        .order_by(EmailBroadcast.created_at.desc(), EmailBroadcast.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = list(result.scalars().all())
    # ONE grouped query for the whole page's delivery counts (Ruling W9),
    # never N per-row queries.
    counts_by_id = await delivery_counts_for_broadcasts(db, [row.id for row in rows])
    items = [
        await _to_response(db, row, counts_by_id.get(row.id))
        for row in rows
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get(
    "/{broadcast_id}/recipients", response_model=ListEnvelope[RecipientResponse]
)
async def list_broadcast_recipients(
    broadcast_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Per-recipient rows for one broadcast, incl. ``delivery_status``
    (Ruling W9) — so an operator can see WHICH addresses bounced/complained
    and clean up dead accounts. Superadmin-gated (Ruling W10); the address
    already lives on the recipient row, so this adds no new PII sink."""
    await _get_broadcast_or_404(db, broadcast_id)
    total = (
        await db.scalar(
            select(func.count())
            .select_from(EmailBroadcastRecipient)
            .where(EmailBroadcastRecipient.broadcast_id == broadcast_id)
        )
    ) or 0
    result = await db.execute(
        select(EmailBroadcastRecipient)
        .where(EmailBroadcastRecipient.broadcast_id == broadcast_id)
        .order_by(EmailBroadcastRecipient.id)
        .limit(limit)
        .offset(offset)
    )
    rows = list(result.scalars().all())
    items = [RecipientResponse.model_validate(row) for row in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{broadcast_id}", response_model=BroadcastResponse)
async def get_broadcast(
    broadcast_id: int,
    _current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Status + counts, for progress polling."""
    row = await _get_broadcast_or_404(db, broadcast_id)
    return await _to_response(db, row)


@router.get("/{broadcast_id}/preview", response_model=PreviewResponse)
async def preview_broadcast(
    broadcast_id: int,
    _current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Render a sample preview (a fixed sample name plus the ``there``
    fallback aren't both returned here — the sample-name render is the one
    an operator eyeballs before dry-running). No side effects."""
    row = await _get_broadcast_or_404(db, broadcast_id)
    html_out, text_out = render_email(row.body_template, _PREVIEW_SAMPLE_FIRST_NAME)
    return PreviewResponse(subject=row.subject, html=html_out, text=text_out)


@router.post("/{broadcast_id}/dry-run", response_model=BroadcastResponse)
async def dry_run_broadcast(
    broadcast_id: int,
    request: Request,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Render and send the email to the calling superadmin's own address
    only. Stamps ``dry_run_sent_at`` (the mandatory pre-send gate). Audit
    ``broadcast.dry_run``.

    Guarded to ``draft`` only: a broadcast that has already moved past
    draft (``sending``/``completed``/``failed``) must not be re-rendered
    and re-sent to the operator's own inbox, and dry-running it wouldn't
    mean anything post-send anyway.
    """
    actor_user_id = current_user.id
    actor_email = current_user.email

    row = await _get_broadcast_or_404(db, broadcast_id)

    if row.status != BroadcastStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "broadcast_not_draft"},
        )

    html_out, text_out = render_email(row.body_template, current_user.first_name)
    await send_email(current_user.email, row.subject, html_out, text_out)

    await db.execute(
        update(EmailBroadcast)
        .where(EmailBroadcast.id == broadcast_id)
        .values(dry_run_sent_at=func.now())
    )
    await db.commit()
    await db.refresh(row)

    await audit_service.record_audit_event(
        session_factory,
        event_type="broadcast.dry_run",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=_audit_detail(row),
    )

    await logger.ainfo("broadcast.dry_run", broadcast_id=row.id)
    return await _to_response(db, row)


@router.post("/{broadcast_id}/send", response_model=BroadcastResponse)
async def send_broadcast(
    broadcast_id: int,
    body: BroadcastSendRequest,
    request: Request,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """The guarded send trigger (spec §API, 5 checks in order):

    1. status is ``draft`` (else 409 ``broadcast_not_draft``)
    2. ``dry_run_sent_at`` is set (else 422 ``dry_run_required``)
    3. ``confirm_subject`` matches exactly (else 422 ``confirm_subject_mismatch``)
    4. ``confirm_recipient_count`` matches the freshly recomputed segment
       count (else 422 ``confirm_count_mismatch``)
    5. the recomputed count is within ``broadcast_max_recipients`` (else 422
       ``recipient_cap_exceeded``)
    6. the subject + body survive the MA1 Mailgun-token guard (else 422
       ``invalid_template_token``) — checked here so a stray ``%`` fails the
       request synchronously instead of failing the background drain

    On pass, in one transaction: FIRST flip ``draft -> sending`` via a
    conditional ``UPDATE`` checked for ``rowcount == 1`` (Ruling 2,
    CAS-before-materialize) — a concurrent double-send loses this race and
    409s here, before touching the recipients table at all, so it can never
    double-``materialize_recipients`` into the ``(broadcast_id, user_id)``
    unique constraint (the old order's IntegrityError-turned-500). Only the
    winner proceeds to materialize recipients (which sets
    ``total_recipients``), stamp ``confirmed_at``, and commit. Only THEN is
    ``launch_drain`` called — after commit, so the drain's own session can
    see the newly materialized ``pending`` rows. Audit ``broadcast.send``.
    """
    actor_user_id = current_user.id
    actor_email = current_user.email

    row = await _get_broadcast_or_404(db, broadcast_id)

    # (1) lifecycle state
    if row.status != BroadcastStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "broadcast_not_draft"},
        )

    # (2) mandatory dry-run
    if row.dry_run_sent_at is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "dry_run_required"},
        )

    # (3) typed subject confirm
    if body.confirm_subject != row.subject:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "confirm_subject_mismatch"},
        )

    # (4) typed count confirm, against a FRESH recompute (no drift between
    # confirm and materialization).
    recomputed_count = await count_segment(db, row.segment)
    if body.confirm_recipient_count != recomputed_count:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "confirm_count_mismatch"},
        )

    # (5) hard backstop cap
    if recomputed_count > settings.broadcast_max_recipients:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "recipient_cap_exceeded"},
        )

    # (6) Template must survive the MA1 Mailgun-token guard BEFORE we
    # materialize or claim anything. The drain builds the tokenized bodies
    # itself and raises on a stray ``%`` / unknown ``%recipient.X%``, but it
    # does so in the background — the operator would get a 200 with status
    # ``sending`` and only later see the broadcast flip to ``failed``. Doing
    # the identical check synchronously here fails fast with a 422 and leaves
    # the broadcast in ``draft``, so the copy can just be fixed and re-sent.
    try:
        build_batch_bodies(row.body_template)
        assert_only_known_tokens(row.subject)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_template_token"},
        )

    # All checks passed. CAS the row draft -> sending FIRST (Ruling 2): only
    # the winner of a concurrent race ever proceeds to materialize. The
    # loser 409s here, before any recipient row is inserted, so a
    # concurrent double-send can never double-materialize into the
    # recipients table's (broadcast_id, user_id) unique constraint.
    result = await db.execute(
        update(EmailBroadcast)
        .where(
            EmailBroadcast.id == broadcast_id,
            EmailBroadcast.status == BroadcastStatus.DRAFT,
        )
        .values(status=BroadcastStatus.SENDING, started_at=func.now())
    )
    if (result.rowcount or 0) != 1:
        # Another concurrent send won the race between our status check
        # above and this CAS. Roll back (nothing else has been touched
        # yet) and report the same 409 as a stale/duplicate send attempt
        # (Ruling 3a).
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "broadcast_not_draft"},
        )

    # Only the CAS winner reaches here: materialize + stamp confirmed_at,
    # same transaction as the CAS, then commit.
    await materialize_recipients(db, row)

    await db.execute(
        update(EmailBroadcast)
        .where(EmailBroadcast.id == broadcast_id)
        .values(confirmed_at=func.now())
    )
    await db.commit()
    await db.refresh(row)

    # Launch the drain AFTER commit (Ruling 2): the drain's independent
    # session must see the materialized `pending` rows.
    launch_drain(session_factory, broadcast_id)

    await audit_service.record_audit_event(
        session_factory,
        event_type="broadcast.send",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=_audit_detail(row),
    )

    await logger.ainfo(
        "broadcast.send",
        broadcast_id=row.id,
        total_recipients=row.total_recipients,
    )
    return await _to_response(db, row)


@router.post("/{broadcast_id}/resume", response_model=BroadcastResponse)
async def resume_broadcast(
    broadcast_id: int,
    request: Request,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Re-launch the drain for any ``pending``/retryable rows left by a
    restart or partial run. Idempotent (the in-process registry no-ops a
    second concurrent launch). Audit ``broadcast.resume``."""
    actor_user_id = current_user.id
    actor_email = current_user.email

    row = await _get_broadcast_or_404(db, broadcast_id)

    launch_resume(session_factory, broadcast_id)

    await audit_service.record_audit_event(
        session_factory,
        event_type="broadcast.resume",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=_audit_detail(row),
    )

    await logger.ainfo("broadcast.resume", broadcast_id=row.id)
    return await _to_response(db, row)
