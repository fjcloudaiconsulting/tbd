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


def _dedicated_hmac_key() -> bytes | None:
    """Dedicated recovery-code HMAC key, or None when not configured.

    When MFA_RECOVERY_HMAC_KEY is set it becomes the primary key for
    hashing recovery codes, decoupling them from jwt_secret_key rotation.
    The key is single-purpose, so it is used directly (unlike the
    multi-purpose JWT secret, which is run through derive_hmac_key). The
    config validator has already stripped/normalized and length-checked it.
    """
    key = settings.mfa_recovery_hmac_key
    return key.encode() if key else None


def _hmac_key() -> bytes:
    """Purpose-bound HMAC key derived from the JWT secret.

    Derived rather than used raw, so the multi-purpose JWT signing key is
    never reused directly as hashing key material. This is the primary key
    when MFA_RECOVERY_HMAC_KEY is unset, and always a permanent verify
    fallback (see verify_recovery_code).
    """
    return derive_hmac_key(RECOVERY_CODE_PURPOSE)


def _legacy_hmac_key() -> bytes:
    """Pre-derivation key (the raw JWT secret). Verify-only fallback for
    recovery-code hashes stored before the purpose-bound scheme."""
    return settings.jwt_secret_key.encode()


def _normalize(code: str) -> str:
    return code.strip().lower().replace("-", "")


def hash_recovery_code(code: str) -> str:
    """HMAC-SHA256 a recovery code for storage (keyed, not brute-forceable).

    Mints under the dedicated MFA_RECOVERY_HMAC_KEY when configured, else
    under the jwt_secret_key-derived key (the historical default).
    """
    key = _dedicated_hmac_key() or _hmac_key()
    return hmac.new(key, _normalize(code).encode(), "sha256").hexdigest()


def _hash_recovery_code_legacy(code: str) -> str:
    return hmac.new(_legacy_hmac_key(), _normalize(code).encode(), "sha256").hexdigest()


def verify_recovery_code(code: str, hashed_codes: list[str]) -> int | None:
    """Check if a code matches any stored HMAC. Constant-time, no early exit.

    Verification tries, in order, the dedicated-key scheme (only when
    MFA_RECOVERY_HMAC_KEY is set), the purpose-bound jwt-derived scheme, and
    the legacy raw-jwt_secret_key scheme. Every fallback is PERMANENT, not
    transitional: recovery hashes enrolled under an older scheme keep
    verifying until the user regenerates their recovery codes (which
    overwrites their whole set under whatever scheme is current). There is no
    lazy in-place migration — the sole caller pops the matched entry to
    enforce single-use (so any re-hash would be discarded), and non-matched
    entries can never be upgraded because their plaintext is unknown (a hash
    cannot be re-HMACed). The fallbacks must NOT be removed: doing so would
    lock out every user who never regenerated their codes.

    All candidates are computed up front and every stored entry is compared
    against each with no early break, so timing does not leak which scheme
    (or whether any) matched. The candidate list length varies only by static
    config (2 when the dedicated key is unset, 3 when set), never by input.
    """
    normalized = _normalize(code).encode()

    def _keyed(key: bytes) -> str:
        return hmac.new(key, normalized, "sha256").hexdigest()

    # Order: dedicated (if configured) -> jwt-derived -> raw-jwt-legacy. Only
    # the dedicated layer is conditional; the two jwt-based layers are always
    # present so no existing hash ever bricks.
    keys = [_hmac_key(), _legacy_hmac_key()]
    dedicated = _dedicated_hmac_key()
    if dedicated is not None:
        keys.insert(0, dedicated)
    candidates = [_keyed(k) for k in keys]

    match_idx: int | None = None
    for i, stored in enumerate(hashed_codes):
        for candidate in candidates:
            if hmac.compare_digest(candidate, stored):
                match_idx = i
    return match_idx
