"""Regression tests for the RequestValidationError redaction handler.

FastAPI's default 422 response echoes the entire submitted input under
`detail[i].input` — including passwords on register/login bodies. The
custom handler in app/main.py walks that input recursively and replaces
known-sensitive field VALUES with the literal '<redacted>' before the
response goes out.

These tests pin the redaction set, the response shape, and the
walk-recursion (nested dicts and lists).
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.main import (
    _REDACTED,
    _SENSITIVE_FIELD_NAMES,
    _loc_targets_sensitive_field,
    _redact_sensitive,
    request_validation_handler,
)


# Module-level models (FastAPI/pydantic introspection can be flaky on
# locally-scoped classes once a process accumulates several apps).
class _RegisterLike(BaseModel):
    username: str = Field(min_length=3)
    email: str
    password: str = Field(min_length=8)


class _WithNested(BaseModel):
    outer: dict[str, Any]
    sibling: int


class _MfaLike(BaseModel):
    """Mirrors the shape of MfaVerifyRequest — bare `code` field with a
    fixed length constraint. A short `code` returns a field-level
    error with `input` = the submitted scalar."""
    code: str = Field(min_length=6, max_length=6)


@pytest.fixture
def app() -> FastAPI:
    """Minimal FastAPI app that wires only the handler under test plus
    one validating endpoint per scenario. Isolated from real app
    lifespan / DB setup / migrations."""
    a = FastAPI()
    a.add_exception_handler(RequestValidationError, request_validation_handler)

    @a.post("/register")
    async def register(body: _RegisterLike):
        return {"ok": True}

    @a.post("/nested")
    async def nested(body: _WithNested):
        return {"ok": True}

    @a.post("/mfa")
    async def mfa(body: _MfaLike):
        return {"ok": True}

    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ── unit: _redact_sensitive ────────────────────────────────────────────────


def test_redact_replaces_top_level_sensitive_field():
    out = _redact_sensitive({"password": "hunter2", "username": "alice"})
    assert out == {"password": _REDACTED, "username": "alice"}


def test_redact_walks_into_nested_dicts():
    out = _redact_sensitive({"outer": {"password": "secret", "ok": "x"}, "sibling": 1})
    assert out == {"outer": {"password": _REDACTED, "ok": "x"}, "sibling": 1}


def test_redact_walks_into_lists_of_dicts():
    out = _redact_sensitive({"items": [{"password": "p1"}, {"username": "u"}]})
    assert out == {"items": [{"password": _REDACTED}, {"username": "u"}]}


def test_redact_passes_scalars_unchanged():
    assert _redact_sensitive("plain string") == "plain string"
    assert _redact_sensitive(42) == 42
    assert _redact_sensitive(None) is None
    assert _redact_sensitive([1, 2, 3]) == [1, 2, 3]


def test_redact_does_not_mutate_input():
    original = {"password": "hunter2", "ok": "x"}
    out = _redact_sensitive(original)
    assert original == {"password": "hunter2", "ok": "x"}
    assert out == {"password": _REDACTED, "ok": "x"}


def test_sensitive_field_set_covers_review_required_names():
    """Per the architect-locked spec — these names MUST be in the set.
    Adding more is fine; removing any is a regression. The bare `code`
    name was added after the first review caught that MFA flows
    (MfaEnableRequest, MfaVerifyRequest, MfaRecoveryRequest,
    MfaEmailVerifyRequest in backend/app/schemas/auth.py) all use
    `code` as the field name."""
    required = {
        "password", "new_password", "current_password", "confirm_password",
        "token", "refresh_token", "mfa_token", "email_token", "recovery_code",
        "code",
    }
    assert required <= _SENSITIVE_FIELD_NAMES


# ── unit: _loc_targets_sensitive_field ─────────────────────────────────────


def test_loc_redaction_matches_sensitive_field_at_tail():
    assert _loc_targets_sensitive_field(["body", "password"]) is True
    assert _loc_targets_sensitive_field(("body", "password")) is True


def test_loc_redaction_matches_sensitive_at_any_depth():
    """A sensitive field nested under a list index (loc=['body', 'items',
    0, 'password']) still triggers the redaction."""
    assert _loc_targets_sensitive_field(["body", "items", 0, "password"]) is True


def test_loc_redaction_matches_bare_code_for_mfa():
    assert _loc_targets_sensitive_field(["body", "code"]) is True


def test_loc_redaction_does_not_match_unrelated_fields():
    assert _loc_targets_sensitive_field(["body", "username"]) is False
    assert _loc_targets_sensitive_field(["body", "email"]) is False
    assert _loc_targets_sensitive_field(["query", "page"]) is False


def test_loc_redaction_handles_non_iterable_loc():
    """Defensive — if loc is missing or malformed, return False rather
    than crash the error handler."""
    assert _loc_targets_sensitive_field(None) is False
    # A bare string is iterable but the parts are characters, not field
    # names; we must not match a stray 'p' from "password" inside it.
    assert _loc_targets_sensitive_field("body.password") is False
    assert _loc_targets_sensitive_field(42) is False


# ── integration: handler returns the standard 422 shape with redacted input ──


def test_handler_redacts_password_in_422_response(client: TestClient):
    res = client.post(
        "/register",
        json={"email": "alice@example.com", "password": "supersecret"},
    )
    assert res.status_code == 422
    body = res.json()
    assert "detail" in body and isinstance(body["detail"], list)
    # The literal password must NOT appear anywhere in the response.
    assert "supersecret" not in res.text
    # And the input echo, where present, has password redacted.
    for err in body["detail"]:
        if isinstance(err.get("input"), dict) and "password" in err["input"]:
            assert err["input"]["password"] == _REDACTED


def test_handler_does_not_redact_unrelated_field_values(client: TestClient):
    """A non-sensitive field's value (here: short username) should still
    surface so the user knows what failed."""
    res = client.post(
        "/register",
        json={"username": "ab", "email": "x@x.io", "password": "longenough123"},
    )
    assert res.status_code == 422
    # Bad username 'ab' visible somewhere in the response (it's not sensitive).
    assert any(
        isinstance(err.get("input"), str) and err["input"] == "ab"
        for err in res.json()["detail"]
    ) or "ab" in res.text
    # Password value still redacted everywhere it appears.
    assert "longenough123" not in res.text


def test_handler_redacts_nested_sensitive_field(client: TestClient):
    """A sensitive key inside a nested dict still gets scrubbed."""
    res = client.post(
        "/nested",
        # Missing 'sibling' -> 422; 'outer.token' is sensitive and must scrub.
        json={"outer": {"token": "abc-secret", "ok": "ok"}},
    )
    assert res.status_code == 422
    assert "abc-secret" not in res.text
    body = res.json()
    found_outer = False
    for err in body["detail"]:
        inp = err.get("input")
        if isinstance(inp, dict) and "outer" in inp:
            found_outer = True
            assert inp["outer"].get("token") == _REDACTED
            assert inp["outer"].get("ok") == "ok"
    assert found_outer, body


def test_handler_preserves_default_error_shape(client: TestClient):
    """Don't reshape FastAPI's 422; only sanitize the 'input' field."""
    res = client.post("/register", json={"email": "x@x.io", "password": "short"})
    assert res.status_code == 422
    body = res.json()
    assert "detail" in body
    for err in body["detail"]:
        assert "type" in err
        assert "loc" in err
        assert "msg" in err


# ── regression: field-level scalar leaks (PR #127 review finding 1) ────────


def test_field_level_password_scalar_is_redacted(client: TestClient):
    """Pydantic field-level errors put the offending value in `input`
    as a SCALAR (not a dict). The recursive walk doesn't catch this,
    so the handler MUST `loc`-match and redact the whole input.
    Regression: a too-short password was leaking the literal value."""
    leaky_password = "shortpw"  # 7 chars — fails min_length=8
    res = client.post(
        "/register",
        json={
            "username": "valid_user",
            "email": "x@x.io",
            "password": leaky_password,
        },
    )
    assert res.status_code == 422
    # The literal password must NOT appear ANYWHERE in the response —
    # not in detail[i].input, not in detail[i].ctx, not anywhere.
    assert leaky_password not in res.text, (
        f"leaked password found in 422 response: {res.text}"
    )
    # And the field-level error for password specifically must redact.
    body = res.json()
    pw_errors = [
        e for e in body["detail"]
        if isinstance(e.get("loc"), list) and "password" in e["loc"]
    ]
    assert pw_errors, f"expected a field-level error for password, got {body}"
    for err in pw_errors:
        if "input" in err:
            assert err["input"] == _REDACTED, err


def test_field_level_mfa_code_scalar_is_redacted(client: TestClient):
    """The bare `code` field is used by MFA verify / recovery / email-
    verify flows. A field-level validation failure on `code` would
    otherwise echo the submitted code in the 422 response, defeating
    the point of code-based auth."""
    leaky_code = "123"  # 3 chars — fails min_length=6
    res = client.post("/mfa", json={"code": leaky_code})
    assert res.status_code == 422
    assert leaky_code not in res.text, (
        f"leaked MFA code found in 422 response: {res.text}"
    )
    body = res.json()
    code_errors = [
        e for e in body["detail"]
        if isinstance(e.get("loc"), list) and "code" in e["loc"]
    ]
    assert code_errors, f"expected a field-level error for code, got {body}"
    for err in code_errors:
        if "input" in err:
            assert err["input"] == _REDACTED, err
