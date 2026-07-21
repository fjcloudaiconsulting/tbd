"""Hashing helpers for superadmin Personal Access Tokens (PAT).

Plaintext tokens are shown to the user exactly once, at creation time, and
are never persisted — only an HMAC-SHA256 hash under ``settings.api_token_hmac_key``
(a dedicated pepper, decoupled from ``jwt_secret_key``; see
``Settings._validate_api_token_hmac_key``) is stored. ``token_hash_candidates``
additionally supports a previous-rotation key (``api_token_hmac_key_prev``,
verify-only) so tokens minted before a key rotation keep validating until
they are re-issued or expire.
"""

import hashlib
import hmac
import secrets

from app.config import settings
from app.security import derive_hmac_key


def _primary_key() -> bytes:
    if settings.api_token_hmac_key:
        return settings.api_token_hmac_key.encode()
    if settings.app_env == "production":
        # Defence-in-depth: the config validator already refuses to boot
        # in production without this key set, so this branch should be
        # unreachable in practice.
        raise RuntimeError("API_TOKEN_HMAC_KEY missing in production")
    return derive_hmac_key(b"api_token")  # dev-only fallback (bytes)


def _hash_with(key: bytes, plaintext: str) -> str:
    return hmac.new(key, plaintext.encode(), hashlib.sha256).hexdigest()


def hash_api_token(plaintext: str) -> str:
    return _hash_with(_primary_key(), plaintext)


def token_hash_candidates(plaintext: str) -> list[str]:
    out = [hash_api_token(plaintext)]
    if settings.api_token_hmac_key_prev:
        out.append(_hash_with(settings.api_token_hmac_key_prev.encode(), plaintext))
    return out


def generate_token() -> tuple[str, str, str]:
    full = "pat_" + secrets.token_urlsafe(32)
    return full, hash_api_token(full), full[:14]
