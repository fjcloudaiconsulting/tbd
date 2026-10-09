"""MCP OAuth 2.1 authorization server endpoints (TBD-587).

Public: the two discovery documents (RFC 9728 / RFC 8414 fix their paths
outside ``/api/v1``), dynamic client registration (RFC 7591, public clients
only) and the token endpoint (credential = code + PKCE verifier, or a refresh
token). The consent endpoints are JWT + interactive-session only and gated on
``ai.agent`` with an open ``mcp.calls`` meter; the consent page itself is a
frontend route (``/oauth/authorize``).

Approving a consent is a credential mint: the per-user mint bucket (shared
with the manual mint, BEFORE the step-up), its own step-up action
``oauth_consent``, the 5-live cap under the owner lock, audit, in-app
notification and owner email, exactly as ``agent_tokens.mint_agent_token``.
Logic lives in ``app.services.oauth_service``.
"""
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.routing import Match

from app.agent import actions
from app.agent.registry import AGENT_FEATURE_KEY, ToolError
from app.auth.feature_deps import require_feature, require_meter_open
from app.auth.pat import require_interactive_session
from app.auth.stepup import consume_stepup
from app.config import settings
from app.database import get_db
from app.deps import get_session_factory
from app.models.notification import NotificationCategory
from app.models.user import User
from app.rate_limit import get_client_ip, limiter, rate_limit_key
from app.rate_limit_overrides import dynamic_limit, load_rate_limit_overrides
from app.routers.agent_tokens import _DAY, MINT_PER_USER_PER_DAY, _audit, _err
from app.routers.api_tokens import _step_up_401, _verify_step_up
from app.security import token_cutoff
from app.services import api_token_service as svc
from app.services import notification_service, oauth_service
from app.services.api_token_service import AGENT_SCOPE_RANK
from app.services.notification_templates import agent_token_created
from app.services.oauth_service import ConsentError, OAuthError

logger = structlog.stdlib.get_logger(__name__)


class _OffSwitchRoute(APIRoute):
    """Off (``MCP_OAUTH_ENABLED`` unset), these routes do not match: the
    request falls through to the plain 404 of an unknown path, never a 405,
    422 or 401 that says the route exists. Read per request."""

    def matches(self, scope):
        if not settings.mcp_oauth_enabled:
            return Match.NONE, {}
        return super().matches(scope)


router = APIRouter(tags=["oauth"], route_class=_OffSwitchRoute)

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_CONSENT_GATES = [
    Depends(require_feature(AGENT_FEATURE_KEY)),
    Depends(require_meter_open(AGENT_FEATURE_KEY, oauth_service.METER)),
]


def _oauth_error(exc: OAuthError) -> JSONResponse:
    content = {"error": exc.error}
    if exc.description:
        content["error_description"] = exc.description
    return JSONResponse(content, status_code=exc.status, headers=NO_STORE)


def _unavailable() -> JSONResponse:
    logger.exception("oauth.unavailable")
    return _oauth_error(OAuthError("temporarily_unavailable", status=503))


def _consent_400(exc: ConsentError) -> HTTPException:
    detail = {"code": exc.code}
    if exc.redirect_to is not None:
        detail["redirect_to"] = exc.redirect_to
    return HTTPException(status_code=400, detail=detail)


async def _validated(db: AsyncSession, params: dict):
    try:
        return await oauth_service.validate_consent(db, params)
    except ConsentError as exc:
        raise _consent_400(exc) from None


# ── discovery ──────────────────────────────────────────────────────────────


@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata():
    return oauth_service.protected_resource_metadata()


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata():
    return oauth_service.authorization_server_metadata()


# ── registration ───────────────────────────────────────────────────────────


@router.post("/api/v1/oauth/register", status_code=status.HTTP_201_CREATED)
@limiter.limit("1000/hour")
async def register_client(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        try:
            body = await request.json()
        except ValueError:
            raise OAuthError("invalid_client_metadata", "a JSON body is required") from None
        row = await oauth_service.register(db, body, rate_limit_key(request))
    except OAuthError as exc:
        return _oauth_error(exc)
    except SQLAlchemyError:
        return _unavailable()
    return JSONResponse(oauth_service.client_metadata(row), status_code=201, headers=NO_STORE)


# ── consent ────────────────────────────────────────────────────────────────


class AuthorizeRequest(BaseModel):
    client_id: Optional[str] = None
    redirect_uri: Optional[str] = None
    response_type: Optional[str] = None
    code_challenge: Optional[str] = None
    code_challenge_method: Optional[str] = None
    scope: Optional[str] = None
    resource: Optional[str] = None
    state: Optional[str] = None
    approve: bool
    granted_scope: Optional[str] = None
    current_password: Optional[str] = None
    stepup_token: Optional[str] = None
    mfa_code: Optional[str] = None


@router.get("/api/v1/oauth/authorize/context", dependencies=_CONSENT_GATES)
async def authorize_context(
    client_id: Optional[str] = None,
    redirect_uri: Optional[str] = None,
    response_type: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
    scope: Optional[str] = None,
    resource: Optional[str] = None,
    state: Optional[str] = None,
    current_user: User = Depends(require_interactive_session),
    db: AsyncSession = Depends(get_db),
):
    """What the consent page shows. Never stamps the client as used."""
    client, redirect, requested = await _validated(db, {
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": response_type,
        "code_challenge": code_challenge, "code_challenge_method": code_challenge_method,
        "scope": scope, "resource": resource, "state": state,
    })
    return {
        "client_id": client.id,
        "client_name": client.client_name,
        "client_name_verified": False,
        "redirect_uri": redirect,
        "redirect_host": oauth_service.redirect_host(redirect),
        "requested_scope": requested,
        "scopes_offered": oauth_service.scopes_offered(requested),
        "resource": oauth_service.resource(),
    }


@router.post(
    "/api/v1/oauth/authorize",
    dependencies=[*_CONSENT_GATES, Depends(load_rate_limit_overrides)],
)
@limiter.limit(dynamic_limit("oauth.authorize", "30/hour"))
async def authorize_decision(
    request: Request,
    response: Response,
    body: AuthorizeRequest,
    current_user: User = Depends(require_interactive_session),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    # Captured up front: a failed consume_stepup or a refused grant rolls the
    # session back, which expires every ORM attribute.
    actor = (current_user.id, current_user.email, current_user.org_id)
    cutoff_seen = token_cutoff(current_user)
    client, redirect, requested = await _validated(db, body.model_dump())
    cid, cname, state = client.id, client.client_name, body.state
    response.headers.update(NO_STORE)
    if not body.approve:
        return {"redirect_to": oauth_service.build_redirect(redirect, {"error": "access_denied"}, state)}
    granted = body.granted_scope
    if granted not in oauth_service.OAUTH_SCOPES or AGENT_SCOPE_RANK[granted] > AGENT_SCOPE_RANK[requested]:
        raise _consent_400(ConsentError(
            "invalid_scope", oauth_service.build_redirect(redirect, {"error": "invalid_scope"}, state)))

    try:
        await actions._hit(
            f"agent:mint:usr:{actor[0]}", MINT_PER_USER_PER_DAY, _DAY, "mint_rate_limited",
        )
    except ToolError as exc:
        raise _err(503 if exc.code == "limits_unavailable" else 429, exc.code, exc.detail) from None

    detail = {"oauth_client_id": cid, "redirect_host": oauth_service.redirect_host(redirect),
              "scope": granted}
    try:
        sso_proof = _verify_step_up(current_user, body, "oauth_consent")
        if sso_proof and not await consume_stepup(db, current_user):
            raise _step_up_401()
        code, row = await svc.create_oauth_grant(
            db, user=current_user, cutoff_seen=cutoff_seen, client_id=cid, client_name=cname,
            scope=granted, code_challenge=body.code_challenge, redirect_uri=redirect,
        )
    except IntegrityError:
        # The purge deleted the client between validation and the insert.
        await db.rollback()
        raise _consent_400(ConsentError("invalid_client")) from None
    except svc.SessionCutoffMoved:
        raise HTTPException(status_code=401, detail="Session has been invalidated") from None
    except svc.AgentTokenCapReached:
        raise _err(
            409, "too_many_agent_tokens",
            f"At most {svc.MAX_LIVE_AGENT_TOKENS} live agent tokens; revoke one first",
        ) from None
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            await _audit(session_factory, request, actor, "agent_token.created", "failure",
                         {**detail, "name": cname, "reason": "step_up_failed"})
        raise

    await _audit(session_factory, request, actor, "agent_token.created", "success", {
        **detail, "api_token_id": row.id, "name": row.name, "prefix": row.token_prefix,
        "expires_at": row.expires_at.isoformat(),
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
    await oauth_service.stamp_client_used(session_factory, cid)
    await logger.ainfo("agent_token.created", api_token_id=row.id, oauth_client_id=cid)
    return {"redirect_to": oauth_service.build_redirect(redirect, {"code": code}, state)}


# ── token ──────────────────────────────────────────────────────────────────


@router.post("/api/v1/oauth/token")
@limiter.limit("600/minute")
async def token_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Form parsed by hand (no ``Form(...)`` params): a 422 could echo the
    verifier or a token, and every error must be RFC 6749 JSON."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    try:
        if ctype != "application/x-www-form-urlencoded":
            raise OAuthError("invalid_request", "application/x-www-form-urlencoded body required")
        form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
        body = await oauth_service.token_request(
            db, session_factory, form, rate_limit_key(request), audit_ip=get_client_ip(request),
        )
    except OAuthError as exc:
        return _oauth_error(exc)
    except SQLAlchemyError:
        return _unavailable()
    return JSONResponse(body, headers=NO_STORE)
