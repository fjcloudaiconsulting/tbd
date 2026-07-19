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
- ``build_batch_bodies`` / ``build_recipient_variables`` (2026-07-19
  batch-sending revision, MA1/MA2) — the Mailgun batch-send primitives:
  translate ``body_template`` into ``%recipient.*%``-tokenized HTML/text
  and build the per-recipient ``recipient-variables`` map. ``render_email``
  is unchanged and stays the dry-run renderer AND the batch-parity oracle
  (MA3).

``active_verified`` is, per Ruling 10, the only segment v1 accepts —
any other value is an app-level ``ValueError`` before it ever reaches
the DB (there is no promotional/re-engagement audience without an
unsubscribe + suppression mechanism first).
"""
from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Sequence

import structlog
from sqlalchemy import and_, func, select, update
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


# ─── Batch-sending primitives (2026-07-19 revision, MA1/MA2) ───
#
# Mailgun batch sending substitutes ``recipient-variables`` across the WHOLE
# payload (not just inside a designated placeholder), so a stray literal
# ``%`` anywhere in operator copy, the shared shell, or the footer is a
# hazard: Mailgun would either leave it alone (harmless) or, if it happens
# to match ``%recipient.<key>%`` for a key we didn't populate, drop the
# token silently. ``_assert_only_known_tokens`` is the send-time guard
# (MA1): it raises before anything reaches Mailgun.

_RECIPIENT_TOKEN_RE = re.compile(r"%recipient\.\w+%")
_KNOWN_RECIPIENT_TOKENS = frozenset(
    {"%recipient.first_name_html%", "%recipient.first_name_text%"}
)


def _assert_only_known_tokens(payload: str) -> None:
    """Raise ``ValueError`` if ``payload`` carries any ``%...%`` sequence
    other than the two known Mailgun recipient tokens (MA1).

    Two checks: (1) every ``%recipient.<word>%``-shaped match must be one
    of the two known tokens — an unexpected one (a typo, or a future
    variable we didn't populate) is refused outright; (2) after removing
    the known-token matches, no bare ``%`` may remain — that would be a
    stray operator ``%`` (e.g. "50% off") that Mailgun's substitution pass
    could otherwise mishandle.
    """
    for match in _RECIPIENT_TOKEN_RE.finditer(payload):
        token = match.group(0)
        if token not in _KNOWN_RECIPIENT_TOKENS:
            raise ValueError(
                "broadcast body contains an unexpected Mailgun recipient "
                f"token {token!r}; only {sorted(_KNOWN_RECIPIENT_TOKENS)} "
                "are populated in recipient-variables"
            )
    stray = _RECIPIENT_TOKEN_RE.sub("", payload)
    if "%" in stray:
        raise ValueError(
            "broadcast body contains a stray '%' character; Mailgun batch "
            "sending substitutes recipient-variables across the whole "
            "payload, so a literal '%' in operator copy is a hazard — "
            "rephrase or escape it before sending"
        )


def build_batch_bodies(body_template: str) -> tuple[str, str]:
    """Build the ``(html, text)`` bodies for ONE Mailgun batch-sending call
    (MA1), carrying Mailgun recipient tokens instead of a rendered name.

    The operator still authors with the literal ``{first_name}`` token
    (same ``body_template`` as ``render_email``). Here we translate it:
    ``%recipient.first_name_html%`` in the HTML part (Mailgun substitutes
    the per-recipient, already-escaped value at send time — see
    ``build_recipient_variables``), ``%recipient.first_name_text%`` in the
    text part. Both parts are wrapped in the SAME shell + footer as
    ``render_email`` so a dry-run (``render_email``) and a real batch send
    are byte-for-byte comparable once Mailgun's substitution is simulated
    (MA3).

    Raises ``ValueError`` (via ``_assert_only_known_tokens``) if the
    resulting payload carries any stray ``%`` or an unrecognized
    ``%recipient.*%`` token — this MUST run before the body ever reaches
    Mailgun (MA1).
    """
    # HTML path: escape the whole body template as literal text FIRST (this
    # is the single html.escape call the revision calls out as "static,
    # shared across the batch" — no per-recipient escaping happens here),
    # then substitute in the HTML recipient token.
    body_html_escaped = html.escape(body_template)
    body_html_tokenized = body_html_escaped.replace(
        "{first_name}", "%recipient.first_name_html%"
    )
    html_out = (
        "<html><body>"
        f"<p>{body_html_tokenized}</p>"
        "<hr>"
        f"<p>{_FOOTER_TEXT}</p>"
        "</body></html>"
    )

    # Text path: raw body template, text recipient token, plain footer.
    body_text_tokenized = body_template.replace(
        "{first_name}", "%recipient.first_name_text%"
    )
    text_out = f"{body_text_tokenized}\n\n{_FOOTER_TEXT}"

    _assert_only_known_tokens(html_out)
    _assert_only_known_tokens(text_out)

    return html_out, text_out


def build_recipient_variables(recipients) -> dict[str, dict[str, str]]:
    """Build the Mailgun ``recipient-variables`` map for a batch (MA2).

    ``recipients`` is an iterable of either ``(email, first_name)`` tuples
    or objects carrying ``.email`` / ``.first_name`` attributes (e.g.
    ``EmailBroadcastRecipient`` rows). Returns
    ``{email: {"first_name_html": ..., "first_name_text": ...}}`` keyed by
    the EXACT snapshot email string, so the same value is usable both as a
    ``to_list`` entry and as this map's key.

    The "there" fallback for a missing ``first_name`` is applied HERE, in
    the map — never baked into the ``%recipient.*%`` token itself — so
    ``first_name_html`` is the HTML-escaped value (Mailgun substitutes it
    raw into ``build_batch_bodies``' already-escaped shell) and
    ``first_name_text`` stays the raw value.
    """
    variables: dict[str, dict[str, str]] = {}
    for recipient in recipients:
        if isinstance(recipient, tuple):
            email, first_name = recipient
        else:
            email, first_name = recipient.email, recipient.first_name
        name = first_name or "there"
        variables[email] = {
            "first_name_html": html.escape(name),
            "first_name_text": name,
        }
    return variables


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
#      ever runs a drain for a given broadcast id. BOTH the send path
#      (``launch_drain``) and the resume path (``launch_resume``) go through the
#      SAME registry via ``_launch``, so a second concurrent launch for the same
#      id — send-vs-send, resume-vs-resume, or send-vs-resume — is an idempotent
#      no-op. This is what actually prevents a recipient being sent twice.
#   2. BACKSTOP double-count guard — every row-finalizing UPDATE is a
#      compare-and-swap on the ``(status, attempts)`` the drain last read, and
#      proceeds only when ``rowcount == 1`` (Ruling 3c). Even if the process
#      guard were ever bypassed (e.g. a future multi-instance change), only the
#      drain that wins the CAS advances the row, and the broadcast counters are
#      recomputed from the authoritative row states (never bumped
#      incrementally), so counts can never be inflated or drift under retry.
#
# The known residual window is bypass-only: if two drains somehow ran the same
# id concurrently, both could call ``send_email`` before either won the CAS. On
# a single instance that window does not exist, and the CAS + recompute keep the
# counters correct regardless. We favour never-double-count and rely on the
# registry for never-double-send, which is sound under the locked
# ``instance_count: 1`` deployment.

# Broadcast ids with a live in-process drain (Ruling 3b). A second launch for
# an id already draining is a no-op.
_ACTIVE_DRAINS: set[int] = set()
# Strong references to the tracked drain tasks so the GC cannot collect a task
# mid-flight (Ruling 1). The done-callback discards from both sets.
_DRAIN_TASKS: set[asyncio.Task] = set()


def _launch(
    session_factory: async_sessionmaker[AsyncSession],
    broadcast_id: int,
    coro_fn,
) -> None:
    """Launch a tracked background drain running ``coro_fn`` (Ruling 1 + 3b).

    Shared machinery behind both ``launch_drain`` (fresh send) and
    ``launch_resume`` (retry). No-op if a drain for this id is already live in
    this process (Ruling 3b): the ``_ACTIVE_DRAINS`` registry is the primary
    mutual-exclusion guard, so the send path and the resume path share ONE
    registry and can never both run the same id concurrently (no double-send).
    Otherwise a tracked ``asyncio.Task`` is created (strong ref held in
    ``_DRAIN_TASKS``) with a done-callback that logs any exception via structlog
    and discards the task from both module sets, so a failure is observed rather
    than silently swallowed.
    """
    if broadcast_id in _ACTIVE_DRAINS:
        return
    _ACTIVE_DRAINS.add(broadcast_id)
    task = asyncio.create_task(coro_fn(session_factory, broadcast_id))
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


def launch_drain(
    session_factory: async_sessionmaker[AsyncSession], broadcast_id: int
) -> None:
    """Launch a tracked background drain (fresh send) for ``broadcast_id``.

    No-op if a drain for this id is already live in this process (Ruling 3b).
    Callers MUST launch this only AFTER the materialization transaction has
    committed (Ruling 2), so the drain's own session sees the ``pending`` rows.
    Only sends ``pending`` recipients; retries are the resume path's job.
    """
    _launch(session_factory, broadcast_id, _drain)


def launch_resume(
    session_factory: async_sessionmaker[AsyncSession], broadcast_id: int
) -> None:
    """Launch a tracked background resume for ``broadcast_id`` (Ruling 3 + 12).

    Mirrors ``launch_drain`` and is the ONLY guarded public entry point for a
    resume — the router (Task 5) calls this, never the bare ``resume_pending``
    coroutine, so two concurrent resume requests can never both send the same
    recipient (the shared ``_ACTIVE_DRAINS`` registry makes the second launch a
    no-op). Retries interrupted ``pending`` rows and ``failed`` rows still below
    ``broadcast_max_attempts`` (Ruling 12).
    """
    _launch(session_factory, broadcast_id, resume_pending)


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
    expected_status: RecipientStatus,
    expected_attempts: int,
    error: str | None,
    set_sent_at: bool,
) -> bool:
    """Atomically move a recipient into ``new_status`` and bump ``attempts``.

    Compare-and-swap on ``(status, attempts)`` (Ruling 3c): the UPDATE only
    lands when the row is STILL exactly as this drain last read it, so it works
    for the fresh-send path (``expected_status='pending'``) AND the resume-retry
    path (``expected_status='failed'`` for a below-cap row). Returns True only
    when it claimed the row (``rowcount == 1``). A False return means another
    drain (or a concurrent resume) already advanced this row, so the caller must
    not act on it — the CAS is the backstop that keeps a double-send from ever
    double-counting even if the process registry were bypassed.
    """
    values: dict = {
        "status": new_status,
        "attempts": expected_attempts + 1,
        "error": error,
    }
    if set_sent_at:
        values["sent_at"] = func.now()
    result = await db.execute(
        update(EmailBroadcastRecipient)
        .where(
            EmailBroadcastRecipient.id == recipient_id,
            EmailBroadcastRecipient.status == expected_status,
            EmailBroadcastRecipient.attempts == expected_attempts,
        )
        .values(**values)
    )
    return (result.rowcount or 0) == 1


async def _recompute_broadcast_counters(
    db: AsyncSession, broadcast_id: int
) -> int:
    """Recompute ``sent/failed/skipped`` counters from the recipient rows.

    Counters are derived from the authoritative row states rather than bumped
    incrementally, so retries stay consistent: when a resume re-attempts a
    ``failed`` row and it now succeeds, the row flips ``failed→sent`` and the
    recompute simply reflects the new totals — no decrement/increment bookkeeping
    and no risk of double-counting a row across attempts. Returns the number of
    rows still ``pending`` (used to decide completion).
    """
    counts = dict(
        (
            await db.execute(
                select(
                    EmailBroadcastRecipient.status, func.count()
                )
                .where(EmailBroadcastRecipient.broadcast_id == broadcast_id)
                .group_by(EmailBroadcastRecipient.status)
            )
        ).all()
    )
    await db.execute(
        update(EmailBroadcast)
        .where(EmailBroadcast.id == broadcast_id)
        .values(
            sent_count=counts.get(RecipientStatus.SENT, 0),
            failed_count=counts.get(RecipientStatus.FAILED, 0),
            skipped_count=counts.get(RecipientStatus.SKIPPED, 0),
        )
    )
    return int(counts.get(RecipientStatus.PENDING, 0))


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

    max_attempts = settings.broadcast_max_attempts
    if only_retryable:
        # ``resume`` re-lists interrupted ``pending`` rows AND ``failed`` rows,
        # but only those still below the attempts cap (Ruling 12) — so a row
        # that keeps failing eventually reaches ``max_attempts`` and is left
        # alone rather than hammered on every resume. The cap applies to both
        # statuses (a ``pending`` row already at the cap is not retried either).
        eligible = and_(
            EmailBroadcastRecipient.status.in_(
                [RecipientStatus.PENDING, RecipientStatus.FAILED]
            ),
            EmailBroadcastRecipient.attempts < max_attempts,
        )
    else:
        # Fresh send: only ever touch ``pending`` rows. Retries are resume-only.
        eligible = EmailBroadcastRecipient.status == RecipientStatus.PENDING
    conditions = [
        EmailBroadcastRecipient.broadcast_id == broadcast_id,
        eligible,
    ]
    target_ids = [
        row[0]
        for row in (
            await db.execute(
                select(EmailBroadcastRecipient.id)
                .where(*conditions)
                .order_by(EmailBroadcastRecipient.id)
            )
        ).all()
    ]

    def _is_eligible(status: RecipientStatus, attempts: int) -> bool:
        if only_retryable:
            return (
                status in (RecipientStatus.PENDING, RecipientStatus.FAILED)
                and attempts < max_attempts
            )
        return status == RecipientStatus.PENDING

    for recipient_id in target_ids:
        # Re-read the row's current snapshot; another drain (or a prior
        # partial run) may have advanced it since we listed the ids.
        row = (
            await db.execute(
                select(
                    EmailBroadcastRecipient.status,
                    EmailBroadcastRecipient.attempts,
                    EmailBroadcastRecipient.user_id,
                    EmailBroadcastRecipient.email,
                    EmailBroadcastRecipient.first_name,
                ).where(EmailBroadcastRecipient.id == recipient_id)
            )
        ).first()
        if row is None:
            continue
        status, attempts, user_id, email, first_name = row
        if not _is_eligible(status, attempts):
            continue

        # (1) Segment re-check at send time (Ruling 9): skip a user who lapsed
        # (deactivated / unverified) after materialization.
        if not await _user_still_targetable(db, user_id):
            if await _claim_recipient(
                db,
                recipient_id,
                RecipientStatus.SKIPPED,
                expected_status=status,
                expected_attempts=attempts,
                error="user no longer active and verified",
                set_sent_at=False,
            ):
                await db.commit()
                # Recompute live so GET /{id} progress polling advances during
                # the drain instead of jumping straight to the final tally.
                await _recompute_broadcast_counters(db, broadcast_id)
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
        # (4) Atomic claim (Ruling 3c): CAS on the (status, attempts) we read,
        # so a concurrent finalize loses. ``attempts`` is bumped on every
        # attempt, so a row that keeps failing marches toward the cap.
        if await _claim_recipient(
            db,
            recipient_id,
            outcome,
            expected_status=status,
            expected_attempts=attempts,
            error=send_error,
            set_sent_at=sent_ok,
        ):
            await db.commit()
            # Recompute live so GET /{id} progress polling advances during the
            # drain instead of jumping straight to the final tally (counters
            # are cheap to recompute at these audience sizes, and pacing gives
            # ample headroom between rows).
            await _recompute_broadcast_counters(db, broadcast_id)
            await db.commit()
        else:
            await db.rollback()

        # (5) Pace between sends (env ``broadcast_pacing_seconds``).
        await asyncio.sleep(settings.broadcast_pacing_seconds)

    # Recompute counters from the authoritative row states (see helper) and read
    # back how many rows are still ``pending``. When none remain, the broadcast
    # is done (even with some ``failed``/``skipped``). Rows still pending (e.g. a
    # resume that left a ``pending`` row already at the attempts cap) keep the
    # broadcast in ``sending``.
    remaining_pending = await _recompute_broadcast_counters(db, broadcast_id)
    await db.commit()
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
    """Re-drain a broadcast, retrying interrupted ``pending`` rows AND
    ``failed`` rows still below the attempts cap (Ruling 12) — so a definitively
    failed recipient is re-attempted until it succeeds or hits
    ``broadcast_max_attempts``, then left alone. Each attempt bumps ``attempts``.
    Idempotent: safe to call after a restart or a partial run. Guard concurrent
    resumes via ``launch_resume`` (the registry), not this bare coroutine."""
    await _drain_with_wrapper(session_factory, broadcast_id, only_retryable=True)
