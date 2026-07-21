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
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.api_token import ApiToken
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
