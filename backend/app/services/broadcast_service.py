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

import html
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_broadcast import (
    SEGMENT_ACTIVE_VERIFIED,
    EmailBroadcast,
    EmailBroadcastRecipient,
    RecipientStatus,
)
from app.models.user import User

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
