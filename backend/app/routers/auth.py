import re
import secrets
import hmac as _hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, Cookie, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.account import AccountType, SYSTEM_ACCOUNT_TYPES
from app.models.settings import OrgSetting
from app.models.category import Category, CategoryType, SYSTEM_CATEGORIES
from app.models.user import AVATAR_URL_MAX_LENGTH, Organization, Role, User
from app.models.subscription import Subscription, Plan
from app.services import subscription_service
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
    TokenResponse,
    UsernameCheckResponse,
    UserResponse,
    VerifyEmailRequest,
)
from app.config import settings as app_settings
from app import redis_client
from app.security import (
    MFA_EMAIL_TOKEN_TTL_SECONDS,
    create_access_token,
    create_email_verification_token,
    create_mfa_challenge_token,
    create_mfa_email_token,
    create_password_reset_token,
    create_refresh_token,
    decode_token,
    hash_password,
    token_cutoff,
    verify_password,
)
from app.rate_limit import limiter
from app.services.email_service import send_mfa_email_code, send_password_reset_email, send_verification_email
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
        mfa_enabled=user.mfa_enabled,
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
async def auth_status(db: AsyncSession = Depends(get_db)):
    """Check if the system needs initial setup (no users exist)."""
    user_count = await db.scalar(select(func.count()).select_from(User))
    return {"needs_setup": user_count == 0}


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
):
    existing = await db.execute(
        select(User).where(or_(User.username == body.username, User.email == body.email))
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
        email=body.email,
        first_name=body.first_name,
        last_name=body.last_name,
        password_hash=hash_password(body.password),
        role=Role.OWNER,
        is_superadmin=is_first_user,
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
    request: Request, body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)
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

    # If MFA is enabled, return a challenge token instead of access tokens
    if user.mfa_enabled:
        mfa_token = create_mfa_challenge_token(user.id)
        return MfaChallengeResponse(mfa_token=mfa_token)

    access_token = create_access_token(user.id, user.org_id, user.role.value)
    refresh_token = create_refresh_token(user.id)

    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
        path="/api/v1/auth/refresh",
    )

    return TokenResponse(access_token=access_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
):
    if refresh_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No refresh token",
        )

    payload = decode_token(refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    user_id = int(payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    # Reject refresh tokens issued before the session cutoff (logout / password change)
    iat = payload.get("iat")
    if iat is not None:
        token_issued_at = datetime.fromtimestamp(iat, tz=timezone.utc)
        if token_issued_at < token_cutoff(user):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been invalidated",
            )

    # Enforce absolute session lifetime
    session_created_at = payload.get("session_created_at")
    if session_created_at:
        session_start = datetime.fromtimestamp(session_created_at, tz=timezone.utc)

        # Check org-level override first, fall back to system default
        max_days = app_settings.session_lifetime_days
        org_setting = await db.scalar(
            select(OrgSetting.value).where(
                OrgSetting.org_id == user.org_id,
                OrgSetting.key == "session_lifetime_days",
            )
        )
        if org_setting:
            try:
                max_days = int(org_setting)
            except ValueError:
                pass

        if datetime.now(timezone.utc) - session_start > timedelta(days=max_days):
            response.delete_cookie("refresh_token", path="/api/v1/auth/refresh")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired — please sign in again",
            )

    # Carry forward session_created_at from the original login
    original_session = (
        datetime.fromtimestamp(session_created_at, tz=timezone.utc)
        if session_created_at
        else None
    )

    access_token = create_access_token(user.id, user.org_id, user.role.value)
    new_refresh_token = create_refresh_token(user.id, session_created_at=original_session)

    response.set_cookie(
        key="refresh_token",
        value=new_refresh_token,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
        path="/api/v1/auth/refresh",
    )

    return TokenResponse(access_token=access_token)


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
):
    # Best-effort server-side invalidation: if the caller still has a valid
    # access token, mark their sessions invalidated so the refresh token and
    # any sibling access tokens are killed. Regardless of auth state, always
    # clear the refresh cookie so the browser stops sending it.
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            payload = decode_token(token)
            user_id = payload.get("sub")
            if user_id is not None:
                result = await db.execute(select(User).where(User.id == int(user_id)))
                user = result.scalar_one_or_none()
                if user is not None:
                    user.sessions_invalidated_at = datetime.now(timezone.utc)
                    await db.commit()
        except Exception:
            # Missing/expired/malformed token: still clear the cookie below.
            pass
    response.delete_cookie("refresh_token", path="/api/v1/auth/refresh")
    return {"detail": "Logged out"}


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
    result = await db.execute(select(User).where(User.email == body.email))
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


def _issue_tokens(user: User, response: Response) -> TokenResponse:
    """Issue access + refresh tokens and set the refresh cookie."""
    access_token = create_access_token(user.id, user.org_id, user.role.value)
    refresh_token = create_refresh_token(user.id)
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
        path="/api/v1/auth/refresh",
    )
    return TokenResponse(access_token=access_token)


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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
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

    return MfaEnableResponse(recovery_codes=codes)


@router.post("/mfa/disable")
async def mfa_disable(
    body: MfaDisableRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
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

    return _issue_tokens(user, response)


@router.post("/mfa/recovery", response_model=TokenResponse)
@limiter.limit("10/minute")
async def mfa_recovery(
    request: Request,
    body: MfaRecoveryRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Use a recovery code during login to complete authentication."""
    user = await _resolve_mfa_user(body.mfa_token, db)

    if not user.recovery_codes:
        raise HTTPException(status_code=400, detail="No recovery codes available")

    hashed_codes = user.recovery_codes.split(",")
    idx = verify_recovery_code(body.code, hashed_codes)
    if idx is None:
        raise HTTPException(status_code=401, detail="Invalid recovery code")

    # Remove the used code
    hashed_codes.pop(idx)
    user.recovery_codes = ",".join(hashed_codes) if hashed_codes else None
    await db.commit()

    return _issue_tokens(user, response)


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

    redis = redis_client.get_client()
    if redis is not None:
        await redis.set(
            f"mfa_email_jti:{jti}", str(user.id), ex=MFA_EMAIL_TOKEN_TTL_SECONDS
        )
    elif app_settings.app_env == "production":
        # In prod we must have Redis; empty REDIS_URL means the single-use
        # guarantee is disabled — refuse to issue a token.
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
    redis = redis_client.get_client()
    if redis is None and app_settings.app_env == "production":
        raise HTTPException(
            status_code=503,
            detail="MFA email flow temporarily unavailable",
        )
    if redis is not None and jti is None:
        raise HTTPException(status_code=401, detail="Invalid or expired email code")

    # Verify the code matches using HMAC (keyed hash, not brute-forceable).
    # Must happen BEFORE consuming the nonce — otherwise a typo burns the
    # token and forces a resend (one-attempt-only regression).
    expected_hmac = _hmac.new(
        app_settings.jwt_secret_key.encode(), body.code.encode(), "sha256"
    ).hexdigest()
    if not _hmac.compare_digest(expected_hmac, email_payload.get("code_hmac", "")):
        raise HTTPException(status_code=401, detail="Invalid code")

    # Only consume the jti after the code is proven valid. Atomic DEL:
    # if it returns 0 the token was already used (replay attempt) → 401.
    # Rate limit (10/min) backs this up against concurrent racing.
    if redis is not None:
        consumed = await redis.delete(f"mfa_email_jti:{jti}")
        if not consumed:
            raise HTTPException(status_code=401, detail="Invalid or expired email code")

    return _issue_tokens(user, response)


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

    # Generate CSRF state token and store in a signed cookie
    state = secrets.token_urlsafe(32)
    response.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=600,  # 10 minutes
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
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
    oauth_state: str | None = Cookie(default=None),
):
    """Handle Google OAuth callback — exchange code for tokens, create or login user.

    IMPORTANT: this handler returns a RedirectResponse directly, so all cookie
    writes (set_cookie / delete_cookie) MUST be applied to the returned response
    object. FastAPI does not merge cookies set on an injected Response parameter
    into a directly-returned Response — they would be silently dropped, which
    is what previously broke the refresh-cookie round-trip for SSO logins.
    """
    _validate_google_config()

    # Validate CSRF state
    if not oauth_state or oauth_state != state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state — possible CSRF")

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
                raise HTTPException(status_code=400, detail="Failed to exchange Google auth code")
            tokens = token_resp.json()

            # Get user info from Google
            userinfo_resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            if userinfo_resp.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to get Google user info")
            google_user = userinfo_resp.json()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Failed to communicate with Google")

    email = google_user.get("email", "")
    if not email:
        raise HTTPException(status_code=400, detail="Google account has no email")

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
        raise HTTPException(
            status_code=400,
            detail=(
                "Google has not verified this email. Verify it with Google "
                "or sign in with a password."
            ),
        )
    first_name = google_user.get("given_name", "")
    last_name = google_user.get("family_name", "")

    # Check if user already exists by email
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user:
        # Existing user — login
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account is deactivated")
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
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        # Create trial subscription for the new org (same as register)
        await subscription_service.create_trial(db, org.id)
        await db.commit()

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
    refresh_token = create_refresh_token(user.id)

    # Redirect to frontend with the access token in the URL fragment. The
    # fragment stays client-side (not sent to servers, not logged), while
    # the refresh token is set as an HttpOnly cookie so apiFetch can use
    # it on /auth/refresh. Both cookies MUST be set on this returned
    # response — see the handler docstring for the FastAPI caveat.
    resp = RedirectResponse(
        url=f"{app_settings.app_url}/auth/google/callback#token={access_token}",
        status_code=302,
    )
    resp.delete_cookie("oauth_state", path="/api/v1/auth/google")
    resp.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=app_settings.cookie_secure,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
        path="/api/v1/auth/refresh",
    )
    return resp
