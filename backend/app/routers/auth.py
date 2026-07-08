import asyncio
import re
import secrets
import hmac as _hmac
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, Cookie, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db
from app.deps import get_current_user, get_current_user_optional, get_session_factory
from app.services.feature_gate import Feature, resolve_feature
from app.models.account import AccountType, SYSTEM_ACCOUNT_TYPES
from app.models.category import Category, CategoryType, SYSTEM_CATEGORIES
from app.models.user import AVATAR_URL_MAX_LENGTH, Organization, Role, User
from app.models.subscription import Subscription, Plan
from app.services import subscription_service
from app.services.user_service import normalize_email
from app.schemas.auth import (
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    USERNAME_PATTERN,
    ForgotPasswordRequest,
    LoginRequest,
    MfaChallengeResponse,
    MfaDisableRequest,
    MfaEmailCodeRequest,
    MfaEmailVerifyRequest,
    MfaEnableRequest,
    MfaEnableResponse,
    MfaRecoveryRequest,
    MfaRegenerateRequest,
    MfaSetupResponse,
    MfaVerifyRequest,
    RegisterRequest,
    ResendVerificationPublicRequest,
    ResetPasswordRequest,
    StepUpInitiateRequest,
    TokenResponse,
    UsernameCheckResponse,
    UserResponse,
    VerifyEmailRequest,
    VerifyResponse,
)
from app.config import settings as app_settings
from app import redis_client
from app.redis_client import RedisRequired
from redis.exceptions import RedisError
from app.security import (
    MFA_EMAIL_TOKEN_TTL_SECONDS,
    create_access_token,
    create_email_verification_token,
    create_mfa_challenge_token,
    create_mfa_email_token,
    create_password_reset_token,
    create_refresh_token,
    decode_refresh_jti_sid,
    decode_token,
    default_session_ttl_seconds,
    get_org_session_ttl_seconds,
    hash_password,
    mfa_email_code_hmac,
    token_cutoff,
    verify_password,
)
from app.captcha import verify_captcha
from app.models.notification import NotificationCategory
from app.rate_limit import get_client_ip, limiter
from app.services import audit_service, notification_service
from app.services.email_service import send_mfa_email_code, send_password_reset_email, send_verification_email
from app.services.notification_templates import (
    user_mfa_disabled as _tpl_user_mfa_disabled,
    user_mfa_enabled as _tpl_user_mfa_enabled,
)
from app.services.mfa_service import (
    MfaConfigError,
    decrypt_secret,
    encrypt_secret,
    generate_qr_base64,
    generate_recovery_codes,
    generate_totp_secret,
    get_totp_uri,
    hash_recovery_code,
    verify_recovery_code,
    verify_totp,
)

GOOGLE_OAUTH_TIMEOUT = httpx.Timeout(10.0)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _user_response(user: User, org: Organization, sub: Subscription | None = None, plan: Plan | None = None) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        phone=user.phone,
        avatar_url=user.avatar_url,
        email_verified=user.email_verified,
        role=user.role.value,
        org_id=org.id,
        org_name=org.name,
        billing_cycle_day=org.billing_cycle_day,
        is_superadmin=user.is_superadmin,
        is_active=user.is_active,
        is_founder=user.is_founder,
        mfa_enabled=user.mfa_enabled,
        password_set=user.password_set,
        onboarded_at=user.onboarded_at.isoformat() if user.onboarded_at else None,
        allow_manual_balance_adjustment=org.allow_manual_balance_adjustment,
        subscription_status=sub.status.value if sub else None,
        subscription_plan=plan.slug if plan else None,
        trial_end=sub.trial_end.isoformat() if sub and sub.trial_end else None,
    )


def _suggest_username(first_name: str | None, last_name: str | None, email: str) -> str:
    """Generate a username suggestion from name or email."""
    parts = [p for p in [first_name, last_name] if p]
    if parts:
        slug = re.sub(r"[^a-z0-9]+", ".", " ".join(parts).lower().strip()).strip(".")
        if slug:
            return slug
    return email.split("@")[0].lower()


async def _find_available_username(db: AsyncSession, base: str) -> str:
    """Return base username if available, otherwise append a number."""
    candidate = base
    for i in range(100):
        exists = await db.scalar(
            select(User.id).where(User.username == candidate)
        )
        if not exists:
            return candidate
        candidate = f"{base}{i + 1}"
    return f"{base}{hash(base) % 10000}"


async def _create_org_with_defaults(db: AsyncSession, org_name: str) -> Organization:
    """Create an organization and seed system account types + categories.

    Delegates the seed to ``org_bootstrap_service.seed_org_defaults`` so
    the same logic backs both initial registration and the post-reset
    re-seed in ``org_data_service.reset_org_data``. Idempotent on the
    seed side; this caller path inserts a fresh org so no preexisting
    defaults can collide.
    """
    org = Organization(name=org_name)
    db.add(org)
    await db.flush()
    from app.services.org_bootstrap_service import seed_org_defaults
    await seed_org_defaults(db, org_id=org.id)
    return org


@router.get("/status")
async def auth_status(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user_optional),
):
    """Boot-time signals the frontend reads before rendering /register
    or /setup.

    ``needs_setup`` toggles the first-run admin bootstrap path.

    ``captcha_required`` mirrors the backend's enforcement gate so the
    Turnstile widget renders only when the server will actually demand
    a token. Exposing the flag here (instead of relying on a build-time
    ``NEXT_PUBLIC_*``) makes a backend ``CAPTCHA_REQUIRED=false`` flip a
    real rollback — the next page load drops both the verify check and
    the widget render together.

    ``billing_ui_enabled`` gates the customer-facing plan / trial /
    billing surface (trial banner, settings billing page + tab,
    landing-page trial copy). Same flip-to-rollback contract as
    ``captcha_required`` — backend False hides the surface on the next
    page load, backend True restores it.

    ``feature_reports_v2`` gates the Reports v2 surface (nav item,
    ``/reports/*`` routes). Same flip-to-rollback contract as the other
    feature flags here — when False the frontend hides the nav item and
    every ``/reports`` route returns 404; when True the surface lights
    up. The backend's own ``/api/v1/reports/*`` routes are independently
    gated via the ``require_reports_v2_enabled`` router dependency, so
    flipping this flag while the backend is False is a no-op.

    ``features`` exposes the resolved on/off state for each named feature
    flag.  When the caller presents a valid bearer token the flags are
    resolved per-org (OrgSetting → SystemSetting → env-floor).  Without a
    token the per-org lookup is skipped and resolution falls through to
    global SystemSetting → env-floor only.
    """
    org_id: int | None = user.org_id if user else None
    user_count = await db.scalar(select(func.count()).select_from(User))
    return {
        "needs_setup": user_count == 0,
        "captcha_required": app_settings.captcha_required,
        "billing_ui_enabled": app_settings.billing_ui_enabled,
        "feature_reports_v2": app_settings.feature_reports_v2,
        "features": {
            "reports": await resolve_feature(Feature.REPORTS, org_id, db),
            "plans": await resolve_feature(Feature.PLANS, org_id, db),
            "custom_dashboard": await resolve_feature(Feature.CUSTOM_DASHBOARD, org_id, db),
        },
    }


@router.get("/check-username", response_model=UsernameCheckResponse)
@limiter.limit("20/minute")
async def check_username(
    request: Request,
    username: str = Query(
        min_length=USERNAME_MIN_LENGTH,
        max_length=USERNAME_MAX_LENGTH,
        pattern=USERNAME_PATTERN,
    ),
    db: AsyncSession = Depends(get_db),
):
    """Check if a username is available and suggest alternatives."""
    exists = await db.scalar(
        select(User.id).where(User.username == username)
    )
    if not exists:
        return UsernameCheckResponse(available=True)
    suggestion = await _find_available_username(db, username)
    return UsernameCheckResponse(available=False, suggestion=suggestion)


@router.post("/register", response_model=UserResponse, status_code=201)
@limiter.limit("5/hour")
async def register(
    request: Request,
    body: RegisterRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    # Bot gate (Cloudflare Turnstile by default). Verify BEFORE any DB
    # lookup, user creation, or verification email so a flood of bad
    # tokens never reaches the database layer or the email provider.
    # Fail-closed on any non-OK result.
    #
    # First-run setup exception: when zero users exist the /setup flow
    # hits the same endpoint and the operator has no way to obtain a
    # token yet (no widget rendered before the app has finished
    # bootstrapping). Skip captcha for the first user only; every
    # subsequent registration goes through the gate.
    user_count = await db.scalar(select(func.count()).select_from(User))
    is_first_user_setup = user_count == 0
    if is_first_user_setup:
        captcha_result = None
    else:
        captcha_result = await verify_captcha(body.captcha_token, get_client_ip(request))
    if captcha_result is not None and not captcha_result.ok:
        await audit_service.record_audit_event(
            session_factory,
            event_type="auth.register.captcha_failed",
            actor_user_id=None,
            actor_email=normalize_email(body.email),
            target_org_id=None,
            target_org_name=None,
            request_id=request.headers.get("x-request-id"),
            ip_address=get_client_ip(request),
            outcome="failure",
            detail={
                "reason": captcha_result.reason,
                "provider_error_codes": list(captcha_result.provider_error_codes),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "captcha_failed",
                "message": "Could not verify you are human. Please try again.",
            },
        )

    email_norm = normalize_email(body.email)
    existing = await db.execute(
        select(User).where(or_(User.username == body.username, User.email == email_norm))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already taken",
        )

    existing_superadmin = await db.scalar(
        select(func.count()).select_from(User).where(User.is_superadmin == True)
    )
    is_first_user = existing_superadmin == 0

    org = await _create_org_with_defaults(
        db, body.org_name or f"{body.username}'s Organization"
    )

    user = User(
        org_id=org.id,
        username=body.username,
        email=email_norm,
        first_name=body.first_name,
        last_name=body.last_name,
        password_hash=hash_password(body.password),
        role=Role.OWNER,
        is_superadmin=is_first_user,
        is_founder=True,
    )
    db.add(user)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Registration conflict, please try again",
        )
    await db.refresh(user)
    await db.refresh(org)

    # Create trial subscription for the new org
    await subscription_service.create_trial(db, org.id)
    await db.commit()

    # Send verification email in background — don't block registration
    token = create_email_verification_token(user.id, user.email)
    background_tasks.add_task(send_verification_email, user.email, token)

    return _user_response(user, org)


@router.post("/login", response_model=TokenResponse | MfaChallengeResponse)
@limiter.limit("10/minute")
async def login(
    request: Request,
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    # Accept username or email
    result = await db.execute(
        select(User).where(
            or_(User.username == body.login, User.email == body.login)
        )
    )
    user = result.scalar_one_or_none()

    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )

    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "email_not_verified",
                "message": "Please verify your email to sign in.",
            },
        )

    # If MFA is enabled, return a challenge token instead of access tokens.
    # The login.success audit fires AFTER MFA completes (in /mfa/*), not
    # here, so the analytics count reflects "user actually signed in" not
    # "user passed first factor".
    if user.mfa_enabled:
        mfa_token = create_mfa_challenge_token(user.id)
        return MfaChallengeResponse(mfa_token=mfa_token)

    access_token = create_access_token(user.id, user.org_id, user.role.value)
    # PR 2: write the Redis primary key + family-set entry BEFORE
    # set_cookie. Fails closed (503) on unreachable Redis so we never
    # emit a cookie that has no corresponding session row.
    #
    # 2026-05-18 session-stability refactor: resolve the per-org TTL
    # once and use it for the JWT exp, the cookie Max-Age, AND the
    # Redis primary-key TTL so the org-level "Maximum session
    # duration" setting actually controls the session.
    ttl_seconds = await get_org_session_ttl_seconds(db, user.org_id)
    refresh_token, _jti, _sid = await _issue_refresh_session(
        user.id, ttl_seconds=ttl_seconds
    )

    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=ttl_seconds,
        path="/",
    )
    _clear_legacy_refresh_cookie(response)

    await _record_login_success(
        session_factory, user=user, request=request, method="password"
    )

    return TokenResponse(access_token=access_token)


# 2026-05-19: structured logging for every terminal refresh rejection.
# Ops needs to know WHICH of the seven 401 paths fired without seeing
# raw tokens — the screenshot from 2026-05-19T07:10 produced 401s with
# no distinguishing signal in the uvicorn access log.
#
# ``reason`` is a stable enum string; ``jti_h`` / ``sid_h`` are 8-char
# SHA-256 prefixes (sufficient for correlation, useless for replay).
# Never log raw jti/sid/cookie values — telemetry retention varies and
# raw refresh-token claims are a session-takeover vector.
_LOGGER = structlog.stdlib.get_logger()


def _hash_for_log(value: str | None) -> str | None:
    """8-char SHA-256 prefix of a refresh-token claim, safe to log.

    Returns None for None input. 8 hex chars (32 bits) is enough for
    ops to correlate the same jti across log lines on a single tab
    over a few minutes; not enough to reverse to the original jti
    even with the source code in hand.
    """
    if not value:
        return None
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _log_refresh_rejected(
    reason: str,
    *,
    jti: str | None = None,
    sid: str | None = None,
    extra: dict | None = None,
) -> None:
    """One structlog event per terminal refresh-cookie rejection with a
    stable ``reason`` enum. Ops correlates by ``request_id`` (already
    bound by ``RequestContextMiddleware``) + ``jti_h``/``sid_h`` to
    distinguish the eleven 401 paths.

    Gated by ``settings.auth_debug_logging`` (env ``AUTH_DEBUG_LOGGING``,
    default ``false``). Production stays quiet under normal operation;
    operators flip the flag on during incident triage to capture the
    reason field, flip it off again once diagnosis is in hand. The
    underlying terminal 401 still fires either way — only the
    diagnostic event is gated."""
    if not app_settings.auth_debug_logging:
        return
    detail = {
        "reason": reason,
        "jti_h": _hash_for_log(jti),
        "sid_h": _hash_for_log(sid),
    }
    if extra:
        detail.update(extra)
    _LOGGER.info("auth.refresh.rejected", **detail)


SESSION_EXPIRED_DETAIL = "Session expired — please sign in again"

# Standard 503 response detail returned from any issue / rotation site
# when Redis is unreachable. The auth-session story fails CLOSED: we
# refuse to issue a refresh JWT that has no corresponding Redis row,
# because such a JWT would 401 forever on /refresh. See
# specs/2026-05-17-backend-session-model.md §7.1.
SESSION_REDIS_UNAVAILABLE_DETAIL = "Authentication temporarily unavailable"


async def _issue_refresh_session(
    user_id: int,
    *,
    ttl_seconds: int | None = None,
    session_created_at: datetime | None = None,
    sid: str | None = None,
) -> tuple[str, str, str]:
    """Mint a refresh JWT AND atomically persist its Redis primary key +
    family-set entry. Returns ``(token, jti, sid)``.

    ``ttl_seconds`` controls the JWT ``exp``, the cookie ``Max-Age`` the
    caller writes alongside, AND the Redis primary-key TTL — all three
    in lockstep. Callers with org context should resolve it via
    ``await get_org_session_ttl_seconds(db, user.org_id)`` so the
    per-org "Maximum session duration" setting actually controls the
    session. When ``None`` the system default applies.

    Fails CLOSED on unreachable / broken Redis by raising
    ``HTTPException(503)`` — callers MUST let that propagate so no
    ``Set-Cookie`` is emitted for a session that has no Redis row.

    Used by every fresh-session issue path: login password branch,
    ``_issue_tokens`` (MFA branches), Google callback, and
    ``org_members.accept_invitation``. The ``/refresh`` rotation site
    uses :func:`_rotate_refresh_session` instead.
    """
    if ttl_seconds is None:
        ttl_seconds = default_session_ttl_seconds()
    token, jti, session_id = create_refresh_token(
        user_id,
        ttl_seconds=ttl_seconds,
        session_created_at=session_created_at,
        sid=sid,
    )
    try:
        await redis_client.session_issue(
            jti, session_id, user_id, ttl_seconds
        )
    except (RedisRequired, RedisError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SESSION_REDIS_UNAVAILABLE_DETAIL,
        ) from exc
    return token, jti, session_id


async def _rotate_refresh_session(
    user_id: int,
    old_jti: str,
    sid: str,
    *,
    ttl_seconds: int | None = None,
    session_created_at: datetime | None = None,
) -> tuple[str, str, str, str]:
    """Mint a successor refresh JWT (same ``sid``, fresh ``jti``) and run
    the atomic Lua rotation script (spec §4.2 step 5).

    Returns ``(token, new_jti, sid, lua_result)`` where ``lua_result``
    is one of ``"ok"``, ``"session_revoked"``, ``"already_rotated"``,
    or ``"jti_collision"``. The router dispatches on the value per
    §5.1 step 6:

    * ``"ok"`` — issue cookie, emit ``auth.session.rotated`` audit.
    * ``"session_revoked"`` — concurrent logout deleted the family
      set; router returns 401, no audit (terminal — frontend redirects).
    * ``"already_rotated"`` — concurrent ``/refresh`` won the race;
      router re-probes the grace key, emits a catch-up Set-Cookie for
      the winner's successor jti via
      ``_issue_catchup_refresh_cookie`` (2026-05-19 fix), and emits
      ``auth.session.grace_accept {via_already_rotated: true}``.
    * ``"jti_collision"`` — 128-bit RNG collision (cosmic). The router
      regenerates ``jti`` and retries once.

    On the ``ok`` path the new primary key is in Redis and the old
    primary has been replaced by a 30s grace key written inside the
    Lua transaction. On any non-``ok`` return the JWT is still freshly
    minted but no Redis writes happened — the router must NOT emit
    its Set-Cookie because no session row exists for the new jti.

    Fails CLOSED on unreachable Redis by raising ``HTTPException(503)``.

    ``ttl_seconds`` aligns the new JWT ``exp``, the new cookie
    ``Max-Age``, and the new Redis primary-key TTL. When ``None`` the
    system default applies; callers that know the org should resolve
    via ``get_org_session_ttl_seconds`` and pass it explicitly so the
    per-org session-length setting takes effect at the rotation site.
    """
    if ttl_seconds is None:
        ttl_seconds = default_session_ttl_seconds()
    token, new_jti, session_id = create_refresh_token(
        user_id,
        ttl_seconds=ttl_seconds,
        session_created_at=session_created_at,
        sid=sid,
    )
    try:
        result = await redis_client.session_rotate_lua(
            old_jti,
            new_jti,
            session_id,
            user_id,
            ttl_seconds,
        )
    except (RedisRequired, RedisError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SESSION_REDIS_UNAVAILABLE_DETAIL,
        ) from exc
    return token, new_jti, session_id, result

# Emitted when the request carries refresh cookies for two or more
# distinct user accounts (e.g. a legacy account-A cookie shadowing a
# current account-B cookie after an account switch). Auto-selecting
# either would silently authenticate the wrong identity, so the only
# safe response is to force a clean re-login.
AMBIGUOUS_SESSION_DETAIL = "Ambiguous session — please sign in again"

# Legacy refresh-cookie path used before PR #211 (commit 70ddd26,
# 2026-05-11) widened the cookie path to ``/``. Cookies set at this
# narrower path cannot be cleared by ``delete_cookie(path="/")`` because
# cookie removal requires an exact path match. Users carrying a pre-PR
# cookie therefore retain it alongside any post-PR ``Path=/`` cookie,
# and the browser sends BOTH on every request to ``/api/v1/auth/refresh``
# (the legacy path is more specific, so RFC 6265 orders it first).
# Whichever value Starlette's cookie parser picks may not be the one
# the user expects, producing spurious 401s. Every response that issues
# or clears the canonical ``Path=/`` cookie also emits a
# ``Path=/api/v1/auth/refresh`` delete so the legacy cookie is actively
# retired. Remove this cleanup once all pre-PR #211 cookies have aged
# out naturally — the legacy cookie's max_age was 7 days when it was
# written, so any browser that has hit /auth/refresh since 2026-05-18
# no longer carries one.
LEGACY_REFRESH_COOKIE_PATH = "/api/v1/auth/refresh"


def _clear_legacy_refresh_cookie(response: Response) -> None:
    """Emit a Set-Cookie that retires any pre-PR #211 ``refresh_token``
    cookie at the old ``Path=/api/v1/auth/refresh``. Safe to call
    alongside ``set_cookie(..., path="/")``: the two operate on
    distinct path-scoped cookie jars in the browser.
    """
    response.delete_cookie("refresh_token", path=LEGACY_REFRESH_COOKIE_PATH)


async def _issue_catchup_refresh_cookie(
    response: Response,
    *,
    user: User,
    successor_jti: str | None,
    sid: str,
    session_start: datetime | None,
    ttl_seconds: int,
) -> str:
    """Emit a Set-Cookie pointing at an EXISTING successor primary key.

    Used by the two ``/refresh`` grace branches that previously returned
    an access token without touching the refresh cookie — leaving the
    browser holding a jti that had been rotated past, and locking it
    out 30s later when the grace key expired. See architect note on PR
    #314 follow-up (2026-05-19).

    Contract:
      * ``successor_jti`` is read from ``grace_row["successor_jti"]``,
        the new primary jti written by the rotation winner inside the
        Lua transaction. Never derive it from a freshly-minted local
        ``new_jti`` — that would be the loser's perspective, not the
        winner's, and the Redis primary key for it does not exist.
      * Verifies the successor primary key is alive in Redis AND binds
        back to the same ``(user_id, sid)`` as the request. Mismatch
        or miss fails closed: logs ``catchup_successor_unavailable``
        and raises ``401`` — same terminal-401 shape the frontend
        classifier already handles. We never emit a Set-Cookie for a
        jti whose Redis row is gone, because that would just re-create
        the bug class one rotation later.
      * Mints a fresh JWT for the successor jti using
        ``create_refresh_token(..., jti=successor_jti)``. No Redis
        write — the row already exists from the winning rotation.
      * Preserves the original ``sid`` and ``session_created_at`` so
        the absolute-lifetime check still measures from the original
        login, not from the catch-up moment.

    Returns the encoded JWT string (for tests / future logging hooks).
    """
    if not successor_jti or not isinstance(successor_jti, str):
        _log_refresh_rejected(
            "catchup_successor_unavailable",
            jti=None,
            sid=sid,
            extra={"sub": user.id, "reason_detail": "missing_or_invalid_successor"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been invalidated",
        )

    try:
        successor_row = await redis_client.session_validate(successor_jti)
    except (RedisRequired, RedisError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SESSION_REDIS_UNAVAILABLE_DETAIL,
        ) from exc

    if (
        successor_row is None
        or successor_row.get("user_id") != user.id
        or successor_row.get("sid") != sid
    ):
        _log_refresh_rejected(
            "catchup_successor_unavailable",
            jti=successor_jti,
            sid=sid,
            extra={
                "sub": user.id,
                "successor_row_missing": successor_row is None,
                "successor_user_mismatch": (
                    successor_row is not None
                    and successor_row.get("user_id") != user.id
                ),
                "successor_sid_mismatch": (
                    successor_row is not None
                    and successor_row.get("sid") != sid
                ),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been invalidated",
        )

    # PR #308 made the family set the authoritative revocation contract:
    # a jti must be a member of ``auth:session:by_sid:{sid}`` to be
    # treated as a live session. The primary row + binding match above
    # are necessary but not sufficient — corrupted/partial Redis state
    # could leave the row in place after the family was revoked, and
    # the next primary-path /refresh would reject the catch-up cookie
    # as ``family_member_missing``. Verify membership here so the
    # browser never receives a cookie the very next request would 401.
    try:
        is_family_member = await redis_client.session_family_member(
            sid, successor_jti
        )
    except (RedisRequired, RedisError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SESSION_REDIS_UNAVAILABLE_DETAIL,
        ) from exc
    if not is_family_member:
        _log_refresh_rejected(
            "catchup_successor_unavailable",
            jti=successor_jti,
            sid=sid,
            extra={
                "sub": user.id,
                "successor_family_member_missing": True,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been invalidated",
        )

    token, _jti, _sid = create_refresh_token(
        user.id,
        ttl_seconds=ttl_seconds,
        session_created_at=session_start,
        sid=sid,
        jti=successor_jti,
    )

    response.set_cookie(
        key="refresh_token",
        value=token,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=ttl_seconds,
        path="/",
    )
    _clear_legacy_refresh_cookie(response)
    return token


def _extract_refresh_cookies(request: Request) -> list[str]:
    """Return ALL ``refresh_token`` cookie values from the request's
    Cookie header, in arrival order.

    Starlette's cookie parser collapses duplicate names to a single value
    (last one wins per dict semantics). After the PR #211 cookie-path
    migration a single browser may carry both a legacy
    ``Path=/api/v1/auth/refresh`` cookie and a current ``Path=/`` cookie,
    sent together as two ``refresh_token=`` entries in the Cookie header.
    Walking the raw header lets ``_validate_refresh_cookie`` try every
    value and accept the first that validates, rather than gambling on
    whichever single value the parser picks.
    """
    cookie_header = request.headers.get("cookie") or ""
    values: list[str] = []
    if not cookie_header:
        return values
    # Cookie names cannot contain ``=`` per RFC 6265; ``partition`` is
    # therefore unambiguous. Cookie values may contain ``=`` (JWT base64
    # padding) — that is why we partition rather than split.
    for part in cookie_header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == "refresh_token":
            values.append(value)
    return values


async def _validate_single_refresh_token(
    refresh_token: str,
    db: AsyncSession,
) -> tuple[User, dict, datetime | None, str, int, dict]:
    """Validate ONE refresh-token JWT value. Returns
    ``(user, payload, session_start, redis_state, ttl_seconds, session_row)``
    or raises ``HTTPException(401)``.

    ``redis_state`` is ``"primary"`` when the active session key
    ``auth:session:{jti}`` is present, or ``"grace"`` when only the
    rotation grace key ``auth:session:grace:{jti}`` is present AND the
    session family ``auth:session:by_sid:{sid}`` still exists. PR 3
    introduces this state so ``/refresh`` and ``/verify`` can absorb
    cross-tab rotation races without forcing a logout — see
    ``specs/2026-05-17-backend-session-model.md`` §5.1 step 4 / §5.2.

    ``session_row`` is the resolved Redis payload — the primary row's
    ``{user_id, sid}`` on the primary branch, OR the grace row's
    ``{user_id, sid, successor_jti}`` on the grace branch. ``/refresh``
    uses ``successor_jti`` to issue a catch-up Set-Cookie that points
    the browser at the live successor primary key — the 2026-05-19
    fix for the stale-cookie / locked-out-after-grace-expiry bug.

    The validation chain:
      1. JWT decode + ``type == "refresh"``
      2. user exists + ``is_active``
      3. ``iat < token_cutoff(user)`` rejects tokens issued before the
         user's last logout / password change / password reset
      4. absolute session lifetime (per-org ``session_lifetime_days``
         setting or system default) — raises with detail
         ``SESSION_EXPIRED_DETAIL`` so callers can recognize and act
         (e.g. ``/refresh`` clears the cookie; ``/verify`` does not)

    Note: this helper never writes a cookie. Cookie management is the
    caller's responsibility so the no-Set-Cookie invariant on ``/verify``
    is absolute.
    """
    payload = decode_token(refresh_token)
    if payload is None or payload.get("type") != "refresh":
        _log_refresh_rejected("invalid_token_decode")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    user_id = int(payload["sub"])
    jti = payload.get("jti")
    sid = payload.get("sid")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        _log_refresh_rejected(
            "user_not_found_or_inactive",
            jti=jti,
            sid=sid,
            extra={"sub": user_id},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    # Reject tokens issued before the user's session cutoff
    # (logout / password change / password reset)
    iat = payload.get("iat")
    if iat is not None:
        token_issued_at = datetime.fromtimestamp(iat, tz=timezone.utc)
        if token_issued_at < token_cutoff(user):
            _log_refresh_rejected(
                "iat_before_cutoff",
                jti=jti,
                sid=sid,
                extra={
                    "sub": user_id,
                    "iat": iat,
                    "cutoff": int(token_cutoff(user).timestamp()),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been invalidated",
            )

    # PR 2 (specs/2026-05-17-backend-session-model.md §5.1 step 3 +
    # step 4): both ``jti`` and ``sid`` are mandatory on every refresh
    # JWT issued after PR 2 ships. Legacy tokens (no jti / no sid) are
    # rejected with the same 401 string the cutoff check uses so the
    # frontend's terminal-vs-transient classifier needs no change. The
    # planned reauth break is operator-decision Q7 — see
    # infra/PR2_REAUTH_BREAK.md.
    if not jti or not sid:
        _log_refresh_rejected(
            "missing_jti_or_sid",
            jti=jti,
            sid=sid,
            extra={"sub": user_id},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been invalidated",
        )

    # Redis primary-key probe. Miss => fall back to grace key (spec §5.1
    # step 4 + §5.2). If both miss => 401. Redis-unreachable => 503; we
    # never silently accept the JWT, because that would defeat the
    # per-session story. See spec §7.1.
    redis_state: str = "primary"
    try:
        session_row = await redis_client.session_validate(jti)
        if session_row is None:
            # PR 3: grace fallback. The primary key has been rotated out
            # but the grace key (30s TTL) may still be alive — that's
            # the cross-tab race the rotation grace window exists to
            # absorb. The grace row carries the same ``user_id`` and
            # ``sid`` so the resolver can still bind back to JWT claims.
            # Defence-in-depth: ALSO verify the family set still exists
            # (concurrent logout deletes it before the grace TTL).
            grace_row = await redis_client.session_grace(jti)
            if grace_row is not None:
                if await redis_client.session_family_exists(sid):
                    session_row = grace_row
                    redis_state = "grace"
    except (RedisRequired, RedisError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SESSION_REDIS_UNAVAILABLE_DETAIL,
        ) from exc
    if session_row is None:
        _log_refresh_rejected(
            "redis_primary_and_grace_missing",
            jti=jti,
            sid=sid,
            extra={"sub": user.id},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been invalidated",
        )

    # Architect P2 finding on PR #306: existence of the Redis row is a
    # necessary but not sufficient success condition. The row stores
    # ``{user_id, sid}`` precisely so the resolver can verify the JWT
    # claims still bind to it; if any of the following diverge, the
    # session must be rejected as corrupt:
    #
    #   * the JWT's ``sub`` (user_id) does not match the row's
    #     ``user_id`` — could be: a forged JWT signed with a stolen
    #     key, an admin merged two users, or (in theory) the
    #     impossible-but-defended-against ``jti`` collision the PR 3
    #     Lua ``NX`` guard exists to catch;
    #   * the JWT's ``sid`` (session family) does not match the row's
    #     ``sid`` — could be: a leaked refresh cookie reused after the
    #     family was reissued under a different ``sid``, or key-level
    #     corruption from a future migration / replica lag.
    #
    # In either case we want the same terminal 401 the missing-key
    # path produces; the frontend's classifier needs no new code path.
    row_user_id = session_row.get("user_id")
    row_sid = session_row.get("sid")
    if row_user_id != user.id or row_sid != sid:
        _log_refresh_rejected(
            "row_binding_mismatch",
            jti=jti,
            sid=sid,
            extra={
                "sub": user.id,
                "row_user_id": row_user_id,
                "row_sid_h": _hash_for_log(row_sid) if isinstance(row_sid, str) else None,
                "redis_state": redis_state,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been invalidated",
        )

    # Architect P1 finding on PR #308: family-set membership is the
    # authoritative revocation contract, not primary-key existence.
    # The Lua rotation script enforces ``SISMEMBER by_sid {jti}`` on
    # ``/refresh`` (spec §4.2 step 5 check 1), but ``/verify`` does
    # NOT run the Lua, so without this check a verify call could
    # accept a primary key whose family set has already been deleted
    # by Round A of a concurrent logout.
    #
    # The window is small but real:
    #   * Logout Round A deletes ``auth:session:by_sid:{sid}``.
    #   * Logout Round B deletes the primary + grace keys for every
    #     jti, but is a separate MULTI/EXEC; a Redis connection drop
    #     between the two rounds leaves primary keys orphaned.
    #   * Any ``/verify`` arriving in that window with the
    #     pre-logout cookie used to silently succeed.
    #
    # Apply only on the primary path; the grace path (above) already
    # gates on ``session_family_exists``. The membership check is
    # stronger than family-exists because it also catches the
    # impossible-but-NX-defended ``jti`` collision where two sessions
    # share a ``sid`` but only one ``jti`` is in the family set.
    if redis_state == "primary":
        try:
            in_family = await redis_client.session_family_member(sid, jti)
        except (RedisRequired, RedisError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=SESSION_REDIS_UNAVAILABLE_DETAIL,
            ) from exc
        if not in_family:
            _log_refresh_rejected(
                "family_member_missing",
                jti=jti,
                sid=sid,
                extra={"sub": user.id},
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been invalidated",
            )

    # Resolve the per-org session TTL once — used BOTH for the absolute-
    # lifetime check below AND propagated to the caller so /refresh
    # rotation can write the new cookie / JWT / Redis TTL in lockstep.
    # Single helper call avoids drift between the validation ceiling
    # and the issue-site ceiling.
    ttl_seconds = await get_org_session_ttl_seconds(db, user.org_id)

    # Enforce absolute session lifetime against the resolved TTL.
    session_created_at = payload.get("session_created_at")
    session_start: datetime | None = None
    if session_created_at:
        session_start = datetime.fromtimestamp(session_created_at, tz=timezone.utc)
        if datetime.now(timezone.utc) - session_start > timedelta(seconds=ttl_seconds):
            _log_refresh_rejected(
                "absolute_lifetime_expired",
                jti=jti,
                sid=sid,
                extra={
                    "sub": user.id,
                    "ttl_seconds": ttl_seconds,
                    "age_seconds": int(
                        (datetime.now(timezone.utc) - session_start).total_seconds()
                    ),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=SESSION_EXPIRED_DETAIL,
            )

    return user, payload, session_start, redis_state, ttl_seconds, session_row


async def _validate_refresh_cookie(
    refresh_tokens: list[str],
    db: AsyncSession,
) -> tuple[User, dict, datetime | None, str, int, dict]:
    """Validate the provided refresh-token cookie values and pick one.

    Rules:
      - No tokens at all → ``401 "No refresh token"``.
      - All tokens fail validation → re-raise the last failure so single-
        cookie error semantics are preserved when only one cookie was
        actually presented.
      - At least one token validates AND every successful token resolves
        to the SAME ``user.id`` → pick the newest token (highest ``iat``)
        for that user and return it.
      - Successful tokens map to MORE THAN ONE distinct ``user.id`` →
        raise ``401 AMBIGUOUS_SESSION_DETAIL``. Auto-selecting either
        would silently authenticate the wrong identity (an attacker who
        could plant a second valid refresh cookie could otherwise switch
        the active account on the next refresh). The route caller is
        responsible for clearing both canonical and legacy cookies on
        this path; ``/verify`` lets the exception propagate without
        touching cookies (no-Set-Cookie invariant).

    Walking every ``refresh_token`` value found in the Cookie header is
    necessary because Starlette's parser collapses duplicate names to a
    single value (last wins) — after the PR #211 path migration a
    browser may carry both a legacy ``Path=/api/v1/auth/refresh`` cookie
    and a current ``Path=/`` cookie, and the legacy one may be the one
    the parser surfaces.
    """
    if not refresh_tokens:
        # 2026-05-19: log the no-cookie case explicitly. The overnight
        # logout symptom (the 2026-05-19T07:10 incident) had the
        # browser stop sending the refresh cookie at some point; this
        # reason code is what lets ops distinguish "cookie missing"
        # from "cookie present but invalid".
        _log_refresh_rejected("no_refresh_token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No refresh token",
        )

    successes: list[tuple[User, dict, datetime | None, str, int, dict]] = []
    last_exc: HTTPException | None = None
    transient_exc: HTTPException | None = None
    for token in refresh_tokens:
        try:
            successes.append(await _validate_single_refresh_token(token, db))
        except HTTPException as exc:
            last_exc = exc
            # 2026-05-19: remember the FIRST 5xx (transport / Redis
            # unavailable) we saw across the cookie list. When a
            # legacy + current cookie pair is present, a Redis
            # transport failure on the valid cookie followed by an
            # invalid-token rejection on the stale one would otherwise
            # let the 401 win as the final ``last_exc`` — terminal
            # logout instead of recoverable retry. Prefer the 5xx.
            if exc.status_code >= 500 and transient_exc is None:
                transient_exc = exc

    if not successes:
        # Prefer the 5xx (transient / Redis unavailable) over any 4xx
        # (terminal-auth) seen across the cookie list. The frontend's
        # classifier treats 5xx as transient and retries on a fresh
        # connection; treating it as a 401 would force a real logout
        # for what is actually a recoverable infra blip.
        chosen = transient_exc or last_exc
        assert chosen is not None  # loop ran at least once
        raise chosen

    distinct_user_ids = {tup[0].id for tup in successes}
    if len(distinct_user_ids) > 1:
        _log_refresh_rejected(
            "ambiguous_session_multiple_users",
            extra={"distinct_user_count": len(distinct_user_ids)},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AMBIGUOUS_SESSION_DETAIL,
        )

    # Single user, possibly multiple valid tokens. Prefer the newest by
    # ``iat`` so a stale legacy cookie never out-votes the current one
    # for the same user.
    successes.sort(key=lambda tup: tup[1].get("iat", 0), reverse=True)
    return successes[0]


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Rotate the refresh cookie + issue a fresh access token.

    Shares the full validation chain with ``/verify`` via
    ``_validate_refresh_cookie``. On session-lifetime expiry this endpoint
    additionally clears the stale cookie before returning 401; ``/verify``
    deliberately does not (it must never emit Set-Cookie).

    PR 3 dispatch (spec §5.1 step 6):

    - If the validation chain says ``redis_state == "grace"`` we're on
      the grace branch already (primary key gone, grace key alive,
      family set alive). Issue an access token AND emit a catch-up
      Set-Cookie pointing at ``grace_row["successor_jti"]`` (the
      2026-05-19 fix) so the browser converges on the live primary
      and isn't locked out 30s later when the grace key expires. No
      Redis writes on this path — the winning rotation already wrote
      the successor row. Emit ``auth.session.grace_accept``.
    - Otherwise run the Lua rotation script and dispatch on its return:
      ``ok`` issues a new cookie via the normal rotation flow;
      ``session_revoked`` returns 401; ``already_rotated`` re-probes
      grace + family set then emits a catch-up Set-Cookie for the
      winner's successor jti (same catch-up helper); ``jti_collision``
      regenerates and retries once, with a 503 on the second collision.

    NOTE: FastAPI does NOT merge cookies set on the injected ``response``
    parameter into the JSONResponse it builds from a raised HTTPException
    (the same gotcha the SSO callback works around by writing cookies
    onto a directly-returned RedirectResponse). For the session-expiry
    path we therefore return a JSONResponse directly so the
    delete-cookie header actually reaches the browser.

    A route-local ``asyncio.wait_for`` bounds the entire handler at
    ``settings.refresh_handler_timeout_s`` (default 25 s). If anything
    in the call chain (Redis pool acquire, MySQL pool checkout,
    pre_ping on a stale socket, etc.) hangs longer than that, the
    handler returns 503 with ``SESSION_REDIS_UNAVAILABLE_DETAIL`` and
    emits ``auth.refresh.handler_timeout`` so operators can correlate.
    Without this bound, a wedged dependency blocks the request
    silently until the frontend's reactive-recovery abort fires at
    45 s with no matching backend access log — the smoking-gun
    signature operators previously had no observability into.
    """
    try:
        return await asyncio.wait_for(
            _refresh_impl(request, response, db, session_factory),
            timeout=app_settings.refresh_handler_timeout_s,
        )
    except asyncio.TimeoutError:
        _LOGGER.warning(
            "auth.refresh.handler_timeout",
            extra={"timeout_s": app_settings.refresh_handler_timeout_s},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SESSION_REDIS_UNAVAILABLE_DETAIL,
        )


async def _refresh_impl(
    request: Request,
    response: Response,
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
):
    """Body of ``/auth/refresh``, extracted so the route handler can
    apply a route-local ``asyncio.wait_for(...)`` ceiling. The full
    spec lives on the public ``refresh()`` handler above; do not
    duplicate it here."""
    refresh_tokens = _extract_refresh_cookies(request)
    try:
        user, payload, session_start, redis_state, ttl_seconds, session_row = await _validate_refresh_cookie(
            refresh_tokens, db
        )
    except HTTPException as exc:
        # Two terminal paths clear BOTH the canonical and the legacy
        # cookie so the browser stops sending them: absolute session
        # expiry (a normal end-of-session signal) and ambiguous session
        # (request carried valid refresh cookies for >1 distinct user;
        # the only safe response is to force a clean re-login). Other
        # 401s leave the cookie in place — they may be transient
        # (e.g. invalid-but-recoverable) or carry their own meaning.
        if exc.detail in (SESSION_EXPIRED_DETAIL, AMBIGUOUS_SESSION_DETAIL):
            from fastapi.responses import JSONResponse

            cleared = JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )
            cleared.delete_cookie("refresh_token", path="/")
            _clear_legacy_refresh_cookie(cleared)
            return cleared
        raise

    # PR 2 rotation: preserve the predecessor's ``sid`` so the family
    # link survives across the rotation chain (per-session logout in
    # PR 4 walks ``auth:session:by_sid:{sid}``). The validation chain
    # has already verified that ``jti`` and ``sid`` are present.
    old_jti = payload["jti"]
    sid = payload["sid"]

    access_token = create_access_token(user.id, user.org_id, user.role.value)

    # ── Grace-path early return (spec §5.1 step 4) ──────────────────────
    # The validator already confirmed the grace key + family set are both
    # alive AND the JWT's sid matches the grace row's sid (the user_id /
    # sid mismatch check on the row catches forged JWTs). Issue a
    # catch-up Set-Cookie pointing at the live successor primary so the
    # browser stops sending the stale jti (the 2026-05-19 fix for
    # post-grace lockout — see ``_issue_catchup_refresh_cookie``).
    if redis_state == "grace":
        await _issue_catchup_refresh_cookie(
            response,
            user=user,
            successor_jti=session_row.get("successor_jti"),
            sid=sid,
            session_start=session_start,
            ttl_seconds=ttl_seconds,
        )
        await _record_session_grace_accept(
            session_factory,
            user=user,
            request=request,
            old_jti=old_jti,
            sid=sid,
            via_already_rotated=False,
        )
        return TokenResponse(access_token=access_token)

    # ── Normal rotation path: Lua script is the authority ───────────────
    new_refresh_token, new_jti, _sid, lua_result = await _rotate_refresh_session(
        user.id,
        old_jti,
        sid,
        ttl_seconds=ttl_seconds,
        session_created_at=session_start,
    )

    if lua_result == redis_client.SESSION_ROTATE_JTI_COLLISION:
        # 128-bit collision — regenerate jti once and retry. If the second
        # attempt also collides, the RNG is broken: 503 + structlog flag.
        new_refresh_token, new_jti, _sid, lua_result = await _rotate_refresh_session(
            user.id,
            old_jti,
            sid,
            ttl_seconds=ttl_seconds,
            session_created_at=session_start,
        )
        if lua_result == redis_client.SESSION_ROTATE_JTI_COLLISION:
            await _record_session_rotated_failed(
                session_factory,
                user=user,
                request=request,
                old_jti=old_jti,
                sid=sid,
                reason="double_jti_collision",
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=SESSION_REDIS_UNAVAILABLE_DETAIL,
            )

    if lua_result == redis_client.SESSION_ROTATE_REVOKED:
        # Concurrent /logout deleted the family set. Terminal 401 — the
        # frontend's classifier already handles this string.
        _log_refresh_rejected(
            "lua_session_revoked",
            jti=old_jti,
            sid=sid,
            extra={"sub": user.id},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been invalidated",
        )

    if lua_result == redis_client.SESSION_ROTATE_ALREADY_ROTATED:
        # Concurrent /refresh won the race. The winner just wrote the
        # grace key inside their Lua transaction — re-probe it, confirm
        # the family set still exists, AND emit a catch-up Set-Cookie
        # pointing at the winner's ``successor_jti`` so the browser
        # converges on the live primary (2026-05-19 fix — without this,
        # the loser walks away holding a jti whose Redis row has been
        # rotated past, and locks out 30s later).
        try:
            grace_row = await redis_client.session_grace(old_jti)
            family_alive = await redis_client.session_family_exists(sid)
        except (RedisRequired, RedisError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=SESSION_REDIS_UNAVAILABLE_DETAIL,
            ) from exc
        if (
            grace_row is None
            or not family_alive
            or grace_row.get("sid") != sid
            or grace_row.get("user_id") != user.id
        ):
            # Lua said "already_rotated" so the winner SHOULD have left
            # a grace key behind, but by the time we re-probe it's
            # gone (TTL expired, concurrent logout, etc.). Log which
            # part failed so ops can tell normal-grace-expiry apart
            # from "family revoked between rotation and re-probe".
            _log_refresh_rejected(
                "already_rotated_grace_revalidation_failed",
                jti=old_jti,
                sid=sid,
                extra={
                    "sub": user.id,
                    "grace_row_missing": grace_row is None,
                    "family_alive": family_alive,
                    "grace_sid_mismatch": (
                        grace_row is not None
                        and grace_row.get("sid") != sid
                    ),
                    "grace_user_mismatch": (
                        grace_row is not None
                        and grace_row.get("user_id") != user.id
                    ),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been invalidated",
            )
        # 2026-05-19 fix: emit a catch-up Set-Cookie pointing at the
        # winner's successor primary so the browser stops sending the
        # losing jti. ``grace_row["successor_jti"]`` is the new jti the
        # winning Lua transaction wrote — NOT the loser's local
        # ``new_jti`` (which has no Redis row).
        await _issue_catchup_refresh_cookie(
            response,
            user=user,
            successor_jti=grace_row.get("successor_jti"),
            sid=sid,
            session_start=session_start,
            ttl_seconds=ttl_seconds,
        )
        await _record_session_grace_accept(
            session_factory,
            user=user,
            request=request,
            old_jti=old_jti,
            sid=sid,
            via_already_rotated=True,
        )
        return TokenResponse(access_token=access_token)

    # lua_result == "ok"
    response.set_cookie(
        key="refresh_token",
        value=new_refresh_token,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=ttl_seconds,
        path="/",
    )
    _clear_legacy_refresh_cookie(response)
    await _record_session_rotated(
        session_factory,
        user=user,
        request=request,
        old_jti=old_jti,
        new_jti=new_jti,
        sid=sid,
    )

    return TokenResponse(access_token=access_token)


@router.post("/verify", response_model=VerifyResponse)
@limiter.limit("120/minute")
async def verify(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Server-side session verification for RSC consumers.

    Validates the refresh cookie without rotating it. Returns the same
    ``UserResponse`` shape as ``/auth/me`` plus a fresh access token.

    Invariants (load-bearing for RSC callers):
    - never emits ``Set-Cookie`` — even on session-lifetime expiry, the
      stale cookie is left in place (it will expire by its own ``max_age``)
    - no audit log on success

    Shares the full validation chain with ``/auth/refresh`` via
    ``_validate_refresh_cookie`` so the security contract cannot drift
    between the two endpoints. Walks every ``refresh_token`` value in the
    Cookie header (PR #211 cookie-shadow guard).
    """
    refresh_tokens = _extract_refresh_cookies(request)
    # /verify never emits Set-Cookie — even when ``redis_state == "grace"``.
    # We deliberately discard ``session_row``: catching up the cookie here
    # would violate the no-Set-Cookie invariant RSC callers rely on. The
    # /refresh endpoint is the only place that advances the browser's
    # cookie state. See the 2026-05-19 catch-up fix.
    user, _payload, _session_start, _redis_state, _ttl_seconds, _session_row = await _validate_refresh_cookie(
        refresh_tokens, db
    )

    await db.refresh(user, ["organization"])
    await subscription_service.check_trial_expiry(db, user.org_id)
    pair = await subscription_service.get_subscription_with_plan(db, user.org_id)
    sub, plan = pair if pair else (None, None)
    user_resp = _user_response(user, user.organization, sub, plan)

    access_token = create_access_token(user.id, user.org_id, user.role.value)

    return VerifyResponse(
        user=user_resp,
        access_token=access_token,
        token_type="bearer",
    )


@router.get("/me", response_model=UserResponse)
async def me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.refresh(current_user, ["organization"])
    # Reconcile trial expiry so user context stays in sync with /subscriptions
    await subscription_service.check_trial_expiry(db, current_user.org_id)
    pair = await subscription_service.get_subscription_with_plan(db, current_user.org_id)
    sub, plan = pair if pair else (None, None)
    return _user_response(current_user, current_user.organization, sub, plan)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Per-session logout — revoke by ``sid`` family (spec §5.3).

    PR 4 of the backend-session-model series. Closes the 2026-05-16
    false-logout incident class: today's handler writes
    ``sessions_invalidated_at = now`` which is global (kills every
    session for the user on every device). The new handler revokes
    ONLY the session family identified by the refresh cookie's ``sid``,
    leaving other devices / browser profiles authenticated.

    Steps (mirror spec §5.3 verbatim):

    1. Read every refresh cookie via ``_extract_refresh_cookies``.
       Decode each value just enough to extract its ``sid`` (and ``jti``
       for diagnostics) — no validation chain, this endpoint accepts
       anonymous logout as a successful cookie clear.
    2. Collect the distinct ``sid`` values (typical case: one).
    3. For each ``sid`` run the atomic family-revoke from
       :func:`redis_client.session_revoke_family`. Round A's
       ``DEL auth:session:by_sid:{sid}`` is what closes the
       architect's PR #301 follow-up race — a concurrent ``/refresh``
       Lua script will see ``SISMEMBER`` return 0 and refuse to write
       a successor.
    4. Clear the refresh cookie at both ``Path=/`` and the legacy path.
    5. Emit ``auth.session.terminated`` audit with detail
       ``{sid_count, jti_count}``. Outcome=success even when 0 (an
       anonymous logout is still a clean cookie clear).

    Critically does NOT touch ``sessions_invalidated_at`` — that field
    is the global-invalidation cutoff and stays reserved for the five
    sites enumerated in spec §6 (password reset / change, email
    change, invitation accept / role swap, admin deactivate). The
    grep-style regression test in
    ``tests/auth/test_sessions_invalidated_at_allowlist.py`` pins that
    invariant.

    Redis-unavailable behaviour (spec §7.1): if the family revoke
    raises (``RedisRequired`` or ``RedisError``), still clear the
    cookie and return 200 — the user-visible effect (cookie out of
    the browser) is the goal, the orphan ``jti`` rows age out on
    their own TTL. The audit detail flags ``redis_partial_revoke``
    so ops can disambiguate the degraded path from the happy path.
    """
    # ── 1. Collect every refresh cookie + decode for sid/jti ────────────
    # Walk the raw Cookie header so duplicate ``refresh_token`` entries
    # (Path=/ + the legacy Path=/api/v1/auth/refresh after PR #211) are
    # both inspected. Each value is decoded ONLY to read its claims; the
    # full validation chain is skipped on logout — even an expired or
    # post-cutoff token still identifies a session family that should be
    # cleaned up.
    refresh_tokens = _extract_refresh_cookies(request)
    sids: list[str] = []
    jtis_seen: list[str] = []
    seen_sids: set[str] = set()
    for raw in refresh_tokens:
        try:
            jti, sid = decode_refresh_jti_sid(raw)
        except (ValueError, Exception):  # noqa: BLE001 — corrupt/missing JWT is fine
            continue
        jtis_seen.append(jti)
        if sid not in seen_sids:
            seen_sids.add(sid)
            sids.append(sid)

    # Best-effort identification of the calling user for the audit row.
    # The Authorization header is the most reliable signal because the
    # access token carries ``sub``; falling back to the refresh JWT's
    # ``sub`` is acceptable when the bearer is missing (the user clicked
    # log-out from a tab whose access token expired). Decoding errors
    # are swallowed — anonymous logout is still a clean cookie clear.
    actor_user_id: int | None = None
    actor_email: str = ""
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            payload = decode_token(token)
            sub = payload.get("sub") if payload else None
            if sub is not None:
                actor_user_id = int(sub)
        except Exception:
            actor_user_id = None
    if actor_user_id is None:
        for raw in refresh_tokens:
            try:
                payload = decode_token(raw)
            except Exception:
                continue
            if payload is None:
                continue
            sub = payload.get("sub")
            if sub is not None:
                try:
                    actor_user_id = int(sub)
                except (TypeError, ValueError):
                    continue
                break
    if actor_user_id is not None:
        try:
            row = await db.execute(select(User).where(User.id == actor_user_id))
            user = row.scalar_one_or_none()
            if user is not None:
                actor_email = user.email
        except Exception:
            actor_email = ""

    # ── 2 + 3. Atomic family revoke per distinct sid ────────────────────
    jti_count = 0
    redis_partial_revoke = False
    for sid in sids:
        try:
            revoked = await redis_client.session_revoke_family(sid)
            jti_count += len(revoked)
        except (RedisRequired, RedisError):
            # Fail-open for logout per spec §7.1: clearing the cookie is
            # the user-visible effect, orphan keys age out on their own
            # TTL. Flag in the audit detail so ops can spot the
            # degraded path.
            redis_partial_revoke = True

    # ── 4. Clear the cookie at Path=/ AND the legacy path ──────────────
    response.delete_cookie("refresh_token", path="/")
    _clear_legacy_refresh_cookie(response)

    # ── 5. Audit. Always-success, even when sid_count = 0 ───────────────
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    detail: dict[str, Any] = {
        "sid_count": len(sids),
        "jti_count": jti_count,
    }
    if redis_partial_revoke:
        detail["redis_partial_revoke"] = True
    await audit_service.record_audit_event(
        session_factory,
        event_type="auth.session.terminated",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=request_id,
        ip_address=get_client_ip(request),
        outcome="success",
        detail=detail,
    )

    body: dict[str, Any] = {"detail": "Logged out"}
    if redis_partial_revoke:
        body["redis_partial_revoke"] = True
    return body


# ── Password Reset ───────────────────────────────────────────────────────────


@router.post("/forgot-password")
@limiter.limit("5/minute")
async def forgot_password(
    request: Request,
    body: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Send a password reset email. Always returns 200 to prevent email enumeration."""
    result = await db.execute(select(User).where(User.email == normalize_email(body.email)))
    user = result.scalar_one_or_none()

    if user and user.is_active:
        token = create_password_reset_token(user.id)
        background_tasks.add_task(send_password_reset_email, user.email, token)

    return {"detail": "If that email exists, a reset link has been sent"}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Reset password using a valid reset token."""
    payload = decode_token(body.token)
    if payload is None or payload.get("type") != "password_reset":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    user_id = int(payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    # Reject tokens issued before the last password change
    if user.password_changed_at:
        token_iat = datetime.fromtimestamp(payload.get("iat", 0), tz=timezone.utc).replace(tzinfo=None)
        if token_iat < user.password_changed_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset token",
            )

    now = datetime.now(timezone.utc)
    user.password_hash = hash_password(body.new_password)
    # Flip `password_set` so an SSO user who reset via token lands in
    # the standard branch on every future /users/me/password call and
    # the UI stops offering "Set a Password". Without this flip the
    # account has working local credentials but the row still claims
    # no password has ever been chosen. (Finding 2 from PR #138.)
    user.password_set = True
    user.password_changed_at = now
    user.sessions_invalidated_at = now
    await db.commit()
    return {"detail": "Password has been reset"}


# ── Email Verification ───────────────────────────────────────────────────────


@router.post("/verify-email")
@limiter.limit("10/minute")
async def verify_email(request: Request, body: VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
    """Verify email address using a verification token."""
    payload = decode_token(body.token)
    if payload is None or payload.get("type") != "email_verify":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification token",
        )

    user_id = int(payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification token",
        )

    # S-P2-1: the token binds the email it was issued for. Reject any
    # token whose email no longer matches the user's current address —
    # that means the user changed email after the token was issued and
    # this link would otherwise verify a stale address. A token without
    # an `email` claim is a pre-migration token and is rejected outright
    # so the new binding is always enforced.
    token_email = payload.get("email")
    if not token_email or token_email != user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification token",
        )

    user.email_verified = True
    await db.commit()
    return {"detail": "Email verified"}


@router.post("/resend-verification")
@limiter.limit("3/hour")
async def resend_verification(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resend verification email for the current user."""
    if current_user.email_verified:
        return {"detail": "Email already verified"}

    token = create_email_verification_token(current_user.id, current_user.email)
    await send_verification_email(current_user.email, token)
    return {"detail": "Verification email sent"}


@router.post("/resend-verification-public")
@limiter.limit("3/hour")
async def resend_verification_public(
    request: Request,
    body: ResendVerificationPublicRequest,
    db: AsyncSession = Depends(get_db),
):
    """Unauthenticated resend used by the login screen when an unverified
    user is blocked by the email gate (L1.8). Returns the same response
    shape regardless of whether the login matches a real, active,
    unverified user — no enumeration."""
    GENERIC_OK = {
        "detail": "If that account exists and is unverified, a new email has been sent."
    }

    result = await db.execute(
        select(User).where(or_(User.username == body.login, User.email == body.login))
    )
    user = result.scalar_one_or_none()
    if user is None or not user.is_active or user.email_verified:
        return GENERIC_OK

    token = create_email_verification_token(user.id, user.email)
    await send_verification_email(user.email, token)
    return GENERIC_OK


# ── MFA ─────────────────────────────────────────────────────────────────────


async def _resolve_mfa_user(mfa_token: str, db: AsyncSession) -> User:
    """Validate an MFA challenge token and return the associated user."""
    payload = decode_token(mfa_token)
    if payload is None or payload.get("type") != "mfa_challenge":
        raise HTTPException(status_code=401, detail="Invalid or expired MFA token")
    user_id = int(payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or expired MFA token")
    # Reject if MFA was disabled after the challenge token was issued
    if not user.mfa_enabled:
        raise HTTPException(status_code=401, detail="MFA is no longer enabled for this account")
    return user


async def _issue_tokens(
    user: User,
    response: Response,
    db: AsyncSession,
) -> TokenResponse:
    """Issue access + refresh tokens and set the refresh cookie.

    Shared by every MFA-completion branch (``/mfa/verify``,
    ``/mfa/recovery``, ``/mfa/email-verify``). Becomes async with PR 2
    because the Redis primary-key + family-set write happens BEFORE
    ``set_cookie`` — fail-closed semantics in spec §7.1.

    Requires ``db`` so the per-org session TTL can be resolved once
    and applied to the JWT exp, the cookie Max-Age, AND the Redis
    primary-key TTL in lockstep (2026-05-18 session-stability fix).
    """
    access_token = create_access_token(user.id, user.org_id, user.role.value)
    ttl_seconds = await get_org_session_ttl_seconds(db, user.org_id)
    refresh_token, _jti, _sid = await _issue_refresh_session(
        user.id, ttl_seconds=ttl_seconds
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=ttl_seconds,
        path="/",
    )
    _clear_legacy_refresh_cookie(response)
    return TokenResponse(access_token=access_token)


async def _record_google_callback_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    request: Request,
    reason: str,
    actor_email: str | None = None,
    event_type: str = "auth.google.callback.failed",
    detail_extra: dict[str, Any] | None = None,
) -> None:
    """Persist a Google SSO callback failure as an audit row.

    Distinct from ``_record_login_success`` because we have no
    authenticated ``User`` yet — at this stage of the flow the actor
    user id is unknown, and the email is only known after the
    userinfo call lands. ``audit_events.actor_email`` is non-nullable
    so we fall back to an empty string when Google hasn't returned
    one yet.

    ``detail_extra`` lets the caller attach extra fields (e.g., the
    raw ``google_error`` and ``google_error_description`` Google
    returned on a cancelled consent) without forcing every call site
    to construct the full detail dict.
    """
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    detail: dict[str, Any] = {"reason": reason}
    if detail_extra:
        detail.update(detail_extra)
    await audit_service.record_audit_event(
        session_factory,
        event_type=event_type,
        actor_user_id=None,
        actor_email=actor_email or "",
        target_org_id=None,
        target_org_name=None,
        request_id=request_id,
        ip_address=get_client_ip(request),
        outcome="failure",
        detail=detail,
    )


def _google_error_redirect(
    reason: str,
    *,
    base_path: str = "/login",
    query_key: str = "sso_error",
    cookie_path: str = "/api/v1/auth/google",
) -> RedirectResponse:
    """Build a 307 redirect to the frontend with the failure reason.

    307 (instead of 302) because the user-agent arrived here via a
    top-level GET navigation from Google; 307 preserves the method
    and avoids any chance of a tooling-induced re-POST. The
    ``oauth_state`` cookie is cleared so a retry starts clean.
    """
    resp = RedirectResponse(
        url=f"{app_settings.app_url}{base_path}?{query_key}={reason}",
        status_code=307,
    )
    resp.delete_cookie("oauth_state", path=cookie_path)
    return resp


async def _record_google_callback_created_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user: User,
    request: Request,
) -> None:
    """Persist an ``auth.google.callback.created_user`` audit event.

    Emitted on the new-user branch of ``/api/v1/auth/google/callback``
    in addition to the existing ``user.login.success`` event, so ops
    can disaggregate "Google identity created a fresh local user"
    from "Google identity logged into an existing local user". No
    token values are persisted, matching ``_record_login_success``.
    """
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    await audit_service.record_audit_event(
        session_factory,
        event_type="auth.google.callback.created_user",
        actor_user_id=user.id,
        actor_email=user.email,
        target_org_id=user.org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"method": "google_sso"},
    )


async def _record_login_success(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user: User,
    request: Request,
    method: str,
) -> None:
    """Persist a ``user.login.success`` audit event.

    Fire-and-forget contract — ``record_audit_event`` swallows DB
    errors so a transient audit write failure can never block a
    successful sign-in. ``method`` distinguishes ``password``,
    ``mfa_totp``, ``mfa_recovery``, ``mfa_email``, and ``google_sso``
    so the L4.6 analytics surface can disaggregate later without a
    schema change. PII (e.g. password, TOTP code) is never recorded;
    only the actor identity, request id, and IP travel into the row.
    """
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    await audit_service.record_audit_event(
        session_factory,
        event_type="user.login.success",
        actor_user_id=user.id,
        actor_email=user.email,
        target_org_id=user.org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"method": method},
    )


# ── PR 3 session-rotation audit events (spec §5.1 step 8) ───────────────────


async def _record_session_rotated(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user: User,
    request: Request,
    old_jti: str,
    new_jti: str,
    sid: str,
) -> None:
    """Persist an ``auth.session.rotated`` audit event on the happy-path
    rotation (Lua returned ``"ok"``).

    Detail carries the predecessor + successor ``jti`` plus the stable
    ``sid`` so operators can reconstruct the rotation chain offline.
    """
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    await audit_service.record_audit_event(
        session_factory,
        event_type="auth.session.rotated",
        actor_user_id=user.id,
        actor_email=user.email,
        target_org_id=user.org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"old_jti": old_jti, "new_jti": new_jti, "sid": sid},
    )


async def _record_session_grace_accept(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user: User,
    request: Request,
    old_jti: str,
    sid: str,
    via_already_rotated: bool,
) -> None:
    """Persist an ``auth.session.grace_accept`` audit event.

    Emitted on the grace branch — either entered directly because the
    app-side primary-key probe missed but the grace key was alive (the
    typical cross-tab race) or because the Lua rotation script returned
    ``already_rotated`` (the in-flight rotation race where two requests
    pass the app-side GET HIT and only one wins). The
    ``via_already_rotated`` flag lets ops disaggregate the two shapes.
    """
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    await audit_service.record_audit_event(
        session_factory,
        event_type="auth.session.grace_accept",
        actor_user_id=user.id,
        actor_email=user.email,
        target_org_id=user.org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "old_jti": old_jti,
            "sid": sid,
            "via_already_rotated": via_already_rotated,
        },
    )


async def _record_session_rotated_failed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user: User,
    request: Request,
    old_jti: str,
    sid: str,
    reason: str,
) -> None:
    """Persist an ``auth.session.rotated.failed`` audit event.

    Emitted on the double-``jti_collision`` 503 path only. Two 128-bit
    collisions in a row signals an RNG problem worth alerting on. The
    structlog event below mirrors the audit row so log-based alerts
    can fire even if the DB write fails.
    """
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    logger = structlog.stdlib.get_logger()
    await logger.aerror(
        "auth.session.rotated.failed",
        user_id=user.id,
        sid=sid,
        old_jti=old_jti,
        reason=reason,
    )
    await audit_service.record_audit_event(
        session_factory,
        event_type="auth.session.rotated.failed",
        actor_user_id=user.id,
        actor_email=user.email,
        target_org_id=user.org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=get_client_ip(request),
        outcome="failure",
        detail={"old_jti": old_jti, "sid": sid, "reason": reason},
    )


@router.post("/mfa/setup", response_model=MfaSetupResponse)
async def mfa_setup(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start MFA enrollment — generate TOTP secret and QR code."""
    if current_user.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is already enabled")

    secret = generate_totp_secret()
    uri = get_totp_uri(secret, current_user.email)
    qr_code = generate_qr_base64(uri)

    # Store encrypted secret (not yet enabled)
    try:
        current_user.totp_secret = encrypt_secret(secret)
    except MfaConfigError:
        raise HTTPException(status_code=503, detail="MFA is not available — encryption not configured")
    await db.commit()

    return MfaSetupResponse(qr_code=qr_code, secret=secret, uri=uri)


@router.post("/mfa/enable", response_model=MfaEnableResponse)
async def mfa_enable(
    body: MfaEnableRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Confirm MFA setup with a TOTP code, activate MFA, return recovery codes."""
    if current_user.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is already enabled")
    if not current_user.totp_secret:
        raise HTTPException(status_code=400, detail="Call /mfa/setup first")

    try:
        secret = decrypt_secret(current_user.totp_secret)
    except (ValueError, MfaConfigError):
        raise HTTPException(status_code=503, detail="MFA configuration error — contact support")
    if not verify_totp(secret, body.code):
        raise HTTPException(status_code=400, detail="Invalid TOTP code")

    codes = generate_recovery_codes()
    current_user.mfa_enabled = True
    current_user.recovery_codes = ",".join(hash_recovery_code(c) for c in codes)
    await db.commit()

    # Audit AFTER the business commit succeeds. PR3 of the notification
    # train uses this row as the trigger source for user.mfa.enabled.
    audit_event_id = await audit_service.record_audit_event(
        session_factory,
        event_type="user.mfa.enabled",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=current_user.org_id,
        target_org_name=None,
        request_id=structlog.contextvars.get_contextvars().get("request_id"),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=None,
    )

    # PR3 of the notification train: dispatch the in-app security
    # notification AFTER the audit row commits. Self-target — the
    # user who flipped MFA on receives the confirmation.
    if audit_event_id is not None:
        title, body, link_url = _tpl_user_mfa_enabled()
        await notification_service.dispatch_notification(
            db,
            user_id=current_user.id,
            category=NotificationCategory.SECURITY,
            event_type="user.mfa.enabled",
            title=title,
            body=body,
            link_url=link_url,
            audit_event_id=audit_event_id,
        )
        await db.commit()

    return MfaEnableResponse(recovery_codes=codes)


@router.post("/mfa/disable")
async def mfa_disable(
    body: MfaDisableRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Disable MFA. Requires password confirmation."""
    if not current_user.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is not enabled")
    if not verify_password(body.password, current_user.password_hash):
        raise HTTPException(status_code=403, detail="Invalid password")

    current_user.mfa_enabled = False
    current_user.totp_secret = None
    current_user.recovery_codes = None
    await db.commit()

    # Audit AFTER the business commit succeeds. PR3 of the notification
    # train uses this row as the trigger source for user.mfa.disabled —
    # a security-critical signal (the user can react if it wasn't them).
    audit_event_id = await audit_service.record_audit_event(
        session_factory,
        event_type="user.mfa.disabled",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=current_user.org_id,
        target_org_name=None,
        request_id=structlog.contextvars.get_contextvars().get("request_id"),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=None,
    )

    # PR3: dispatch the in-app security notification AFTER the audit
    # row commits. MFA-disabled is the louder of the two MFA signals
    # since a stolen credential is now sufficient for full access; the
    # template recommends re-enable.
    if audit_event_id is not None:
        title, body, link_url = _tpl_user_mfa_disabled()
        await notification_service.dispatch_notification(
            db,
            user_id=current_user.id,
            category=NotificationCategory.SECURITY,
            event_type="user.mfa.disabled",
            title=title,
            body=body,
            link_url=link_url,
            audit_event_id=audit_event_id,
        )
        await db.commit()

    return {"detail": "MFA disabled"}


@router.post("/mfa/recovery-codes", response_model=MfaEnableResponse)
async def mfa_regenerate_codes(
    body: MfaRegenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Regenerate recovery codes. Requires password confirmation."""
    if not current_user.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is not enabled")
    if not verify_password(body.password, current_user.password_hash):
        raise HTTPException(status_code=403, detail="Invalid password")

    codes = generate_recovery_codes()
    current_user.recovery_codes = ",".join(hash_recovery_code(c) for c in codes)
    await db.commit()

    return MfaEnableResponse(recovery_codes=codes)


@router.post("/mfa/verify", response_model=TokenResponse)
@limiter.limit("10/minute")
async def mfa_verify(
    request: Request,
    body: MfaVerifyRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Verify TOTP code during login to complete authentication."""
    user = await _resolve_mfa_user(body.mfa_token, db)

    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="MFA not configured")

    try:
        secret = decrypt_secret(user.totp_secret)
    except (ValueError, MfaConfigError):
        raise HTTPException(status_code=503, detail="MFA configuration error — contact support")
    if not verify_totp(secret, body.code):
        raise HTTPException(status_code=401, detail="Invalid TOTP code")

    tokens = await _issue_tokens(user, response, db)
    await _record_login_success(
        session_factory, user=user, request=request, method="mfa_totp"
    )
    return tokens


@router.post("/mfa/recovery", response_model=TokenResponse)
@limiter.limit("10/minute")
async def mfa_recovery(
    request: Request,
    body: MfaRecoveryRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Use a recovery code during login to complete authentication."""
    user = await _resolve_mfa_user(body.mfa_token, db)

    if not user.recovery_codes:
        raise HTTPException(status_code=400, detail="No recovery codes available")

    hashed_codes = user.recovery_codes.split(",")
    idx = verify_recovery_code(body.code, hashed_codes)
    if idx is None:
        raise HTTPException(status_code=401, detail="Invalid recovery code")

    # Remove the used code. Architect P1 finding on PR #306: hold the
    # commit until AFTER the Redis-backed session-issue inside
    # ``_issue_tokens`` succeeds. Otherwise a Redis 503 would consume
    # the recovery code (durable side effect on a tiny finite pool)
    # without giving the user a session, forcing them to burn another
    # code on retry.
    hashed_codes.pop(idx)
    user.recovery_codes = ",".join(hashed_codes) if hashed_codes else None
    await db.flush()

    tokens = await _issue_tokens(user, response, db)
    await db.commit()
    await _record_login_success(
        session_factory, user=user, request=request, method="mfa_recovery"
    )
    return tokens


@router.post("/mfa/email-code")
@limiter.limit("3/minute")
async def mfa_email_code(
    request: Request,
    body: MfaEmailCodeRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Send a one-time code to the user's email as MFA fallback."""
    user = await _resolve_mfa_user(body.mfa_token, db)

    # Generate 6-digit numeric code
    code = f"{secrets.randbelow(1000000):06d}"

    # Store as a JWT so we don't need DB state. The jti is recorded in
    # Redis so /mfa/email-verify can enforce single-use (pentest L1).
    email_token, jti = create_mfa_email_token(user.id, code)

    # 2026-05-19: route through the redis_client helper so the
    # transport-normalizer wrapper covers this path. Without the
    # wrapper, a closed-transport RuntimeError from uvloop here would
    # produce a 500 instead of a recoverable 503. The helper returns
    # False when REDIS_URL is unset (dev mode); production must fail
    # closed.
    try:
        stored = await redis_client.mfa_email_nonce_set(
            jti, user.id, MFA_EMAIL_TOKEN_TTL_SECONDS
        )
    except (RedisRequired, RedisError) as exc:
        raise HTTPException(
            status_code=503,
            detail="MFA email flow temporarily unavailable",
        ) from exc
    if not stored and app_settings.app_env == "production":
        # Empty REDIS_URL in prod = single-use guarantee disabled. Refuse.
        raise HTTPException(
            status_code=503,
            detail="MFA email flow temporarily unavailable",
        )

    background_tasks.add_task(send_mfa_email_code, user.email, code)

    # Return the email token — frontend stores it to verify later
    return {"detail": "Code sent", "email_token": email_token}


@router.post("/mfa/email-verify", response_model=TokenResponse)
@limiter.limit("10/minute")
async def mfa_email_verify(
    request: Request,
    body: MfaEmailVerifyRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Verify an email-based MFA code to complete authentication."""
    user = await _resolve_mfa_user(body.mfa_token, db)

    # Validate the email_token and extract the code HMAC
    email_payload = decode_token(body.email_token)
    if email_payload is None or email_payload.get("type") != "mfa_email":
        raise HTTPException(status_code=401, detail="Invalid or expired email code")

    # Ensure the email token belongs to the same user
    if int(email_payload["sub"]) != user.id:
        raise HTTPException(status_code=401, detail="Invalid or expired email code")

    # Legacy tokens (pre-jti) are rejected so users re-request under the
    # new single-use flow.
    jti = email_payload.get("jti")
    redis_configured = redis_client.get_client() is not None
    if not redis_configured and app_settings.app_env == "production":
        raise HTTPException(
            status_code=503,
            detail="MFA email flow temporarily unavailable",
        )
    if redis_configured and jti is None:
        raise HTTPException(status_code=401, detail="Invalid or expired email code")

    # Verify the code matches using HMAC (keyed hash, not brute-forceable).
    # Must happen BEFORE consuming the nonce — otherwise a typo burns the
    # token and forces a resend (one-attempt-only regression).
    expected_hmac = mfa_email_code_hmac(body.code)
    if not _hmac.compare_digest(expected_hmac, email_payload.get("code_hmac", "")):
        raise HTTPException(status_code=401, detail="Invalid code")

    # Only consume the jti after the code is proven valid. Atomic DEL:
    # if it returns 0 the token was already used (replay attempt) → 401.
    # Rate limit (10/min) backs this up against concurrent racing.
    #
    # 2026-05-19: route through the redis_client helper so the
    # transport-normalizer wrapper covers this path too.
    if redis_configured:
        try:
            consumed = await redis_client.mfa_email_nonce_consume(jti)
        except (RedisRequired, RedisError) as exc:
            raise HTTPException(
                status_code=503,
                detail="MFA email flow temporarily unavailable",
            ) from exc
        if not consumed:
            raise HTTPException(status_code=401, detail="Invalid or expired email code")

    tokens = await _issue_tokens(user, response, db)
    await _record_login_success(
        session_factory, user=user, request=request, method="mfa_email"
    )
    return tokens


# ── Google SSO ───────────────────────────────────────────────────────────────


def _safe_avatar_url(url: str | None) -> str | None:
    """Accept a Google avatar URL only if it fits the column.

    Google profile pictures routinely run 900+ chars and the column is
    sized for AVATAR_URL_MAX_LENGTH, but outlier URLs do exist in the
    wild. Dropping to None on overflow keeps the commit from crashing and
    lets the user upload their own avatar later via profile edit — strictly
    better than storing a truncated, broken URL. Sharing the cap with the
    ProfileUpdate schema means a client can also round-trip whatever we
    stored through PUT /users/me without hitting a 422.
    """
    if not url:
        return None
    if len(url) > AVATAR_URL_MAX_LENGTH:
        return None
    return url


def _validate_google_config() -> None:
    """Raise 501 if Google SSO is not fully configured."""
    if not app_settings.google_client_id or not app_settings.google_client_secret:
        raise HTTPException(status_code=501, detail="Google SSO not configured")


@router.get("/google")
async def google_login(response: Response):
    """Redirect to Google OAuth consent screen."""
    _validate_google_config()

    # Generate CSRF state token and store in a signed cookie. The TTL
    # (30 min) covers the user dwelling on Google's "Choose an account"
    # dialog. The previous 10-min budget produced a hard 400 at the
    # callback when users hesitated for ~11 min, which DO App Platform
    # then wrapped in its generic "Error / check logs" page.
    state = secrets.token_urlsafe(32)
    response.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=1800,  # 30 minutes
        path="/api/v1/auth/google",
    )

    params = {
        "client_id": app_settings.google_client_id,
        "redirect_uri": f"{app_settings.app_url}/api/v1/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
        "state": state,
    }
    return {"redirect_url": f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"}


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    oauth_state: str | None = Cookie(default=None),
):
    """Handle Google OAuth callback — exchange code for tokens, create or login user.

    IMPORTANT: this handler returns a RedirectResponse directly, so all cookie
    writes (set_cookie / delete_cookie) MUST be applied to the returned response
    object. FastAPI does not merge cookies set on an injected Response parameter
    into a directly-returned Response — they would be silently dropped, which
    is what previously broke the refresh-cookie round-trip for SSO logins.

    ``code`` and ``state`` are typed Optional because Google calls us back
    without a ``code`` in two important cases: (1) the user clicked
    Cancel/Back on the consent screen (``?error=access_denied``), and
    (2) any other provider-side failure (``?error=server_error`` etc.).
    Declaring them required would 422 before we reach the friendly
    redirect, leaving the user staring at App Platform's generic error
    page instead of /login with banner copy.
    """
    # _validate_google_config stays a 501 raise rather than a redirect:
    # missing client_id/client_secret is operator misconfiguration, not
    # a user-recoverable retry. Surfacing it as the real status preserves
    # the alert-worthy signal in DO logs / dashboards.
    _validate_google_config()

    # ── Provider-side failure branch ─────────────────────────────────
    # If Google attached ``?error=...`` (the standard OAuth2 error
    # response), the user-facing flow already failed at the consent
    # screen. There is no code to exchange. Skip state validation
    # entirely (we want a friendly message even if the cookie also
    # got nuked) and route to /login with the matching banner code.
    if error is not None:
        google_reason = "cancelled" if error == "access_denied" else "provider_error"
        await _record_google_callback_failure(
            session_factory,
            request=request,
            reason=google_reason,
            detail_extra={
                "google_error": error,
                "google_error_description": error_description,
            },
        )
        return _google_error_redirect(google_reason)

    # Malformed callback: neither a code nor an error. Surface to the
    # user as ``token`` so the existing banner copy covers it, but
    # audit the specific reason (``missing_code``) so ops can tell it
    # apart from a real token exchange failure.
    if code is None:
        await _record_google_callback_failure(
            session_factory, request=request, reason="missing_code"
        )
        return _google_error_redirect("token")

    # Validate CSRF state. The cookie miss case is the common one in
    # production — DO App Platform was wrapping the 400 in its generic
    # "Error / check logs" splash, so users saw a broken-app screen
    # instead of "your sign-in expired, try again". Redirect to /login
    # with ?sso_error=state so the frontend can render the right copy.
    if not oauth_state or not state or oauth_state != state:
        await _record_google_callback_failure(
            session_factory, request=request, reason="state"
        )
        return _google_error_redirect("state")

    # Exchange authorization code for tokens
    try:
        async with httpx.AsyncClient(timeout=GOOGLE_OAUTH_TIMEOUT) as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": app_settings.google_client_id,
                    "client_secret": app_settings.google_client_secret,
                    "redirect_uri": f"{app_settings.app_url}/api/v1/auth/google/callback",
                    "grant_type": "authorization_code",
                },
            )
            if token_resp.status_code != 200:
                await _record_google_callback_failure(
                    session_factory, request=request, reason="token"
                )
                return _google_error_redirect("token")
            tokens = token_resp.json()

            # Get user info from Google
            userinfo_resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            if userinfo_resp.status_code != 200:
                await _record_google_callback_failure(
                    session_factory, request=request, reason="userinfo"
                )
                return _google_error_redirect("userinfo")
            google_user = userinfo_resp.json()
    except httpx.HTTPError:
        await _record_google_callback_failure(
            session_factory, request=request, reason="token"
        )
        return _google_error_redirect("token")

    email = normalize_email(google_user.get("email", ""))
    if not email:
        await _record_google_callback_failure(
            session_factory, request=request, reason="no_email"
        )
        return _google_error_redirect("no_email")

    # Only trust Google's verification flag if it's explicitly present.
    # The userinfo payload may expose this as either `verified_email`
    # (OAuth2 v2 endpoint) or `email_verified` (OIDC userinfo) — accept
    # both, default to False otherwise.
    raw = google_user.get("verified_email")
    if raw is None:
        raw = google_user.get("email_verified", False)
    google_verified = bool(raw)
    if not google_verified:
        # Refuse SSO for unverified Google accounts. Prevents an attacker
        # who created an unverified Google account at the victim's email
        # from silently merging with an existing password-based user, and
        # prevents new registrations under unverified addresses.
        await _record_google_callback_failure(
            session_factory,
            request=request,
            reason="unverified",
            actor_email=email,
        )
        return _google_error_redirect("unverified")
    first_name = google_user.get("given_name", "")
    last_name = google_user.get("family_name", "")

    # Check if user already exists by email
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Track whether this callback created a new local user. The flag
    # drives two downstream effects: an audit row distinct from the
    # login-success row, and a fragment-only signal to the frontend
    # so it can show the first-run privacy disclosure surface before
    # the standard onboarding wizard.
    created_user = False

    if user:
        # Existing user — login
        if not user.is_active:
            await _record_google_callback_failure(
                session_factory,
                request=request,
                reason="deactivated",
                actor_email=email,
            )
            return _google_error_redirect("deactivated")
        # google_verified is guaranteed True by the guard above.
        mutated = False
        if not user.email_verified:
            user.email_verified = True
            mutated = True
        # Backfill profile fields from Google only when ours are empty so
        # we never overwrite values the user has edited themselves. Useful
        # when a password-registered user later links via Google and our
        # side never had the name/avatar populated.
        if not user.first_name and first_name:
            user.first_name = first_name
            mutated = True
        if not user.last_name and last_name:
            user.last_name = last_name
            mutated = True
        picture = _safe_avatar_url(google_user.get("picture"))
        if not user.avatar_url and picture:
            user.avatar_url = picture
            mutated = True
        if mutated:
            await db.commit()
    else:
        created_user = True
        # New user — register with Google profile
        existing_superadmin = await db.scalar(
            select(func.count()).select_from(User).where(User.is_superadmin == True)
        )
        is_first_user = existing_superadmin == 0

        base_username = _suggest_username(first_name, last_name, email)
        username = await _find_available_username(db, base_username)

        org = await _create_org_with_defaults(db, f"{username}'s Organization")

        user = User(
            org_id=org.id,
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
            avatar_url=_safe_avatar_url(google_user.get("picture")),
            password_hash=hash_password(secrets.token_urlsafe(32)),
            email_verified=True,  # guaranteed by the verified_email guard
            role=Role.OWNER,
            is_superadmin=is_first_user,
            is_founder=True,
            # SSO users get a random unguessable hash they cannot use to
            # sign in with. Flag the row so the change-password endpoint
            # accepts a first-time set without `current_password` and so
            # the email-change endpoint takes the step-up path. Flips
            # back to True the moment they set a real password.
            password_set=False,
        )
        db.add(user)
        # Architect P1 finding on PR #306: do NOT commit yet. The Redis
        # session write below must succeed BEFORE we commit the new
        # user + trial, otherwise a Redis 503 leaves the user durably
        # created without a session — the next Google SSO retry would
        # treat them as existing and skip the ``created_user=true``
        # first-run disclosure branch entirely.
        await db.flush()
        await db.refresh(user)

        # Create trial subscription for the new org (same as register).
        # Still no commit — single transaction across user, trial, and
        # Redis session-issue. Flush only so ``Subscription.id`` is
        # populated for the audit row that follows.
        await subscription_service.create_trial(db, org.id)
        await db.flush()

    # Issue tokens (or MFA challenge if enabled)
    await db.refresh(user, ["organization"])

    if user.mfa_enabled:
        mfa_token = create_mfa_challenge_token(user.id)
        resp = RedirectResponse(
            url=f"{app_settings.app_url}/mfa-verify?mfa_token={mfa_token}",
            status_code=302,
        )
        resp.delete_cookie("oauth_state", path="/api/v1/auth/google")
        return resp

    access_token = create_access_token(user.id, user.org_id, user.role.value)
    # PR 2: write the Redis primary key + family-set entry BEFORE
    # set_cookie. Fails closed (503) on unreachable Redis.
    #
    # 2026-05-18 session-stability refactor: resolve the per-org TTL
    # so the Google-SSO branch lands the same cookie / JWT / Redis
    # TTL as the password-login branch.
    ttl_seconds = await get_org_session_ttl_seconds(db, user.org_id)
    refresh_token, _jti, _sid = await _issue_refresh_session(
        user.id, ttl_seconds=ttl_seconds
    )

    # Architect P1 finding on PR #306: on the new-user branch above we
    # switched ``db.commit()`` to ``db.flush()`` so the user + trial
    # creation only land in the database AFTER Redis has accepted the
    # session. A Redis 503 above would have raised before reaching
    # here, rolling back the entire transaction; the next Google SSO
    # retry would correctly see no existing user and re-enter the
    # ``created_user=true`` first-run disclosure branch. Now that
    # ``_issue_refresh_session`` has succeeded, commit the user +
    # trial so they survive past this handler.
    #
    # The existing-user branch (lines ~1565-1571 above) already
    # committed any mutated-profile changes earlier, so this second
    # commit is a no-op for it.
    await db.commit()

    # Redirect to frontend with the access token in the URL fragment. The
    # fragment stays client-side (not sent to servers, not logged), while
    # the refresh token is set as an HttpOnly cookie so apiFetch can use
    # it on /auth/refresh. Both cookies MUST be set on this returned
    # response — see the handler docstring for the FastAPI caveat.
    #
    # On the new-user branch we append `&created_user=true` AFTER the
    # token in the fragment. The frontend callback page parses the
    # fragment, hands the token to apiFetch, and uses the flag to
    # stash a sessionStorage marker that triggers the first-run
    # privacy disclosure step at the start of the onboarding wizard.
    # The flag rides on the fragment (never the query string) so it
    # is not surfaced in Referer headers or server access logs, on
    # the same privacy posture as the token itself.
    fragment = f"token={access_token}"
    if created_user:
        fragment = f"{fragment}&created_user=true"
    resp = RedirectResponse(
        url=f"{app_settings.app_url}/auth/google/callback#{fragment}",
        status_code=302,
    )
    resp.delete_cookie("oauth_state", path="/api/v1/auth/google")
    resp.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=ttl_seconds,
        path="/",
    )
    _clear_legacy_refresh_cookie(resp)
    if created_user:
        # Distinct audit event for the "we just created a local user
        # from a Google identity" branch. Sits alongside the
        # `user.login.success` event (still emitted below) so existing
        # login analytics keep working unchanged, while ops/admin can
        # filter on `auth.google.callback.created_user` for the
        # account-creation slice (first-run disclosure rollout, growth
        # metrics, abuse triage). No token values are persisted —
        # only the user id / email / request id, matching the
        # _record_login_success privacy posture.
        await _record_google_callback_created_user(
            session_factory, user=user, request=request
        )
    await _record_login_success(
        session_factory, user=user, request=request, method="google_sso"
    )
    return resp


# ── SSO Step-Up (L1.7) ──────────────────────────────────────────────────────
#
# SSO users without a password (`password_set=False`) cannot satisfy the
# email-change re-auth gate the password branch enforces. Rather than
# silently swap email on the session (which would convert any session
# compromise to permanent account takeover, since email is the recovery
# channel), we require a fresh round-trip through Google: the user clicks
# "Verify with Google", we redirect them to Google's consent screen, and
# the callback writes a 5-minute single-use token onto their `users` row.
# The PUT /users/me handler then accepts that token in place of
# `current_password` for the email-change branch.
#
# Cookie path is scoped to /api/v1/auth/sso-stepup so it never collides
# with the main Google login `oauth_state` cookie at /api/v1/auth/google.

STEPUP_TOKEN_TTL_SECONDS = 5 * 60


# Allowlist of pages the step-up callback may redirect back to. We
# encode the chosen target into `state` (and validate it on the way
# back) so the Google round-trip cannot be twisted into an open
# redirect. New entries here must remain same-origin first-party
# settings paths.
_STEPUP_RETURN_TARGETS: dict[str, str] = {
    "settings": "/settings",
    "security": "/settings/security",
}
_STEPUP_DEFAULT_TARGET = "settings"


@router.post("/sso-stepup/initiate")
async def sso_stepup_initiate(
    response: Response,
    body: StepUpInitiateRequest | None = None,
    current_user: User = Depends(get_current_user),
):
    """Begin a Google step-up flow for the signed-in user.

    Returns the Google consent URL the frontend should navigate to.
    The state cookie embeds the `current_user.id` so the callback can
    verify the same user finished the round-trip and reject any state
    coming back to a different session. State also encodes the chosen
    return target (validated against an allowlist) so the callback can
    redirect to either /settings (email change) or /settings/security
    (first-time password set) without a query-string open redirect.
    """
    _validate_google_config()

    return_key = (body.return_to if body else None) or _STEPUP_DEFAULT_TARGET
    if return_key not in _STEPUP_RETURN_TARGETS:
        return_key = _STEPUP_DEFAULT_TARGET

    nonce = secrets.token_urlsafe(32)
    state = f"stepup:{current_user.id}:{nonce}:{return_key}"
    response.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=1800,  # 30 minutes — matches /google login TTL so a slow
                      # consent screen never trips the CSRF cookie miss.
        path="/api/v1/auth/sso-stepup",
    )

    params = {
        "client_id": app_settings.google_client_id,
        "redirect_uri": f"{app_settings.app_url}/api/v1/auth/sso-stepup/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
        "state": state,
    }
    return {"redirect_url": f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"}


@router.get("/sso-stepup/callback")
async def sso_stepup_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    oauth_state: str | None = Cookie(default=None),
):
    """Finalize a Google step-up. Issues a 5-minute single-use token.

    Browser-driven redirect from Google: no Authorization header is
    present, so this endpoint cannot use `get_current_user`. Identity
    is bound through the state-cookie + state-string round trip
    (same pattern as the SSO login `google_callback`):

      - state cookie matches the URL `state` (CSRF)
      - state is shaped `stepup:{user_id}:{nonce}`; `user_id` is the
        target user (looked up directly from the DB)
      - the Google account that completed the consent has the same
        verified email as that user (no swapping accounts at the
        consent screen)

    On success, writes a random 32-byte token + 5min expiry onto the
    `users` row and redirects back to /settings with the token in the
    URL fragment. Like the SSO login flow, fragments stay client-side
    (not sent to servers, not in access logs).

    ``code`` and ``state`` are typed Optional so the user-cancelled
    consent (``?error=access_denied``) and other provider-side error
    branches reach our friendly redirect instead of FastAPI's 422.
    """
    # Same rationale as in google_callback: a missing client_id/secret
    # is operator misconfiguration, not user-recoverable. Keep as a 501.
    _validate_google_config()

    # Pre-parse the return target so we can redirect to the right page
    # even when state itself is broken. Falls back to the default
    # /settings landing when the shape doesn't parse.
    def _resolve_return_path(raw_state: str | None) -> str:
        parts = (raw_state or "").split(":")
        if len(parts) == 4 and parts[3] in _STEPUP_RETURN_TARGETS:
            return _STEPUP_RETURN_TARGETS[parts[3]]
        return _STEPUP_RETURN_TARGETS[_STEPUP_DEFAULT_TARGET]

    async def _stepup_failure(
        reason: str,
        *,
        actor_email: str | None = None,
        detail_extra: dict[str, Any] | None = None,
    ) -> RedirectResponse:
        """Record the audit row and build the friendly redirect."""
        return_path = _resolve_return_path(state)
        await _record_google_callback_failure(
            session_factory,
            request=request,
            reason=reason,
            actor_email=actor_email,
            event_type="auth.google.sso_stepup.callback.failed",
            detail_extra=detail_extra,
        )
        resp = RedirectResponse(
            url=f"{app_settings.app_url}{return_path}?sso_stepup_error={reason}",
            status_code=307,
        )
        resp.delete_cookie("oauth_state", path="/api/v1/auth/sso-stepup")
        return resp

    # ── Provider-side failure branch ─────────────────────────────────
    # Google attached ``?error=...`` — the user cancelled at consent or
    # the provider returned its own error. There is no code to exchange.
    # Surface the friendly redirect regardless of state validity.
    if error is not None:
        stepup_reason = "cancelled" if error == "access_denied" else "provider_error"
        return await _stepup_failure(
            stepup_reason,
            detail_extra={
                "google_error": error,
                "google_error_description": error_description,
            },
        )

    # Malformed callback: neither a code nor an error. Surface the
    # ``token`` UI code (matches the existing copy) but audit the
    # specific ``missing_code`` reason so ops can tell it apart from a
    # real token-exchange failure. The frontend banner copy only keys
    # off the URL ``sso_stepup_error=token`` value.
    if code is None:
        await _record_google_callback_failure(
            session_factory,
            request=request,
            reason="missing_code",
            event_type="auth.google.sso_stepup.callback.failed",
        )
        return_path = _resolve_return_path(state)
        resp = RedirectResponse(
            url=f"{app_settings.app_url}{return_path}?sso_stepup_error=token",
            status_code=307,
        )
        resp.delete_cookie("oauth_state", path="/api/v1/auth/sso-stepup")
        return resp

    if not oauth_state or not state or oauth_state != state:
        return await _stepup_failure("state")

    # State binds the redemption to a specific user_id chosen at
    # initiate time. Without an Authorization header here, the state
    # cookie + state string round trip is the identity proof. The
    # 4-part shape carries the return-target chosen at initiate so the
    # callback redirects to the correct settings page (validated
    # against `_STEPUP_RETURN_TARGETS` to prevent open redirect).
    parts = state.split(":")
    if len(parts) != 4 or parts[0] != "stepup":
        return await _stepup_failure("state")
    try:
        state_user_id = int(parts[1])
    except ValueError:
        return await _stepup_failure("state")
    return_key = parts[3]
    if return_key not in _STEPUP_RETURN_TARGETS:
        return await _stepup_failure("state")

    user = await db.get(User, state_user_id)
    if user is None:
        # Treat a missing user as a bad state rather than 404, so we
        # don't leak which user_ids exist.
        return await _stepup_failure("state")

    # Exchange code → tokens → userinfo, identical shape to /google/callback
    try:
        async with httpx.AsyncClient(timeout=GOOGLE_OAUTH_TIMEOUT) as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": app_settings.google_client_id,
                    "client_secret": app_settings.google_client_secret,
                    "redirect_uri": f"{app_settings.app_url}/api/v1/auth/sso-stepup/callback",
                    "grant_type": "authorization_code",
                },
            )
            if token_resp.status_code != 200:
                return await _stepup_failure("token", actor_email=user.email)
            tokens = token_resp.json()

            userinfo_resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            if userinfo_resp.status_code != 200:
                return await _stepup_failure("userinfo", actor_email=user.email)
            google_user = userinfo_resp.json()
    except httpx.HTTPError:
        return await _stepup_failure("token", actor_email=user.email)

    google_email = (google_user.get("email") or "").strip().lower()
    raw = google_user.get("verified_email")
    if raw is None:
        raw = google_user.get("email_verified", False)
    if not bool(raw):
        return await _stepup_failure("unverified", actor_email=google_email or user.email)
    if not google_email or google_email != user.email.strip().lower():
        # The user must complete the step-up with the same Google
        # identity attached to this account. Otherwise this would let
        # an attacker who initiated step-up for someone else's user_id
        # swap the email by consenting on their own Google account.
        return await _stepup_failure("email_mismatch", actor_email=google_email or user.email)

    token = secrets.token_urlsafe(32)
    user.stepup_token = token
    user.stepup_token_expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=STEPUP_TOKEN_TTL_SECONDS
    )
    await db.commit()

    return_path = _STEPUP_RETURN_TARGETS[return_key]
    resp = RedirectResponse(
        url=f"{app_settings.app_url}{return_path}#stepup_token={token}",
        status_code=302,
    )
    resp.delete_cookie("oauth_state", path="/api/v1/auth/sso-stepup")
    return resp
