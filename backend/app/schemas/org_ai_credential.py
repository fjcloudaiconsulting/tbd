"""Pydantic schemas for per-org AI provider credentials (PR1).

Write paths accept a plaintext ``api_key`` but the response shape NEVER
returns plaintext or ciphertext — only the last-4 + fingerprint + provider
metadata. ``base_url`` is stored in
plaintext, so it must not carry credentials (DB-02).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse, urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.models.org_ai_credential import (
    AiProvider,
    OrgAICredential,
    is_supported_provider,
    provider_key,
)
from app.services.ai_providers.egress_guard import (
    BlockedAddressError,
    check_ip,
    ip_literal_or_none,
)


LABEL_MAX_LENGTH = 120
API_KEY_MIN_LENGTH = 4
API_KEY_MAX_LENGTH = 4096
BASE_URL_MAX_LENGTH = 512


def _reject_non_public(host: str) -> None:
    """Refuse a literal IP that is not a public egress target: cloud
    metadata, link-local, multicast, unspecified, reserved, loopback,
    RFC1918/ULA and any other non-global address. Every provider gets the
    full denylist; no provider has a private-network escape hatch since
    TBD-584 removed ``ollama``.

    Delegates to the canonical denylist in
    ``services.ai_providers.egress_guard``. Literal IPs only — DNS names
    pass through here and are enforced at connect time by
    ``egress_guard.GuardedTransport`` (resolve + validate + pin)."""
    ip = ip_literal_or_none(host)
    if ip is None:
        return
    try:
        check_ip(ip, allow_private=False)
    except BlockedAddressError as exc:
        raise ValueError(f"base_url is not allowed: {exc}") from None


def _validate_base_url(value: str) -> str:
    """Reject base_url values that open an SSRF surface, regardless of
    provider.

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
    # Endpoints are appended by plain string concat (TBD-590), so a query or
    # fragment would swallow them. New writes only; stored rows untouched.
    if "?" in value or "#" in value:
        raise ValueError("base_url must not include a query string or fragment")
    # The column is plaintext and listed back to every org admin, so URL
    # credentials are refused for every provider (DB-02). New writes only;
    # stored rows untouched.
    if "@" in parsed.netloc:
        raise ValueError("base_url must not include a username or password")
    _reject_non_public(parsed.hostname)
    return value


def _mask_userinfo(value: Optional[str]) -> Optional[str]:
    """``value`` with any URL userinfo replaced by ``***`` (rows stored
    before DB-02 may still carry it)."""
    if not value:
        return value
    parts = urlsplit(value)
    if "@" not in parts.netloc:
        return value
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit(parts._replace(netloc=f"***@{host}"))


class OrgAICredentialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Validated against the provider registry: a retired provider such as
    # ``ollama`` or ``native`` is a 422 (TBD-584).
    provider: AiProvider
    api_key: Optional[str] = Field(
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
        if self.provider == AiProvider.OPENAI_COMPATIBLE and not self.base_url:
            raise ValueError(
                "base_url is required for the openai_compatible provider"
            )
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

    # Rotation is "swap to a new key", never "remove the key".
    api_key: str = Field(
        min_length=API_KEY_MIN_LENGTH, max_length=API_KEY_MAX_LENGTH
    )


# Columns copied as-is into ``OrgAICredentialResponse``; ``provider`` and
# ``provider_supported`` are derived.
_RESPONSE_COLUMNS = (
    "id",
    "org_id",
    "last_four",
    "key_fingerprint",
    "base_url",
    "label",
    "discovered_capabilities",
    "discovered_models",
    "created_at",
    "updated_at",
    "last_used_at",
    "last_validated_at",
    "validation_error",
    "platform_provider",
)


class OrgAICredentialResponse(BaseModel):
    """Sanitized response shape — NEVER includes encrypted_* or plaintext."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    # A plain string: a stored row may name a retired provider (TBD-584),
    # which ``provider_supported`` then reports as False.
    provider: str
    provider_supported: bool
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
    platform_provider: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _from_row(cls, data: Any) -> Any:
        if isinstance(data, OrgAICredential):
            return {
                **{f: getattr(data, f) for f in _RESPONSE_COLUMNS},
                "provider": provider_key(data.provider),
                "provider_supported": is_supported_provider(data.provider),
            }
        return data

    @field_serializer("base_url")
    def _serialize_base_url(self, value: Optional[str]) -> Optional[str]:
        return _mask_userinfo(value)


class OpenRouterOAuthStart(BaseModel):
    """sha256 hex of the client's nonce; the nonce itself never reaches a URL."""

    model_config = ConfigDict(extra="forbid")

    nonce_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class OpenRouterOAuthComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=512)
    state: str = Field(min_length=1, max_length=2048)
    nonce: str = Field(min_length=22, max_length=128)
