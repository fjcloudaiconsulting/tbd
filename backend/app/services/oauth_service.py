"""MCP OAuth 2.1 authorization server (TBD-587): registration, consent
validation and the token endpoint. HTTP lives in ``app.routers.oauth``.

An OAuth grant IS one ``api_tokens`` row (``api_token_service.
create_oauth_grant`` writes it at consent, under the manual mint's owner lock
and cap). The authorization code lives ON that row, hashed, for 60 s; the
exchange redeems it with one conditional UPDATE (single use under
concurrency). Refresh rotates the access and refresh token IN PLACE, so the
row id (the principal of pending actions and rate-limit keys) and
``created_at`` (the session-cutoff anchor) never change. A rotated-out
refresh token presented again, or a redeemed code replayed WITH its verifier,
revokes the grant (OAuth 2.1 reuse detection).

Issuer and the protected resource derive from ``settings.app_url`` at call
time, never hardcoded: a branch deploy advertises its own origin.

Every limits bucket fails closed (``temporarily_unavailable``, 503).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import actions
from app.agent.registry import AGENT_FEATURE_KEY, ToolError
from app.config import settings
from app.models.api_token import ApiToken
from app.models.oauth_client import OAuthClient
from app.models.user import User
from app.security import token_cutoff
from app.services import audit_service, feature_service
from app.services.api_token_service import (
    AGENT_SCOPE_RANK,
    _aware,
    _naive_utc_now,
    generate_token,
    hash_api_token,
    sha256_hex,
    token_hash_candidates,
)

logger = structlog.stdlib.get_logger(__name__)

METER = "mcp.calls"
# OAuth never grants ``agent:auto`` (A1.2): it exists on manual tokens only.
OAUTH_SCOPES = ("agent:read", "agent:write")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
DEFAULT_CLIENT_NAME = "MCP client"
MAX_REDIRECT_URIS, MAX_URI_LEN, MAX_NAME_LEN, MAX_STATE_LEN = 5, 512, 100, 1024
MAX_CLIENTS = 20_000
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL, REFRESH_MAX = timedelta(days=30), timedelta(days=90)
MINUTE, HOUR, DAY = 60, 3600, 86_400
CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")
VERIFIER_RE = re.compile(r"[A-Za-z0-9._~-]{43,128}")


class OAuthError(Exception):
    """An RFC 6749 5.2 / RFC 7591 error: ``{"error", "error_description"}``."""

    def __init__(self, error: str, description: str = "", status: int = 400) -> None:
        super().__init__(error)
        self.error, self.description, self.status = error, description, status


class ConsentError(Exception):
    """A consent-request error. Without ``redirect_to`` the client or its
    redirect is not trusted yet, so the error stays on our page (F-O2)."""

    def __init__(self, code: str, redirect_to: Optional[str] = None) -> None:
        super().__init__(code)
        self.code, self.redirect_to = code, redirect_to


def issuer() -> str:
    return settings.app_url.rstrip("/")


def resource() -> str:
    return issuer() + "/mcp"


def protected_resource_metadata() -> dict[str, Any]:
    return {
        "resource": resource(),
        "authorization_servers": [issuer()],
        "scopes_supported": list(OAUTH_SCOPES),
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata() -> dict[str, Any]:
    iss = issuer()
    return {
        "issuer": iss,
        "authorization_endpoint": iss + "/oauth/authorize",
        "token_endpoint": iss + "/api/v1/oauth/token",
        "registration_endpoint": iss + "/api/v1/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(OAUTH_SCOPES),
        "authorization_response_iss_parameter_supported": True,
    }


async def _bucket(key: str, limit: int, window: int) -> None:
    try:
        await actions._hit(key, limit, window, "rate_limited")
    except ToolError as exc:
        if exc.code == "limits_unavailable":
            raise OAuthError("temporarily_unavailable", "rate limiting is unavailable", 503) from None
        raise OAuthError("rate_limited", status=429) from None


def parse_scope(raw: Optional[str]) -> Optional[str]:
    """RFC 6749 3.3 space-separated scope: ``agent:auto`` anywhere is
    ``invalid_scope``; unknown values are dropped; the highest-rank known
    value wins (``None`` when none is known)."""
    parts = (raw or "").split()
    if "agent:auto" in parts:
        raise OAuthError("invalid_scope", "agent:auto is never granted over OAuth")
    known = [p for p in parts if p in OAUTH_SCOPES]
    return max(known, key=AGENT_SCOPE_RANK.__getitem__) if known else None


# ── redirect URIs ──────────────────────────────────────────────────────────


def _bad_uri() -> OAuthError:
    return OAuthError("invalid_redirect_uri", "redirect URI not allowed")


def classify_redirect(uri: Any) -> tuple[str, str]:
    """``("https", host)``, ``("loopback", host)`` or ``("private", scheme)``.

    https needs a host; http only on an exact loopback host, any port
    (RFC 8252 7.3); a private-use scheme must contain a dot (RFC 8252 7.1).
    No fragment, no userinfo, no whitespace or control characters."""
    if (
        not isinstance(uri, str) or not uri or len(uri) > MAX_URI_LEN or "#" in uri
        or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in uri)
    ):
        raise _bad_uri()
    try:
        parts = urlsplit(uri)
        host = parts.hostname
        parts.port  # noqa: B018 -- raises ValueError on a malformed port
    except ValueError:
        raise _bad_uri() from None
    if "@" in parts.netloc:
        raise _bad_uri()
    if parts.scheme == "https" and host:
        return "https", host
    if parts.scheme == "http" and host in LOOPBACK_HOSTS:
        return "loopback", host
    if parts.scheme not in ("http", "https") and "." in parts.scheme:
        return "private", parts.scheme
    raise _bad_uri()


def match_key(uri: str) -> str:
    """The form compared at consent and hashed into ``metadata_key``: the URI
    itself, except a loopback ``http`` URI drops its port (RFC 8252 7.3)."""
    parts = urlsplit(uri)
    if parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        return urlunsplit(("http", host, parts.path, parts.query, ""))
    return uri


def _redirect_registered(client: OAuthClient, uri: Any) -> bool:
    if not isinstance(uri, str) or not uri:
        return False
    try:
        key = match_key(uri)
    except ValueError:
        return False
    return any(match_key(u) == key for u in client.redirect_uris)


def build_redirect(uri: str, params: dict[str, str], state: Optional[str]) -> str:
    """One builder for every redirect (success, deny, error): the params,
    then ``state`` when sent, then ``iss`` (RFC 9207)."""
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
    if state is not None:
        query.append(("state", state))
    query.append(("iss", issuer()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def redirect_host(uri: str) -> str:
    parts = urlsplit(uri)
    return parts.hostname or parts.scheme


# ── registration (RFC 7591) ────────────────────────────────────────────────


def metadata_key(name: str, uris: list[str]) -> str:
    canonical = json.dumps([name, sorted(match_key(u) for u in uris)], separators=(",", ":"))
    return sha256_hex(canonical)


def client_metadata(row: OAuthClient) -> dict[str, Any]:
    return {
        "client_id": row.id,
        "client_id_issued_at": int(_aware(row.created_at).timestamp()),
        "client_name": row.client_name,
        "redirect_uris": list(row.redirect_uris),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


async def _client_by_key(db: AsyncSession, key: str) -> Optional[OAuthClient]:
    return (await db.execute(
        select(OAuthClient).where(OAuthClient.metadata_key == key)
    )).scalar_one_or_none()


async def register(db: AsyncSession, body: Any, ip: str) -> OAuthClient:
    """Register (or return) a public client. Extra fields are ignored."""
    if not isinstance(body, dict):
        raise OAuthError("invalid_client_metadata", "a JSON object is required")
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= MAX_REDIRECT_URIS:
        raise OAuthError("invalid_redirect_uri", f"1 to {MAX_REDIRECT_URIS} redirect_uris required")
    kinds = {classify_redirect(u) for u in uris}
    name = body.get("client_name", DEFAULT_CLIENT_NAME)
    if name is None:
        name = DEFAULT_CLIENT_NAME
    if not isinstance(name, str) or len(name.strip()) > MAX_NAME_LEN:
        raise OAuthError("invalid_client_metadata", f"client_name must be at most {MAX_NAME_LEN} characters")
    name = name.strip() or DEFAULT_CLIENT_NAME
    key = metadata_key(name, uris)
    existing = await _client_by_key(db, key)
    if existing is not None:
        return existing  # idempotent: no pool charge
    if await db.scalar(select(func.count()).select_from(OAuthClient)) >= MAX_CLIENTS:
        raise OAuthError("rate_limited", status=429)
    await _bucket(f"oauth:dcr:ip:{ip}", 100, HOUR)
    if all(kind == "loopback" for kind, _ in kinds):
        await _bucket("oauth:dcr:loopback", 2000, DAY)
        await _bucket(f"oauth:dcr:loopback:ip:{ip}", 20, DAY)
    else:
        for kind, value in sorted(kinds):
            if kind == "https":
                await _bucket(f"oauth:dcr:host:{value}", 50, DAY)
            elif kind == "private":
                await _bucket(f"oauth:dcr:scheme:{value}", 50, DAY)
    row = OAuthClient(
        id=secrets.token_hex(16), client_name=name, redirect_uris=list(uris),
        metadata_key=key, created_at=_naive_utc_now().replace(microsecond=0),
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent identical registration won the unique key.
        await db.rollback()
        existing = await _client_by_key(db, key)
        if existing is None:
            raise
        return existing
    return row


# ── consent ────────────────────────────────────────────────────────────────


async def validate_consent(
    db: AsyncSession, p: Mapping[str, Any]
) -> tuple[OAuthClient, str, str]:
    """Shared by ``/authorize/context`` and ``/authorize``: returns
    ``(client, redirect_uri as presented, requested scope)``.

    The client and its redirect come first and never redirect; every later
    error is the error redirect (RFC 6749 4.1.2.1)."""
    cid = p.get("client_id")
    client = await db.get(OAuthClient, cid) if isinstance(cid, str) and cid else None
    if client is None:
        raise ConsentError("invalid_client")
    redirect = p.get("redirect_uri")
    if not _redirect_registered(client, redirect):
        raise ConsentError("invalid_redirect_uri")
    state = p.get("state")
    echo = state if isinstance(state, str) and len(state) <= MAX_STATE_LEN else None

    def fail(code: str) -> ConsentError:
        return ConsentError(code, build_redirect(redirect, {"error": code}, echo))

    if p.get("response_type") != "code":
        raise fail("unsupported_response_type")
    if p.get("code_challenge_method") != "S256" or not CHALLENGE_RE.fullmatch(
        p.get("code_challenge") or ""
    ):
        raise fail("invalid_request")
    try:
        requested = parse_scope(p.get("scope")) or "agent:read"
    except OAuthError:
        raise fail("invalid_scope") from None
    if p.get("resource") is not None and p.get("resource") != resource():
        raise fail("invalid_target")
    if state is not None and echo is None:
        raise fail("invalid_request")
    return client, redirect, requested


def scopes_offered(requested: str) -> list[str]:
    return [s for s in OAUTH_SCOPES if AGENT_SCOPE_RANK[s] <= AGENT_SCOPE_RANK[requested]]


async def stamp_client_used(
    session_factory: async_sessionmaker[AsyncSession], client_id: str
) -> None:
    """After an APPROVED consent, in its own short transaction (two consents
    to one shared client must not deadlock on it). At most daily; never
    fails the consent."""
    now = _naive_utc_now().replace(microsecond=0)
    try:
        async with session_factory() as s:
            await s.execute(
                update(OAuthClient)
                .where(
                    OAuthClient.id == client_id,
                    or_(OAuthClient.last_used_at.is_(None),
                        OAuthClient.last_used_at < now - timedelta(days=1)),
                )
                .values(last_used_at=now)
            )
            await s.commit()
    except Exception:  # noqa: BLE001 -- best effort
        logger.warning("oauth.client_stamp_failed", oauth_client_id=client_id)


# ── token endpoint ─────────────────────────────────────────────────────────


def _invalid_grant() -> OAuthError:
    return OAuthError("invalid_grant", "the grant is invalid, expired or revoked")


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _check_resource(form: Mapping[str, str]) -> None:
    if form.get("resource") is not None and form.get("resource") != resource():
        raise OAuthError("invalid_target", "resource must be " + resource())


def _refresh_expiry(created_at: datetime, now: datetime) -> datetime:
    return min(now + REFRESH_TTL, created_at.replace(tzinfo=None) + REFRESH_MAX)


def _tokens(access: str, refresh: str, scope: str) -> dict[str, Any]:
    return {"access_token": access, "token_type": "Bearer", "expires_in": int(ACCESS_TTL.total_seconds()),
            "refresh_token": refresh, "scope": scope}


async def _owner_ok(db: AsyncSession, owner_id: Optional[int], created_at: datetime) -> None:
    """Owner present and active, grant newer than the session cutoff, and the
    org still entitled (``ai.agent`` on, ``mcp.calls`` limit non-zero)."""
    owner = await db.get(User, owner_id) if owner_id is not None else None
    if owner is None or not owner.is_active or _aware(created_at) <= token_cutoff(owner):
        raise _invalid_grant()
    ent = await feature_service.get_entitlements(db, owner.org_id)
    if not ent.features.get(AGENT_FEATURE_KEY) or ent.limits[METER].limit == 0:
        raise _invalid_grant()


async def _revoke(
    db: AsyncSession, session_factory, snap: dict[str, Any], reason: str, ip: str
) -> None:
    res = await db.execute(
        update(ApiToken)
        .where(ApiToken.id == snap["api_token_id"], ApiToken.revoked_at.is_(None))
        .values(revoked_at=_naive_utc_now())
    )
    await db.commit()
    if not res.rowcount:
        return
    owner_id = snap.pop("owner_id")
    org_id = await db.scalar(select(User.org_id).where(User.id == owner_id)) if owner_id else None
    await audit_service.record_audit_event(
        session_factory,
        event_type="agent_token.revoked",
        actor_user_id=owner_id,
        actor_email=snap.pop("owner_email"),
        target_org_id=org_id,
        target_org_name=None,
        request_id=structlog.contextvars.get_contextvars().get("request_id"),
        ip_address=ip,
        outcome="success",
        detail={**snap, "reason": reason},
    )
    await logger.awarning("agent_token.revoked", api_token_id=snap["api_token_id"], reason=reason)


def _snap(row: ApiToken) -> dict[str, Any]:
    # Read before any commit/rollback: a rollback expires the ORM row.
    return {
        "api_token_id": row.id, "name": row.name, "scope": row.scope,
        "prefix": row.token_prefix, "oauth_client_id": row.oauth_client_id,
        "owner_id": row.created_by_user_id, "owner_email": row.created_by_email,
    }


async def _exchange(db, session_factory, form, ip, audit_ip) -> dict[str, Any]:
    code, client_id, redirect_uri = form.get("code"), form.get("client_id"), form.get("redirect_uri")
    if not code or not client_id or not redirect_uri:
        raise OAuthError("invalid_request", "code, client_id and redirect_uri are required")
    code_hash = hash_api_token(code)
    await _bucket(f"oauth:code:{code_hash}", 5, MINUTE)
    _check_resource(form)
    row = (await db.execute(
        select(ApiToken).where(ApiToken.code_hash == code_hash)
    )).scalar_one_or_none()
    if row is None:
        # Only a code that does not exist is counted: junk sent under a hosted
        # client's shared public id never blocks that client's real codes.
        await _bucket(f"oauth:codefail:cli:{client_id}", 1000, MINUTE)
        await _bucket(f"oauth:codefail:ip:{ip}", 60, MINUTE)
        raise _invalid_grant()
    verifier = form.get("code_verifier") or ""
    if not (
        client_id == row.oauth_client_id
        and secrets.compare_digest(sha256_hex(redirect_uri), row.code_redirect_hash or "")
        and VERIFIER_RE.fullmatch(verifier)
        and secrets.compare_digest(_s256(verifier), row.code_challenge or "")
    ):
        raise _invalid_grant()  # no state change: the right client can still redeem
    snap = _snap(row)
    if row.refresh_hash is not None:
        # Replayed WITH the verifier: only the real client holds it, so the
        # code leaked from it after redemption (RFC 6749 4.1.2).
        await _revoke(db, session_factory, snap, "code_reuse", audit_ip)
        raise _invalid_grant()
    scope, created_at = row.scope, row.created_at
    await _owner_ok(db, row.created_by_user_id, created_at)
    now = _naive_utc_now()
    access, access_hash, _ = generate_token()
    refresh = "rt_" + secrets.token_urlsafe(32)
    res = await db.execute(
        update(ApiToken)
        .where(
            ApiToken.id == snap["api_token_id"], ApiToken.code_hash == code_hash,
            ApiToken.refresh_hash.is_(None), ApiToken.revoked_at.is_(None),
            ApiToken.expires_at > now,
        )
        .values(
            token_hash=access_hash, expires_at=now + ACCESS_TTL,
            refresh_hash=hash_api_token(refresh),
            refresh_expires_at=_refresh_expiry(created_at, now),
        )
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        raise _invalid_grant()  # expired, revoked, or a concurrent exchange won
    await db.commit()
    return _tokens(access, refresh, scope)


async def _refresh(db, session_factory, form, ip, audit_ip) -> dict[str, Any]:
    presented = form.get("refresh_token")
    if not presented:
        raise OAuthError("invalid_request", "refresh_token is required")
    await _bucket(f"oauth:refresh:{hash_api_token(presented)}", 10, MINUTE)
    _check_resource(form)
    asked = parse_scope(form.get("scope"))
    candidates = token_hash_candidates(presented)
    # Plain lookup first, then lock by primary key: a locking read on the
    # hash index would gap-lock on every miss (junk would block rotations).
    rid = await db.scalar(
        select(ApiToken.id).where(or_(
            ApiToken.refresh_hash.in_(candidates), ApiToken.refresh_prev_hash.in_(candidates),
        )).limit(1)
    )
    if rid is None:
        raise _invalid_grant()
    row = (await db.execute(
        select(ApiToken).where(ApiToken.id == rid).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    snap = _snap(row)
    if row.refresh_hash in candidates:
        matched = row.refresh_hash
    elif row.refresh_prev_hash in candidates:
        await _revoke(db, session_factory, snap, "refresh_reuse", audit_ip)
        raise _invalid_grant()
    else:
        raise _invalid_grant()  # rotated twice since our plain read
    client_id = form.get("client_id")
    if client_id is not None and client_id != row.oauth_client_id:
        raise _invalid_grant()
    now = _naive_utc_now()
    if row.revoked_at is not None or row.refresh_expires_at is None or row.refresh_expires_at <= now:
        raise _invalid_grant()
    current, created_at = row.scope, row.created_at
    await _owner_ok(db, row.created_by_user_id, created_at)
    # A wider known scope is clamped to the grant's (never widens); only a
    # narrower one changes it.
    narrowed = asked if asked and AGENT_SCOPE_RANK[asked] < AGENT_SCOPE_RANK[current] else None
    access, access_hash, _ = generate_token()
    refresh = "rt_" + secrets.token_urlsafe(32)
    values: dict[str, Any] = dict(
        token_hash=access_hash, expires_at=now + ACCESS_TTL, refresh_prev_hash=matched,
        refresh_hash=hash_api_token(refresh), refresh_expires_at=_refresh_expiry(created_at, now),
    )
    if narrowed:
        values["scope"] = narrowed
    res = await db.execute(
        update(ApiToken)
        .where(
            ApiToken.id == snap["api_token_id"], ApiToken.refresh_hash == matched,
            ApiToken.revoked_at.is_(None), ApiToken.scope == current,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        still = await db.scalar(
            select(ApiToken.refresh_hash).where(ApiToken.id == snap["api_token_id"])
            .with_for_update()
        )
        if still == matched:
            raise _invalid_grant()  # only the scope (or a revoke) moved: no reuse
        # A concurrent rotation won with the same token: a replay.
        await _revoke(db, session_factory, snap, "refresh_reuse", audit_ip)
        raise _invalid_grant()
    await db.commit()
    return _tokens(access, refresh, narrowed or current)


async def token_request(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    form: Mapping[str, str],
    ip: str,
    *,
    audit_ip: Optional[str] = None,
) -> dict[str, Any]:
    """``POST /token``: the token response, or raise ``OAuthError``.

    ``ip`` keys the per-IP bucket (``rate_limit_key``); ``audit_ip`` is the
    full client address for audit rows."""
    grant_type = form.get("grant_type")
    try:
        if not grant_type:
            raise OAuthError("invalid_request", "grant_type is required")
        if grant_type == "authorization_code":
            return await _exchange(db, session_factory, form, ip, audit_ip or ip)
        if grant_type == "refresh_token":
            return await _refresh(db, session_factory, form, ip, audit_ip or ip)
        raise OAuthError("unsupported_grant_type", "authorization_code or refresh_token")
    except OAuthError:
        await db.rollback()  # release a row lock before answering
        raise

