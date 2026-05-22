"""Lifespan KEK separation guard tests.

The guard MUST refuse to boot when AI_CREDENTIAL_ENCRYPTION_KEY shares
its value with MFA_ENCRYPTION_KEY (or either of their _PREV slots).
The check is gated on APP_ENV != 'test' so the unit suite can use a
single fixture key, so these tests flip the env to 'production'
before invoking the guard.
"""
from __future__ import annotations

import base64
import os

import pytest

from app.config import settings as app_settings
from app.main import (
    AiCredentialKeyReusesMfaKey,
    verify_ai_credential_kek_separation,
)


def _new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def test_guard_raises_when_ai_key_equals_mfa_key(monkeypatch):
    shared = _new_key()
    monkeypatch.setattr(app_settings, "app_env", "production")
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key", shared)
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key_prev", "")
    monkeypatch.setattr(app_settings, "mfa_encryption_key", shared)
    with pytest.raises(AiCredentialKeyReusesMfaKey):
        verify_ai_credential_kek_separation()


def test_guard_raises_when_prev_ai_key_equals_mfa_key(monkeypatch):
    shared = _new_key()
    monkeypatch.setattr(app_settings, "app_env", "production")
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key", _new_key())
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key_prev", shared)
    monkeypatch.setattr(app_settings, "mfa_encryption_key", shared)
    with pytest.raises(AiCredentialKeyReusesMfaKey):
        verify_ai_credential_kek_separation()


def test_guard_passes_when_keys_differ(monkeypatch):
    monkeypatch.setattr(app_settings, "app_env", "production")
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key", _new_key())
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key_prev", "")
    monkeypatch.setattr(app_settings, "mfa_encryption_key", _new_key())
    # Should not raise.
    verify_ai_credential_kek_separation()


def test_guard_skips_in_test_env(monkeypatch):
    shared = _new_key()
    monkeypatch.setattr(app_settings, "app_env", "test")
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key", shared)
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key_prev", "")
    monkeypatch.setattr(app_settings, "mfa_encryption_key", shared)
    # Skipped — no exception even with a collision.
    verify_ai_credential_kek_separation()


def test_guard_skips_when_ai_key_empty(monkeypatch):
    monkeypatch.setattr(app_settings, "app_env", "production")
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key", "")
    monkeypatch.setattr(app_settings, "ai_credential_encryption_key_prev", "")
    monkeypatch.setattr(app_settings, "mfa_encryption_key", _new_key())
    verify_ai_credential_kek_separation()
