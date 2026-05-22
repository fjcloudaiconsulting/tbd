"""Pydantic schemas for per-org AI provider credentials (PR1).

Write paths accept plaintext keys (``api_key`` / ``bearer_token``)
but the response shape NEVER returns plaintext or ciphertext — only
the last-4 + fingerprint + provider metadata.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime
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


def _reject_private_ip_literal(host: str) -> None:
    """Raise ValueError if ``host`` is a literal IP in a blocked range.

    Blocks loopback, RFC1918 private, link-local (covers IMDS 169.254/16),
    multicast, unspecified, reserved, and cloud-metadata IPs. Also catches
    IPv4-mapped IPv6 of any of the above (``::ffff:127.0.0.1`` etc.).

    DNS names pass through this check — DNS rebinding is a residual risk
    for v1; the operator is responsible for not pointing ``base_url`` at
    a private DNS name that resolves to a metadata IP. A future iteration
    can add a custom httpx transport that re-checks the resolved address
    before connect.
    """
    if not host:
        return
    # Strip surrounding brackets used by RFC3986 for IPv6 literals.
    candidate = host.strip("[]")
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        # Not a literal IP — DNS name, fall through (see docstring).
        return
    # Cloud-metadata catch (also caught by is_link_local, but explicit so
    # the error message tells the operator exactly what was rejected).
    if str(ip) in _METADATA_IPS:
        raise ValueError("base_url cannot point at a cloud metadata endpoint")
    # IPv4-mapped IPv6 — unwrap and re-check on the IPv4 side.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        raise ValueError(
            "base_url cannot point at a private, loopback, link-local, "
            "multicast, or reserved IP"
        )


def _validate_base_url(value: str) -> str:
    """Reject base_url values that open an SSRF surface.

    Allowed: http/https scheme + public hostname/IP. Private DNS names
    (``ollama.internal``, ``my-llm.local``) ARE allowed in v1 — operators
    fronting Ollama in their VPC need them. Literal private/loopback IPs
    are rejected so a malicious org admin can't pivot through the backend
    onto 127.0.0.1 or the cloud metadata service.
    """
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url must use http or https scheme")
    if not parsed.hostname:
        raise ValueError("base_url must include a hostname")
    _reject_private_ip_literal(parsed.hostname)
    return value


class OrgAICredentialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: AiProvider
    api_key: str = Field(
        min_length=API_KEY_MIN_LENGTH, max_length=API_KEY_MAX_LENGTH
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
        if self.provider != AiProvider.OLLAMA and self.bearer_token:
            raise ValueError(
                "bearer_token is only valid for the ollama provider"
            )
        return self


class OrgAICredentialUpdate(BaseModel):
    """Label-only update. Key rotation has its own endpoint."""

    model_config = ConfigDict(extra="forbid")

    label: Optional[str] = Field(default=None, max_length=LABEL_MAX_LENGTH)


class OrgAICredentialRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    last_four: str
    key_fingerprint: str
    base_url: Optional[str]
    label: Optional[str]
    discovered_capabilities: Optional[list[str]] = None
    discovered_models: Optional[list[str]] = None
    created_at: datetime
    updated_at: datetime
    last_used_at: Optional[datetime] = None
    last_validated_at: Optional[datetime] = None
    validation_error: Optional[str] = None
