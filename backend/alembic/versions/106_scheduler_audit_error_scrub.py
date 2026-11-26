"""Scrub stored scheduler failure exception messages (TBD-822).

Revision ID: 106_scheduler_audit_error_scrub
Revises: 105_transaction_occurrence_bind

Preserve existing summaries and recover the exception class and optional
driver errno from a leading ``(module.Class)`` prefix. Replace other scheduler
failure errors with ``redacted``. Downgrade has no original messages to restore.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "106_scheduler_audit_error_scrub"
down_revision = "105_transaction_occurrence_bind"
branch_labels = None
depends_on = None

BATCH = 500
_SUMMARY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}(?: errno=-?\d+)?")
_CLASS_PREFIX = re.compile(
    r"\((?:[A-Za-z_][A-Za-z0-9_]*\.)+([A-Za-z_][A-Za-z0-9_]{0,99})\)(?: \((-?\d+),)?"
)
_audit = sa.table(
    "audit_events",
    sa.column("id", sa.BigInteger()),
    sa.column("event_type", sa.String(80)),
    sa.column("detail", sa.JSON()),
)


def upgrade() -> None:
    conn = op.get_bind()
    page = (
        sa.select(_audit.c.id, _audit.c.event_type, _audit.c.detail)
        .where(_audit.c.event_type.like("scheduler.%.failure"), _audit.c.detail.is_not(None))
        .order_by(_audit.c.id)
        .limit(BATCH)
    )
    update = _audit.update().where(_audit.c.id == sa.bindparam("event_id"))
    query = page
    while True:
        rows = conn.execute(query).all()
        if not rows:
            return
        changes = []
        for event_id, event_type, detail in rows:
            # MySQL's collation can fold case in LIKE.
            if not (event_type.startswith("scheduler.") and event_type.endswith(".failure")):
                continue
            if not isinstance(detail, dict) or "error" not in detail:
                continue
            error = detail["error"]
            if isinstance(error, str) and _SUMMARY.fullmatch(error):
                continue
            match = _CLASS_PREFIX.match(error) if isinstance(error, str) else None
            summary = match[1] if match else "redacted"
            if match and match[2] is not None:
                summary = f"{summary} errno={match[2]}"
            changes.append({"event_id": event_id, "detail": {**detail, "error": summary}})
        if changes:
            conn.execute(update, changes)
        query = page.where(_audit.c.id > rows[-1].id)


def downgrade() -> None:
    pass
