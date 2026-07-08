"""Pydantic schemas for per-org AI provider credentials (PR1).

Write paths accept plaintext keys (``api_key`` / ``bearer_token``)
but the response shape NEVER returns plaintext or ciphertext — only
the last-4 + fingerprint + provider metadata.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
from app.models.org_ai_credential import AiProvider
from app.services.ai_providers.egress_guard import (
    BlockedAddressError,
    check_ip,
    ip_literal_or_none,
)


LABEL_MAX_LENGTH = 120
API_KEY_MIN_LENGTH = 4
API_KEY_MAX_LENGTH = 4096
BASE_URL_MAX_LENGTH = 512


def _reject_metadata_or_unsafe(host: str) -> None:
    """Always-blocked classes (for all providers, even Ollama with
    AI_PROVIDER_ALLOW_PRIVATE_NETWORKS set): cloud-metadata IPs,
    link-local, multicast, unspecified, and IETF-reserved IPs.

    Delegates to the canonical denylist in
    ``services.ai_providers.egress_guard`` (``allow_private=True`` is
    exactly the always-blocked layer). Literal IPs only — DNS names
    pass through here and are enforced at connect time by
    ``egress_guard.GuardedTransport`` (resolve + validate + pin)."""
    ip = ip_literal_or_none(host)
    if ip is None:
        return
    try:
        check_ip(ip, allow_private=True)
    except BlockedAddressError as exc:
        raise ValueError(f"base_url is not allowed: {exc}") from None


def _reject_private_or_loopback(host: str) -> None:
    """Strict layer: loopback, RFC1918/ULA, and any non-public literal.
    Applied to every provider except Ollama when the
    AI_PROVIDER_ALLOW_PRIVATE_NETWORKS escape hatch is enabled."""
    ip = ip_literal_or_none(host)
    if ip is None:
        return
    try:
        check_ip(ip, allow_private=False)
    except BlockedAddressError as exc:
        raise ValueError(f"base_url is not allowed: {exc}") from None


def _validate_base_url(value: str) -> str:
    """Reject base_url values that open an SSRF surface, regardless of
    provider. Provider-conditional checks (RFC1918 / loopback) run in
    the model validator where ``provider`` is known.

    This save-time check is fast-feedback defense-in-depth over literal
    IPs; the enforcement point is the connect-time guard
    (``services.ai_providers.egress_guard``), which resolves DNS names,
    validates every record, and pins the connection — so a hostname
    whose A record points at a private/metadata address is refused at
    request time even though it passes here.
    """
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url must use http or https scheme")
    if not parsed.hostname:
        raise ValueError("base_url must include a hostname")
    _reject_metadata_or_unsafe(parsed.hostname)
    return value


class OrgAICredentialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: AiProvider
    api_key: Optional[str] = Field(
        default=None, max_length=API_KEY_MAX_LENGTH
    )
    bearer_token: Optional[str] = Field(
        default=None, max_length=API_KEY_MAX_LENGTH
    )
    base_url: Optional[str] = Field(default=None, max_length=BASE_URL_MAX_LENGTH)
    label: Optional[str] = Field(default=None, max_length=LABEL_MAX_LENGTH)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        return _validate_base_url(v)

    @model_validator(mode="after")
    def _check_provider_requirements(self):
        if self.provider in (AiProvider.OLLAMA, AiProvider.OPENAI_COMPATIBLE):
            if not self.base_url:
                raise ValueError(
                    "base_url is required for ollama and openai_compatible providers"
                )
        # Provider-conditional SSRF policy:
        # - Ollama with AI_PROVIDER_ALLOW_PRIVATE_NETWORKS=1 (operator's
        #   own LAN/homelab escape hatch): allow RFC1918 + loopback.
        # - Everything else (including Ollama with the flag OFF — the
        #   default): strict block. Mirrors the connect-time guard in
        #   services.ai_providers.egress_guard, which is the actual
        #   enforcement point.
        allow_private = (
            self.provider == AiProvider.OLLAMA
            and settings.ai_provider_allow_private_networks
        )
        if self.base_url and not allow_private:
            parsed = urlparse(self.base_url)
            if parsed.hostname:
                _reject_private_or_loopback(parsed.hostname)
        if self.provider != AiProvider.OLLAMA and self.bearer_token:
            raise ValueError(
                "bearer_token is only valid for the ollama provider"
            )
        # Ollama: api_key is optional (LAN-only homelab mode, spec line 37).
        # All other providers: api_key is required and must meet min length.
        if self.provider != AiProvider.OLLAMA:
            stripped = (self.api_key or "").strip()
            if len(stripped) < API_KEY_MIN_LENGTH:
                raise ValueError(
                    f"api_key is required (min {API_KEY_MIN_LENGTH} characters)"
                    f" for provider '{self.provider.value}'"
                )
        return self


class OrgAICredentialUpdate(BaseModel):
    """Label-only update. Key rotation has its own endpoint."""

    model_config = ConfigDict(extra="forbid")

    label: Optional[str] = Field(default=None, max_length=LABEL_MAX_LENGTH)


class OrgAICredentialRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Rotate is symmetric with Create EXCEPT that api_key is non-optional and
    # min_length=4: rotation is "swap to a new key", never "remove the key".
    # Users who want to clear an Ollama credential's key delete + recreate.
    api_key: str = Field(
        min_length=API_KEY_MIN_LENGTH, max_length=API_KEY_MAX_LENGTH
    )
    # bearer_token is Ollama-only. Unlike OrgAICredentialCreate, the
    # provider isn't in the request body (the rotate route looks up the
    # existing credential by id), so the schema can't enforce the rule
    # here — the service layer does (see ai_credential_service.
    # rotate_credential). Mirrors the create-path schema check.
    bearer_token: Optional[str] = Field(
        default=None, max_length=API_KEY_MAX_LENGTH
    )


class OrgAICredentialResponse(BaseModel):
    """Sanitized response shape — NEVER includes encrypted_* or plaintext."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    provider: AiProvider
    last_four: Optional[str]
    key_fingerprint: Optional[str]
    base_url: Optional[str]
    label: Optional[str]
    discovered_capabilities: Optional[list[str]] = None
    discovered_models: Optional[list[str]] = None
    created_at: datetime
    updated_at: datetime
    last_used_at: Optional[datetime] = None
    last_validated_at: Optional[datetime] = None
    validation_error: Optional[str] = None
