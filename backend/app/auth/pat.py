"""Superadmin Personal Access Token (PAT) authentication path — spec §5/§6/§7.

The security-critical seam is deliberately kept *inside* ``get_current_user``
(``app/deps.py``): a ``pat_``-prefixed bearer branches here, everything else
falls through to the untouched JWT body. Because every route resolves identity
via ``Depends(get_current_user)``, both PAT and JWT identities flow through the
same ``require_superadmin`` / ``require_permission`` gates (ARC-R3).

Posture (see spec §2/§6):

* **Generic 401 for every rejection** — unknown / expired / revoked /
  owner-null / inactive / not-superadmin all return the identical
  ``"Invalid or expired token"``. No oracle in the HTTP body distinguishes the
  states; the true reason goes to the structured log only.
* **Scope is the only 403** — method-based, fail-closed (spec §5).
* **Live re-check** of ``is_active AND is_superadmin`` on the freshly-loaded
  owner row makes demotion / deactivation an instant kill switch.
* **No ``token_cutoff``** — PATs are deliberately independent of password
  change / global session invalidation (GitHub model, spec §6 step 6).
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import get_current_user
from app.models.user import User
from app.rate_limit import get_client_ip
from app.services.api_token_service import lookup_token, maybe_stamp_last_used

logger = structlog.stdlib.get_logger(__name__)

# Safe (read) vs unsafe (write) HTTP methods for the coarse v1 scope model
# (spec §5). Anything not in either set is unmapped and denied fail-closed.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_READ_METHODS = frozenset({"GET", "HEAD"})


def _generic_401() -> HTTPException:
    """The single, indistinguishable rejection response (spec §6, SEC-R8)."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
    )


def _aware(dt: datetime) -> datetime:
    """Treat a tz-naive DB datetime as UTC before comparing to an aware now.

    ``expires_at`` / ``revoked_at`` are stored naive-UTC (spec §4 / ARC-R7);
    comparing a naive value to ``datetime.now(timezone.utc)`` would raise
    ``TypeError``.
    """
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def authenticate_pat(
    request: Request,
    raw_token: str,
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> User:
    """Resolve a ``pat_`` bearer to its owning superadmin ``User`` (spec §6).

    Raises a generic ``401`` for every failure mode except a scope mismatch,
    which is the only ``403`` the auth path emits.
    """
    now = datetime.now(timezone.utc)

    # 1-2. Look up by full HMAC digest (primary + verify-only PREV key).
    row = await lookup_token(db, raw_token)
    if row is None:
        # Unknown token → structlog ONLY, never an audit row, to avoid an
        # audit-flood DoS from `pat_<garbage>` spraying (spec §11 / ARC-R12).
        logger.info("pat.auth_rejected", reason="unknown")
        raise _generic_401()

    # 3. Revoked / expired (naive-UTC normalized).
    if row.revoked_at is not None:
        logger.info("pat.auth_rejected", reason="revoked", api_token_id=row.id)
        raise _generic_401()
    if _aware(row.expires_at) <= now:
        logger.info("pat.auth_rejected", reason="expired", api_token_id=row.id)
        raise _generic_401()

    # 4. Owner gone (ON DELETE SET NULL) → fails to authenticate.
    if row.created_by_user_id is None:
        logger.info("pat.auth_rejected", reason="owner_null", api_token_id=row.id)
        raise _generic_401()

    result = await db.execute(
        select(User).where(User.id == row.created_by_user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        logger.info("pat.auth_rejected", reason="owner_missing", api_token_id=row.id)
        raise _generic_401()

    # 5. Live re-check on the freshly-read row — the instant kill switch for
    # demotion / deactivation, even under the password-independent model.
    if not user.is_active or not user.is_superadmin:
        logger.info(
            "pat.auth_rejected",
            reason="owner_inactive_or_not_superadmin",
            api_token_id=row.id,
        )
        raise _generic_401()

    # 6. Deliberately NO token_cutoff — PATs survive password change / global
    # session invalidation (spec §6 step 6).

    # 7. Scope vs request method, fail-closed (spec §5).
    method = request.method.upper()
    if method in _WRITE_METHODS:
        if row.scope != "write":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token scope insufficient",
            )
    elif method in _READ_METHODS:
        if row.scope not in ("read", "write"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token scope insufficient",
            )
    else:
        # Unmapped method (OPTIONS, TRACE, ...) → deny.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token scope insufficient",
        )

    # 8. Mark auth provenance for the interactive-only guard (§7) and audit
    # attribution (§8/§11): a leaked token's actions stay forensically
    # separable from the human's.
    request.state.auth_method = "pat"
    request.state.api_token_id = row.id

    # 9. Bind the request-scoped structlog context exactly as the JWT branch
    # does, plus the token id.
    structlog.contextvars.bind_contextvars(
        user_id=user.id,
        org_id=user.org_id,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
        api_token_id=row.id,
    )

    # Throttled last-used stamp on an independent session (swallows errors).
    # IP MUST come from get_client_ip (never raw request.client — CI-banned).
    await maybe_stamp_last_used(
        session_factory, row.id, row.last_used_at, get_client_ip(request)
    )

    # 10. Return the acting superadmin.
    return user


async def require_interactive_session(
    request: Request,
    user: User = Depends(get_current_user),
) -> User:
    """Gate that admits only interactive (JWT) sessions, never PATs (spec §7).

    It declares ``Depends(get_current_user)`` so that dependency — which stamps
    ``request.state.auth_method`` — is guaranteed to have run first (FastAPI
    does not order sibling dependencies; ``get_current_user`` is request-cached,
    so this is free). State is read defensively so an unset value stays
    fail-closed.
    """
    if getattr(request.state, "auth_method", None) != "jwt":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an interactive session",
        )
    return user
