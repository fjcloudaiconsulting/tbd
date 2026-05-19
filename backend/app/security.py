import hmac as _hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


def default_session_ttl_seconds() -> int:
    """System-default session TTL (seconds).

    Single source of truth for the fallback used when an org has no
    ``OrgSetting(key="session_lifetime_days")`` row, or the row is
    malformed / out of bounds. Drives the refresh cookie ``Max-Age``,
    the refresh JWT ``exp``, the Redis primary-key TTL, AND the
    absolute-lifetime check — unified by the 2026-05-18 session-
    stability refactor so the UI-configurable "Maximum session
    duration" setting actually controls the session length end-to-end.
    """
    return settings.session_lifetime_days * 86400


async def get_org_session_ttl_seconds(
    db: AsyncSession,
    org_id: int,
) -> int:
    """Resolve the session TTL (seconds) for an org.

    Reads ``OrgSetting(key="session_lifetime_days", org_id=…)``. Falls
    back to :func:`default_session_ttl_seconds` when the row is absent,
    non-numeric, or outside the supported range ``[1, 365]``. Out-of-
    bounds rows fall back silently here AND are also rejected at the
    settings-PUT site so they should never exist in practice; this is
    defence-in-depth for future migrations or direct DB writes.

    Returns seconds (days × 86400). This single value is what the
    caller passes to :func:`create_refresh_token` (drives JWT ``exp``),
    to the refresh cookie's ``Max-Age``, to the Redis primary-key TTL,
    and to the absolute-lifetime check in ``_validate_single_refresh_token``.
    """
    # Lazy import so security.py stays importable from models bootstrapping
    # paths that don't have OrgSetting on the metadata yet.
    from app.models.settings import OrgSetting

    raw = await db.scalar(
        select(OrgSetting.value).where(
            OrgSetting.org_id == org_id,
            OrgSetting.key == "session_lifetime_days",
        )
    )
    if raw is not None:
        try:
            days = int(raw)
            if 1 <= days <= 365:
                return days * 86400
        except (TypeError, ValueError):
            pass
    return default_session_ttl_seconds()


def create_refresh_token(
    subject: int,
    *,
    ttl_seconds: int | None = None,
    session_created_at: datetime | None = None,
    sid: str | None = None,
    jti: str | None = None,
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

    ``jti`` is normally freshly minted via ``secrets.token_urlsafe(16)``
    (128 bits of entropy). It rotates on every issue and serves as the
    Redis primary-key suffix. Catch-up cookie issuance (the grace-path
    fix) passes the EXISTING successor jti so the new cookie points at
    a primary key that is already live in Redis; that path must NOT
    write Redis again, because the row already exists from the winning
    rotation. All other callers leave this ``None``.

    ``ttl_seconds`` is the session TTL in seconds — drives the JWT
    ``exp`` claim AND must match the cookie ``Max-Age`` AND the Redis
    primary-key TTL at the caller's set_cookie / session_issue sites.
    Callers that know the org context should pass
    ``await get_org_session_ttl_seconds(db, org_id)``. When ``None``
    the system default applies — only useful for tests or contexts
    where org_id is unavailable.
    """
    now = datetime.now(timezone.utc)
    if ttl_seconds is None:
        ttl_seconds = default_session_ttl_seconds()
    expire = now + timedelta(seconds=ttl_seconds)
    jti = jti if jti is not None else secrets.token_urlsafe(16)
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
