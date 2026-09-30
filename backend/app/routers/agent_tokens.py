"""Agent access tokens for every user (TBD-578), ``/api/v1/agent/tokens``.

Any active user whose org has ``ai.agent`` mints personal tokens with scope
``agent:read``, ``agent:write`` or ``agent:auto`` for their own AI harness.
They reuse ``api_tokens``; REST refuses these scopes (``app.auth.pat``), so
the only consumer is the MCP front door (``app.agent.auth``).

Every route is interactive-only (``require_interactive_session`` FIRST): a
token can never mint, widen or list tokens. Only mint needs ``ai.agent``; a
user whose org lost it must still be able to see and revoke tokens.

Mint = the PAT mint's step-up (own ``agent_token_mint`` action), an IP limit,
a per-user daily bucket counted BEFORE the step-up (so proofs cannot be
ground from rotating IPs; the flip side, accepted: a stolen session can spend
the owner's bucket for the day), max 5 live, reveal-once, owner email. Scope
changes only go DOWN (``agent:auto -> agent:write -> agent:read``); going up
means a new mint with step-up.
"""

from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import actions
from app.agent.registry import AGENT_FEATURE_KEY, ToolError
from app.auth.feature_deps import require_feature
from app.auth.org_permissions import require_org_admin
from app.auth.pat import require_interactive_session
from app.auth.stepup import consume_stepup
from app.database import get_db
from app.deps import get_session_factory
from app.models.api_token import ApiToken
from app.models.notification import NotificationCategory
from app.models.user import User
from app.rate_limit import get_client_ip, limiter
from app.rate_limit_overrides import dynamic_limit, load_rate_limit_overrides
from app.routers.api_tokens import _step_up_401, _verify_step_up
from app.schemas.agent_token import (
    AgentTokenMintRequest,
    AgentTokenMintResponse,
    AgentTokenOut,
    AgentTokenScopeUpdate,
    OrgAgentTokenOut,
)
from app.schemas.common import ListEnvelope
from app.security import token_cutoff
from app.services import api_token_service as svc
from app.services import audit_service, notification_service
from app.services.notification_templates import agent_token_created

logger = structlog.stdlib.get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/agent/tokens",
    tags=["agent"],
    dependencies=[Depends(require_interactive_session)],
)

MINT_PER_USER_PER_DAY = 10
_DAY = 86400


def _err(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _not_found() -> HTTPException:
    return _err(404, "token_not_found", "Token not found")


def _out(row: ApiToken, cutoff) -> dict[str, Any]:
    return dict(
        id=row.id,
        name=row.name,
        prefix=row.token_prefix,
        scope=row.scope,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        last_used_ip=row.last_used_ip,
        status=svc.agent_token_status(row, cutoff),
    )


def _envelope(items: list) -> dict[str, Any]:
    return {"items": items, "total": len(items), "limit": len(items), "offset": 0}


async def _audit(
    session_factory, request: Request, actor: tuple[int, str, int],
    event_type: str, outcome: str, detail: dict[str, Any],
) -> None:
    actor_id, actor_email, org_id = actor
    await audit_service.record_audit_event(
        session_factory,
        event_type=event_type,
        actor_user_id=actor_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=structlog.contextvars.get_contextvars().get("request_id"),
        ip_address=get_client_ip(request),
        outcome=outcome,
        detail=detail,
    )


@router.post(
    "",
    response_model=AgentTokenMintResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require_feature(AGENT_FEATURE_KEY)),
        Depends(load_rate_limit_overrides),
    ],
)
@limiter.limit(dynamic_limit("agent_tokens.mint", "10/hour"))
async def mint_agent_token(
    request: Request,
    response: Response,
    body: AgentTokenMintRequest,
    current_user: User = Depends(require_interactive_session),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    # Captured up front: after a failed consume_stepup or a refused mint the
    # session is rolled back and the ORM attributes are expired.
    actor = (current_user.id, current_user.email, current_user.org_id)
    cutoff_seen = token_cutoff(current_user)

    try:
        await actions._hit(
            f"agent:mint:usr:{current_user.id}", MINT_PER_USER_PER_DAY, _DAY,
            "mint_rate_limited",
        )
    except ToolError as exc:
        code = 503 if exc.code == "limits_unavailable" else 429
        raise _err(code, exc.code, exc.detail) from None

    try:
        sso_proof = _verify_step_up(current_user, body, "agent_token_mint")
        if sso_proof and not await consume_stepup(db, current_user):
            raise _step_up_401()
        plaintext, row = await svc.mint_agent(
            db,
            user=current_user,
            name=body.name,
            scope=body.scope,
            expires_in_days=body.expires_in_days,
            cutoff_seen=cutoff_seen,
        )
    except svc.SessionCutoffMoved:
        # Signed out everywhere (or password changed) mid-request: that wins.
        raise HTTPException(status_code=401, detail="Session has been invalidated") from None
    except svc.AgentTokenCapReached:
        raise _err(
            409, "too_many_agent_tokens",
            f"At most {svc.MAX_LIVE_AGENT_TOKENS} live agent tokens; revoke one first",
        ) from None
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            await _audit(session_factory, request, actor, "agent_token.created", "failure", {
                "name": body.name, "scope": body.scope,
                "expires_in_days": body.expires_in_days, "reason": "step_up_failed",
            })
        raise

    await _audit(session_factory, request, actor, "agent_token.created", "success", {
        "api_token_id": row.id, "name": row.name, "scope": row.scope,
        "prefix": row.token_prefix, "expires_at": row.expires_at.isoformat(),
    })
    title, ntf_body, link_url = agent_token_created(
        name=row.name, prefix=row.token_prefix, scope=row.scope
    )
    await notification_service.dispatch_notification_best_effort(
        db, user_id=actor[0], category=NotificationCategory.SECURITY,
        event_type="agent_token.created", title=title, body=ntf_body, link_url=link_url,
    )
    await notification_service.send_security_email_best_effort(
        db, user_id=actor[0], email=actor[1], event_type="agent_token.created",
        title=title, body=ntf_body, link_url=link_url,
    )
    await logger.ainfo("agent_token.created", api_token_id=row.id, scope=row.scope)

    response.headers["Cache-Control"] = "no-store"
    return AgentTokenMintResponse(
        token=plaintext, id=row.id, name=row.name, prefix=row.token_prefix,
        scope=row.scope, created_at=row.created_at, expires_at=row.expires_at,
    )


@router.get("", response_model=ListEnvelope[AgentTokenOut])
async def list_agent_tokens(
    current_user: User = Depends(require_interactive_session),
    db: AsyncSession = Depends(get_db),
):
    cutoff = token_cutoff(current_user)
    rows = await svc.list_agent_for(db, current_user)
    return _envelope([_out(r, cutoff) for r in rows])


@router.get("/org", response_model=ListEnvelope[OrgAgentTokenOut])
async def list_org_agent_tokens(
    current_user: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = await svc.list_agent_for_org(db, current_user.org_id)
    return _envelope([
        {**_out(t, token_cutoff(u)), "owner_user_id": u.id, "owner_email": u.email}
        for t, u in rows
    ])


@router.patch(
    "/{token_id}", response_model=AgentTokenOut,
    dependencies=[Depends(load_rate_limit_overrides)],
)
@limiter.shared_limit(dynamic_limit("agent_tokens.update", "30/minute"), scope="agent_tokens.update")
async def downgrade_agent_token(
    request: Request,
    token_id: int,
    body: AgentTokenScopeUpdate,
    current_user: User = Depends(require_interactive_session),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    actor = (current_user.id, current_user.email, current_user.org_id)
    row = await svc.get_own_agent(db, token_id, current_user)
    if row is None or svc.token_status(row) != "active":
        raise _not_found()
    before = row.scope
    if svc.AGENT_SCOPE_RANK[body.scope] >= svc.AGENT_SCOPE_RANK[before]:
        raise _err(
            422, "scope_not_downward",
            "A token's scope can only be lowered; mint a new token for more access",
        )
    row.scope = body.scope
    await db.commit()
    await _audit(session_factory, request, actor, "agent_token.downgraded", "success", {
        "api_token_id": row.id, "prefix": row.token_prefix, "from": before, "to": row.scope,
    })
    return _out(row, token_cutoff(current_user))


async def _revoke(
    db: AsyncSession, session_factory, request: Request, actor: tuple[int, str, int],
    row: Optional[ApiToken], extra: dict[str, Any],
) -> dict[str, Any]:
    if row is None or row.revoked_at is not None:
        raise _not_found()
    row.revoked_at = svc._naive_utc_now()
    await db.commit()
    await _audit(session_factory, request, actor, "agent_token.revoked", "success", {
        "api_token_id": row.id, "name": row.name, "scope": row.scope,
        "prefix": row.token_prefix, **extra,
    })
    await logger.ainfo("agent_token.revoked", api_token_id=row.id)
    return {"ok": True, "id": row.id}


@router.delete("/org/{token_id}", dependencies=[Depends(load_rate_limit_overrides)])
@limiter.shared_limit(
    dynamic_limit("agent_tokens.org_revoke", "30/minute"), scope="agent_tokens.org_revoke"
)
async def revoke_org_agent_token(
    request: Request,
    token_id: int,
    current_user: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    actor = (current_user.id, current_user.email, current_user.org_id)
    found = await svc.get_org_agent(db, token_id, current_user.org_id)
    row, owner_id = (found[0], found[1].id) if found else (None, None)
    return await _revoke(
        db, session_factory, request, actor, row,
        {"by_org_admin": True, "owner_user_id": owner_id},
    )


@router.delete("/{token_id}", dependencies=[Depends(load_rate_limit_overrides)])
@limiter.shared_limit(dynamic_limit("agent_tokens.revoke", "30/minute"), scope="agent_tokens.revoke")
async def revoke_agent_token(
    request: Request,
    token_id: int,
    current_user: User = Depends(require_interactive_session),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    actor = (current_user.id, current_user.email, current_user.org_id)
    row = await svc.get_own_agent(db, token_id, current_user)
    return await _revoke(db, session_factory, request, actor, row, {})


@router.post("/revoke-all", dependencies=[Depends(load_rate_limit_overrides)])
@limiter.limit(dynamic_limit("agent_tokens.revoke_all", "10/minute"))
async def revoke_all_agent_tokens(
    request: Request,
    current_user: User = Depends(require_interactive_session),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    actor = (current_user.id, current_user.email, current_user.org_id)
    count = await svc.revoke_all_agent(db, current_user)
    await _audit(session_factory, request, actor, "agent_token.revoked_all", "success",
                 {"count": count})
    await logger.ainfo("agent_token.revoked_all", count=count)
    return {"revoked": count}
