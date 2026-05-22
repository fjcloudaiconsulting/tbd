"""Round-trip + rotation tests for the AI credential crypto helper."""
from __future__ import annotations

import base64
import hashlib
import os

import pytest
from cryptography.fernet import Fernet

from app.config import settings as app_settings
from app.services import ai_credential_crypto as crypto


def _new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


@pytest.fixture(autouse=True)
def _isolate_keys(monkeypatch):
    """Each test gets a fresh AI KEK so settings cross-talk can't bleed."""
    monkeypatch.setattr(
        app_settings, "ai_credential_encryption_key", _new_key()
    )
    monkeypatch.setattr(
        app_settings, "ai_credential_encryption_key_prev", ""
    )


def test_encrypt_decrypt_round_trip_current_key():
    plaintext = "sk-test-AAA-BBB-1234-XYZ"
    token = crypto.encrypt(plaintext)
    assert token != plaintext
    assert crypto.decrypt(token) == plaintext


def test_decrypt_falls_back_to_prev_when_current_fails(monkeypatch):
    prev_key = _new_key()
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key", prev_key)
    token = crypto.encrypt("rotated-secret")
    # Rotate: current becomes a brand new key, prev is the OLD one
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key", _new_key())
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key_prev", prev_key)
    assert crypto.decrypt(token) == "rotated-secret"


def test_decrypt_without_prev_raises_when_token_is_stale(monkeypatch):
    old_key = _new_key()
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key", old_key)
    token = crypto.encrypt("orphan")
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key", _new_key())
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key_prev", "")
    with pytest.raises(ValueError):
        crypto.decrypt(token)


def test_fingerprint_is_stable_first_16_hex_of_sha256():
    plaintext = "sk-fixture-secret-value"
    expected = hashlib.sha256(plaintext.encode()).hexdigest()[:16]
    assert crypto.fingerprint(plaintext) == expected
    assert len(crypto.fingerprint(plaintext)) == 16
    # Stable across calls
    assert crypto.fingerprint(plaintext) == crypto.fingerprint(plaintext)


def test_last_four_extracts_trailing_four_chars_or_empty():
    assert crypto.last_four("sk-test-abcd1234") == "1234"
    assert crypto.last_four("abcd") == "abcd"
    assert crypto.last_four("abc") == ""
    assert crypto.last_four("") == ""
