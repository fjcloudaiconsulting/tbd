"""Fernet-based at-rest encryption for per-org AI provider credentials.

KEK comes from ``settings.ai_credential_encryption_key``; the optional
``settings.ai_credential_encryption_key_prev`` is the decrypt-only
fallback during rotation. The Fernet token format already carries
version + timestamp + auth tag so no extra envelope JSON is needed.
"""

from __future__ import annotations

import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.security import derive_hmac_key


class AiCredentialCryptoError(RuntimeError):
    """Raised when AI credential encryption is misconfigured."""


def _fernet_current() -> Fernet:
    key = settings.ai_credential_encryption_key
    if not key:
        raise AiCredentialCryptoError(
            "AI_CREDENTIAL_ENCRYPTION_KEY is not configured"
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise AiCredentialCryptoError(
            f"AI_CREDENTIAL_ENCRYPTION_KEY is malformed: {exc}"
        ) from exc


def _fernet_prev() -> Fernet | None:
    key = settings.ai_credential_encryption_key_prev
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise AiCredentialCryptoError(
            f"AI_CREDENTIAL_ENCRYPTION_KEY_PREV is malformed: {exc}"
        ) from exc


def encrypt(plaintext: str) -> str:
    """Encrypt with the current KEK and return the Fernet token string."""
    return _fernet_current().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(envelope: str) -> str:
    """Decrypt a Fernet token; falls back to PREV when current fails."""
    raw = envelope.encode("ascii")
    try:
        return _fernet_current().decrypt(raw).decode("utf-8")
    except InvalidToken:
        prev = _fernet_prev()
        if prev is None:
            raise ValueError("Failed to decrypt AI credential envelope")
        try:
            return prev.decrypt(raw).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Failed to decrypt AI credential envelope (current + prev)"
            ) from exc


# TBD-719: marks a keyed fingerprint. A row stored before it holds 16 bare hex
# chars of an unkeyed sha256, which stays valid (the value is only displayed,
# never compared) until a revalidate or rotate rewrites it with ``fingerprint``.
FINGERPRINT_PREFIX = "h1:"
_FINGERPRINT_PURPOSE = b"ai-key-fingerprint-v1"


def fingerprint(plaintext: str) -> str:
    """``h1:`` + the first 16 hex chars of HMAC-SHA256 under a key derived for
    this purpose from JWT_SECRET_KEY, so a leaked row cannot be checked against
    a candidate key without the server secret.

    Rotating JWT_SECRET_KEY changes the value computed for the same key; the
    stored one is refreshed on the next revalidate or rotate.
    """
    digest = hmac.new(
        derive_hmac_key(_FINGERPRINT_PURPOSE), plaintext.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return FINGERPRINT_PREFIX + digest[:16]


def last_four(plaintext: str) -> str:
    """Return the trailing four characters, or ``""`` when shorter."""
    if len(plaintext) < 4:
        return ""
    return plaintext[-4:]
