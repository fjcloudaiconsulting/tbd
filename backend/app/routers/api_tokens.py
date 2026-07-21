"""Superadmin Personal Access Token (PAT) management API (spec §8).

Mounted at ``/api/v1/system/api-tokens``. Every route is gated by BOTH:

* ``require_superadmin`` — the caller must be a superadmin, and
* ``require_interactive_session`` (spec §7A) — the request must be an
  interactive JWT session, never a PAT. This is the token-mints-successor
  guard: a leaked ``write`` PAT can call the rest of the API, but it can
  never mint, list, or revoke tokens (all four routes here → 403 for a PAT).

Security-critical paths:

* **Step-up on mint (spec §8, mirrors ``users.py``):** a ``password_set``
  superadmin proves presence with ``current_password`` (``verify_password``);
  an SSO superadmin (``password_set=False``) with a fresh, constant-time
  ``stepup_token`` that is **consumed** on success; and — *additionally* —
  any operator with ``mfa_enabled`` must supply a fresh TOTP ``mfa_code``.
  Missing/wrong proof → 401. Operators without MFA are never asked for it.
* **Reveal-once (SEC-R5):** the plaintext token appears ONLY in the mint
  response body, under ``Cache-Control: no-store``. It is never logged and
  never written to an audit ``detail`` (asserted by a test).
* **Audit (spec §11):** ``api_token.created`` on success and on step-up
  failure; ``api_token.revoked``; ``api_token.revoked_all`` (with count).
  Detail carries name/scope/expiry/prefix — never the secret.
"""
import secrets
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.pat import require_interactive_session
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.notification import NotificationCategory
from app.models.user import User
from app.rate_limit import get_client_ip, limiter
from app.schemas.api_token import ApiTokenOut, MintTokenRequest, MintTokenResponse
from app.schemas.common import ListEnvelope
from app.security import verify_password
from app.services import api_token_service, audit_service, notification_service
from app.services.mfa_service import MfaConfigError, decrypt_secret, verify_totp
from app.services.notification_templates import api_token_created as _tpl_api_token_created


logger = structlog.stdlib.get_logger(__name__)

router = APIRouter(prefix="/api/v1/system/api-tokens", tags=["api-tokens"])


def _request_id() -> Optional[str]:
    return structlog.contextvars.get_contextvars().get("request_id")


def _aware(dt: datetime) -> datetime:
    """Treat a naive DB datetime as UTC before comparing to an aware now
    (the ``users.py`` step-up idiom — ``stepup_token_expires_at`` is naive)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def require_superadmin(
    current_user: User = Depends(get_current_user),
) -> User:
    """403 unless the caller is a superadmin (mirrors ``admin_broadcasts``).

    PAT management is platform-level, above the role system, so the gate is
    ``is_superadmin`` directly rather than ``require_permission``.
    """
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        )
    return current_user


def _step_up_401() -> HTTPException:
    """Generic step-up rejection (spec §8). One message for missing/wrong
    password, stepup token, or TOTP — no oracle for which factor failed."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Step-up verification required",
    )


def _verify_step_up(user: User, body: MintTokenRequest) -> bool:
    """Validate the mint step-up proofs against the LIVE user row (spec §8).

    Returns ``True`` when an SSO ``stepup_token`` was consumed-worthy (caller
    must null it after the mint transaction), ``False`` otherwise. Raises a
    generic 401 for any missing/invalid proof; raises 503 only for a genuine
    MFA config error (undecryptable secret), matching ``auth.py``.

    Does NOT mutate ``user`` — the SSO token consumption happens in the
    handler after every proof (including MFA) has passed, so a failed MFA
    check can't burn a valid step-up token.
    """
    now = datetime.now(timezone.utc)

    consume_stepup = False
    if user.password_set:
        if not body.current_password or not verify_password(
            body.current_password, user.password_hash
        ):
            raise _step_up_401()
    else:
        stored = user.stepup_token
        expires_at = user.stepup_token_expires_at
        valid = (
            bool(body.stepup_token)
            and stored is not None
            and expires_at is not None
            and _aware(expires_at) > now
            and secrets.compare_digest(body.stepup_token, stored)
        )
        if not valid:
            raise _step_up_401()
        consume_stepup = True

    # Additionally require a fresh TOTP for MFA-enabled operators. The trigger
    # is the canonical ``mfa_enabled`` flag, NOT ``totp_secret`` non-null
    # (which is set mid-enrollment before confirmation — SEC re-review F3).
    if user.mfa_enabled:
        if not body.mfa_code:
            raise _step_up_401()
        try:
            secret = decrypt_secret(user.totp_secret)
        except (ValueError, MfaConfigError):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="MFA configuration error — contact support",
            )
        if not verify_totp(secret, body.mfa_code):
            raise _step_up_401()

    return consume_stepup


def _out(row) -> ApiTokenOut:
    return ApiTokenOut(
        id=row.id,
        name=row.name,
        prefix=row.token_prefix,
        scope=row.scope,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        status=api_token_service.token_status(row),
    )


@router.post(
    "",
    response_model=MintTokenResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_interactive_session)],
)
@limiter.limit("10/hour")
async def mint_token(
    request: Request,
    response: Response,
    body: MintTokenRequest,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Mint a reveal-once PAT after step-up. Audit ``api_token.created`` on
    success AND on step-up failure; email + in-app notify on success."""
    actor_user_id = current_user.id
    actor_email = current_user.email

    # Step-up FIRST — a failed proof is a security event (someone with a
    # live-but-hijacked session trying to plant a backdoor token), so it is
    # audited before we reject.
    try:
        consume_stepup = _verify_step_up(current_user, body)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            await audit_service.record_audit_event(
                session_factory,
                event_type="api_token.created",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                target_org_id=None,
                target_org_name=None,
                request_id=_request_id(),
                ip_address=get_client_ip(request),
                outcome="failure",
                detail={
                    "name": body.name,
                    "scope": body.scope,
                    "expires_in_days": body.expires_in_days,
                    "reason": "step_up_failed",
                    "created_by": actor_email,
                },
            )
        raise

    # Consume the SSO step-up token now that every proof has passed, so it
    # can't be replayed across mint + another sensitive action (SEC F4). The
    # mutation rides the same ``db`` session that ``mint`` commits below.
    if consume_stepup:
        current_user.stepup_token = None
        current_user.stepup_token_expires_at = None

    plaintext, row = await api_token_service.mint(
        db,
        user=current_user,
        name=body.name,
        scope=body.scope,
        expires_in_days=body.expires_in_days,
    )

    # Audit success. Detail carries metadata only — NEVER the secret (SEC-R5).
    await audit_service.record_audit_event(
        session_factory,
        event_type="api_token.created",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "api_token_id": row.id,
            "name": row.name,
            "scope": row.scope,
            "prefix": row.token_prefix,
            "expires_at": row.expires_at.isoformat(),
            "created_by": actor_email,
        },
    )

    # Email + in-app notify the minting superadmin (SEC-R6a) so an
    # attacker-minted token is immediately visible. SECURITY category is
    # force-on and best-effort — a mailer failure never breaks the mint.
    title, ntf_body, link_url = _tpl_api_token_created(
        name=row.name, prefix=row.token_prefix
    )
    await notification_service.dispatch_notification_best_effort(
        db,
        user_id=actor_user_id,
        category=NotificationCategory.SECURITY,
        event_type="api_token.created",
        title=title,
        body=ntf_body,
        link_url=link_url,
    )
    await notification_service.send_security_email_best_effort(
        db,
        user_id=actor_user_id,
        email=actor_email,
        event_type="api_token.created",
        title=title,
        body=ntf_body,
        link_url=link_url,
    )

    await logger.ainfo(
        "api_token.created", api_token_id=row.id, scope=row.scope
    )

    # Reveal-once transport: plaintext only here, under no-store (SEC-R5).
    response.headers["Cache-Control"] = "no-store"
    return MintTokenResponse(
        token=plaintext,
        id=row.id,
        name=row.name,
        prefix=row.token_prefix,
        scope=row.scope,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


@router.get(
    "",
    response_model=ListEnvelope[ApiTokenOut],
    dependencies=[Depends(require_interactive_session)],
)
async def list_tokens(
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Metadata-only list of the caller's tokens, newest first. No secrets."""
    rows = await api_token_service.list_for(db, current_user)
    items = [_out(row) for row in rows]
    return {"items": items, "total": len(items), "limit": len(items), "offset": 0}


@router.delete(
    "/{token_id}",
    dependencies=[Depends(require_interactive_session)],
)
async def revoke_token(
    token_id: int,
    request: Request,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Soft-revoke one of the caller's tokens. 404 if not found / not owned /
    already revoked. Audit ``api_token.revoked``."""
    actor_user_id = current_user.id
    actor_email = current_user.email

    row = await api_token_service.revoke(db, token_id, current_user)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token not found",
        )

    await audit_service.record_audit_event(
        session_factory,
        event_type="api_token.revoked",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "api_token_id": row.id,
            "name": row.name,
            "scope": row.scope,
            "prefix": row.token_prefix,
            "created_by": actor_email,
        },
    )
    await logger.ainfo("api_token.revoked", api_token_id=row.id)
    return {"ok": True, "id": row.id}


@router.post(
    "/revoke-all",
    dependencies=[Depends(require_interactive_session)],
)
async def revoke_all_tokens(
    request: Request,
    current_user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Panic button — revoke every active token owned by the caller. Audit
    ``api_token.revoked_all`` with the count."""
    actor_user_id = current_user.id
    actor_email = current_user.email

    count = await api_token_service.revoke_all(db, current_user)

    await audit_service.record_audit_event(
        session_factory,
        event_type="api_token.revoked_all",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"count": count, "created_by": actor_email},
    )
    await logger.ainfo("api_token.revoked_all", count=count)
    return {"revoked": count}
