"""Fernet-based at-rest encryption for per-org AI provider credentials.

KEK comes from ``settings.ai_credential_encryption_key``; the optional
``settings.ai_credential_encryption_key_prev`` is the decrypt-only
fallback during rotation. The Fernet token format already carries
version + timestamp + auth tag so no extra envelope JSON is needed.
"""

from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


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


def fingerprint(plaintext: str) -> str:
    """Return the first 16 hex chars of sha256(plaintext)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()[:16]


def last_four(plaintext: str) -> str:
    """Return the trailing four characters, or ``""`` when shorter."""
    if len(plaintext) < 4:
        return ""
    return plaintext[-4:]
