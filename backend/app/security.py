import asyncio
import base64
import hashlib
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


def derive_hmac_key(purpose: bytes) -> bytes:
    """Derive a purpose-bound HMAC key from the JWT signing secret.

    Keeps the app's key-separation posture without provisioning a new
    secret: HMAC-SHA256(jwt_secret_key, purpose) gives each use of the
    JWT secret (MFA recovery codes, MFA email codes, ...) its own key,
    so no purpose uses the raw signing secret directly.
    Computed at call time so a rotated jwt_secret_key is picked up.
    """
    return _hmac.new(settings.jwt_secret_key.encode(), purpose, hashlib.sha256).digest()


MFA_EMAIL_CODE_PURPOSE = b"mfa-email-code-v1"


def mfa_email_code_hmac(code: str) -> str:
    """HMAC an MFA email code with the purpose-bound email-code key.

    Email codes live only inside a 10-minute signed token (plus a used_tokens
    jti nonce with the same TTL), so unlike recovery codes there is no
    long-lived stored hash and no legacy-key fallback is needed: codes
    issued before a deploy of this scheme simply fail and the user
    re-requests one.
    """
    return _hmac.new(
        derive_hmac_key(MFA_EMAIL_CODE_PURPOSE), code.encode(), "sha256"
    ).hexdigest()


# bcrypt only reads 72 bytes. bcrypt 4 truncated silently; bcrypt 5 raises.
# Truncating here keeps every hash bcrypt 4 stored verifying (TBD-635).
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    return bcrypt.hashpw(
        password.encode()[:_BCRYPT_MAX_BYTES], bcrypt.gensalt()
    ).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode()[:_BCRYPT_MAX_BYTES], hashed.encode())


_dummy_password_hash: str | None = None


def verify_password_or_dummy(plain: str, hashed: str | None) -> bool:
    """``verify_password``, or the same bcrypt work against a throwaway hash
    when there is no stored one (BE-25): a login for a missing account then
    takes as long as a wrong password, so the response time does not say
    whether the username or email exists. Always False without a hash.

    The throwaway hash is made once per process with ``hash_password``, so it
    carries the current bcrypt cost."""
    global _dummy_password_hash
    if hashed is not None:
        return verify_password(plain, hashed)
    if _dummy_password_hash is None:
        _dummy_password_hash = hash_password(secrets.token_urlsafe(16))
    verify_password(plain, _dummy_password_hash)
    return False


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
    the refresh JWT ``exp``, the session expiry, AND the
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
    to the refresh cookie's ``Max-Age``, to the session expiry,
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
    the corresponding session family row and its first member
    (``state_db.session_issue``) before emitting the
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
    (128 bits of entropy). It rotates on every issue and is the member
    key. Catch-up cookie issuance (the grace-path
    fix) passes the EXISTING successor jti so the new cookie points at
    a head jti that is already live in the session store; that path must NOT
    write the store again, because the row already exists from the winning
    rotation. All other callers leave this ``None``.

    ``ttl_seconds`` is the session TTL in seconds — drives the JWT
    ``exp`` claim AND must match the cookie ``Max-Age`` AND the family expiry
    at the caller's set_cookie / session_issue sites.
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


MFA_CHALLENGE_TTL_SECONDS = 5 * 60


def create_mfa_challenge_token(user_id: int) -> str:
    """Create a short-lived token for MFA challenge (5 minutes).

    The ``jti`` makes it single-use (BE-02): the second-factor routes claim it
    in ``used_tokens`` on success, and burn it after too many wrong codes.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "mfa_challenge",
        "jti": secrets.token_urlsafe(16),
        "iat": int(now.timestamp()),
        "exp": now + timedelta(seconds=MFA_CHALLENGE_TTL_SECONDS),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


MFA_EMAIL_TOKEN_TTL_SECONDS = 10 * 60


def create_mfa_email_token(user_id: int, code: str) -> tuple[str, str]:
    """Create a short-lived token containing an MFA email code (10 minutes).

    Uses HMAC-SHA256 keyed with a purpose-bound key derived from
    jwt_secret_key so the code hash cannot be brute-forced offline even
    though JWT payloads are readable.

    Returns (token, jti). The caller claims the jti in ``used_tokens`` on the first
    successful verify to enforce single-use semantics. Without that
    bookkeeping the token is replayable within its TTL.
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=MFA_EMAIL_TOKEN_TTL_SECONDS)
    code_hmac = mfa_email_code_hmac(code)
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


OPENROUTER_OAUTH_STATE_TTL_SECONDS = 10 * 60
_OPENROUTER_STATE_CLAIMS = ("sub", "org_id", "sid_h", "nh", "jti", "iat", "exp")


def _b64u_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def openrouter_oauth_sid_hmac(sid: str) -> str:
    """Hash of the refresh sid carried in the OAuth state, so the raw sid never
    travels to OpenRouter or into browser history."""
    return _hmac.new(
        derive_hmac_key(b"openrouter-oauth-sid-v1"), sid.encode(), "sha256"
    ).hexdigest()


def openrouter_pkce_verifier(jti: str) -> str:
    """PKCE verifier derived from the state's jti (43 chars). Never stored or sent
    to the browser; only its S256 challenge is."""
    return _b64u_nopad(
        _hmac.new(derive_hmac_key(b"openrouter-pkce-v1"), jti.encode(), "sha256").digest()
    )


def openrouter_pkce_challenge(verifier: str) -> str:
    return _b64u_nopad(hashlib.sha256(verifier.encode()).digest())


def create_openrouter_oauth_state(
    user_id: int, org_id: int, sid: str, nonce_hash: str
) -> tuple[str, str]:
    """Signed state for the OpenRouter OAuth round trip. Returns (token, jti)."""
    now = datetime.now(timezone.utc)
    jti = secrets.token_urlsafe(16)
    payload = {
        "type": "openrouter_oauth",
        "sub": str(user_id),
        "org_id": org_id,
        "sid_h": openrouter_oauth_sid_hmac(sid),
        "nh": nonce_hash,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": now + timedelta(seconds=OPENROUTER_OAUTH_STATE_TTL_SECONDS),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm), jti


def decode_openrouter_oauth_state(token: str) -> dict | None:
    """The state's claims, or None on a bad signature, expiry, wrong type or a
    missing/ill-typed claim."""
    payload = decode_token(token)
    if payload is None or payload.get("type") != "openrouter_oauth":
        return None
    if not all(k in payload for k in _OPENROUTER_STATE_CLAIMS):
        return None
    try:
        int(payload["sub"])
    except (TypeError, ValueError):
        return None
    if not all(isinstance(payload[k], str) for k in ("sid_h", "nh", "jti")) or not isinstance(
        payload["org_id"], int
    ) or not isinstance(payload["iat"], int):
        return None
    return payload


def create_email_verification_token(
    user_id: int, email: str, *, admin_initiated: bool = False
) -> str:
    """Create a token for email verification (24 hours).

    The email is baked into the token so a token issued for one address
    can't be used to verify a different address if the user changes
    their email between issuance and click (S-P2-1). The /verify-email
    handler rejects the token if the email claim does not match the
    user's current email.

    ``admin_initiated`` (TBD-362) marks a token minted by
    ``POST /api/v1/admin/users/{id}/email-change``, where an OPERATOR moved
    the claim rather than the account holder. It is the CLAIM PROVENANCE
    carrier, and it exists because that endpoint's ``user_already_verified``
    guard reads ``email_verified`` at TRIGGER time while the claim redeems up
    to 24 hours later — during which four separate arms can verify the row.
    ``_promote_pending_email`` refuses an admin-initiated token that meets a
    row which has since become verified.

    ⚠ The provenance rides in the TOKEN, deliberately, not in a
    ``users.pending_email_admin_initiated`` column. A column needs a
    migration and would have to be cleared at four existing sites; a flag
    missed at one of them fails CLOSED on a legitimate claim, a new failure
    mode with no counterpart here. The claim cannot be stripped or forged
    without breaking the HS signature.

    ⚠ The key is OMITTED, never written ``False``, on the user-initiated
    path. A token minted before TBD-362 shipped carries no key either, so
    both fail OPEN into the user-initiated path — the right direction, since
    no admin-initiated token existed before this shipped.

    ⚠ Deliberately NO ``verified_at_mint`` companion claim. The endpoint
    refuses verified targets, so such a claim would always be ``False`` and
    the redeem check reduces to this one anyway; a fence asserting it
    "worked" would be vacuous by construction.
    """
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    payload: dict = {
        "sub": str(user_id),
        "email": email,
        "type": "email_verify",
        "exp": expire,
    }
    if admin_initiated:
        payload["admin_initiated"] = True
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


def session_cutoff(now: datetime | None = None) -> datetime:
    """The cutoff a credential change writes to ``password_changed_at`` /
    ``sessions_invalidated_at``: the start of the NEXT whole second (BE-04).

    Token ``iat`` is floored to the second and every validator rejects
    ``iat < cutoff``. Inside the event's own second a token minted just before
    it and one minted just after carry the same ``iat``, so the cutoff has to
    reject that whole second: rounding up does. Having no fraction also keeps
    MySQL's fsp-0 columns from moving it; they round on insert, and a sub-.5
    fraction rounded DOWN used to let a token from earlier in that second
    survive. A sign-in inside that second waits it out (``wait_out_cutoff``).
    """
    now = now or datetime.now(timezone.utc)
    return now.replace(microsecond=0) + timedelta(seconds=1)


async def wait_out_cutoff(user: User) -> None:
    """Sleep until ``user``'s session cutoff has passed, so a token minted next
    carries an ``iat`` the cutoff accepts. Only a sign-in in the second after a
    credential change waits, and for under a second."""
    delay = (token_cutoff(user) - datetime.now(timezone.utc)).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)


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
