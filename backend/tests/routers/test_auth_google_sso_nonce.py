"""FE-02: the Google sign-in result is bound to the tab that started it.

``GET /api/v1/auth/google`` returns a nonce and keeps a copy in an httpOnly
cookie scoped to the Google routes; the callback echoes that copy into the
success fragment (never the query) and clears it. The callback page compares
it with what the starting tab stored (frontend test), so a link carrying
someone else's token is refused.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.routers.test_auth_google_callback_first_run import (  # noqa: F401  (fixtures + helpers)
    _drive_callback,
    _make_app,
    _patch_httpx,
    _seed_default_plan,
    _seed_existing_sso_user,
    google_config,
    reset_limiter,
    session_factory,
)

NONCE = "Nonce_value-0123456789"


def _fragment(res) -> dict[str, list[str]]:
    parts = urlsplit(res.headers["location"])
    assert "nonce" not in parse_qs(parts.query)
    return parse_qs(parts.fragment)


def _cleared(res, name: str) -> bool:
    return any(
        h.startswith(f"{name}=") and "Max-Age=0" in h and "Path=/api/v1/auth/google" in h
        for h in res.headers.get_list("set-cookie")
    )


@pytest.mark.asyncio
async def test_start_returns_the_nonce_and_keeps_it_in_an_httponly_cookie(
    session_factory, google_config
) -> None:
    with TestClient(_make_app(session_factory)) as client:
        res = client.get("/api/v1/auth/google")
    assert res.status_code == 200
    nonce = res.json()["nonce"]
    assert len(nonce) >= 16
    assert nonce not in res.json()["redirect_url"]  # never sent to Google
    cookie = next(h for h in res.headers.get_list("set-cookie") if h.startswith("oauth_nonce="))
    assert cookie.startswith(f"oauth_nonce={nonce};")
    assert "HttpOnly" in cookie and "Path=/api/v1/auth/google" in cookie


@pytest.mark.asyncio
async def test_the_callback_echoes_the_nonce_in_the_fragment_and_clears_it(
    session_factory, google_config, monkeypatch
) -> None:
    await _seed_default_plan(session_factory)
    await _seed_existing_sso_user(session_factory, email="returning@example.com")
    _patch_httpx(monkeypatch, userinfo_email="returning@example.com")
    with TestClient(_make_app(session_factory)) as client:
        client.cookies.set("oauth_nonce", NONCE)
        res = _drive_callback(client)
    assert res.status_code == 302, res.text
    fragment = _fragment(res)
    assert fragment["token"]
    assert fragment["nonce"] == [NONCE]
    assert _cleared(res, "oauth_nonce")


@pytest.mark.asyncio
@pytest.mark.parametrize("planted", [None, "short", "x" * 16 + "&token=other", "a" * 65])
async def test_a_missing_or_malformed_nonce_cookie_is_never_echoed(
    session_factory, google_config, monkeypatch, planted
) -> None:
    await _seed_default_plan(session_factory)
    await _seed_existing_sso_user(session_factory, email="returning@example.com")
    _patch_httpx(monkeypatch, userinfo_email="returning@example.com")
    with TestClient(_make_app(session_factory)) as client:
        if planted is not None:
            client.cookies.set("oauth_nonce", planted)
        res = _drive_callback(client)
    fragment = _fragment(res)
    assert "nonce" not in fragment
    assert len(fragment["token"]) == 1


@pytest.mark.asyncio
async def test_an_error_redirect_clears_the_nonce(session_factory, google_config) -> None:
    with TestClient(_make_app(session_factory)) as client:
        client.cookies.set("oauth_nonce", NONCE)
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"error": "access_denied"},
            follow_redirects=False,
        )
    assert "sso_error=cancelled" in res.headers["location"]
    assert _cleared(res, "oauth_nonce")


# ── FE-03: the MFA challenge rides in the fragment, never a query ───────────


@pytest.mark.asyncio
async def test_an_mfa_account_gets_its_challenge_in_the_callback_fragment(
    session_factory, google_config, monkeypatch
) -> None:
    from sqlalchemy import update

    from app.models.user import User
    from app.security import decode_token

    await _seed_default_plan(session_factory)
    user_id = await _seed_existing_sso_user(session_factory, email="returning@example.com")
    async with session_factory() as db:
        await db.execute(update(User).where(User.id == user_id).values(mfa_enabled=True))
        await db.commit()
    _patch_httpx(monkeypatch, userinfo_email="returning@example.com")
    with TestClient(_make_app(session_factory)) as client:
        client.cookies.set("oauth_nonce", NONCE)
        res = _drive_callback(client)
    assert res.status_code == 302, res.text
    parts = urlsplit(res.headers["location"])
    assert parts.path == "/auth/google/callback"
    assert parts.query == ""  # nothing a server or Referer would see
    fragment = parse_qs(parts.fragment)
    assert decode_token(fragment["mfa_token"][0])["type"] == "mfa_challenge"
    assert fragment["nonce"] == [NONCE]
    assert "token" not in fragment
    assert _cleared(res, "oauth_nonce")
