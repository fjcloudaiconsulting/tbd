import hmac as _hmac
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(subject: int, org_id: int, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_access_token_expire_minutes
    )
    payload = {
        "sub": str(subject),
        "org_id": org_id,
        "role": role,
        "type": "access",
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(
    subject: int,
    session_created_at: datetime | None = None,
) -> str:
    """Create a refresh token.

    session_created_at tracks when the original login happened. It is set
    on first login and carried forward on every refresh so the backend can
    enforce an absolute session lifetime regardless of activity.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=settings.jwt_refresh_token_expire_days)
    payload = {
        "sub": str(subject),
        "type": "refresh",
        "session_created_at": (session_created_at or now).timestamp(),
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_password_reset_token(user_id: int) -> str:
    """Create a short-lived token for password reset (1 hour)."""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(hours=1)
    payload = {
        "sub": str(user_id),
        "type": "password_reset",
        "iat": now,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_mfa_challenge_token(user_id: int) -> str:
    """Create a short-lived token for MFA challenge (5 minutes)."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=5)
    payload = {
        "sub": str(user_id),
        "type": "mfa_challenge",
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_mfa_email_token(user_id: int, code: str) -> str:
    """Create a short-lived token containing an MFA email code (10 minutes).

    Uses HMAC-SHA256 keyed with jwt_secret_key so the code hash cannot be
    brute-forced offline even though JWT payloads are readable.
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=10)
    code_hmac = _hmac.new(
        settings.jwt_secret_key.encode(), code.encode(), "sha256"
    ).hexdigest()
    payload = {
        "sub": str(user_id),
        "type": "mfa_email",
        "code_hmac": code_hmac,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_email_verification_token(user_id: int) -> str:
    """Create a token for email verification (24 hours)."""
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    payload = {
        "sub": str(user_id),
        "type": "email_verify",
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError:
        return None
