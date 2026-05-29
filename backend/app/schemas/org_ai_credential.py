"""Pydantic schemas for per-org AI provider credentials (PR1).

Write paths accept plaintext keys (``api_key`` / ``bearer_token``)
but the response shape NEVER returns plaintext or ciphertext — only
the last-4 + fingerprint + provider metadata.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.org_ai_credential import AiProvider


LABEL_MAX_LENGTH = 120
API_KEY_MIN_LENGTH = 4
API_KEY_MAX_LENGTH = 4096
BASE_URL_MAX_LENGTH = 512

# Cloud metadata IPs (AWS / GCP / Azure / DO all converge on this address;
# also the AWS IPv6 metadata address).
_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})


def _ip_or_none(host: str) -> IPv4Address | IPv6Address | None:
    """Return the parsed IP if ``host`` is a literal address (with IPv4-mapped
    IPv6 unwrapped to its IPv4 form), or None if it's a DNS name."""
    if not host:
        return None
    candidate = host.strip("[]")
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip


def _reject_metadata_or_unsafe(host: str) -> None:
    """Always-blocked classes (safe for all providers including Ollama):
    cloud-metadata IPs, link-local (covers the rest of 169.254/16 beyond
    the metadata constant), multicast, unspecified, and IETF-reserved IPs
    (240/4, 255.255.255.255, etc.).

    Loopback (127/8, ::1) is intentionally excluded here — it is handled
    by _reject_private_or_loopback, which Ollama bypasses. Python 3.12
    marks ::1 as is_reserved=True, so we must check is_loopback first to
    avoid accidentally blocking it in this always-blocked layer.

    RFC1918 private addresses (is_private) are intentionally absent — they
    are the whole point of the provider-conditional layer; non-Ollama
    providers hit them in _reject_private_or_loopback.

    DNS names pass through (see _validate_base_url docstring for the DNS
    rebinding note)."""
    ip = _ip_or_none(host)
    if ip is None:
        return
    if str(ip) in _METADATA_IPS:
        raise ValueError("base_url cannot point at a cloud metadata endpoint")
    # Loopback is provider-conditional (allowed for Ollama); skip it here.
    if ip.is_loopback:
        return
    if (
        ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        raise ValueError(
            "base_url cannot point at a link-local, multicast, "
            "unspecified, or reserved IP"
        )


def _reject_private_or_loopback(host: str) -> None:
    """RFC1918 (10/8, 172.16/12, 192.168/16) and loopback (127.0.0.0/8, ::1).
    Blocked for hosted providers; allowed for Ollama (operator's own LAN /
    homelab — see spec 2026-05-29 section 3)."""
    ip = _ip_or_none(host)
    if ip is None:
        return
    if ip.is_private or ip.is_loopback:
        raise ValueError(
            "base_url cannot point at a private (RFC1918) or loopback IP"
        )


def _validate_base_url(value: str) -> str:
    """Reject base_url values that open an SSRF surface, regardless of
    provider. Provider-conditional checks (RFC1918 / loopback) run in
    the model validator where ``provider`` is known.

    Allowed: http/https scheme + public hostname/IP. Private DNS names
    (``ollama.internal``, ``my-llm.local``) ARE allowed — operators
    fronting Ollama in their VPC need them. DNS rebinding remains a
    residual v1 risk; a future iteration can add a custom httpx
    transport that re-checks the resolved address before connect.
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
        # - Ollama: operator's own LAN/homelab, allow RFC1918 + loopback.
        # - All other providers: strict block per the v1 SSRF guard.
        if self.base_url and self.provider != AiProvider.OLLAMA:
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
