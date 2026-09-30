"""Agent access token authentication (TBD-578).

The credential check for the MCP front door. An agent token is a ``pat_``
bearer in ``api_tokens`` with an ``agent:*`` scope, owned by ANY active user
(superadmin not required). Unlike the superadmin PATs (``app.auth.pat``), it
dies with the owner's session cutoff: "sign out everywhere" and a password
change kill every agent token minted at or before that second.

Every rejection is the same 401 (body and ``WWW-Authenticate``), so a caller
cannot tell a revoked token from an unknown one; the reason goes to the log.
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.pat import _aware, _record_auth_rejected
from app.models.api_token import ApiToken
from app.models.user import User
from app.rate_limit import get_client_ip
from app.security import token_cutoff
from app.services.api_token_service import (
    AGENT_SCOPE_RANK,
    lookup_token,
    maybe_stamp_last_used,
)

logger = structlog.stdlib.get_logger(__name__)


def _reject() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def authenticate_agent_token(
    request: Request,
    raw_token: str,
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[User, ApiToken]:
    """Resolve an agent bearer to ``(owner, token row)`` or raise the 401."""
    if not raw_token.startswith("pat_"):
        logger.info("agent_token.auth_rejected", reason="not_pat")
        raise _reject()
    row = await lookup_token(db, raw_token)
    if row is None:
        # Unknown bearer: structlog only, never an audit row (spray DoS).
        logger.info("agent_token.auth_rejected", reason="unknown")
        raise _reject()

    # Bound BEFORE the rejection branches: ``_record_auth_rejected`` audits
    # through this contextvar (see the bind site in ``app.auth.pat``).
    structlog.contextvars.bind_contextvars(api_token_id=row.id)

    now = datetime.now(timezone.utc)
    if row.revoked_at is not None:
        logger.info("agent_token.auth_rejected", reason="revoked", api_token_id=row.id)
        await _record_auth_rejected(session_factory, request, row, "revoked")
        raise _reject()
    if _aware(row.expires_at) <= now:
        logger.info("agent_token.auth_rejected", reason="expired", api_token_id=row.id)
        await _record_auth_rejected(session_factory, request, row, "expired")
        raise _reject()
    if row.scope not in AGENT_SCOPE_RANK:
        # A superadmin REST PAT is not an agent credential.
        logger.info("agent_token.auth_rejected", reason="scope", api_token_id=row.id)
        raise _reject()
    if row.created_by_user_id is None:
        logger.info("agent_token.auth_rejected", reason="owner_null", api_token_id=row.id)
        raise _reject()
    user = (
        await db.execute(select(User).where(User.id == row.created_by_user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        logger.info("agent_token.auth_rejected", reason="owner_inactive", api_token_id=row.id)
        raise _reject()
    # ``<=``, not the JWT path's ``<``: both sides are whole seconds (created_at
    # is floored at mint, the cutoff columns are DATETIME(0) on MySQL), so a
    # token minted in the same second as a logout-everywhere must die.
    if _aware(row.created_at) <= token_cutoff(user):
        logger.info("agent_token.auth_rejected", reason="cutoff", api_token_id=row.id)
        raise _reject()

    structlog.contextvars.bind_contextvars(
        user_id=user.id,
        org_id=user.org_id,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
    )
    await maybe_stamp_last_used(
        session_factory, row.id, row.last_used_at, get_client_ip(request)
    )
    return user, row
