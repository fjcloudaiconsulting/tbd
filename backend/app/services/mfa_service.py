"""MFA service — TOTP setup, verification, recovery codes, and encryption."""

import hashlib
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
    """Generate high-entropy recovery codes (xxxx-xxxx format)."""
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(4)  # 8 hex chars
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def hash_recovery_code(code: str) -> str:
    """SHA-256 hash a recovery code for storage."""
    normalized = code.strip().lower().replace("-", "")
    return hashlib.sha256(normalized.encode()).hexdigest()


def verify_recovery_code(code: str, hashed_codes: list[str]) -> int | None:
    """Check if a code matches any stored hash. Returns index if found."""
    h = hash_recovery_code(code)
    for i, stored in enumerate(hashed_codes):
        if h == stored:
            return i
    return None
