import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, Cookie, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.account import AccountType, SYSTEM_ACCOUNT_TYPES
from app.models.category import Category, CategoryType, SYSTEM_CATEGORIES
from app.models.user import Organization, Role, User
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UsernameCheckResponse,
    UserResponse,
    VerifyEmailRequest,
)
from app.config import settings as app_settings
from app.security import (
    create_access_token,
    create_email_verification_token,
    create_password_reset_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.services.email_service import send_password_reset_email, send_verification_email

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _user_response(user: User, org: Organization) -> UserResponse:
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


@router.get("/status")
async def auth_status(db: AsyncSession = Depends(get_db)):
    """Check if the system needs initial setup (no users exist)."""
    user_count = await db.scalar(select(func.count()).select_from(User))
    return {"needs_setup": user_count == 0}


@router.get("/check-username", response_model=UsernameCheckResponse)
async def check_username(
    username: str = Query(min_length=1),
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
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
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

    org = Organization(name=body.org_name or f"{body.username}'s Organization")
    db.add(org)
    await db.flush()

    for sat in SYSTEM_ACCOUNT_TYPES:
        db.add(AccountType(org_id=org.id, name=sat["name"], slug=sat["slug"], is_system=True))

    for master_def in SYSTEM_CATEGORIES:
        master = Category(
            org_id=org.id,
            name=master_def["name"],
            slug=master_def["slug"],
            description=master_def["description"],
            type=CategoryType(master_def["type"]),
            is_system=True,
        )
        db.add(master)
        await db.flush()
        for child_def in master_def.get("children", []):
            db.add(Category(
                org_id=org.id,
                parent_id=master.id,
                name=child_def["name"],
                slug=child_def["slug"],
                description=child_def["description"],
                type=CategoryType(master_def["type"]),
                is_system=True,
            ))

    db.add(Category(
        org_id=org.id, name="Transfer", slug="transfer",
        description="Internal transfers between accounts",
        type=CategoryType.BOTH, is_system=True,
    ))

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

    # Send verification email (non-blocking — don't fail registration if email fails)
    token = create_email_verification_token(user.id)
    await send_verification_email(user.email, token)

    return _user_response(user, org)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)
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

    access_token = create_access_token(user.id, user.org_id, user.role.value)
    new_refresh_token = create_refresh_token(user.id)

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
    return _user_response(current_user, current_user.organization)


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("refresh_token", path="/api/v1/auth/refresh")
    return {"detail": "Logged out"}


# ── Password Reset ───────────────────────────────────────────────────────────


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Send a password reset email. Always returns 200 to prevent email enumeration."""
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if user and user.is_active:
        token = create_password_reset_token(user.id)
        await send_password_reset_email(user.email, token)

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

    user.password_hash = hash_password(body.new_password)
    await db.commit()
    return {"detail": "Password has been reset"}


# ── Email Verification ───────────────────────────────────────────────────────


@router.post("/verify-email")
async def verify_email(body: VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
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

    user.email_verified = True
    await db.commit()
    return {"detail": "Email verified"}


@router.post("/resend-verification")
async def resend_verification(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resend verification email for the current user."""
    if current_user.email_verified:
        return {"detail": "Email already verified"}

    token = create_email_verification_token(current_user.id)
    await send_verification_email(current_user.email, token)
    return {"detail": "Verification email sent"}


# ── Google SSO ───────────────────────────────────────────────────────────────


@router.get("/google")
async def google_login():
    """Redirect to Google OAuth consent screen."""
    if not app_settings.google_client_id:
        raise HTTPException(status_code=501, detail="Google SSO not configured")

    params = {
        "client_id": app_settings.google_client_id,
        "redirect_uri": f"{app_settings.app_url}/api/v1/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return {"redirect_url": f"https://accounts.google.com/o/oauth2/v2/auth?{query}"}


@router.get("/google/callback")
async def google_callback(
    code: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Handle Google OAuth callback — exchange code for tokens, create or login user."""
    if not app_settings.google_client_id:
        raise HTTPException(status_code=501, detail="Google SSO not configured")

    # Exchange authorization code for tokens
    async with httpx.AsyncClient() as client:
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

    email = google_user.get("email", "")
    first_name = google_user.get("given_name", "")
    last_name = google_user.get("family_name", "")

    # Check if user already exists by email
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user:
        # Existing user — login
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account is deactivated")
        # Update email_verified since Google verified it
        if not user.email_verified:
            user.email_verified = True
            await db.commit()
    else:
        # New user — register with Google profile
        # Check for existing superadmin
        existing_superadmin = await db.scalar(
            select(func.count()).select_from(User).where(User.is_superadmin == True)
        )
        is_first_user = existing_superadmin == 0

        # Generate a username from Google name
        base_username = _suggest_username(first_name, last_name, email)
        username = await _find_available_username(db, base_username)

        # Create org + user + system data (same as register)
        org = Organization(name=f"{username}'s Organization")
        db.add(org)
        await db.flush()

        for sat in SYSTEM_ACCOUNT_TYPES:
            db.add(AccountType(org_id=org.id, name=sat["name"], slug=sat["slug"], is_system=True))

        for master_def in SYSTEM_CATEGORIES:
            master = Category(
                org_id=org.id,
                name=master_def["name"],
                slug=master_def["slug"],
                description=master_def["description"],
                type=CategoryType(master_def["type"]),
                is_system=True,
            )
            db.add(master)
            await db.flush()
            for child_def in master_def.get("children", []):
                db.add(Category(
                    org_id=org.id,
                    parent_id=master.id,
                    name=child_def["name"],
                    slug=child_def["slug"],
                    description=child_def["description"],
                    type=CategoryType(master_def["type"]),
                    is_system=True,
                ))

        db.add(Category(
            org_id=org.id, name="Transfer", slug="transfer",
            description="Internal transfers between accounts",
            type=CategoryType.BOTH, is_system=True,
        ))

        # Google users get a random password (they login via Google)
        import secrets
        random_password = secrets.token_urlsafe(32)

        user = User(
            org_id=org.id,
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
            avatar_url=google_user.get("picture"),
            password_hash=hash_password(random_password),
            email_verified=True,  # Google verified the email
            role=Role.OWNER,
            is_superadmin=is_first_user,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    # Issue tokens
    await db.refresh(user, ["organization"])
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

    # Redirect to frontend with access token
    from urllib.parse import urlencode
    redirect_params = urlencode({"token": access_token})
    return Response(
        status_code=302,
        headers={"Location": f"{app_settings.app_url}/auth/google/callback?{redirect_params}"},
    )
