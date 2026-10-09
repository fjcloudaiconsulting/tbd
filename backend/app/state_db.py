"""Refresh sessions, single-use tokens and leases in MySQL: sessions move to
MySQL (INFRA-122).

The only place this SQL lives. Sync, short transactions on an own engine (same
builder as the rate limits, own pool); async callers await the public
coroutines, which run the sync core in ``asyncio.to_thread``. Every expiry is
the database clock (``db_now``), never the app clock.

Session families (spec 2026-05-17 backend session model, formerly Lua):
the family row (sid) is locked FOR UPDATE first by every write, so two
refreshes, a logout and a reuse detection on one family serialize on it; every
guard after that lock is a locking read. The revoke is the family row being
gone; a rotated-out jti's 30 s grace is bounded by its successor's insert time.

No exception is caught here except duplicate keys, which carry meaning. A
database error propagates as ``SQLAlchemyError`` and the caller decides.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor

import structlog
from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement
from sqlalchemy.types import DateTime

from app import rate_limit_db
from app.models.session_state import AuthSessionFamily, AuthSessionMember, Lease, UsedToken

logger = structlog.stdlib.get_logger(__name__)

_F = AuthSessionFamily.__table__
_M = AuthSessionMember.__table__
_U = UsedToken.__table__
_L = Lease.__table__

_engine: Engine = rate_limit_db._build_engine()

# Rows are purged only this long after expiry, in small batches.
_PURGE_MARGIN = 60
_PURGE_BATCH = 100


class db_now(FunctionElement):
    """The database clock plus ``offset`` whole seconds."""

    type = DateTime()
    # The offset is rendered inline, so statements using it must not share a cache entry.
    inherit_cache = False

    def __init__(self, offset: int = 0):
        self.offset = int(offset)
        super().__init__()


@compiles(db_now, "mysql")
def _db_now_mysql(element, compiler, **kw):
    if element.offset == 0:
        return "NOW(6)"
    return f"(NOW(6) + INTERVAL {element.offset} SECOND)"


@compiles(db_now, "sqlite")
def _db_now_sqlite(element, compiler, **kw):
    return f"strftime('%Y-%m-%d %H:%M:%f', 'now', '{element.offset:+d} seconds')"


def _is_dup(exc: IntegrityError, pk_of: str | None = None) -> bool:
    """A duplicate key; with ``pk_of``, only on that table's primary key."""
    args = getattr(exc.orig, "args", ())
    msg = str(exc.orig)
    if args and args[0] == 1062:
        return pk_of is None or f"'{pk_of}.PRIMARY'" in msg
    if "UNIQUE constraint failed" not in msg:
        return False
    return pk_of is None or msg.endswith(f"{pk_of}.jti")


# Own threads, sized to the engine pool (2 + 5): a stalled database queues
# store calls here instead of filling the loop's default executor.
_POOL = ThreadPoolExecutor(max_workers=7, thread_name_prefix="state_db")


async def _aio(fn, *args):
    ctx = contextvars.copy_context()  # keeps request_id in log lines, as to_thread does
    return await asyncio.get_running_loop().run_in_executor(_POOL, ctx.run, fn, *args)


# ── Refresh-session families ──────────────────────────────────────────────

SESSION_GRACE_TTL_SECONDS = 30

SESSION_ROTATE_OK = "ok"
SESSION_ROTATE_REVOKED = "session_revoked"
SESSION_ROTATE_ALREADY_ROTATED = "already_rotated"
SESSION_ROTATE_JTI_COLLISION = "jti_collision"

SESSION_REUSE_LIVE = "live"
SESSION_REUSE_GRACE = "grace"
SESSION_REUSE_UNKNOWN = "unknown"
SESSION_REUSE_REUSED = "reused"

# A revoked family larger than this is logged (the revoke still happens).
REUSE_REVOKE_FAMILY_SIZE_WARN_THRESHOLD = 10000
# Members kept per family. /refresh is unlimited (TBD-353), so without a cap
# a looping client grows one family without bound. A jti rotated out more than
# this many rotations ago reads as unknown (401, no revoke) instead of reuse.
_KEEP_MEMBERS = 1000

_live = _F.c.expires_at > db_now()
_S = _M.alias("successor")


def _graced():
    """The jti was rotated out less than SESSION_GRACE_TTL_SECONDS ago: its
    successor member (seq + 1) was created inside the window. Per jti, never
    sliding: later rotations do not extend an older jti's grace."""
    return and_(
        _M.c.jti != _F.c.head_jti,
        select(1)
        .where(
            _S.c.sid == _M.c.sid,
            _S.c.seq == _M.c.seq + 1,
            _S.c.created_at > db_now(-SESSION_GRACE_TTL_SECONDS),
        )
        .exists(),
    )


def _lock_family(c: Connection, sid: str):
    return c.execute(
        select(_F.c.user_id, _F.c.head_jti, _F.c.rotations, _live.label("live"))
        .where(_F.c.sid == sid)
        .with_for_update()
    ).first()


def _member_seq(c: Connection, sid: str, jti: str) -> int | None:
    return c.execute(
        select(_M.c.seq).where(_M.c.jti == jti, _M.c.sid == sid).with_for_update(read=True)
    ).scalar()


def _purge_families() -> None:
    try:
        with _engine.begin() as c:
            stale = c.execute(
                select(_F.c.sid)
                .where(_F.c.expires_at < db_now(-_PURGE_MARGIN))
                .limit(20)
                .with_for_update(skip_locked=True)
            ).scalars().all()
            if stale:
                c.execute(delete(_M).where(_M.c.sid.in_(stale)))
                c.execute(delete(_F).where(_F.c.sid.in_(stale)))
    except SQLAlchemyError as exc:  # the session itself is committed
        logger.warning("auth.session.purge_failed", error_class=type(exc).__name__)


def _issue(jti: str, sid: str, user_id: int, ttl_seconds: int) -> None:
    with _engine.begin() as c:
        c.execute(
            insert(_F).values(
                sid=sid, user_id=user_id, head_jti=jti, rotations=0, expires_at=db_now(ttl_seconds)
            )
        )
        c.execute(insert(_M).values(jti=jti, sid=sid, seq=0, created_at=db_now()))
    _purge_families()


def _validate(jti: str) -> dict | None:
    with _engine.connect() as c:
        row = c.execute(
            select(_F.c.user_id, _F.c.sid)
            .select_from(_M.join(_F, _M.c.sid == _F.c.sid))
            .where(_M.c.jti == jti, _F.c.head_jti == _M.c.jti, _live)
        ).first()
    return {"user_id": row.user_id, "sid": row.sid} if row else None


def _grace(jti: str) -> dict | None:
    with _engine.connect() as c:
        row = c.execute(
            select(_F.c.user_id, _F.c.sid, _F.c.head_jti)
            .select_from(_M.join(_F, _M.c.sid == _F.c.sid))
            .where(_M.c.jti == jti, _live, _graced())
        ).first()
    if row is None:
        return None
    return {"user_id": row.user_id, "sid": row.sid, "successor_jti": row.head_jti}


def _family_exists(sid: str) -> bool:
    with _engine.connect() as c:
        return c.execute(select(1).where(_F.c.sid == sid, _live)).first() is not None


def _family_member(sid: str, jti: str) -> bool:
    with _engine.connect() as c:
        return (
            c.execute(
                select(1)
                .select_from(_M.join(_F, _M.c.sid == _F.c.sid))
                .where(_M.c.jti == jti, _M.c.sid == sid, _live)
            ).first()
            is not None
        )


class _Collision(Exception):
    pass


def _rotate(
    old_jti: str,
    new_jti: str,
    sid: str,
    user_id: int,
    idle_ttl_seconds: int,
) -> str:
    try:
        with _engine.begin() as c:
            fam = _lock_family(c, sid)
            if fam is None or not fam.live or fam.user_id != user_id:
                return SESSION_ROTATE_REVOKED
            if _member_seq(c, sid, old_jti) is None:
                return SESSION_ROTATE_REVOKED
            if fam.head_jti != old_jti:
                return SESSION_ROTATE_ALREADY_ROTATED
            try:
                c.execute(
                    insert(_M).values(
                        jti=new_jti, sid=sid, seq=fam.rotations + 1, created_at=db_now()
                    )
                )
            except IntegrityError as exc:
                # only a jti clash is a collision; a (sid, seq) clash is a broken family
                if not _is_dup(exc, pk_of="auth_session_members"):
                    raise
                raise _Collision from None  # rolls the transaction back
            c.execute(
                update(_F)
                .where(_F.c.sid == sid)
                .values(head_jti=new_jti, rotations=fam.rotations + 1, expires_at=db_now(idle_ttl_seconds))
            )
            c.execute(
                delete(_M).where(_M.c.sid == sid, _M.c.seq <= fam.rotations + 1 - _KEEP_MEMBERS)
            )
    except _Collision:
        return SESSION_ROTATE_JTI_COLLISION
    return SESSION_ROTATE_OK


def _delete_family(c: Connection, sid: str) -> list[str]:
    jtis = sorted(c.execute(select(_M.c.jti).where(_M.c.sid == sid)).scalars().all())
    c.execute(delete(_M).where(_M.c.sid == sid))
    c.execute(delete(_F).where(_F.c.sid == sid))
    return jtis


def _revoke_family(sid: str) -> list[str]:
    with _engine.begin() as c:
        fam = _lock_family(c, sid)
        if fam is None or not fam.live:
            return []  # already expired: the purge removes it
        return _delete_family(c, sid)


def _detect_reuse_and_revoke(jti: str, sid: str) -> tuple[str, int] | tuple[str]:
    with _engine.begin() as c:
        fam = _lock_family(c, sid)
        if fam is None or not fam.live:
            return (SESSION_REUSE_UNKNOWN,)
        if fam.head_jti == jti:
            return (SESSION_REUSE_LIVE,)
        if _member_seq(c, sid, jti) is None:
            return (SESSION_REUSE_UNKNOWN,)
        graced = c.execute(
            select(1).select_from(_M.join(_F, _M.c.sid == _F.c.sid)).where(_M.c.jti == jti, _graced())
        ).first()
        if graced is not None:
            return (SESSION_REUSE_GRACE,)
        count = len(_delete_family(c, sid))
    if count > REUSE_REVOKE_FAMILY_SIZE_WARN_THRESHOLD:
        logger.warning(
            "auth.session.reuse_revoke.large_family",
            jti_count=count,
            threshold=REUSE_REVOKE_FAMILY_SIZE_WARN_THRESHOLD,
        )
    return (SESSION_REUSE_REUSED, count)


def _probe() -> None:
    with _engine.connect() as c:
        c.execute(select(1))


async def session_store_probe() -> None:
    """Raise if the session store is unreachable, so login answers one 503 for
    every branch before any credential check (INFRA-121/132)."""
    await _aio(_probe)


async def session_issue(jti: str, sid: str, user_id: int, ttl_seconds: int) -> None:
    """Create the family row and its first member. Every fresh-session issue
    path calls this BEFORE setting the cookie; an error means 503, no cookie."""
    await _aio(_issue, jti, sid, user_id, ttl_seconds)


async def session_validate(jti: str) -> dict | None:
    """``{"user_id", "sid"}`` when ``jti`` is the live head of its family."""
    return await _aio(_validate, jti)


async def session_grace(jti: str) -> dict | None:
    """``{"user_id", "sid", "successor_jti"}`` when ``jti`` was rotated out
    less than 30 s ago; ``successor_jti`` is the current head."""
    return await _aio(_grace, jti)


async def session_family_exists(sid: str) -> bool:
    return await _aio(_family_exists, sid)


async def session_family_member(sid: str, jti: str) -> bool:
    return await _aio(_family_member, sid, jti)


async def session_rotate(
    old_jti: str,
    new_jti: str,
    sid: str,
    user_id: int,
    idle_ttl_seconds: int,
) -> str:
    """Rotate the family head from ``old_jti`` to ``new_jti``. Returns ``ok``,
    ``session_revoked`` (no live family, or not a member), ``already_rotated``
    (a concurrent refresh won) or ``jti_collision``. Non-ok writes nothing."""
    return await _aio(_rotate, old_jti, new_jti, sid, user_id, idle_ttl_seconds)


async def session_revoke_family(sid: str) -> list[str]:
    """Delete the family and every member; returns the jtis (sorted)."""
    return await _aio(_revoke_family, sid)


async def session_detect_reuse_and_revoke(jti: str, sid: str) -> tuple[str, int] | tuple[str]:
    """Classify a refresh jti that is neither head nor graced. Only a consumed
    member outside the grace window is reuse: the whole family is revoked in
    the same transaction, so concurrent callers revoke (and audit) once.
    Returns ``("reused", n)``, ``("grace",)``, ``("live",)`` or ``("unknown",)``."""
    return await _aio(_detect_reuse_and_revoke, jti, sid)


# ── Single-use tokens, nonces, dedupe markers ─────────────────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _purge_used_tokens() -> None:
    try:
        with _engine.begin() as c:
            stale = c.execute(
                select(_U.c.scope, _U.c.token)
                .where(_U.c.expires_at < db_now(-_PURGE_MARGIN))
                .limit(_PURGE_BATCH)
                .with_for_update(skip_locked=True)
            ).all()
            for scope, token in stale:
                c.execute(delete(_U).where(_U.c.scope == scope, _U.c.token == token))
    except SQLAlchemyError as exc:  # the claim itself is committed
        logger.warning("used_tokens.purge_failed", error_class=type(exc).__name__)


def _claim(scope: str, token: str, ttl_seconds: int) -> bool:
    th = _token_hash(token)
    try:
        with _engine.begin() as c:
            c.execute(
                delete(_U).where(_U.c.scope == scope, _U.c.token == th, _U.c.expires_at <= db_now())
            )
            c.execute(insert(_U).values(scope=scope, token=th, expires_at=db_now(ttl_seconds)))
    except IntegrityError as exc:
        if not _is_dup(exc):
            raise
        return False
    _purge_used_tokens()
    return True


async def claim_token(scope: str, token: str, ttl_seconds: int) -> bool:
    """Record ``token`` in ``scope`` for ``ttl_seconds``. True on first sight,
    False when it is already recorded and unexpired (a replay)."""
    return await _aio(_claim, scope, token, ttl_seconds)


async def mark_webhook_token_seen(token: str, ttl_s: int) -> bool:
    """Mailgun webhook replay dedup. FAILS OPEN: the signature check is the
    security boundary, this is DoS hygiene, so a database error must not
    reject valid signed events. True = first sight (process it)."""
    try:
        return await claim_token("mailgun_webhook", token, ttl_s)
    except SQLAlchemyError as exc:
        logger.warning("webhook.mailgun.token_seen.fail_open", error_class=type(exc).__name__)
        return True


# ── Leases ────────────────────────────────────────────────────────────────


def _acquire_lease(name: str, ttl_seconds: int) -> str | None:
    holder = secrets.token_hex(16)
    with _engine.begin() as c:
        took = c.execute(
            update(_L)
            .where(_L.c.name == name, _L.c.expires_at <= db_now())
            .values(holder=holder, expires_at=db_now(ttl_seconds))
        ).rowcount
    if took == 1:
        return holder
    try:
        with _engine.begin() as c:
            c.execute(insert(_L).values(name=name, holder=holder, expires_at=db_now(ttl_seconds)))
    except IntegrityError as exc:
        if not _is_dup(exc):
            raise
        return None
    return holder


def _release_lease(name: str, holder: str) -> None:
    with _engine.begin() as c:
        c.execute(delete(_L).where(_L.c.name == name, _L.c.holder == holder))


async def acquire_lease(name: str, ttl_seconds: int) -> str | None:
    """Take lease ``name`` for ``ttl_seconds`` if it is free or expired.
    Returns the holder token, or None when someone else holds it."""
    return await _aio(_acquire_lease, name, ttl_seconds)


async def release_lease(name: str, holder: str) -> None:
    """Release only if ``holder`` still holds it (a lease re-taken after
    expiry is never released by the old holder)."""
    await _aio(_release_lease, name, holder)

