"""AI provider credentials router (PR1 of the AI tier train).

Mounted at ``/api/v1/settings/ai-providers``. Org-admin gating via
``require_org_admin``; cross-org isolation is enforced by querying
through ``get_credential_for_org(org_id=current_user.org_id)``
on every read/write path.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import logging
from typing import Optional
from urllib.parse import urlencode

import anyio
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import rate_limit_db, state_db
from app.auth.org_permissions import require_org_admin
from app.auth.pat import require_interactive_session
from app.config import settings
from app.database import get_db
from app.deps import get_session_factory
from app.models.org_ai_credential import AiProvider
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.common import ListEnvelope
from app.schemas.org_ai_credential import (
    OpenRouterOAuthComplete,
    OpenRouterOAuthStart,
    OrgAICredentialCreate,
    OrgAICredentialResponse,
    OrgAICredentialRotate,
    OrgAICredentialUpdate,
)
from app.schemas.org_ai_caps import (
    CapsBundleResponse,
    CapsWrite,
    DefaultCapsResponse,
    FeatureCapsResponse,
)
from app.schemas.org_ai_consent import (
    ConsentCreate,
    ConsentResponse,
    ConsentSnapshotResponse,
)
from app.schemas.org_ai_routing import (
    DefaultRoutingResponse,
    DefaultRoutingWrite,
    FeatureRoutingResponse,
    FeatureRoutingWrite,
    ModelListResponse,
    ModelOptionResponse,
    RoutingBundleResponse,
)
from app.security import (
    create_openrouter_oauth_state,
    decode_openrouter_oauth_state,
    decode_refresh_jti_sid,
    openrouter_oauth_sid_hmac,
    openrouter_pkce_challenge,
    openrouter_pkce_verifier,
    token_cutoff,
)
from app.services import (
    ai_caps_service,
    ai_consent_service,
    ai_credential_service,
    ai_routing_service,
    audit_service,
    platform_ai,
    platform_ai_settings,
    usage_service,
)
from app.services.ai_feature_map import CHAT_FEATURE
from app.services.ai_providers import openrouter_oauth
from app.services.ai_providers.openai_compatible import (
    OPENAI_COMPATIBLE_PRESETS,
    OPENROUTER_BASE_URL,
)
from app.services.ai_routing_service import (
    CrossOrgRoutingDenied,
    ModelListUnavailable,
    ModelNotListed,
    UnknownFeatureName,
)
from app.services.exceptions import ValidationError
from app import logs


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/settings/ai-providers",
    tags=["ai-providers"],
)


def _request_id() -> Optional[str]:
    return logs.CONTEXT.get({}).get("request_id")


@router.get("/options")
async def get_provider_options(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_org_admin),
) -> dict:
    """Provider picker metadata for the admin UI.

    Always returns ``not_yet_available`` for native in PR1. The
    ``AI_NATIVE_ENABLED`` env flag is reported back for visibility, but
    it does NOT control this field: even when the gate is flipped on,
    the credential-create path still refuses native (no backend ships
    in PR1). A future PR will gate this on ``AI_NATIVE_ENABLED`` once a
    real native backend exists. Returning ``available`` here while the
    create path rejects would be a UI lie.
    """
    models = await platform_ai_settings.offered(db, await platform_ai_settings.load(db), current_user.org_id)
    return {
        "providers": [
            {"key": AiProvider.OPENAI.value, "label": "OpenAI", "availability": "available"},
            {"key": AiProvider.ANTHROPIC.value, "label": "Anthropic", "availability": "available"},
            {"key": AiProvider.OLLAMA.value, "label": "Ollama", "availability": "available"},
            {"key": AiProvider.OPENAI_COMPATIBLE.value, "label": "OpenAI-compatible", "availability": "available"},
            {
                "key": AiProvider.NATIVE.value,
                "label": "Native (hosted by The Better Decision)",
                "availability": "not_yet_available",
            },
        ],
        # Hosts reached through the openai_compatible adapter; the form
        # offers them as extra choices that prefill ``base_url`` (TBD-590).
        "presets": [
            {**preset, "provider": AiProvider.OPENAI_COMPATIBLE.value}
            for preset in OPENAI_COMPATIBLE_PRESETS
        ],
        "ai_native_enabled": settings.ai_native_enabled,
        # Platform AI this org can turn on right now (TBD-586).
        "platform_providers": [
            {"key": "openrouter", "label": platform_ai.PLATFORM_LABEL, "models": models}
        ] if models else [],
    }


@router.get("", response_model=ListEnvelope[OrgAICredentialResponse])
async def list_credentials(
    sort_by: Optional[str] = Query(default=None),
    sort_dir: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_org_admin),
) -> ListEnvelope[OrgAICredentialResponse]:
    """Org-scoped AI credentials as a ``ListEnvelope`` so the
    settings/ai-providers credentials table can sort + page server-side.
    Org-scoped count + page share the same filter."""
    total = await ai_credential_service.count_credentials_for_org(
        db, org_id=current_user.org_id
    )
    try:
        rows = await ai_credential_service.list_credentials_for_org(
            db,
            org_id=current_user.org_id,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.detail
        ) from exc
    return ListEnvelope[OrgAICredentialResponse](
        items=[OrgAICredentialResponse.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=OrgAICredentialResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_interactive_session)],
)
async def create_credential(
    payload: OrgAICredentialCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> OrgAICredentialResponse:
    row = await ai_credential_service.create_credential(
        db,
        org_id=current_user.org_id,
        payload=payload,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return OrgAICredentialResponse.model_validate(row)


# --------------------------------------------------------------------
# Connect OpenRouter with OAuth PKCE (TBD-632). Declared BEFORE /{credential_id}.
# --------------------------------------------------------------------

_OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
_OAUTH_CALLBACK_PATH = "/settings/ai-providers/openrouter/callback"


def _oauth_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )


def _oauth_unavailable() -> HTTPException:
    return _oauth_error(
        status.HTTP_503_SERVICE_UNAVAILABLE, "oauth_unavailable", "Try again in a moment."
    )


async def _oauth_rate_limit(scope: str, user_id: int) -> None:
    # Per user (not per org): a second admin cannot exhaust a victim's budget.
    try:
        hits = await asyncio.to_thread(rate_limit_db.hit, f"{scope}:{user_id}", 3600)
    except SQLAlchemyError as exc:
        raise _oauth_unavailable() from exc
    if hits > 10:
        raise _oauth_error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "oauth_rate_limited",
            "Too many attempts. Try again later.",
        )


def _refresh_sid(request: Request) -> Optional[str]:
    raw = request.cookies.get("refresh_token")
    if not raw:
        return None
    try:
        return decode_refresh_jti_sid(raw)[1]
    # The decode raises PyJWTError subclasses as well as the ValueError it documents.
    except (ValueError, jwt.PyJWTError):
        return None


async def _audit_oauth_refused(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    reason: str,
    ref: Optional[str] = None,
) -> None:
    # The detail never holds the code, state, verifier, nonce or key.
    detail = {"reason": reason}
    if ref:
        detail["ref"] = ref
    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.credential.oauth_refused",
        actor_user_id=user.id,
        actor_email=user.email,
        target_org_id=user.org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="failure",
        detail=detail,
    )


@router.post(
    "/oauth/openrouter/start",
    dependencies=[Depends(require_interactive_session)],
)
async def openrouter_oauth_start(
    payload: OpenRouterOAuthStart,
    request: Request,
    current_user: User = Depends(require_org_admin),
) -> dict:
    await _oauth_rate_limit("ai_or_oauth_start", current_user.id)
    sid = _refresh_sid(request)
    if sid is None:
        raise _oauth_error(
            status.HTTP_409_CONFLICT,
            "oauth_session_unavailable",
            "Sign in again to connect OpenRouter.",
        )
    state, jti = create_openrouter_oauth_state(
        current_user.id, current_user.org_id, sid, payload.nonce_hash
    )
    params = {
        # From config, never from the request.
        "callback_url": settings.app_url + _OAUTH_CALLBACK_PATH,
        "code_challenge": openrouter_pkce_challenge(openrouter_pkce_verifier(jti)),
        "code_challenge_method": "S256",
        "key_label": "The Better Decision (" + jti[:8] + ")",
        "state": state,
    }
    return {"url": _OPENROUTER_AUTH_URL + "?" + urlencode(params)}


@router.post(
    "/oauth/openrouter/complete",
    response_model=OrgAICredentialResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_interactive_session)],
)
async def openrouter_oauth_complete(
    payload: OpenRouterOAuthComplete,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> OrgAICredentialResponse:
    # Rate limit before any audit write, so garbage states cannot flood audit_events.
    await _oauth_rate_limit("ai_or_oauth_complete", current_user.id)

    claims = decode_openrouter_oauth_state(payload.state)
    if claims is None:
        await _audit_oauth_refused(
            request, session_factory, current_user, "state_invalid"
        )
        raise _oauth_error(
            status.HTTP_400_BAD_REQUEST,
            "oauth_state_invalid",
            "The connection link is invalid or expired.",
        )
    jti = claims["jti"]
    ref = jti[:8]

    sid = _refresh_sid(request)
    bound = (
        int(claims["sub"]) == current_user.id
        and claims["org_id"] == current_user.org_id
        and sid is not None
        and hmac.compare_digest(openrouter_oauth_sid_hmac(sid), claims["sid_h"])
    )
    if bound:
        try:
            bound = await state_db.session_family_exists(sid)
        except SQLAlchemyError as exc:
            raise _oauth_unavailable() from exc
    bound = (
        bound
        and claims["iat"] >= token_cutoff(current_user).timestamp()
        and hmac.compare_digest(
            hashlib.sha256(payload.nonce.encode()).hexdigest(), claims["nh"]
        )
    )
    if not bound:
        await _audit_oauth_refused(
            request, session_factory, current_user, "state_mismatch", ref
        )
        raise _oauth_error(
            status.HTTP_409_CONFLICT,
            "oauth_state_mismatch",
            "This connection was started from a different session. Start again.",
        )

    # After the binding (a stranger cannot burn the victim's state), before the
    # exchange (a race cannot mint two keys).
    try:
        first_use = await state_db.claim_token("openrouter_oauth", jti, 660)
    except SQLAlchemyError as exc:
        raise _oauth_unavailable() from exc
    if not first_use:
        await _audit_oauth_refused(
            request, session_factory, current_user, "state_used", ref
        )
        raise _oauth_error(
            status.HTTP_409_CONFLICT,
            "oauth_state_used",
            "This connection link was already used. Start again.",
        )

    # Once the exchange is sent, any outcome but a clear refusal or a created
    # row may leave a live key at OpenRouter. The audit is shielded so an anyio
    # cancellation (client disconnect) cannot cancel it too; a native
    # task.cancel() (e.g. at shutdown) can still skip this row, which is accepted.
    async def _refused(reason: str) -> None:
        with anyio.CancelScope(shield=True):
            await _audit_oauth_refused(
                request, session_factory, current_user, reason, ref
            )

    try:
        key = await openrouter_oauth.exchange_code(
            payload.code, openrouter_pkce_verifier(jti)
        )
        row = await ai_credential_service.create_credential(
            db,
            org_id=current_user.org_id,
            payload=OrgAICredentialCreate(
                provider=AiProvider.OPENAI_COMPATIBLE,
                api_key=key,
                base_url=OPENROUTER_BASE_URL,
                label="OpenRouter (" + ref + ")",
            ),
            session_factory=session_factory,
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
        )
    except openrouter_oauth.CodeRejected as exc:
        await _refused("exchange_rejected")
        if exc.busy:
            raise _oauth_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "openrouter_busy",
                "OpenRouter is busy. Wait a minute and connect again.",
            ) from None
        raise _oauth_error(
            status.HTTP_400_BAD_REQUEST,
            "openrouter_code_rejected",
            "OpenRouter rejected the authorization. Start again.",
        ) from None
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        await _refused(
            "validation_failed"
            if detail.get("code") == "credential_validation_failed"
            else "orphan_possible"
        )
        raise
    except BaseException as exc:
        # error_class only: a ValidationError's str() carries the raw key.
        logger.warning(
            "ai.openrouter_oauth.orphan_possible",
            extra={"error_class": type(exc).__name__},
        )
        await _refused("orphan_possible")
        if not isinstance(exc, Exception):
            raise
        raise _oauth_error(
            status.HTTP_502_BAD_GATEWAY,
            "openrouter_unavailable",
            "Could not finish connecting OpenRouter.",
        ) from None
    # The row exists from here on: nothing below may claim an orphan.
    return OrgAICredentialResponse.model_validate(row)


# --------------------------------------------------------------------
# Platform AI switch (TBD-586). Declared BEFORE /{credential_id}.
# --------------------------------------------------------------------

PlatformProvider = platform_ai.PlatformProvider


@router.post(
    "/platform/{platform_provider}",
    response_model=OrgAICredentialResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_interactive_session)],
)
async def enable_platform(
    platform_provider: PlatformProvider,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> OrgAICredentialResponse:
    row = await ai_credential_service.create_platform_credential(
        db,
        org_id=current_user.org_id,
        platform_provider=platform_provider,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return OrgAICredentialResponse.model_validate(row)


# Deliberately no interactive-session requirement (TBD-586): turning platform
# AI OFF is the fail-safe direction and must stay PAT-reachable for incident
# response. Fenced by test_write_pat_can_still_disable_platform_provider.
@router.delete(
    "/platform/{platform_provider}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def disable_platform(
    platform_provider: PlatformProvider,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
):
    deleted = await ai_credential_service.delete_platform_credential(
        db,
        org_id=current_user.org_id,
        platform_provider=platform_provider,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------
# Routing endpoints (PR1). Declared BEFORE the /{credential_id}
# endpoints so the literal /routing prefix wins the route match.
# Service-layer cross-org check + DB composite FK both refuse
# cross-org credential references (T14). The DB FK is the catch-all;
# the service check returns a clear message instead of a raw FK
# violation. See spec §4.
# --------------------------------------------------------------------

ROUTING_PREFIX = "/routing"


def _cross_org_denied() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "cross_org_routing_denied",
            "message": "credential does not belong to this organization",
        },
    )


def _model_not_listed() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "model_not_listed",
            "message": "This model is not in the list for this credential. Refresh the model list or pick a listed model.",
        },
    )


def _unknown_feature(feature_name: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "unknown_feature",
            "message": f"feature_name '{feature_name}' is not routable",
        },
    )


@router.get(ROUTING_PREFIX, response_model=RoutingBundleResponse)
async def get_routing(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_org_admin),
) -> RoutingBundleResponse:
    default = await ai_routing_service.get_default_routing(
        db, org_id=current_user.org_id
    )
    features = await ai_routing_service.get_feature_routings(
        db, org_id=current_user.org_id
    )
    return RoutingBundleResponse(
        default=(
            DefaultRoutingResponse.model_validate(default)
            if default is not None
            else None
        ),
        features=[
            FeatureRoutingResponse.model_validate(f) for f in features
        ],
    )


@router.put(
    f"{ROUTING_PREFIX}/default",
    response_model=DefaultRoutingResponse,
    dependencies=[Depends(require_interactive_session)],
)
async def put_default_routing(
    payload: DefaultRoutingWrite,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> DefaultRoutingResponse:
    try:
        row = await ai_routing_service.set_default_routing(
            db,
            org_id=current_user.org_id,
            credential_id=payload.credential_id,
            model=payload.model,
            session_factory=session_factory,
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
        )
    except CrossOrgRoutingDenied:
        raise _cross_org_denied()
    except ModelNotListed:
        raise _model_not_listed()
    except ModelListUnavailable:
        raise HTTPException(status_code=402, detail={"code": "platform_ai_unavailable"})
    return DefaultRoutingResponse.model_validate(row)


@router.put(
    f"{ROUTING_PREFIX}/features/{{feature_name}}",
    response_model=FeatureRoutingResponse,
    dependencies=[Depends(require_interactive_session)],
)
async def put_feature_routing(
    feature_name: str,
    payload: FeatureRoutingWrite,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> FeatureRoutingResponse:
    try:
        row = await ai_routing_service.set_feature_routing(
            db,
            org_id=current_user.org_id,
            feature_name=feature_name,
            credential_id=payload.credential_id,
            model=payload.model,
            session_factory=session_factory,
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
        )
    except UnknownFeatureName:
        raise _unknown_feature(feature_name)
    except CrossOrgRoutingDenied:
        raise _cross_org_denied()
    except ModelNotListed:
        raise _model_not_listed()
    except ModelListUnavailable:
        raise HTTPException(status_code=402, detail={"code": "platform_ai_unavailable"})
    return FeatureRoutingResponse.model_validate(row)


@router.delete(
    f"{ROUTING_PREFIX}/features/{{feature_name}}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(require_interactive_session)],
)
async def delete_feature_routing(
    feature_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
):
    deleted = await ai_routing_service.delete_feature_routing(
        db,
        org_id=current_user.org_id,
        feature_name=feature_name,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------
# Caps endpoints (PR1). CRUD only — enforcement lands in PR2 alongside
# the ai_usage ledger. Declared BEFORE /{credential_id} to win the
# match for the literal /caps prefix.
# --------------------------------------------------------------------

CAPS_PREFIX = "/caps"


@router.get(CAPS_PREFIX, response_model=CapsBundleResponse)
async def get_caps(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_org_admin),
) -> CapsBundleResponse:
    default = await ai_caps_service.get_default_caps(
        db, org_id=current_user.org_id
    )
    features = await ai_caps_service.get_feature_caps(
        db, org_id=current_user.org_id
    )
    return CapsBundleResponse(
        default=(
            DefaultCapsResponse.model_validate(default)
            if default is not None
            else None
        ),
        features=[
            FeatureCapsResponse.model_validate(f) for f in features
        ],
    )


@router.put(
    f"{CAPS_PREFIX}/default",
    response_model=DefaultCapsResponse,
    dependencies=[Depends(require_interactive_session)],
)
async def put_default_caps(
    payload: CapsWrite,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> DefaultCapsResponse:
    row = await ai_caps_service.set_default_caps(
        db,
        org_id=current_user.org_id,
        soft_cap_cents=payload.soft_cap_cents,
        hard_cap_cents=payload.hard_cap_cents,
        period=payload.period,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return DefaultCapsResponse.model_validate(row)


@router.put(
    f"{CAPS_PREFIX}/features/{{feature_key}}",
    response_model=FeatureCapsResponse,
    dependencies=[Depends(require_interactive_session)],
)
async def put_feature_caps(
    feature_key: str,
    payload: CapsWrite,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> FeatureCapsResponse:
    try:
        row = await ai_caps_service.set_feature_caps(
            db,
            org_id=current_user.org_id,
            feature_key=feature_key,
            soft_cap_cents=payload.soft_cap_cents,
            hard_cap_cents=payload.hard_cap_cents,
            period=payload.period,
            session_factory=session_factory,
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
        )
    except UnknownFeatureName:
        raise _unknown_feature(feature_key)
    return FeatureCapsResponse.model_validate(row)


@router.delete(
    f"{CAPS_PREFIX}/features/{{feature_key}}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(require_interactive_session)],
)
async def delete_feature_caps(
    feature_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
):
    deleted = await ai_caps_service.delete_feature_caps(
        db,
        org_id=current_user.org_id,
        feature_key=feature_key,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------
# Consent endpoints (PR1). Append-only — POST writes a new row, GET
# returns the effective snapshot. Refusal logic in the native adapter
# lands in PR4 alongside the AI_NATIVE_ENABLED gate flip.
# --------------------------------------------------------------------

CONSENT_PREFIX = "/consent"


@router.get(CONSENT_PREFIX, response_model=ConsentSnapshotResponse)
async def get_consent(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_org_admin),
) -> ConsentSnapshotResponse:
    snapshot = await ai_consent_service.get_current_consents(
        db, org_id=current_user.org_id
    )
    return ConsentSnapshotResponse(
        allow_training=snapshot.allow_training,
        allow_rag=snapshot.allow_rag,
        allow_telemetry=snapshot.allow_telemetry,
        consent_version=snapshot.consent_version,
        consented_by_user_id=snapshot.consented_by_user_id,
        consented_at=snapshot.consented_at,
        has_consent=snapshot.has_consent,
        current_consent_version=settings.ai_native_current_consent_version,
    )


@router.post(
    CONSENT_PREFIX,
    response_model=ConsentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_interactive_session)],
)
async def post_consent(
    payload: ConsentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> ConsentResponse:
    row = await ai_consent_service.write_consent_row(
        db,
        org_id=current_user.org_id,
        consent_version=payload.consent_version,
        allow_training=payload.allow_training,
        allow_rag=payload.allow_rag,
        allow_telemetry=payload.allow_telemetry,
        revoked=payload.revoked,
        consented_by_user_id=current_user.id,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return ConsentResponse.model_validate(row)


@router.get("/{credential_id}", response_model=OrgAICredentialResponse)
async def get_credential(
    credential_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_org_admin),
) -> OrgAICredentialResponse:
    row = await ai_credential_service.get_credential_for_org(
        db,
        org_id=current_user.org_id,
        credential_id=credential_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return OrgAICredentialResponse.model_validate(row)


@router.get("/{credential_id}/models", response_model=ModelListResponse)
async def list_credential_models(
    credential_id: int,
    feature: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_org_admin),
) -> ModelListResponse:
    row = await ai_credential_service.get_credential_for_org(
        db, org_id=current_user.org_id, credential_id=credential_id
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if feature is not None:
        try:
            ai_routing_service.assert_known_feature(feature)
        except UnknownFeatureName:
            raise _unknown_feature(feature)
    # Only the reads sit in the try: a bug building the response stays a 500.
    period, left = None, None
    try:
        lst = await ai_routing_service.list_models(db, row, feature)
        if row.platform_provider is not None and feature in (None, CHAT_FEATURE):
            period, left = platform_ai.replies_for(
                await usage_service.current_usage(db, current_user.org_id, include_platform=True))
    except (SQLAlchemyError, PydanticValidationError):
        await db.rollback()
        raise HTTPException(status_code=402, detail={"code": "platform_ai_unavailable"})
    return ModelListResponse(
        default=lst.default,
        replies_period=period,
        models=[
            ModelOptionResponse(
                **dataclasses.asdict(o),
                replies_left=left(o.prompt_micros_per_mtok, o.completion_micros_per_mtok) if left else None)
            for o in lst.options
        ],
    )


@router.patch(
    "/{credential_id}",
    response_model=OrgAICredentialResponse,
    dependencies=[Depends(require_interactive_session)],
)
async def update_credential(
    credential_id: int,
    payload: OrgAICredentialUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> OrgAICredentialResponse:
    row = await ai_credential_service.get_credential_for_org(
        db,
        org_id=current_user.org_id,
        credential_id=credential_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ai_credential_service.assert_not_platform(row)
    updated = await ai_credential_service.update_credential_label(
        db,
        credential=row,
        label=payload.label,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return OrgAICredentialResponse.model_validate(updated)


@router.post(
    "/{credential_id}/rotate",
    response_model=OrgAICredentialResponse,
    dependencies=[Depends(require_interactive_session)],
)
async def rotate_credential(
    credential_id: int,
    payload: OrgAICredentialRotate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> OrgAICredentialResponse:
    row = await ai_credential_service.get_credential_for_org(
        db,
        org_id=current_user.org_id,
        credential_id=credential_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ai_credential_service.assert_not_platform(row)
    updated = await ai_credential_service.rotate_credential(
        db,
        credential=row,
        new_api_key=payload.api_key,
        new_bearer_token=payload.bearer_token,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return OrgAICredentialResponse.model_validate(updated)


@router.post(
    "/{credential_id}/validate",
    response_model=OrgAICredentialResponse,
    dependencies=[Depends(require_interactive_session)],
)
async def validate_credential(
    credential_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
) -> OrgAICredentialResponse:
    row = await ai_credential_service.get_credential_for_org(
        db,
        org_id=current_user.org_id,
        credential_id=credential_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ai_credential_service.assert_not_platform(row)
    # Per-(org, credential) 5 s cooldown (spec §6 T10), counted in the limits
    # DB (rate limits move to MySQL, INFRA-121). Acquired AFTER the 404 check
    # so probing nonexistent credentials can't poison real cooldown slots.
    if await asyncio.to_thread(
        rate_limit_db.hit, f"ai_validate:{current_user.org_id}:{credential_id}", 5
    ) > 1:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "validate_rate_limited",
                "message": "Please wait before validating this credential again.",
            },
        )
    updated = await ai_credential_service.validate_credential(
        db,
        credential=row,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return OrgAICredentialResponse.model_validate(updated)


@router.delete(
    "/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(require_interactive_session)],
)
async def delete_credential(
    credential_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_session_factory
    ),
    current_user: User = Depends(require_org_admin),
):
    row = await ai_credential_service.get_credential_for_org(
        db,
        org_id=current_user.org_id,
        credential_id=credential_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ai_credential_service.assert_not_platform(row)
    await ai_credential_service.delete_credential(
        db,
        credential=row,
        session_factory=session_factory,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
