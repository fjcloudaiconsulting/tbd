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


# ── Encryption ──────────────────────────────────────────────────────────────


def _get_fernet() -> Fernet:
    key = settings.mfa_encryption_key
    if not key:
        raise RuntimeError("MFA_ENCRYPTION_KEY is not configured")
    return Fernet(key.encode())


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


def _hmac_key() -> bytes:
    """Return the HMAC key for recovery code hashing (derived from JWT secret)."""
    return settings.jwt_secret_key.encode()


def hash_recovery_code(code: str) -> str:
    """HMAC-SHA256 a recovery code for storage (keyed, not brute-forceable)."""
    normalized = code.strip().lower().replace("-", "")
    return hmac.new(_hmac_key(), normalized.encode(), "sha256").hexdigest()


def verify_recovery_code(code: str, hashed_codes: list[str]) -> int | None:
    """Check if a code matches any stored HMAC. Constant-time, no early exit."""
    candidate = hash_recovery_code(code)
    match_idx: int | None = None
    for i, stored in enumerate(hashed_codes):
        if hmac.compare_digest(candidate, stored):
            match_idx = i
    return match_idx
