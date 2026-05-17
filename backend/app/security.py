import hmac as _hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import settings
from app.models.user import User


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(subject: int, org_id: int, role: str) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload = {
        "sub": str(subject),
        "org_id": org_id,
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def refresh_cookie_max_age() -> int:
    """Cookie ``Max-Age`` (seconds) for the ``refresh_token`` cookie.

    Single source of truth across every issue site — login password
    branch, ``/refresh`` rotation, MFA branches via ``_issue_tokens``,
    the Google OAuth callback, AND invitation accept in
    ``routers/org_members.py``. Derived from
    ``settings.refresh_idle_ttl_days`` so the operator can tune session
    idle TTL via one env var (``REFRESH_IDLE_TTL_DAYS``) and have the
    change land in lockstep at every cookie write.

    Lives here in ``security.py`` rather than ``routers/auth.py`` so
    that ``routers/org_members.py`` (and any future router that issues
    a refresh cookie) does not have to reach into auth.py's private
    helpers. See ``specs/2026-05-17-backend-session-model.md`` §2.2
    and §5.4.
    """
    return settings.refresh_idle_ttl_days * 86400


def create_refresh_token(
    subject: int,
    session_created_at: datetime | None = None,
    sid: str | None = None,
) -> tuple[str, str, str]:
    """Create a refresh token.

    Returns ``(token, jti, sid)``. The caller is responsible for writing
    the corresponding Redis primary key (``auth:session:{jti}``) and
    family-set entry (``auth:session:by_sid:{sid}``) before emitting the
    ``Set-Cookie`` — see ``specs/2026-05-17-backend-session-model.md`` §5.4.

    ``session_created_at`` tracks when the original login happened. It is set
    on first login and carried forward on every refresh so the backend can
    enforce an absolute session lifetime regardless of activity.

    ``sid`` identifies the session FAMILY (the chain of refresh tokens
    that descend from a single login). On first login the caller passes
    ``None`` and a fresh UUID4 hex is minted. On ``/refresh`` rotation
    the caller MUST pass the predecessor's ``sid`` so the family link
    survives across the rotation chain — that is what makes per-session
    logout (PR 4) revoke every successor.

    ``jti`` is always freshly minted via ``secrets.token_urlsafe(16)``
    (128 bits of entropy). It rotates on every issue and serves as the
    Redis primary-key suffix.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=settings.refresh_idle_ttl_days)
    jti = secrets.token_urlsafe(16)
    session_id = sid if sid is not None else uuid.uuid4().hex
    payload = {
        "sub": str(subject),
        "type": "refresh",
        "session_created_at": (session_created_at or now).timestamp(),
        "iat": int(now.timestamp()),
        "exp": expire,
        "jti": jti,
        "sid": session_id,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, jti, session_id


def decode_refresh_jti_sid(token: str) -> tuple[str, str]:
    """Decode a refresh JWT and return ``(jti, sid)``.

    Raises ``ValueError`` if the token cannot be decoded, is not of type
    ``refresh``, or is missing either claim. Both claims are mandatory
    after PR 2 ships — legacy refresh JWTs without ``jti``/``sid`` are
    rejected by the validation chain in ``auth.py``.
    """
    payload = jwt.decode(
        token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
    )
    if payload.get("type") != "refresh":
        raise ValueError("token is not a refresh token")
    jti = payload.get("jti")
    sid = payload.get("sid")
    if not jti or not sid:
        raise ValueError("refresh token missing jti or sid claim")
    return jti, sid


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


MFA_EMAIL_TOKEN_TTL_SECONDS = 10 * 60


def create_mfa_email_token(user_id: int, code: str) -> tuple[str, str]:
    """Create a short-lived token containing an MFA email code (10 minutes).

    Uses HMAC-SHA256 keyed with jwt_secret_key so the code hash cannot be
    brute-forced offline even though JWT payloads are readable.

    Returns (token, jti). The caller stores the jti in Redis (key with the
    same TTL) and deletes it on first successful verify to enforce
    single-use semantics. Without Redis bookkeeping the token is replayable
    within its TTL.
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=MFA_EMAIL_TOKEN_TTL_SECONDS)
    code_hmac = _hmac.new(
        settings.jwt_secret_key.encode(), code.encode(), "sha256"
    ).hexdigest()
    jti = secrets.token_urlsafe(16)
    payload = {
        "sub": str(user_id),
        "type": "mfa_email",
        "code_hmac": code_hmac,
        "jti": jti,
        "exp": expire,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, jti


def create_email_verification_token(user_id: int, email: str) -> str:
    """Create a token for email verification (24 hours).

    The email is baked into the token so a token issued for one address
    can't be used to verify a different address if the user changes
    their email between issuance and click (S-P2-1). The /verify-email
    handler rejects the token if the email claim does not match the
    user's current email.
    """
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    payload = {
        "sub": str(user_id),
        "email": email,
        "type": "email_verify",
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_invitation_token(invitation_id: int, email: str) -> str:
    """Create a token for an org-membership invitation (7 days).

    Email is baked in so a token issued for one address can't be reused
    against a different address if an admin retypes the email — the
    accept endpoint rejects the token if the email claim doesn't match
    the row.
    """
    expire = datetime.now(timezone.utc) + timedelta(days=7)
    payload = {
        "sub": str(invitation_id),
        "email": email,
        "type": "invitation",
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


def token_cutoff(user: User) -> datetime:
    """Earliest iat that is still valid for this user.

    Tokens issued before this timestamp are rejected. Updated on logout,
    password reset, and password change.
    """
    ts = []
    if user.password_changed_at is not None:
        # password_changed_at is stored as a naive datetime (no tz) in MySQL
        if user.password_changed_at.tzinfo is None:
            ts.append(user.password_changed_at.replace(tzinfo=timezone.utc))
        else:
            ts.append(user.password_changed_at)
    if user.sessions_invalidated_at is not None:
        if user.sessions_invalidated_at.tzinfo is None:
            ts.append(user.sessions_invalidated_at.replace(tzinfo=timezone.utc))
        else:
            ts.append(user.sessions_invalidated_at)
    return max(ts) if ts else datetime.min.replace(tzinfo=timezone.utc)
