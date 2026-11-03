"""TBD-654: the email-first signup token and the per-address mail-cap key.

The token proves an inbox before any account exists, so it carries the
address, encrypted, and no user id. It must never raise on a bad input:
``/signup/complete`` answers 400 on every failure, never 500.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings
from app.security import (
    SIGNUP_TOKEN_TTL_SECONDS,
    create_email_verification_token,
    create_signup_token,
    decode_signup_token,
    signup_mail_key,
)


def test_round_trip_returns_normalised_email_and_jti():
    claims = decode_signup_token(create_signup_token(" Bob@Example.com "))
    assert claims is not None
    assert claims.email == "bob@example.com"
    assert len(claims.jti) >= 16
    assert claims.exp > datetime.now(timezone.utc).timestamp()


def test_token_payload_never_contains_plain_email():
    tok = create_signup_token("bob@example.com")
    payload = jwt.decode(tok, options={"verify_signature": False})
    assert "bob" not in json.dumps(payload)
    assert payload["type"] == "signup"
    assert "sub" not in payload


def test_each_token_has_its_own_jti():
    a = decode_signup_token(create_signup_token("a@b.co"))
    b = decode_signup_token(create_signup_token("a@b.co"))
    assert a.jti != b.jti


def test_decode_rejects_wrong_type():
    assert decode_signup_token(create_email_verification_token(1, "a@b.co")) is None


def test_decode_rejects_expired():
    tok = create_signup_token("a@b.co")
    payload = jwt.decode(tok, options={"verify_signature": False})
    past = datetime.now(timezone.utc) - timedelta(seconds=SIGNUP_TOKEN_TTL_SECONDS + 1)
    payload["iat"], payload["exp"] = past - timedelta(hours=1), past
    expired = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    assert decode_signup_token(expired) is None


def test_decode_rejects_tampered_signature():
    tok = create_signup_token("a@b.co")
    head, body, sig = tok.split(".")
    flipped = sig[:-2] + ("A" if sig[-2] != "A" else "B") + sig[-1]
    assert decode_signup_token(f"{head}.{body}.{flipped}") is None


def test_decode_rejects_undecryptable_or_missing_claims():
    def signed(extra: dict) -> str:
        now = datetime.now(timezone.utc)
        payload = {"type": "signup", "jti": "j" * 22, "iat": now, "exp": now + timedelta(hours=1)}
        payload.update(extra)
        return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

    assert decode_signup_token(signed({"enc_email": "not-a-fernet-token"})) is None
    assert decode_signup_token(signed({"enc_email": 7})) is None
    assert decode_signup_token(signed({})) is None
    good = jwt.decode(create_signup_token("a@b.co"), options={"verify_signature": False})
    no_jti = signed({"enc_email": good["enc_email"]})
    no_jti_payload = jwt.decode(no_jti, options={"verify_signature": False})
    no_jti_payload.pop("jti")
    assert decode_signup_token(
        jwt.encode(no_jti_payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    ) is None


def test_decode_rejects_garbage():
    assert decode_signup_token("") is None
    assert decode_signup_token("not.a.jwt") is None


def test_decode_signup_token_wrong_key_returns_none(monkeypatch):
    tok = create_signup_token("a@b.co")
    monkeypatch.setattr(settings, "jwt_secret_key", "x" * 64)
    assert decode_signup_token(tok) is None


def test_decode_signup_token_wrong_encryption_key_returns_none(monkeypatch):
    """Signed with today's secret, encrypted under another: decryption fails
    after the signature passes, and that still answers None."""
    import app.security as sec

    real = sec.derive_hmac_key
    monkeypatch.setattr(sec, "derive_hmac_key", lambda purpose: real(purpose + b"-other"))
    tok = create_signup_token("a@b.co")
    monkeypatch.setattr(sec, "derive_hmac_key", real)
    assert decode_signup_token(tok) is None


def test_signup_mail_key_is_keyed_and_normalised():
    assert signup_mail_key(" A@B.co ") == signup_mail_key("a@b.co")
    assert signup_mail_key("a@b.co") != hashlib.sha256(b"a@b.co").hexdigest()
    assert len(signup_mail_key("a@b.co")) == 64
