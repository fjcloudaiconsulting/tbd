"""Unit tests for ``app.captcha.verify_captcha``.

Covers the contract enforced by the register endpoint:
  * disabled flag short-circuits to ok=True
  * missing token → fail-closed
  * provider success=true + hostname/action match → ok=True
  * provider success=false → fail-closed with provider error codes
  * hostname/action mismatch → fail-closed
  * timeout / network error / 5xx → fail-closed
  * token value never appears in captured log/event output
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from app import captcha
from app.captcha import (
    REASON_ACTION_MISMATCH,
    REASON_DISABLED,
    REASON_HOSTNAME_MISMATCH,
    REASON_MISSING_TOKEN,
    REASON_NETWORK_ERROR,
    REASON_OK,
    REASON_PROVIDER_ERROR,
    REASON_PROVIDER_REJECTED,
    REASON_TIMEOUT,
    verify_captcha,
)
from app.config import settings as app_settings


# ── shared fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def configure_captcha(monkeypatch):
    """Default the settings to an "enabled" Turnstile-like config so
    each test only overrides what it needs."""
    monkeypatch.setattr(app_settings, "captcha_required", True)
    monkeypatch.setattr(app_settings, "captcha_provider", "turnstile")
    monkeypatch.setattr(app_settings, "captcha_secret", "test-secret")
    monkeypatch.setattr(
        app_settings,
        "captcha_verify_url",
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    )
    monkeypatch.setattr(app_settings, "captcha_verify_timeout_s", 5.0)
    monkeypatch.setattr(app_settings, "captcha_expected_hostname", "")
    monkeypatch.setattr(app_settings, "captcha_expected_action", "")
    yield


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("invalid json")
        return self._payload


def _patch_httpx(monkeypatch, *, response: _FakeResponse | None = None, raises: Exception | None = None):
    captured_calls: list[dict[str, Any]] = []

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._kwargs = kwargs

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
            captured_calls.append({"url": url, **kwargs})
            if raises is not None:
                raise raises
            assert response is not None
            return response

    monkeypatch.setattr(captcha.httpx, "AsyncClient", _FakeClient)
    return captured_calls


# ── disabled / missing-token short-circuits ─────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_short_circuits_ok(monkeypatch):
    monkeypatch.setattr(app_settings, "captcha_required", False)
    # httpx should NOT be touched when disabled.
    calls = _patch_httpx(monkeypatch, response=_FakeResponse(200, {"success": True}))

    result = await verify_captcha("any-token", "1.2.3.4")

    assert result.ok is True
    assert result.reason == REASON_DISABLED
    assert calls == []


@pytest.mark.asyncio
async def test_missing_token_fails_closed(monkeypatch):
    calls = _patch_httpx(monkeypatch, response=_FakeResponse(200, {"success": True}))

    result = await verify_captcha(None, "1.2.3.4")

    assert result.ok is False
    assert result.reason == REASON_MISSING_TOKEN
    assert calls == []


@pytest.mark.asyncio
async def test_empty_token_fails_closed(monkeypatch):
    _patch_httpx(monkeypatch, response=_FakeResponse(200, {"success": True}))
    result = await verify_captcha("", "1.2.3.4")
    assert result.ok is False
    assert result.reason == REASON_MISSING_TOKEN


# ── happy path ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_success_returns_ok(monkeypatch):
    calls = _patch_httpx(
        monkeypatch,
        response=_FakeResponse(
            200,
            {
                "success": True,
                "challenge_ts": "2026-05-21T07:00:00Z",
                "hostname": "app.thebetterdecision.com",
                "action": "register",
            },
        ),
    )

    result = await verify_captcha("good-token", "203.0.113.7")

    assert result.ok is True
    assert result.reason == REASON_OK
    assert result.observed_hostname == "app.thebetterdecision.com"
    assert result.observed_action == "register"
    assert len(calls) == 1
    body = calls[0]["data"]
    assert body["secret"] == "test-secret"
    assert body["response"] == "good-token"
    assert body["remoteip"] == "203.0.113.7"
    # idempotency_key is a fresh UUID per call — must be present.
    assert "idempotency_key" in body
    assert len(body["idempotency_key"]) >= 32


@pytest.mark.asyncio
async def test_remote_ip_optional(monkeypatch):
    calls = _patch_httpx(
        monkeypatch,
        response=_FakeResponse(200, {"success": True}),
    )
    result = await verify_captcha("good-token", None)
    assert result.ok is True
    assert "remoteip" not in calls[0]["data"]


# ── provider rejection / mismatch ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_rejection_fails_closed(monkeypatch):
    _patch_httpx(
        monkeypatch,
        response=_FakeResponse(
            200,
            {
                "success": False,
                "error-codes": ["invalid-input-response", "timeout-or-duplicate"],
            },
        ),
    )

    result = await verify_captcha("stale-token", None)

    assert result.ok is False
    assert result.reason == REASON_PROVIDER_REJECTED
    assert result.provider_error_codes == (
        "invalid-input-response",
        "timeout-or-duplicate",
    )


@pytest.mark.asyncio
async def test_hostname_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(app_settings, "captcha_expected_hostname", "app.thebetterdecision.com")
    _patch_httpx(
        monkeypatch,
        response=_FakeResponse(
            200,
            {"success": True, "hostname": "evil.example.com", "action": "register"},
        ),
    )

    result = await verify_captcha("token", None)

    assert result.ok is False
    assert result.reason == REASON_HOSTNAME_MISMATCH
    assert result.observed_hostname == "evil.example.com"


@pytest.mark.asyncio
async def test_action_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(app_settings, "captcha_expected_action", "register")
    _patch_httpx(
        monkeypatch,
        response=_FakeResponse(
            200,
            {"success": True, "hostname": "app.thebetterdecision.com", "action": "login"},
        ),
    )

    result = await verify_captcha("token", None)

    assert result.ok is False
    assert result.reason == REASON_ACTION_MISMATCH


# ── transport / provider failures ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_fails_closed(monkeypatch):
    _patch_httpx(monkeypatch, raises=httpx.TimeoutException("boom"))

    result = await verify_captcha("token", None)

    assert result.ok is False
    assert result.reason == REASON_TIMEOUT


@pytest.mark.asyncio
async def test_network_error_fails_closed(monkeypatch):
    _patch_httpx(monkeypatch, raises=httpx.ConnectError("nope"))

    result = await verify_captcha("token", None)

    assert result.ok is False
    assert result.reason == REASON_NETWORK_ERROR


@pytest.mark.asyncio
async def test_5xx_fails_closed(monkeypatch):
    _patch_httpx(monkeypatch, response=_FakeResponse(503, {"error": "down"}))

    result = await verify_captcha("token", None)

    assert result.ok is False
    assert result.reason == REASON_PROVIDER_ERROR


@pytest.mark.asyncio
async def test_4xx_fails_closed(monkeypatch):
    _patch_httpx(monkeypatch, response=_FakeResponse(400, {"error": "bad"}))

    result = await verify_captcha("token", None)

    assert result.ok is False
    assert result.reason == REASON_PROVIDER_ERROR


@pytest.mark.asyncio
async def test_invalid_json_fails_closed(monkeypatch):
    _patch_httpx(monkeypatch, response=_FakeResponse(200, None))

    result = await verify_captcha("token", None)

    assert result.ok is False
    assert result.reason == REASON_PROVIDER_ERROR


# ── secrecy ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_never_appears_in_structlog_events(monkeypatch, caplog):
    """The captcha response token is the only sensitive value in the
    happy-path payload (it's a server-validated bearer good for 300s).
    Any code path must NOT echo it into our structured logs.
    """
    _patch_httpx(
        monkeypatch,
        response=_FakeResponse(
            200,
            {"success": False, "error-codes": ["timeout-or-duplicate"]},
        ),
    )

    secret_token = "DO-NOT-LEAK-THIS-VALUE-123456"
    await verify_captcha(secret_token, "203.0.113.7")

    for record in caplog.records:
        assert secret_token not in record.getMessage()
        for arg in getattr(record, "args", []) or []:
            assert secret_token not in str(arg)
