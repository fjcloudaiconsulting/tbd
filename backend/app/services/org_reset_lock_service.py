"""Per-org exclusive lease for in-flight org-data resets.

The reset path commits per batch and runs an app-level idempotent
seed (``seed_org_defaults``). Two concurrent reset POSTs that
interleave on the same org could duplicate seeded defaults because
``account_types`` and ``categories`` carry no DB-level UNIQUE on
``(org_id, slug, is_system)``. This service provides a server-side
lock keyed on the org PK to make the reset path strictly serial
per org.

**Lease tokens (PR #135 follow-up):** the release path fences on
``WHERE org_id = :id AND lease_token = :token``. Without this fence,
a long-stalled reset whose lock was already stale-taken-over by a
new caller would, on resuming, ``DELETE WHERE org_id = :id`` and
release the *successor's* fresh lease — reopening the concurrent
window the lock is meant to close. Each acquire (fresh INSERT or
stale-takeover UPDATE) generates a new UUID; release is a no-op
unless the caller's token matches the row's current token.

Contract:
- ``acquire_reset_lock`` returns the lease token (UUID4 string) on
  success, or ``None`` if another reset is in flight. Stale locks
  (older than ``LOCK_TTL_MINUTES``) are auto-recovered so a crashed
  worker cannot indefinitely block future resets.
- ``release_reset_lock(org_id, token)`` is idempotent and fenced —
  it deletes only when the row's ``lease_token`` matches the caller's
  ``token``. Safe to call from a ``finally`` even if acquire raised.
- The endpoint commits each lock state change immediately.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app._time import utcnow_naive
from app.models.org_data_reset_lock import OrgDataResetLock

# How long a lock is considered fresh. After this window an in-flight
# acquire can override the existing lock — recovery path for workers
# that crashed mid-reset and never released. 30 minutes is far longer
# than the worst-case real reset duration but short enough that an
# operator doesn't have to manually clear stuck rows.
LOCK_TTL_MINUTES = 30


async def acquire_reset_lock(
    db: AsyncSession,
    *,
    org_id: int,
    user_id: int,
) -> str | None:
    """Try to acquire the exclusive reset lease for ``org_id``.

    Returns a freshly-generated UUID4 lease token on success. Caller
    MUST pass that token to ``release_reset_lock`` in finally — the
    fenced release uses it to avoid deleting a successor's lock if
    this caller stalled past the TTL.

    Returns ``None`` if another (fresh) reset is already in flight.

    Stale-lock recovery: if a row exists but its ``acquired_at`` is
    older than ``LOCK_TTL_MINUTES``, the lock is forcibly taken over
    by the new caller (with a new lease token). This keeps a crashed
    worker from blocking future resets indefinitely.
    """
    now = utcnow_naive()
    cutoff = now - timedelta(minutes=LOCK_TTL_MINUTES)
    new_token = uuid.uuid4().hex  # 32-char hex; fits in String(36).

    # Fast path: try INSERT. If no row exists for this org, this
    # succeeds atomically against the PK constraint.
    try:
        await db.execute(
            insert(OrgDataResetLock).values(
                org_id=org_id,
                acquired_by_user_id=user_id,
                acquired_at=now,
                lease_token=new_token,
            )
        )
        await db.commit()
        return new_token
    except IntegrityError:
        await db.rollback()

    # Slow path: a row already exists. Take it over only if stale.
    # The conditional WHERE makes the takeover atomic — if a racing
    # caller refreshes the row between our SELECT and UPDATE, rowcount=0
    # and we report contention. The new lease_token replaces the old.
    result = await db.execute(
        update(OrgDataResetLock)
        .where(
            OrgDataResetLock.org_id == org_id,
            OrgDataResetLock.acquired_at < cutoff,
        )
        .values(
            acquired_by_user_id=user_id,
            acquired_at=now,
            lease_token=new_token,
        )
    )
    if (result.rowcount or 0) > 0:
        await db.commit()
        return new_token

    await db.rollback()
    return None


async def release_reset_lock(
    db: AsyncSession, *, org_id: int, token: str
) -> None:
    """Fenced release. Deletes the row ONLY if the stored
    ``lease_token`` matches ``token``. Calling with a stale token
    (i.e., the row was taken over by a successor) is a no-op — the
    successor's lock survives.

    Idempotent on success and safe to call from a finally block
    whether acquire succeeded or raised. Always commits, so the
    caller's session is in a clean state regardless of rowcount.
    """
    await db.execute(
        delete(OrgDataResetLock).where(
            OrgDataResetLock.org_id == org_id,
            OrgDataResetLock.lease_token == token,
        )
    )
    await db.commit()


async def is_reset_locked(db: AsyncSession, *, org_id: int) -> bool:
    """Diagnostic helper for tests + admin debugging. Returns True if
    a fresh (non-stale) lock exists for ``org_id``.
    """
    cutoff = utcnow_naive() - timedelta(minutes=LOCK_TTL_MINUTES)
    row = await db.scalar(
        select(OrgDataResetLock).where(
            OrgDataResetLock.org_id == org_id,
            OrgDataResetLock.acquired_at >= cutoff,
        )
    )
    return row is not None
