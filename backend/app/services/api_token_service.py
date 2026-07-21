"""Hashing helpers for superadmin Personal Access Tokens (PAT).

Plaintext tokens are shown to the user exactly once, at creation time, and
are never persisted — only an HMAC-SHA256 hash under ``settings.api_token_hmac_key``
(a dedicated pepper, decoupled from ``jwt_secret_key``; see
``Settings._validate_api_token_hmac_key``) is stored. ``token_hash_candidates``
additionally supports a previous-rotation key (``api_token_hmac_key_prev``,
verify-only) so tokens minted before a key rotation keep validating until
they are re-issued or expire.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.api_token import ApiToken
from app.models.user import User
from app.security import derive_hmac_key

logger = structlog.stdlib.get_logger(__name__)


def _primary_key() -> bytes:
    if settings.api_token_hmac_key:
        return settings.api_token_hmac_key.encode()
    if settings.app_env == "production":
        # Defence-in-depth: the config validator already refuses to boot
        # in production without this key set, so this branch should be
        # unreachable in practice.
        raise RuntimeError("API_TOKEN_HMAC_KEY missing in production")
    return derive_hmac_key(b"api_token")  # dev-only fallback (bytes)


def _hash_with(key: bytes, plaintext: str) -> str:
    return hmac.new(key, plaintext.encode(), hashlib.sha256).hexdigest()


def hash_api_token(plaintext: str) -> str:
    return _hash_with(_primary_key(), plaintext)


def token_hash_candidates(plaintext: str) -> list[str]:
    out = [hash_api_token(plaintext)]
    if settings.api_token_hmac_key_prev:
        out.append(_hash_with(settings.api_token_hmac_key_prev.encode(), plaintext))
    return out


def generate_token() -> tuple[str, str, str]:
    full = "pat_" + secrets.token_urlsafe(32)
    return full, hash_api_token(full), full[:14]


async def lookup_token(db: AsyncSession, plaintext: str) -> ApiToken | None:
    """Resolve the ``ApiToken`` row for a raw ``pat_`` secret, or ``None``.

    Looks up by the full HMAC digest against the unique index (spec §3 —
    never fetch-by-prefix-then-compare). ``token_hash_candidates`` yields the
    primary hash plus the verify-only ``_PREV`` hash, so tokens minted before
    a pepper rotation keep resolving. Matching a full keyed digest against a
    unique column means an attacker cannot produce a digest without the
    secret, so no separate constant-time compare is required.
    """
    candidates = token_hash_candidates(plaintext)
    result = await db.execute(
        select(ApiToken).where(ApiToken.token_hash.in_(candidates))
    )
    return result.scalar_one_or_none()


def _naive_utc_now() -> datetime:
    """Wall-clock now as naive UTC, matching how the columns are stored
    (spec §4 / ARC-R7 — every ``ApiToken`` datetime is ``sa.DateTime()``,
    no ``timezone=True``)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def token_status(row: ApiToken, *, now: datetime | None = None) -> str:
    """Derive the display status of a token row (spec §8 GET).

    Precedence: ``revoked`` (explicit action) beats ``expired`` (time),
    which beats ``active``. ``now`` is naive-UTC; defaults to wall clock.
    """
    if row.revoked_at is not None:
        return "revoked"
    ref = now if now is not None else _naive_utc_now()
    exp = row.expires_at
    exp = exp.replace(tzinfo=None) if exp.tzinfo else exp
    if exp <= ref:
        return "expired"
    return "active"


async def mint(
    db: AsyncSession,
    *,
    user: User,
    name: str,
    scope: str,
    expires_in_days: int,
) -> tuple[str, ApiToken]:
    """Generate, persist, and return a new PAT for ``user``.

    Returns ``(plaintext, row)`` where ``plaintext`` is the reveal-once
    secret — the ONLY moment it exists outside the caller's memory. Only the
    HMAC ``token_hash`` and a non-secret ``token_prefix`` are stored (spec §3).
    The expiry cap is assumed already validated by the schema + router; this
    function trusts ``expires_in_days`` as bounded.
    """
    plaintext, token_hash, prefix = generate_token()
    expires_at = _naive_utc_now() + timedelta(days=expires_in_days)
    row = ApiToken(
        token_hash=token_hash,
        token_prefix=prefix,
        name=name,
        scope=scope,
        created_by_user_id=user.id,
        created_by_email=user.email,
        expires_at=expires_at,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return plaintext, row


async def list_for(db: AsyncSession, user: User) -> list[ApiToken]:
    """All tokens minted by ``user``, newest first (spec §8 GET).

    Owner-scoped: v1 tokens are all superadmin-owned, and scoping the list to
    the caller keeps the surface aligned with ``revoke`` / ``revoke_all`` and
    forward-compatible with the deferred per-user phase.
    """
    result = await db.execute(
        select(ApiToken)
        .where(ApiToken.created_by_user_id == user.id)
        .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
    )
    return list(result.scalars().all())


async def revoke(db: AsyncSession, token_id: int, user: User) -> ApiToken | None:
    """Soft-revoke a single token owned by ``user`` (spec §8 DELETE).

    Returns the row on success, or ``None`` when no active, caller-owned
    token with ``token_id`` exists (already-revoked or foreign tokens both
    resolve to ``None`` so the router can 404 without an ownership oracle).
    Instant effect — every auth hits the DB (``authenticate_pat`` step 3).
    """
    result = await db.execute(
        select(ApiToken).where(
            ApiToken.id == token_id,
            ApiToken.created_by_user_id == user.id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    row.revoked_at = _naive_utc_now()
    await db.commit()
    await db.refresh(row)
    return row


async def revoke_all(db: AsyncSession, user: User) -> int:
    """Panic button (spec §8) — revoke every active token owned by ``user``.

    Returns the number of tokens revoked by this call (already-revoked rows
    are not re-stamped and not counted).
    """
    result = await db.execute(
        update(ApiToken)
        .where(
            ApiToken.created_by_user_id == user.id,
            ApiToken.revoked_at.is_(None),
        )
        .values(revoked_at=_naive_utc_now())
    )
    await db.commit()
    return result.rowcount or 0


async def maybe_stamp_last_used(
    session_factory: async_sessionmaker[AsyncSession],
    token_id: int,
    current: datetime | None,
    client_ip: str | None,
) -> None:
    """Stamp ``last_used_at = now`` / ``last_used_ip`` for ``token_id`` when the
    stored value is stale (``None`` or older than the throttle window). No-op
    when fresh — a PAT hammering the API must not write a row per request.

    Best-effort, exactly like ``maybe_stamp_last_active``: opens its own
    INDEPENDENT session and swallows any error so a failed stamp can never
    break the authenticated request that triggered it. ``client_ip`` MUST come
    from ``rate_limit.get_client_ip`` (never raw ``request.client``).
    """
    now = datetime.now(timezone.utc)
    if current is not None:
        cur = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
        if (now - cur).total_seconds() < settings.api_token_last_used_throttle_seconds:
            return
    try:
        async with session_factory() as session:
            await session.execute(
                update(ApiToken)
                .where(ApiToken.id == token_id)
                .values(last_used_at=now, last_used_ip=client_ip)
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — never break auth on a stamp failure
        logger.warning("api_token.last_used_stamp_failed", api_token_id=token_id)
