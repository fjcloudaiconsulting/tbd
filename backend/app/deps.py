from datetime import datetime, timezone

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, get_db
from app.models.user import User
from app.security import decode_token, token_cutoff
from app.services.user_activity_service import maybe_stamp_last_active


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the engine-wide session factory for callers that need
    to open an independent transaction (audit-event recording, etc.).

    Wrapped in a dependency so tests can override with an in-memory
    factory the same way they override ``get_db``.
    """
    return async_session

bearer_scheme = HTTPBearer()
bearer_optional = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> User:
    payload = decode_token(credentials.credentials)
    if payload is None or payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id = int(payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    # Reject tokens issued before the session cutoff (logout / password change)
    iat = payload.get("iat")
    if iat is not None:
        token_issued_at = datetime.fromtimestamp(iat, tz=timezone.utc)
        if token_issued_at < token_cutoff(user):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been invalidated",
            )

    # L4.9: bind authenticated context onto structlog's request-scoped
    # contextvars (request_id was already bound by RequestContextMiddleware
    # before deps resolved). Every structlog event emitted in this
    # request from here on — including the uvicorn access log line at
    # response time — carries user_id / org_id / role for triage.
    structlog.contextvars.bind_contextvars(
        user_id=user.id,
        org_id=user.org_id,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
    )

    # Founding-members program: stamp activity (throttled, on an independent
    # session so the request transaction is untouched and a stamp failure
    # can never break auth). No-op when last_active_at is still fresh.
    await maybe_stamp_last_active(session_factory, user.id, user.last_active_at)

    return user


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_optional),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Like ``get_current_user`` but returns ``None`` instead of raising on any
    auth failure (missing token, invalid/expired token, inactive user, invalidated
    session).  Used by endpoints that serve both authenticated and anonymous callers
    with different resolution behaviour (e.g. ``/auth/status`` feature flags).

    Does NOT bind structlog context — that is ``get_current_user``'s job.
    """
    if credentials is None:
        return None
    try:
        payload = decode_token(credentials.credentials)
        if payload is None or payload.get("type") != "access":
            return None

        user_id = int(payload["sub"])
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

        if user is None or not user.is_active:
            return None

        iat = payload.get("iat")
        if iat is not None:
            token_issued_at = datetime.fromtimestamp(iat, tz=timezone.utc)
            if token_issued_at < token_cutoff(user):
                return None

        return user
    except Exception:
        return None
