import base64
import hashlib
import hmac
from base64 import urlsafe_b64encode

import pyotp
import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.security import derive_hmac_key, mfa_email_code_hmac
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


# ── Purpose-bound HMAC key derivation ────────────────────────────────────────


def _legacy_hash(code: str) -> str:
    """Hash a recovery code the pre-derivation way: raw jwt_secret_key."""
    normalized = code.strip().lower().replace("-", "")
    return hmac.new(
        settings.jwt_secret_key.encode(), normalized.encode(), "sha256"
    ).hexdigest()


def test_hash_recovery_code_uses_derived_key_not_raw_jwt_secret() -> None:
    code = "aaaa-bbbb-cccc-dddd"

    expected = hmac.new(
        derive_hmac_key(b"mfa-recovery-code-v1"),
        "aaaabbbbccccdddd".encode(),
        "sha256",
    ).hexdigest()

    assert hash_recovery_code(code) == expected
    assert hash_recovery_code(code) != _legacy_hash(code)


def test_verify_recovery_code_accepts_legacy_hash_and_migrates_it() -> None:
    """Hashes stored under the raw jwt_secret_key (pre-derivation deploys)
    must keep verifying, and get lazily re-hashed to the derived scheme."""
    legacy = _legacy_hash("aaaa-bbbb-cccc-dddd")
    other = hash_recovery_code("1111-2222-3333-4444")
    hashed_codes = [legacy, other]

    idx = verify_recovery_code("aaaa-bbbb-cccc-dddd", hashed_codes)

    assert idx == 0
    # The matched entry was migrated in place to the derived-key scheme.
    assert hashed_codes[0] != legacy
    assert hashed_codes[0] == hash_recovery_code("aaaa-bbbb-cccc-dddd")
    # Untouched entries stay as they were.
    assert hashed_codes[1] == other


def test_verify_recovery_code_wrong_code_fails_under_both_schemes() -> None:
    hashed_codes = [
        _legacy_hash("aaaa-bbbb-cccc-dddd"),
        hash_recovery_code("1111-2222-3333-4444"),
    ]
    snapshot = list(hashed_codes)

    assert verify_recovery_code("ffff-eeee-dddd-cccc", hashed_codes) is None
    assert hashed_codes == snapshot  # no migration on failure


def test_derived_keys_are_purpose_bound_and_distinct() -> None:
    recovery_key = derive_hmac_key(b"mfa-recovery-code-v1")
    email_key = derive_hmac_key(b"mfa-email-code-v1")
    raw = settings.jwt_secret_key.encode()

    assert recovery_key != email_key
    assert recovery_key != raw
    assert email_key != raw
    # Derivation is HMAC-SHA256(jwt_secret_key, purpose)
    assert recovery_key == hmac.new(raw, b"mfa-recovery-code-v1", hashlib.sha256).digest()


def test_mfa_email_code_hmac_uses_derived_email_key() -> None:
    code = "482913"
    expected = hmac.new(
        derive_hmac_key(b"mfa-email-code-v1"), code.encode(), "sha256"
    ).hexdigest()
    raw_keyed = hmac.new(settings.jwt_secret_key.encode(), code.encode(), "sha256").hexdigest()

    assert mfa_email_code_hmac(code) == expected
    assert mfa_email_code_hmac(code) != raw_keyed
    assert mfa_email_code_hmac(code) != hash_recovery_code(code)
