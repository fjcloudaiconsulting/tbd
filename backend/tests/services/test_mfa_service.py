import base64
from base64 import urlsafe_b64encode

import pyotp
import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.services.mfa_service import (
    MfaConfigError,
    decrypt_secret,
    encrypt_secret,
    generate_qr_base64,
    generate_recovery_codes,
    generate_totp_secret,
    get_totp_uri,
    hash_recovery_code,
    verify_recovery_code,
    verify_totp,
)


def test_encrypt_and_decrypt_secret_roundtrip(monkeypatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "mfa_encryption_key", key)

    encrypted = encrypt_secret("super-secret-totp-seed")

    assert encrypted != "super-secret-totp-seed"
    assert decrypt_secret(encrypted) == "super-secret-totp-seed"


def test_encrypt_secret_requires_configured_encryption_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mfa_encryption_key", "")

    with pytest.raises(MfaConfigError, match="not configured"):
        encrypt_secret("secret")


def test_encrypt_secret_rejects_malformed_encryption_key(monkeypatch) -> None:
    malformed = urlsafe_b64encode(b"too-short").decode()
    monkeypatch.setattr(settings, "mfa_encryption_key", malformed)

    with pytest.raises(MfaConfigError, match="malformed"):
        encrypt_secret("secret")


def test_decrypt_secret_rejects_tampered_ciphertext(monkeypatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "mfa_encryption_key", key)
    encrypted = encrypt_secret("seed")
    tampered = f"{encrypted[:-1]}{'A' if encrypted[-1] != 'A' else 'B'}"

    with pytest.raises(ValueError, match="Failed to decrypt TOTP secret"):
        decrypt_secret(tampered)


def test_generate_totp_secret_produces_base32_secret() -> None:
    secret = generate_totp_secret()

    assert len(secret) >= 32
    assert secret.isupper()


def test_get_totp_uri_includes_issuer_and_email() -> None:
    secret = "JBSWY3DPEHPK3PXP"
    uri = get_totp_uri(secret, "alice@example.com")

    assert "alice%40example.com" in uri
    assert f"issuer={settings.app_name.replace(' ', '%20')}" in uri


def test_verify_totp_accepts_current_code() -> None:
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()

    assert verify_totp(secret, code) is True
    assert verify_totp(secret, "000000") is False


def test_generate_qr_base64_returns_png_bytes() -> None:
    png_base64 = generate_qr_base64("otpauth://totp/Test?secret=ABC123")
    decoded = base64.b64decode(png_base64)

    assert decoded.startswith(b"\x89PNG\r\n\x1a\n")


def test_generate_recovery_codes_respects_count_and_format() -> None:
    codes = generate_recovery_codes(count=4)

    assert len(codes) == 4
    for code in codes:
        parts = code.split("-")
        assert len(parts) == 4
        assert all(len(part) == 4 for part in parts)


def test_hash_recovery_code_normalizes_case_and_hyphens() -> None:
    assert hash_recovery_code("ABCD-1234-EF56-7890") == hash_recovery_code(
        "abcd1234ef567890"
    )


def test_verify_recovery_code_returns_matching_index_or_none() -> None:
    hashed_codes = [
        hash_recovery_code("aaaa-bbbb-cccc-dddd"),
        hash_recovery_code("1111-2222-3333-4444"),
    ]

    assert verify_recovery_code("1111222233334444", hashed_codes) == 1
    assert verify_recovery_code("ffff-eeee-dddd-cccc", hashed_codes) is None
