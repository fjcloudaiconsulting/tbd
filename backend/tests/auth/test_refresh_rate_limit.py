"""BE-10: ``POST /auth/refresh`` is limited to 30 a minute per session.

* The bucket is the refresh cookie's session family (``sid``), which every
  rotation keeps: call 31 within the minute is a 429, while another session
  from the same address still refreshes.
* A request whose cookie does not verify as a refresh token (missing,
  garbage, expired, another token type) is bucketed per IP instead, apart
  from the per-session buckets.

Calls 1..N assert the exact normal status and call N+1 the 429, so a loosened
number fails as well as a missing limit.
"""
from __future__ import annotations

from datetime import datetime, timezone

import jwt
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import settings as app_settings
from app.rate_limit import rate_limit_key
from app.routers.auth import _refresh_rate_limit_key
from app.security import create_access_token, create_refresh_token

from tests.auth.test_session_grace import (  # noqa: F401  (fixtures + helpers)
    _canonical_refresh_cookie,
    _login,
    _make_app,
    _refresh_token_from_set_cookie,
    _seed_user,
    reset_limiter,
    session_factory,
)
from tests.conftest import set_refresh_cookie

REFRESH_LIMIT = 30
REFRESH = "/api/v1/auth/refresh"


def _request(*, cookies: list[str] = (), client: str = "198.51.100.7") -> Request:
    header = "; ".join(f"refresh_token={c}" for c in cookies)
    headers = [(b"cookie", header.encode())] if header else []
    return Request({"type": "http", "method": "POST", "path": REFRESH, "headers": headers, "client": (client, 5555)})


def _refresh_chain(client, token: str, n: int) -> tuple[str, list[int]]:
    """Refresh ``n`` times, following the rotated cookie. Returns the last
    cookie and every status."""
    statuses = []
    for _ in range(n):
        set_refresh_cookie(client, token)
        res = client.post(REFRESH)
        statuses.append(res.status_code)
        raw = _canonical_refresh_cookie(res.headers)
        if raw is not None:
            token = _refresh_token_from_set_cookie(raw)
    return token, statuses


async def test_one_session_gets_30_refreshes_a_minute(session_factory):
    await _seed_user(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        token = _login(client)
        token, statuses = _refresh_chain(client, token, REFRESH_LIMIT)
        assert statuses == [200] * REFRESH_LIMIT
        set_refresh_cookie(client, token)
        over = client.post(REFRESH)
    assert over.status_code == 429


async def test_another_session_on_the_same_address_is_not_held(session_factory):
    """Keyed on the session, not the IP: TestClient sends every request from
    one address, and the second session still refreshes."""
    await _seed_user(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        first = _login(client)
        second = _login(client)
        first, statuses = _refresh_chain(client, first, REFRESH_LIMIT + 1)
        assert statuses[-1] == 429
        _second, second_statuses = _refresh_chain(client, second, 1)
    assert second_statuses == [200]


async def test_an_unparseable_cookie_falls_back_to_the_ip_bucket(session_factory):
    await _seed_user(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        statuses = []
        for _ in range(REFRESH_LIMIT + 1):
            set_refresh_cookie(client, "not-a-jwt")
            statuses.append(client.post(REFRESH).status_code)
        # A real session from the same address has its own bucket.
        _token, session_statuses = _refresh_chain(client, _login(client), 1)
    assert statuses == [401] * REFRESH_LIMIT + [429]
    assert session_statuses == [200]


def test_the_key_is_the_session_family_wherever_it_calls_from():
    token, _jti, sid = create_refresh_token(1)
    here = _refresh_rate_limit_key(_request(cookies=[token], client="198.51.100.7"))
    there = _refresh_rate_limit_key(_request(cookies=[token], client="203.0.113.9"))
    assert here == there == f"refresh-session:{sid}"


def test_the_newest_of_several_cookies_names_the_bucket():
    now = int(datetime.now(timezone.utc).timestamp())
    older = jwt.encode(
        {"sub": "1", "type": "refresh", "jti": "j-old", "sid": "s-old", "iat": now - 60, "exp": now + 600},
        app_settings.jwt_secret_key,
        algorithm=app_settings.jwt_algorithm,
    )
    newer = jwt.encode(
        {"sub": "1", "type": "refresh", "jti": "j-new", "sid": "s-new", "iat": now, "exp": now + 600},
        app_settings.jwt_secret_key,
        algorithm=app_settings.jwt_algorithm,
    )
    assert _refresh_rate_limit_key(_request(cookies=[newer, older])) == "refresh-session:s-new"
    assert _refresh_rate_limit_key(_request(cookies=[older, newer])) == "refresh-session:s-new"


def test_anything_that_is_not_a_live_refresh_token_is_keyed_per_ip():
    now = int(datetime.now(timezone.utc).timestamp())

    def _sign(payload: dict, key: str | None = None) -> str:
        return jwt.encode(payload, key or app_settings.jwt_secret_key, algorithm=app_settings.jwt_algorithm)

    claims = {"sub": "1", "type": "refresh", "jti": "j", "sid": "s", "iat": now}
    unusable = {
        "none": [],
        "garbage": ["not-a-jwt"],
        "expired": [_sign({**claims, "exp": now - 1})],
        "forged": [_sign({**claims, "exp": now + 600}, key="x" * 64)],
        "access token": [create_access_token(1, 1, "owner")],
        "no sid": [_sign({k: v for k, v in claims.items() if k != "sid"} | {"exp": now + 600})],
    }
    for name, cookies in unusable.items():
        request = _request(cookies=cookies, client="203.0.113.9")
        assert _refresh_rate_limit_key(request) == rate_limit_key(request) == "203.0.113.9", name
    # IPv6 keeps the /64 bucketing of the per-IP key.
    v6 = _request(client="2001:db8:1:2:3:4:5:6")
    assert _refresh_rate_limit_key(v6) == "2001:db8:1:2::/64"
