"""MFA service — TOTP setup, verification, recovery codes, and encryption."""

import hmac
import io
import secrets
from base64 import b64encode

import pyotp
import qrcode
import qrcode.constants
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.security import derive_hmac_key


# ── Encryption ──────────────────────────────────────────────────────────────


class MfaConfigError(RuntimeError):
    """Raised when MFA encryption is misconfigured or unavailable."""


def _get_fernet() -> Fernet:
    key = settings.mfa_encryption_key
    if not key:
        raise MfaConfigError("MFA_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise MfaConfigError(f"MFA_ENCRYPTION_KEY is malformed: {exc}") from exc


def encrypt_secret(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_secret(cipher: str) -> str:
    try:
        return _get_fernet().decrypt(cipher.encode()).decode()
    except InvalidToken:
        raise ValueError("Failed to decrypt TOTP secret")


# ── TOTP ────────────────────────────────────────────────────────────────────


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def get_totp_uri(secret: str, email: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(
        name=email, issuer_name=settings.app_name
    )


def verify_totp(secret: str, code: str) -> bool:
    """Verify a TOTP code with +/- 1 window for clock drift."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def generate_qr_base64(uri: str) -> str:
    """Generate a QR code PNG as a base64-encoded string."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return b64encode(buf.getvalue()).decode()


# ── Recovery Codes ──────────────────────────────────────────────────────────


def generate_recovery_codes(count: int = 8) -> list[str]:
    """Generate high-entropy recovery codes (xxxx-xxxx-xxxx-xxxx format, 64-bit)."""
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(8)  # 16 hex chars = 64 bits
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:]}")
    return codes


RECOVERY_CODE_PURPOSE = b"mfa-recovery-code-v1"


def _hmac_key() -> bytes:
    """Purpose-bound HMAC key for recovery code hashing.

    Derived from the JWT secret rather than using it raw, so the JWT
    signing key is never reused directly as hashing key material.
    """
    return derive_hmac_key(RECOVERY_CODE_PURPOSE)


def _legacy_hmac_key() -> bytes:
    """Pre-derivation key (the raw JWT secret). Verify-only fallback for
    recovery-code hashes stored before the purpose-bound scheme."""
    return settings.jwt_secret_key.encode()


def _normalize(code: str) -> str:
    return code.strip().lower().replace("-", "")


def hash_recovery_code(code: str) -> str:
    """HMAC-SHA256 a recovery code for storage (keyed, not brute-forceable)."""
    return hmac.new(_hmac_key(), _normalize(code).encode(), "sha256").hexdigest()


def _hash_recovery_code_legacy(code: str) -> str:
    return hmac.new(_legacy_hmac_key(), _normalize(code).encode(), "sha256").hexdigest()


def verify_recovery_code(code: str, hashed_codes: list[str]) -> int | None:
    """Check if a code matches any stored HMAC. Constant-time, no early exit.

    Tries the purpose-bound derived key first, then falls back to the
    legacy raw-jwt_secret_key scheme so hashes stored before the derived
    key shipped keep verifying. On a legacy match the entry is re-hashed
    in place under the new scheme (lazy migration); callers that persist
    ``hashed_codes`` after a match therefore write the upgraded value.
    """
    candidate = hash_recovery_code(code)
    legacy_candidate = _hash_recovery_code_legacy(code)
    match_idx: int | None = None
    for i, stored in enumerate(hashed_codes):
        if hmac.compare_digest(candidate, stored):
            match_idx = i
        elif hmac.compare_digest(legacy_candidate, stored):
            hashed_codes[i] = candidate
            match_idx = i
    return match_idx
