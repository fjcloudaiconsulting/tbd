"""Superadmin email broadcast service (spec ``2026-07-18-admin-email-broadcast-design.md``).

This module grows across Tasks 2-4 of the implementation plan.

- ``count_segment`` — live COUNT for a segment, used both for the
  draft's advertised ``recipient_count`` and for the send-time
  recipient-cap check.
- ``iter_segment_users`` — the rows materialization will snapshot into
  ``email_broadcast_recipients`` in a later task.
- ``render_email`` (Task 3) — per-recipient HTML + text render of the
  admin-authored ``body_template``.
- ``materialize_recipients`` (Task 3) — snapshots the segment into
  ``EmailBroadcastRecipient`` rows at send time.

``active_verified`` is, per Ruling 10, the only segment v1 accepts —
any other value is an app-level ``ValueError`` before it ever reaches
the DB (there is no promotional/re-engagement audience without an
unsubscribe + suppression mechanism first).
"""
from __future__ import annotations

import asyncio
import html
from collections.abc import Sequence

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.email_broadcast import (
    SEGMENT_ACTIVE_VERIFIED,
    BroadcastStatus,
    EmailBroadcast,
    EmailBroadcastRecipient,
    RecipientStatus,
)
from app.models.user import User
from app.services.email_service import send_email

logger = structlog.get_logger()

# Static account-context footer for every broadcast email (Ruling 11 /
# spec "Email content"). No user-controlled content, so no escaping needed,
# but it is still routed through both the HTML and text renders verbatim.
_FOOTER_TEXT = "You're receiving this because you have a The Better Decision account."


def _require_known_segment(segment: str) -> None:
    if segment != SEGMENT_ACTIVE_VERIFIED:
        raise ValueError(f"unknown broadcast segment: {segment!r}")


async def count_segment(db: AsyncSession, segment: str) -> int:
    """Return the live count of users targeted by ``segment``.

    Raises ``ValueError`` for any segment other than
    ``SEGMENT_ACTIVE_VERIFIED`` (Ruling 10).
    """
    _require_known_segment(segment)
    result = await db.execute(
        select(func.count())
        .select_from(User)
        .where(User.is_active.is_(True), User.email_verified.is_(True))
    )
    return int(result.scalar_one())


async def iter_segment_users(
    db: AsyncSession, segment: str
) -> Sequence[tuple[int, str, str | None]]:
    """Return ``(user_id, email, first_name)`` for every user in ``segment``.

    Materialization (a later task) snapshots these tuples into
    ``EmailBroadcastRecipient`` rows. Raises ``ValueError`` for any
    segment other than ``SEGMENT_ACTIVE_VERIFIED`` (Ruling 10).
    """
    _require_known_segment(segment)
    result = await db.execute(
        select(User.id, User.email, User.first_name)
        .where(User.is_active.is_(True), User.email_verified.is_(True))
        .order_by(User.id)
    )
    return [tuple(row) for row in result.all()]


def render_email(body_template: str, first_name: str | None) -> tuple[str, str]:
    """Render one recipient's broadcast email as ``(html, text)``.

    ``body_template`` is admin-authored copy that may contain the literal
    ``{first_name}`` token. Substitution goes through ``str.replace`` —
    **never** ``str.format()`` (Ruling 11) — so a stray ``{`` or ``}``
    elsewhere in the operator's copy can't raise ``KeyError`` or open a
    format-string vector.

    The greeting name falls back to ``"there"`` when the recipient has no
    first name on file. Both the name and the body template are
    HTML-escaped for the HTML path, since ``body_template`` is
    admin-authored free text, not a trusted literal; the text path keeps
    everything raw. A static account-context footer is appended to both.
    """
    name = first_name or "there"

    # Text path: raw name, raw body, plain footer.
    text_body = body_template.replace("{first_name}", name)
    text = f"{text_body}\n\n{_FOOTER_TEXT}"

    # HTML path: escape the body template as literal text FIRST (html.escape
    # leaves braces untouched, so the {first_name} token survives intact),
    # then substitute in the escaped name. This way user-controlled and
    # admin-authored content are both escaped before ever reaching the
    # shell markup below.
    name_safe = html.escape(name)
    body_html = html.escape(body_template).replace("{first_name}", name_safe)
    html_out = (
        "<html><body>"
        f"<p>{body_html}</p>"
        "<hr>"
        f"<p>{_FOOTER_TEXT}</p>"
        "</body></html>"
    )
    return html_out, text


async def materialize_recipients(db: AsyncSession, broadcast: EmailBroadcast) -> int:
    """Snapshot the broadcast's segment into ``PENDING`` recipient rows.

    Reads ``(user_id, email, first_name)`` via ``iter_segment_users`` for
    ``broadcast.segment`` and inserts one ``EmailBroadcastRecipient`` per
    user with ``status=PENDING``. Sets ``broadcast.total_recipients`` to
    the count and returns it. Does not commit; the caller commits.
    """
    rows = await iter_segment_users(db, broadcast.segment)
    for user_id, email, first_name in rows:
        db.add(
            EmailBroadcastRecipient(
                broadcast_id=broadcast.id,
                user_id=user_id,
                email=email,
                first_name=first_name,
                status=RecipientStatus.PENDING,
            )
        )
    broadcast.total_recipients = len(rows)
    return len(rows)


# ─── Send-drain engine (Task 4, spec §"Send execution") ───
#
# Atomic-claim design (Ruling 3). There is no per-row "sending" marker in
# ``RecipientStatus``, so a recipient cannot be moved out of ``pending``
# *before* its send outcome is known. Two guards give the no-double-send
# guarantee instead:
#
#   1. PRIMARY mutual-exclusion — the in-process ``_ACTIVE_DRAINS`` registry
#      (Ruling 3b). ``instance_count: 1`` in prod means at most one event loop
#      ever runs a drain for a given broadcast id, and ``launch_drain`` makes a
#      second concurrent launch for the same id an idempotent no-op. This is
#      what actually prevents a recipient being sent twice.
#   2. BACKSTOP double-count guard — every row-finalizing UPDATE is conditional
#      on ``status == 'pending'`` and proceeds only when ``rowcount == 1``
#      (Ruling 3c). Even if the process guard were ever bypassed (e.g. a future
#      multi-instance change), only the drain that wins the conditional UPDATE
#      increments the broadcast counters, so counts can never be inflated.
#
# The known residual window is bypass-only: if two drains somehow ran the same
# id concurrently, both could call ``send_email`` before either finalized the
# row. On a single instance that window does not exist, and the conditional
# UPDATE keeps the counters correct regardless. We favour never-double-count
# and rely on the registry for never-double-send, which is sound under the
# locked ``instance_count: 1`` deployment.

# Broadcast ids with a live in-process drain (Ruling 3b). A second launch for
# an id already draining is a no-op.
_ACTIVE_DRAINS: set[int] = set()
# Strong references to the tracked drain tasks so the GC cannot collect a task
# mid-flight (Ruling 1). The done-callback discards from both sets.
_DRAIN_TASKS: set[asyncio.Task] = set()


def launch_drain(
    session_factory: async_sessionmaker[AsyncSession], broadcast_id: int
) -> None:
    """Launch a tracked background drain for ``broadcast_id`` (Ruling 1).

    No-op if a drain for this id is already live in this process (Ruling 3b):
    the registry is the primary mutual-exclusion guard. Otherwise a tracked
    ``asyncio.Task`` is created (strong ref held in ``_DRAIN_TASKS``) with a
    done-callback that logs any exception via structlog and discards the task
    from both module sets, so a failure is observed rather than silently
    swallowed. Callers MUST launch this only AFTER the materialization
    transaction has committed (Ruling 2), so the drain's own session sees the
    ``pending`` rows.
    """
    if broadcast_id in _ACTIVE_DRAINS:
        return
    _ACTIVE_DRAINS.add(broadcast_id)
    task = asyncio.create_task(_drain(session_factory, broadcast_id))
    _DRAIN_TASKS.add(task)

    def _on_done(finished: asyncio.Task) -> None:
        _DRAIN_TASKS.discard(finished)
        _ACTIVE_DRAINS.discard(broadcast_id)
        if finished.cancelled():
            logger.warning("broadcast_drain_cancelled", broadcast_id=broadcast_id)
            return
        exc = finished.exception()
        if exc is not None:
            logger.error(
                "broadcast_drain_failed",
                broadcast_id=broadcast_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    task.add_done_callback(_on_done)


async def _user_still_targetable(db: AsyncSession, user_id: int | None) -> bool:
    """Return True if ``user_id`` is still active + verified (Ruling 9)."""
    if user_id is None:
        return False
    result = await db.execute(
        select(User.id).where(
            User.id == user_id,
            User.is_active.is_(True),
            User.email_verified.is_(True),
        )
    )
    return result.scalar_one_or_none() is not None


async def _claim_recipient(
    db: AsyncSession,
    recipient_id: int,
    new_status: RecipientStatus,
    *,
    error: str | None,
    set_sent_at: bool,
) -> bool:
    """Atomically move a recipient out of ``pending`` into ``new_status``.

    The UPDATE is conditional on ``status == 'pending'`` (Ruling 3c); returns
    True only when it claimed the row (``rowcount == 1``). A False return means
    another drain already finalized this row, so the caller must not double
    count it.
    """
    values: dict = {
        "status": new_status,
        "attempts": EmailBroadcastRecipient.attempts + 1,
        "error": error,
    }
    if set_sent_at:
        values["sent_at"] = func.now()
    result = await db.execute(
        update(EmailBroadcastRecipient)
        .where(
            EmailBroadcastRecipient.id == recipient_id,
            EmailBroadcastRecipient.status == RecipientStatus.PENDING,
        )
        .values(**values)
    )
    return (result.rowcount or 0) == 1


async def _bump_broadcast_counter(
    db: AsyncSession, broadcast_id: int, field: str
) -> None:
    """Increment one of the broadcast's ``sent/failed/skipped`` counters."""
    column_name = {
        "sent": "sent_count",
        "failed": "failed_count",
        "skipped": "skipped_count",
    }[field]
    column = getattr(EmailBroadcast, column_name)
    await db.execute(
        update(EmailBroadcast)
        .where(EmailBroadcast.id == broadcast_id)
        .values({column_name: column + 1})
    )


async def _run_drain_loop(
    db: AsyncSession, broadcast_id: int, *, only_retryable: bool
) -> None:
    """Drain every eligible ``pending`` recipient of ``broadcast_id``.

    Commits per row so progress is durable across a crash. One recipient's
    send failure never halts the batch (Ruling 12): a falsy/raising
    ``send_email`` marks that row ``failed`` and the loop continues.
    """
    broadcast = (
        await db.execute(
            select(EmailBroadcast).where(EmailBroadcast.id == broadcast_id)
        )
    ).scalar_one()
    subject = broadcast.subject
    body_template = broadcast.body_template

    conditions = [
        EmailBroadcastRecipient.broadcast_id == broadcast_id,
        EmailBroadcastRecipient.status == RecipientStatus.PENDING,
    ]
    if only_retryable:
        # ``resume`` retries only rows below the attempts cap (Ruling 12) so a
        # permanently bad recipient is not hammered on every resume.
        conditions.append(
            EmailBroadcastRecipient.attempts < settings.broadcast_max_attempts
        )
    pending_ids = [
        row[0]
        for row in (
            await db.execute(
                select(EmailBroadcastRecipient.id)
                .where(*conditions)
                .order_by(EmailBroadcastRecipient.id)
            )
        ).all()
    ]

    for recipient_id in pending_ids:
        # Re-read the row's current snapshot; another drain (or a prior
        # partial run) may have finalized it since we listed the ids.
        row = (
            await db.execute(
                select(
                    EmailBroadcastRecipient.status,
                    EmailBroadcastRecipient.user_id,
                    EmailBroadcastRecipient.email,
                    EmailBroadcastRecipient.first_name,
                ).where(EmailBroadcastRecipient.id == recipient_id)
            )
        ).first()
        if row is None:
            continue
        status, user_id, email, first_name = row
        if status != RecipientStatus.PENDING:
            continue

        # (1) Segment re-check at send time (Ruling 9): skip a user who lapsed
        # (deactivated / unverified) after materialization.
        if not await _user_still_targetable(db, user_id):
            if await _claim_recipient(
                db,
                recipient_id,
                RecipientStatus.SKIPPED,
                error="user no longer active and verified",
                set_sent_at=False,
            ):
                await _bump_broadcast_counter(db, broadcast_id, "skipped")
                await db.commit()
            else:
                await db.rollback()
            continue

        # (2) Render per-recipient (escaped for HTML, Ruling 11). A render
        # error is NOT swallowed per-row: it propagates to the drain-level
        # wrapper which sets status=FAILED and re-raises for the done-callback.
        body_html, body_text = render_email(body_template, first_name)

        # (3) Send. Key SENT/FAILED off the return bool (Ruling 12); still
        # try/except defensively so a raising send_email fails just this row.
        try:
            sent_ok = await send_email(email, subject, body_html, body_text)
            send_error = None if sent_ok else "send_email returned a falsy result"
        except Exception as exc:  # noqa: BLE001 - one row's failure must not halt the batch
            sent_ok = False
            send_error = f"send_email raised: {exc}"

        outcome = RecipientStatus.SENT if sent_ok else RecipientStatus.FAILED
        # (4) Atomic claim (Ruling 3c): finalize only if still pending.
        if await _claim_recipient(
            db,
            recipient_id,
            outcome,
            error=send_error,
            set_sent_at=sent_ok,
        ):
            await _bump_broadcast_counter(
                db, broadcast_id, "sent" if sent_ok else "failed"
            )
            await db.commit()
        else:
            await db.rollback()

        # (5) Pace between sends (env ``broadcast_pacing_seconds``).
        await asyncio.sleep(settings.broadcast_pacing_seconds)

    # When no ``pending`` rows remain at all, the broadcast is done (even with
    # some ``failed``/``skipped``). Rows still pending (e.g. a resume that left
    # attempts-capped rows) keep the broadcast in ``sending``.
    remaining_pending = (
        await db.execute(
            select(func.count())
            .select_from(EmailBroadcastRecipient)
            .where(
                EmailBroadcastRecipient.broadcast_id == broadcast_id,
                EmailBroadcastRecipient.status == RecipientStatus.PENDING,
            )
        )
    ).scalar_one()
    if remaining_pending == 0:
        await db.execute(
            update(EmailBroadcast)
            .where(EmailBroadcast.id == broadcast_id)
            .values(status=BroadcastStatus.COMPLETED, completed_at=func.now())
        )
        await db.commit()


async def _drain_with_wrapper(
    session_factory: async_sessionmaker[AsyncSession],
    broadcast_id: int,
    *,
    only_retryable: bool,
) -> None:
    """Open an OWN session and run the drain, wrapping the whole body so an
    unhandled error sets ``status=FAILED`` and re-raises (Ruling 1) — the
    done-callback then observes and logs it. The drain's session is always
    separate from any request session (Ruling 2)."""
    async with session_factory() as db:
        try:
            await _run_drain_loop(db, broadcast_id, only_retryable=only_retryable)
        except Exception:
            await db.rollback()
            try:
                await db.execute(
                    update(EmailBroadcast)
                    .where(EmailBroadcast.id == broadcast_id)
                    .values(status=BroadcastStatus.FAILED)
                )
                await db.commit()
            except Exception:  # noqa: BLE001 - best-effort status flip; re-raise original below
                await db.rollback()
            raise


async def _drain(
    session_factory: async_sessionmaker[AsyncSession], broadcast_id: int
) -> None:
    """Drain all ``pending`` recipients of ``broadcast_id`` (fresh send)."""
    await _drain_with_wrapper(session_factory, broadcast_id, only_retryable=False)


async def resume_pending(
    session_factory: async_sessionmaker[AsyncSession], broadcast_id: int
) -> None:
    """Re-drain a broadcast, retrying only ``pending`` rows still below the
    attempts cap (Ruling 12). Idempotent: safe to call after a restart or a
    partial run left rows ``pending``."""
    await _drain_with_wrapper(session_factory, broadcast_id, only_retryable=True)
