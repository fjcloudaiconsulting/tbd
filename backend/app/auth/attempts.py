"""Failed sign-in attempts counted per account and per MFA challenge (BE-02).

The per-IP limits on ``/login`` and the ``/mfa/*`` legs are multiplied by every
address a client can use. These counters are keyed on the account (the
submitted login, or the user id) and on the challenge token's ``jti`` instead,
in the rate-limit store (``rate_limit_db``: fixed windows, shared by every
replica). Only failures count.

Windows are short and nothing locks permanently: a full window refuses that
account's attempts until it ends (``Retry-After``), a success empties it, and
the moment it fills is audited by the caller. A full challenge window burns the
challenge, so the next try starts again from the password.

No exception is caught here: a store error propagates as ``SQLAlchemyError``
and the caller answers 503, never a silent pass.
"""
from __future__ import annotations

import asyncio
import math
import time

from app import rate_limit_db, state_db
from app.security import MFA_CHALLENGE_TTL_SECONDS

LOGIN_FAILURES_PER_ACCOUNT = 10
MFA_FAILURES_PER_USER = 10
MFA_FAILURES_PER_CHALLENGE = 5
FAILURE_WINDOW_SECONDS = 15 * 60

# used_tokens scope of an MFA challenge jti: claimed on success, or burned.
MFA_CHALLENGE_SCOPE = "mfa_challenge"
_CHALLENGE_CLAIM_TTL_SECONDS = MFA_CHALLENGE_TTL_SECONDS + 60


def _login_key(login: str) -> str:
    # Keyed on what was typed, so an unknown account is counted exactly like a
    # real one and the 429 says nothing about which exists. Stored hashed.
    return f"auth:login_fail:{login.strip().lower()}"


def _mfa_user_key(user_id: int) -> str:
    return f"auth:mfa_fail:user:{user_id}"


def _mfa_challenge_key(jti: str) -> str:
    return f"auth:mfa_fail:challenge:{jti}"


async def _retry_after(key: str, limit: int) -> int | None:
    """Seconds until ``key``'s window ends when it holds ``limit`` failures."""
    if await asyncio.to_thread(rate_limit_db.get, key) < limit:
        return None
    expires_at = await asyncio.to_thread(rate_limit_db.get_expiry, key)
    return max(1, math.ceil(expires_at - time.time()))


async def _fail(key: str, limit: int) -> bool:
    """Count one failure; True when this one filled the window."""
    return await asyncio.to_thread(rate_limit_db.hit, key, FAILURE_WINDOW_SECONDS) == limit


async def login_retry_after(login: str) -> int | None:
    """Seconds to wait when ``login`` has used its failure budget, else None."""
    return await _retry_after(_login_key(login), LOGIN_FAILURES_PER_ACCOUNT)


async def record_login_failure(login: str) -> bool:
    """Count a wrong password for ``login``; True when the window just filled."""
    return await _fail(_login_key(login), LOGIN_FAILURES_PER_ACCOUNT)


async def clear_login_failures(*logins: str) -> None:
    for login in logins:
        await asyncio.to_thread(rate_limit_db.clear, _login_key(login))


async def mfa_user_retry_after(user_id: int) -> int | None:
    """Seconds to wait when the user has used their second-factor budget."""
    return await _retry_after(_mfa_user_key(user_id), MFA_FAILURES_PER_USER)


async def mfa_challenge_exhausted(jti: str) -> bool:
    return await _retry_after(_mfa_challenge_key(jti), MFA_FAILURES_PER_CHALLENGE) is not None


async def record_mfa_failure(user_id: int, jti: str) -> bool:
    """Count a wrong second factor against the user and the challenge. Burns
    the challenge when its own budget fills. True when the USER window just
    filled (the caller audits that once)."""
    if await _fail(_mfa_challenge_key(jti), MFA_FAILURES_PER_CHALLENGE):
        await state_db.claim_token(MFA_CHALLENGE_SCOPE, jti, _CHALLENGE_CLAIM_TTL_SECONDS)
    return await _fail(_mfa_user_key(user_id), MFA_FAILURES_PER_USER)


async def claim_mfa_challenge(jti: str) -> bool:
    """Spend the challenge on a successful second factor. False when it was
    already spent or burned."""
    return await state_db.claim_token(MFA_CHALLENGE_SCOPE, jti, _CHALLENGE_CLAIM_TTL_SECONDS)


async def clear_mfa_failures(user_id: int) -> None:
    await asyncio.to_thread(rate_limit_db.clear, _mfa_user_key(user_id))
